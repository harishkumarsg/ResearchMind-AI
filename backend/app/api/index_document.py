import os
import tempfile
import uuid

from fastapi import APIRouter, Depends
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct
from sqlalchemy.orm import Session

from typing import Callable, Optional

from app.core import limits as limits_config
from app.core.limits import QuotaExceeded
from app.core.providers import classify_provider_error
from app.core.indexing_slot import indexing_slot
from app.core.quota import charge as charge_quota
from app.core.usage_guard import precheck_index_run
from app.db.models import Paper
from app.db.session import get_db_session
from app.rag.chunker import create_chunks
from app.rag.embedder import create_embeddings
from app.rag.vector_store import COLLECTION_NAME, client, create_collection
from app.services.metadata_extractor import (
    extract_abstract,
    extract_authors,
    extract_keywords,
)
from app.services.pdf_loader import extract_pdf_pages
from app.services.storage import fetch_pdf
from app.services.text_cleaner import clean_text

router = APIRouter()


def _clear_existing_points_for_paper(paper_id: str, owner_id: str) -> None:
    """Makes re-indexing a single paper idempotent (safe to retry after a
    failure) and scoped to exactly that paper's own points — this is
    NOT the same operation as the old delete_collection() bug: it only
    ever removes points matching this exact paper_id AND owner_id."""
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=Filter(
            must=[
                FieldCondition(key="paper_id", match=MatchValue(value=paper_id)),
                FieldCondition(key="owner_id", match=MatchValue(value=owner_id)),
            ]
        ),
    )


def _build_chunks_for_paper(paper: Paper, pages: list) -> list:
    full_text = clean_text("\n".join(p.get("text", "") for p in pages))
    if len(full_text) < 1000:
        raise ValueError("Extracted text is too short to index")

    authors = extract_authors(full_text)
    abstract = extract_abstract(full_text)
    keywords = extract_keywords(full_text)

    owner_id = str(paper.owner_id)
    paper_id = str(paper.id)

    all_chunks = []
    for page_data in pages:
        page_number = page_data.get("page", 0)
        page_text = clean_text(page_data.get("text", ""))
        if len(page_text.strip()) < 100:
            continue

        chunks = create_chunks(page_text)
        for chunk_index, chunk in enumerate(chunks):
            all_chunks.append(
                {
                    "text": chunk,
                    "source": paper.title,
                    "paper": paper.title,
                    "paper_id": paper_id,
                    "owner_id": owner_id,
                    "authors": authors,
                    "abstract": abstract,
                    "keywords": keywords,
                    "page": page_number,
                    "total_pages": len(pages),
                    "chunk_id": chunk_index,
                    "chunk_count": len(chunks),
                }
            )

    if not all_chunks:
        raise ValueError("No valid text extracted from this PDF")

    return all_chunks


def index_one_paper(
    paper: Paper,
    before_provider_work: Optional[Callable[[], None]] = None,
) -> int:
    """Fetches this paper's PDF from persistent Storage (never from
    Render's local disk), extracts/chunks/embeds it, and upserts — as a
    single all-or-nothing operation per paper. Returns points written.

    Raises on any failure; the caller is responsible for setting
    paper.status accordingly. Never leaves partial chunks in Qdrant: the
    embedding buffer is fully built in memory before Qdrant is touched
    at all, and any existing points for this paper are cleared
    immediately before the fresh upsert, not left mixed with old data.
    """
    owner_id = str(paper.owner_id)
    paper_id = str(paper.id)

    pdf_bytes = fetch_pdf(owner_id=owner_id, paper_id=paper_id)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(tmp_fd, "wb") as tmp:
            tmp.write(pdf_bytes)
        pages = extract_pdf_pages(tmp_path)
    finally:
        os.remove(tmp_path)

    if not pages:
        raise ValueError("Unable to extract any pages from this PDF")

    # Cost cap D, applied once the real page count is known and BEFORE any
    # embedding call. Raised as ValueError so it travels the existing
    # per-paper failure path: this paper is marked failed with a readable
    # reason, other pending papers still index, and neither Voyage nor
    # Qdrant is touched for this one.
    max_pages = limits_config.max_pages_per_paper()
    if len(pages) > max_pages:
        raise ValueError(
            f"Document too large to index: {len(pages)} pages (limit {max_pages})"
        )

    all_chunks = _build_chunks_for_paper(paper, pages)

    max_chunks = limits_config.max_chunks_per_paper()
    if len(all_chunks) > max_chunks:
        raise ValueError(
            f"Document too large to index: {len(all_chunks)} passages (limit {max_chunks})"
        )

    # All-or-nothing: the WHOLE paper's embeddings are computed before
    # Qdrant is touched at all, so a Voyage/embedding failure — even one
    # that exhausts create_embeddings()'s own internal rate-limit
    # retries partway through — never leaves a partially-indexed paper.
    # create_embeddings() (embedder.py) handles its own token-budget
    # batching, request pacing, and rate-limit retry/backoff internally;
    # this call site does not need its own batching loop.
    texts = [item["text"] for item in all_chunks]

    # The last point at which nothing has been spent: the PDF is fetched,
    # the page and chunk caps have passed, and the very next call is the
    # embedding provider. A paper rejected by the caps above therefore
    # costs no quota, because this hook is never reached for it.
    if before_provider_work is not None:
        before_provider_work()

    embeddings = create_embeddings(texts)
    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            # Voyage's embed() already returns plain Python lists (unlike
            # the previous SentenceTransformer numpy arrays), so no
            # .tolist() conversion is needed or valid here.
            vector=embedding,
            payload=item,
        )
        for item, embedding in zip(all_chunks, embeddings)
    ]

    create_collection()
    _clear_existing_points_for_paper(paper_id, owner_id)
    client.upsert(collection_name=COLLECTION_NAME, points=points)

    return len(points)


@router.post("/index-document")
def index_document(
    owner_id: str = Depends(precheck_index_run),
    db: Session = Depends(get_db_session),
):
    """Indexes only the CALLING user's own uploaded-but-not-yet-indexed
    papers. There is no global "index everything" behavior anymore —
    that would have crossed ownership boundaries by construction."""

    owner_uuid = uuid.UUID(owner_id)  # SQLAlchemy's Uuid columns require an actual UUID object
    pending = (
        db.query(Paper)
        .filter(Paper.owner_id == owner_uuid, Paper.status == "uploaded")
        .all()
    )

    indexed_ids = []
    failed = []

    # Nothing pending: no slot is taken and no quota is spent, because no
    # indexing run happens. Returning the same shape as before keeps the
    # "already indexed" path on the frontend unchanged.
    if not pending:
        return {
            "status": "success",
            "papers_found": 0,
            "papers_indexed": 0,
            "papers_failed": 0,
            "indexed": indexed_ids,
            "failed": failed,
        }

    # QUOTA MODEL: one INDEX_RUN unit per PAPER actually accepted for
    # provider work — not one per HTTP request. Cost scales with papers
    # embedded, so the unit must too, otherwise a single request could
    # index an unbounded batch for one unit.
    #
    # The charge sits inside index_one_paper, immediately before the first
    # embedding call, so a paper rejected by the page/chunk caps or by a
    # failed PDF fetch consumes nothing.
    #
    # Slot BEFORE quota: a caller turned away because someone else is
    # indexing has consumed nothing, so being told "busy" never costs a
    # unit of the daily allowance.
    with indexing_slot():
        skipped = []
        quota_error = None

        for paper in pending:
            # Once the allowance runs out mid-batch, the remaining papers
            # are left exactly as they were — status "uploaded", so the
            # next run picks them up — rather than marked failed, which
            # would strand them outside the pending query forever.
            if quota_error is not None:
                skipped.append(
                    {
                        "paper_id": str(paper.id),
                        "reason": "Daily indexing limit reached — try again tomorrow.",
                    }
                )
                continue

            paper.status = "indexing"
            db.commit()

            try:
                points_written = index_one_paper(
                    paper,
                    before_provider_work=lambda: charge_quota(
                        owner_id, limits_config.INDEX_RUN
                    ),
                )
                paper.status = "indexed"
                paper.status_detail = None
                db.commit()
                indexed_ids.append({"paper_id": str(paper.id), "points": points_written})
            except QuotaExceeded as exc:
                # Raised by the hook above, so no embedding was requested
                # and nothing was written to Qdrant for this paper.
                paper.status = "uploaded"
                paper.status_detail = None
                db.commit()
                quota_error = exc
                skipped.append(
                    {
                        "paper_id": str(paper.id),
                        "reason": "Daily indexing limit reached — try again tomorrow.",
                    }
                )
            except Exception as e:
                # A provider failure is recorded with neutral wording, since
                # status_detail is shown to the user; any other failure keeps
                # its existing, application-authored message.
                provider_failure = classify_provider_error(e)
                detail = provider_failure.message if provider_failure is not None else str(e)
                paper.status = "failed"
                paper.status_detail = detail
                db.commit()
                failed.append({"paper_id": str(paper.id), "error": detail})

        # Nothing at all got through: report it as the limit rejection it
        # is, rather than a "success" with zero papers, which the frontend
        # would read as "already indexed".
        if quota_error is not None and not indexed_ids and not failed:
            raise quota_error

    return {
        "status": "success",
        "papers_found": len(pending),
        "papers_indexed": len(indexed_ids),
        "papers_failed": len(failed),
        "indexed": indexed_ids,
        "failed": failed,
        # Additive field: papers left pending because the daily indexing
        # allowance ran out partway through this batch.
        "skipped": skipped,
    }
