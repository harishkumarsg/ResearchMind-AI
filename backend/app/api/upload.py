import hashlib
import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedIdentity, get_current_identity
from app.core.usage_guard import charge_upload_unit, precheck_upload
from app.db.errors import log_database_failure
from app.db.models import Paper
from app.db.session import get_db_session
from app.services import paper_lifecycle
from app.services.storage import (
    build_storage_path,
    delete_pdf,
    log_storage_failure,
    storage_object_exists,
    upload_pdf,
)

router = APIRouter()

#: Failures are logged through helpers only, never by a direct logger call:
#: Storage failures through storage.log_storage_failure (exception type and
#: status codes, never the message, URL or key), a failed row insert
#: through db.errors.log_database_failure (exception type only, never the
#: message, SQL or values).
logger = logging.getLogger(__name__)

#: Authored here, never interpolated from an exception. status_detail is
#: rendered in the UI, so a raw driver or SDK message would put internal
#: wording — and potentially a path or a URL — in front of the user.
STORAGE_FAILED_DETAIL = "The file could not be stored. Please try uploading it again."

#: Answers when another request holds this paper. Authored here too.
UPLOAD_IN_PROGRESS_DETAIL = "This file is already being uploaded — try again shortly."

#: Any other failure to write the paper's row. The driver's message can
#: name tables, columns, constraints and values, so it is never shown.
UPLOAD_NOT_SAVED_DETAIL = "The upload could not be saved. Please try again."

#: The unique constraint that admits one row per (owner, file):
#: `unique (owner_id, content_hash)` in migrations/0001, named by Postgres.
#: Production's unique index carries exactly this name.
CONTENT_UNIQUE_CONSTRAINT = "papers_owner_id_content_hash_key"
PAPER_BEING_REMOVED_DETAIL = "This paper is being removed — upload it again once it is gone."
PAPER_REMOVED_DURING_UPLOAD_DETAIL = (
    "This paper was removed while it was uploading — please upload it again."
)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
PDF_MAGIC_BYTES = b"%PDF-"
READ_CHUNK_SIZE = 1024 * 1024  # 1 MB

# PAPER IDENTITY. Every new paper row gets a fresh random id, so each
# incarnation of a paper — upload, delete, the same file uploaded again —
# has its own id, and with it its own Storage path and its own Qdrant
# payload. A worker still running on an earlier incarnation (one whose
# claim expired and was taken over) can then only ever touch that dead
# incarnation's resources, never the new one's.
#
# Ids used to be uuid5(owner_id:content_hash): the same file always got the
# same id, so a re-upload after a delete reused the deleted paper's Storage
# path and Qdrant paper_id. Rows created that way keep their ids. Nothing
# recomputes an id: duplicates are found by (owner_id, content_hash), which
# Postgres keeps unique, and every other caller uses the id stored on the
# row.


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

    # Duplicate detection is by content. (owner_id, content_hash) is unique
    # in Postgres, so this finds at most one row, and the owner_id filter
    # means it can never match another owner's paper.
    existing = _find_by_content(db, owner_uuid, content_hash)

    if existing is not None and existing.status != "failed":
        return _answer_existing(existing)

    needs_upload = True

    # Every write below is made under a claim on the row
    # (app/services/paper_lifecycle.py), so an upload never overwrites a
    # deletion that has claimed the paper, and a deletion never removes a
    # paper while its PDF is being written.
    if existing is not None:
        # A previous attempt for this exact (owner, content) failed —
        # reuse the same row/paper_id, but only if it is STILL 'failed': a
        # deletion may have claimed it since it was read.
        paper_id = existing.id
        title, storage_path = existing.title, existing.storage_path
        token = paper_lifecycle.claim(
            db,
            paper_id,
            owner_uuid,
            from_status="failed",
            to_status="uploading",
            status_detail=None,
            file_size_bytes=len(buffer),
        )
        if token is None:
            _refuse(db, paper_id, owner_uuid)

        # That earlier failure may have happened AFTER Storage already
        # succeeded (e.g. during indexing), so blindly re-uploading would
        # 409 against Supabase's no-overwrite default. Check first — the
        # path is reconstructed from owner_id (server-verified, from the
        # JWT) and this row's own id (already owner-scoped by the query
        # above), never from existing.storage_path, so this can never be
        # pointed at another owner's object.
        try:
            needs_upload = not storage_object_exists(owner_id, str(paper_id), identity.token)
        except Exception as e:
            log_storage_failure(logger, "existence check", e)
            # Existence could not be verified — fail safe by assuming it
            # is NOT confirmed present, so the normal upload path below
            # is attempted rather than skipping a possibly-necessary one.
            needs_upload = True
    else:
        # A new incarnation: a fresh id (see PAPER IDENTITY above).
        paper_id = uuid.uuid4()
        title = file.filename
        storage_path = build_storage_path(owner_id, str(paper_id))
        # Postgres row written BEFORE the Storage upload is attempted. If
        # the Storage write then fails, we have a visible, retryable
        # 'failed' row rather than an orphaned blob with nothing pointing
        # to it. If this insert itself fails, no Storage write is ever
        # attempted at all. The insert is this request's claim: the unique
        # (owner_id, content_hash) constraint admits one row per file.
        token = paper_lifecycle.now()
        db.add(
            Paper(
                id=paper_id,
                owner_id=owner_uuid,
                title=file.filename,
                content_hash=content_hash,
                storage_path=storage_path,
                file_size_bytes=len(buffer),
                status="uploading",
                updated_at=token,
            )
        )
        try:
            db.commit()
        except IntegrityError as error:
            db.rollback()
            if not _is_duplicate_file(error):
                # A foreign key, NOT NULL or other constraint — a fault,
                # not a race, so it is not reported as one. Logged by the
                # driver exception's type only: its message, the SQL and
                # the values never reach the log or the client.
                log_database_failure(logger, "upload row insert", error)
                raise HTTPException(status_code=500, detail=UPLOAD_NOT_SAVED_DETAIL)
            # The same file, uploaded twice at once: the other request
            # created its row first. Answer as for any duplicate, by what
            # that row is now.
            other = _find_by_content(db, owner_uuid, content_hash)
            if other is not None and other.status != "failed":
                return _answer_existing(other)
            raise HTTPException(status_code=409, detail=UPLOAD_IN_PROGRESS_DETAIL)

    if needs_upload:
        # Last safe point: the next statement is the Storage write. The
        # filename, PDF magic bytes and size cap were all checked above,
        # so a deterministic rejection has already happened without
        # costing a unit. precheck_upload turned away the ordinary
        # out-of-allowance case before the file was even read.
        charge_upload_unit(owner_id)

        # Confirm the claim, and restart its lease, immediately before the
        # write: a request that has lost it must not write the PDF.
        token = paper_lifecycle.renew(db, paper_id, owner_uuid, "uploading", token)
        if token is None:
            _refuse(db, paper_id, owner_uuid)

        try:
            upload_pdf(
                owner_id=owner_id,
                paper_id=str(paper_id),
                user_jwt=identity.token,
                file_bytes=bytes(buffer),
            )
        except Exception as e:
            log_storage_failure(logger, "upload", e)
            paper_lifecycle.transition(
                db,
                paper_id,
                owner_uuid,
                "uploading",
                token,
                to_status="failed",
                status_detail=STORAGE_FAILED_DETAIL,
            )
            raise HTTPException(status_code=502, detail="Failed to store the uploaded file")

    if not paper_lifecycle.transition(
        db, paper_id, owner_uuid, "uploading", token, to_status="uploaded"
    ):
        # The claim expired during the write and a deletion took the paper
        # over. Remove the PDF just written so it cannot outlive the row —
        # unless another upload of the same file now holds the paper.
        if needs_upload and paper_lifecycle.current_status(db, paper_id, owner_uuid) in (None, "deleting"):
            _discard_pdf(owner_id, str(paper_id), identity.token)
        _refuse(db, paper_id, owner_uuid)

    return {
        "message": "PDF uploaded successfully",
        "paper_id": str(paper_id),
        "filename": title,
        "status": "uploaded",
        "saved_path": storage_path,
    }


def _is_duplicate_file(error: IntegrityError) -> bool:
    """Is this the unique (owner_id, content_hash) conflict — another
    request inserted this file first — and not some other failure?

    Postgres (psycopg) names the violated constraint, so that name decides.
    SQLite, which the offline suite runs on, reports no constraint name,
    only the columns of the violated unique index.
    """
    original = getattr(error, "orig", None)
    diagnostics = getattr(original, "diag", None)
    if diagnostics is not None:
        return getattr(diagnostics, "constraint_name", None) == CONTENT_UNIQUE_CONSTRAINT
    return str(original).startswith(
        "UNIQUE constraint failed: papers.owner_id, papers.content_hash"
    )


def _find_by_content(db: Session, owner_uuid: uuid.UUID, content_hash: str):
    return (
        db.query(Paper)
        .filter(Paper.owner_id == owner_uuid, Paper.content_hash == content_hash)
        .first()
    )


def _answer_existing(existing: Paper) -> dict:
    """This file already has a row that is not this request's to reuse."""
    if existing.status == "deleting":
        # Not a duplicate to report as uploaded: it is on its way out, and
        # its row belongs to the deletion until that finishes.
        raise HTTPException(status_code=409, detail=PAPER_BEING_REMOVED_DETAIL)
    # Genuine duplicate: this exact content was already uploaded (and
    # possibly indexed) — nothing to retry.
    return {
        "message": "This file was already uploaded",
        "paper_id": str(existing.id),
        "filename": existing.title,
        "status": existing.status,
        "saved_path": existing.storage_path,
        "duplicate": True,
    }


def _refuse(db: Session, paper_id: uuid.UUID, owner_uuid: uuid.UUID):
    """Another request holds this paper."""
    status = paper_lifecycle.current_status(db, paper_id, owner_uuid)
    if status == "deleting":
        detail = PAPER_BEING_REMOVED_DETAIL
    elif status is None:
        detail = PAPER_REMOVED_DURING_UPLOAD_DETAIL
    else:
        detail = UPLOAD_IN_PROGRESS_DETAIL
    raise HTTPException(status_code=409, detail=detail)


def _discard_pdf(owner_id: str, paper_id: str, user_jwt: str) -> None:
    try:
        delete_pdf(owner_id=owner_id, paper_id=paper_id, user_jwt=user_jwt)
    except Exception as exc:
        log_storage_failure(logger, "discard", exc)
