import hashlib
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedIdentity, get_current_identity
from app.core.usage_guard import charge_upload_unit, precheck_upload
from app.db.models import Paper
from app.db.session import get_db_session
from app.services.storage import build_storage_path, storage_object_exists, upload_pdf

router = APIRouter()

#: Authored here, never interpolated from an exception. status_detail is
#: rendered in the UI, so a raw driver or SDK message would put internal
#: wording — and potentially a path or a URL — in front of the user.
STORAGE_FAILED_DETAIL = "The file could not be stored. Please try uploading it again."

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
PDF_MAGIC_BYTES = b"%PDF-"
READ_CHUNK_SIZE = 1024 * 1024  # 1 MB

# Fixed namespace for deterministic paper_id derivation. Do not change
# once real papers exist in production — doing so would make every
# previously-computed paper_id stop matching on re-upload/dedup checks.
PAPER_ID_NAMESPACE = uuid.UUID("6ba7b813-9dad-11d1-80b4-00c04fd430c8")


def compute_paper_id(owner_id: str, content_hash: str) -> uuid.UUID:
    """Deterministic per (owner, content) — the same user re-uploading the
    identical bytes always resolves to the same paper_id, which is what
    makes duplicate-upload detection and indexing retries safe."""
    return uuid.uuid5(PAPER_ID_NAMESPACE, f"{owner_id}:{content_hash}")


@router.post("/upload")
async def upload_pdf_endpoint(
    file: UploadFile = File(...),
    identity: AuthenticatedIdentity = Depends(get_current_identity),
    _upload_allowance: str = Depends(precheck_upload),
    db: Session = Depends(get_db_session),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    # Buffered fully in memory (capped at MAX_UPLOAD_BYTES) — never written
    # to Render's local disk. Storage is Supabase Storage, not local disk.
    hasher = hashlib.sha256()
    buffer = bytearray()
    first_bytes = b""

    while True:
        chunk = await file.read(READ_CHUNK_SIZE)
        if not chunk:
            break
        if not first_bytes:
            first_bytes = chunk[: len(PDF_MAGIC_BYTES)]
        buffer.extend(chunk)
        hasher.update(chunk)
        if len(buffer) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB upload limit",
            )

    if not first_bytes.startswith(PDF_MAGIC_BYTES):
        raise HTTPException(status_code=400, detail="File is not a valid PDF")

    content_hash = hasher.hexdigest()
    owner_id = identity.owner_id  # string form — used for Storage calls, hashing, response JSON
    owner_uuid = uuid.UUID(owner_id)  # SQLAlchemy's Uuid columns require an actual UUID object
    paper_id = compute_paper_id(owner_id, content_hash)

    # Existing-row lookup is owner-scoped (Paper.owner_id == owner_uuid),
    # and paper_id itself is derived from f"{owner_id}:{content_hash}" —
    # so this can never match another owner's row by construction, even
    # before the owner_id filter is applied.
    existing = (
        db.query(Paper)
        .filter(Paper.id == paper_id, Paper.owner_id == owner_uuid)
        .first()
    )

    if existing is not None and existing.status != "failed":
        # Genuine duplicate: this exact content was already uploaded (and
        # possibly indexed) successfully — nothing to retry.
        return {
            "message": "This file was already uploaded",
            "paper_id": str(existing.id),
            "filename": existing.title,
            "status": existing.status,
            "saved_path": existing.storage_path,
            "duplicate": True,
        }

    needs_upload = True

    if existing is not None:
        # A previous attempt for this exact (owner, content) failed —
        # reuse the same row/paper_id. That earlier failure may have
        # happened AFTER Storage already succeeded (e.g. during
        # indexing), so blindly re-uploading would 409 against Supabase's
        # no-overwrite default. Check first — the path is reconstructed
        # from owner_id (server-verified, from the JWT) and this row's
        # own id (already owner-scoped by the query above), never from
        # existing.storage_path, so this can never be pointed at another
        # owner's object.
        paper = existing
        try:
            needs_upload = not storage_object_exists(owner_id, str(paper.id), identity.token)
        except Exception:
            # Existence could not be verified — fail safe by assuming it
            # is NOT confirmed present, so the normal upload path below
            # is attempted rather than skipping a possibly-necessary one.
            needs_upload = True

        paper.status = "uploading"
        paper.status_detail = None
        paper.file_size_bytes = len(buffer)
        db.commit()
    else:
        storage_path = build_storage_path(owner_id, str(paper_id))
        # Postgres row written BEFORE the Storage upload is attempted. If
        # the Storage write then fails, we have a visible, retryable
        # 'failed' row rather than an orphaned blob with nothing pointing
        # to it. If this insert itself fails, no Storage write is ever
        # attempted at all.
        paper = Paper(
            id=paper_id,
            owner_id=owner_uuid,
            title=file.filename,
            content_hash=content_hash,
            storage_path=storage_path,
            file_size_bytes=len(buffer),
            status="uploading",
        )
        db.add(paper)
        db.commit()

    if needs_upload:
        # Last safe point: the next statement is the Storage write. The
        # filename, PDF magic bytes and size cap were all checked above,
        # so a deterministic rejection has already happened without
        # costing a unit. precheck_upload turned away the ordinary
        # out-of-allowance case before the file was even read.
        charge_upload_unit(owner_id)

        try:
            upload_pdf(
                owner_id=owner_id,
                paper_id=str(paper.id),
                user_jwt=identity.token,
                file_bytes=bytes(buffer),
            )
        except Exception as e:
            paper.status = "failed"
            paper.status_detail = STORAGE_FAILED_DETAIL
            db.commit()
            raise HTTPException(status_code=502, detail="Failed to store the uploaded file")

    paper.status = "uploaded"
    paper.status_detail = None
    db.commit()

    return {
        "message": "PDF uploaded successfully",
        "paper_id": str(paper.id),
        "filename": paper.title,
        "status": "uploaded",
        "saved_path": paper.storage_path,
    }
