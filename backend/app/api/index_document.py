import os
import tempfile
import uuid

from fastapi import APIRouter, Depends
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct
from sqlalchemy.orm import Session

from app.core.auth import get_current_owner_id
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


def index_one_paper(paper: Paper) -> int:
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

    all_chunks = _build_chunks_for_paper(paper, pages)

    # All-or-nothing: the WHOLE paper's embeddings are computed before
    # Qdrant is touched at all, so a Voyage/embedding failure — even one
    # that exhausts create_embeddings()'s own internal rate-limit
    # retries partway through — never leaves a partially-indexed paper.
    # create_embeddings() (embedder.py) handles its own token-budget
    # batching, request pacing, and rate-limit retry/backoff internally;
    # this call site does not need its own batching loop.
    texts = [item["text"] for item in all_chunks]
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
    owner_id: str = Depends(get_current_owner_id),
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

    for paper in pending:
        paper.status = "indexing"
        db.commit()

        try:
            points_written = index_one_paper(paper)
            paper.status = "indexed"
            paper.status_detail = None
            db.commit()
            indexed_ids.append({"paper_id": str(paper.id), "points": points_written})
        except Exception as e:
            paper.status = "failed"
            paper.status_detail = str(e)
            db.commit()
            failed.append({"paper_id": str(paper.id), "error": str(e)})

    return {
        "status": "success",
        "papers_found": len(pending),
        "papers_indexed": len(indexed_ids),
        "papers_failed": len(failed),
        "indexed": indexed_ids,
        "failed": failed,
    }
