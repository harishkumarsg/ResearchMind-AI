from fastapi import APIRouter
import os
import uuid

from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue

from app.services.pdf_loader import (
    extract_pdf_pages
)

from app.services.text_cleaner import clean_text

from app.services.metadata_extractor import (
    extract_authors,
    extract_abstract,
    extract_keywords
)

from app.rag.chunker import create_chunks
from app.rag.embedder import create_embeddings

from app.rag.vector_store import (
    create_collection,
    client
)

router = APIRouter()

COLLECTION_NAME = "researchmind"

EMBEDDING_BATCH_SIZE = 50

UPLOAD_DIR = "uploads/papers"


def get_already_indexed_files() -> set:
    """Return the set of filenames already stored in Qdrant."""
    try:
        # Scroll through all points collecting unique source values
        indexed = set()
        next_offset = None

        while True:
            response = client.scroll(
                collection_name=COLLECTION_NAME,
                scroll_filter=None,
                limit=250,
                offset=next_offset,
                with_payload=["source"],
                with_vectors=False,
            )
            points, next_offset = response

            for point in points:
                source = point.payload.get("source", "")
                if source:
                    indexed.add(source)

            if next_offset is None:
                break

        return indexed

    except Exception:
        # Collection probably doesn't exist yet
        return set()


@router.get("/index-document")
def index_document():

    try:

        # ----------------------------------
        # Ensure Collection Exists
        # ----------------------------------

        create_collection()

        # ----------------------------------
        # Validate Upload Directory
        # ----------------------------------

        if not os.path.exists(UPLOAD_DIR):
            return {
                "status": "error",
                "message": "uploads/papers folder not found"
            }

        # ----------------------------------
        # Load PDFs
        # ----------------------------------

        files = [
            f for f in os.listdir(UPLOAD_DIR)
            if f.lower().endswith(".pdf")
        ]

        if not files:
            return {
                "status": "error",
                "message": "No PDFs found in uploads/papers"
            }

        # ----------------------------------
        # Incremental: skip already indexed
        # ----------------------------------

        already_indexed = get_already_indexed_files()

        new_files = [f for f in files if f not in already_indexed]

        print(f"\nTotal PDFs: {len(files)}")
        print(f"Already indexed: {len(already_indexed)}")
        print(f"New files to index: {len(new_files)}")

        if not new_files:
            return {
                "status": "success",
                "message": "All PDFs are already indexed. Nothing to do.",
                "pdfs_found": len(files),
                "pdfs_indexed": 0,
                "pdfs_skipped": 0,
                "already_indexed": len(already_indexed),
                "total_pages": 0,
                "chunks_indexed": 0,
                "points_uploaded": 0,
            }

        all_chunks = []
        indexed_papers = 0
        skipped_papers = 0
        total_pages = 0

        # ----------------------------------
        # Process Only New PDFs
        # ----------------------------------

        for pdf_file in new_files:

            pdf_path = os.path.join(UPLOAD_DIR, pdf_file)

            print(f"\nProcessing: {pdf_file}")

            pages = extract_pdf_pages(pdf_path)

            if not pages:
                print(f"Skipped unreadable PDF: {pdf_file}")
                skipped_papers += 1
                continue

            full_text = "\n".join(
                page.get("text", "") for page in pages
            )

            full_text = clean_text(full_text)

            if len(full_text) < 1000:
                print(f"Skipped tiny PDF: {pdf_file}")
                skipped_papers += 1
                continue

            # ----------------------------------
            # Metadata
            # ----------------------------------

            paper_title = os.path.splitext(pdf_file)[0]
            paper_id = str(uuid.uuid4())
            authors = extract_authors(full_text)
            abstract = extract_abstract(full_text)
            keywords = extract_keywords(full_text)

            indexed_papers += 1
            total_pages += len(pages)
            total_chunks_for_pdf = 0

            print(f"Pages: {len(pages)}")

            # ----------------------------------
            # Process Pages
            # ----------------------------------

            for page_data in pages:

                page_number = page_data.get("page", 0)
                page_text = clean_text(page_data.get("text", ""))

                if len(page_text.strip()) < 100:
                    continue

                chunks = create_chunks(page_text)
                total_chunks_for_pdf += len(chunks)

                for chunk_index, chunk in enumerate(chunks):
                    all_chunks.append({
                        "text": chunk,
                        "source": pdf_file,
                        "paper": paper_title,
                        "paper_id": paper_id,
                        "authors": authors,
                        "abstract": abstract,
                        "keywords": keywords,
                        "page": page_number,
                        "total_pages": len(pages),
                        "chunk_id": chunk_index,
                        "chunk_count": len(chunks),
                    })

            print(f"Chunks: {total_chunks_for_pdf}")

        # ----------------------------------
        # No New Chunks
        # ----------------------------------

        if not all_chunks:
            return {
                "status": "error",
                "message": "No valid text extracted from new PDFs"
            }

        # ----------------------------------
        # Create Embeddings + Upsert
        # ----------------------------------

        total_points = 0

        for i in range(0, len(all_chunks), EMBEDDING_BATCH_SIZE):

            batch = all_chunks[i:i + EMBEDDING_BATCH_SIZE]
            texts = [item["text"] for item in batch]
            embeddings = create_embeddings(texts)

            points = []

            for item, embedding in zip(batch, embeddings):
                points.append(
                    PointStruct(
                        id=str(uuid.uuid4()),
                        vector=embedding.tolist(),
                        payload={
                            "text": item["text"],
                            "source": item["source"],
                            "paper": item["paper"],
                            "paper_id": item["paper_id"],
                            "authors": item["authors"],
                            "abstract": item["abstract"],
                            "keywords": item["keywords"],
                            "page": item["page"],
                            "total_pages": item["total_pages"],
                            "chunk_id": item["chunk_id"],
                            "chunk_count": item["chunk_count"],
                        }
                    )
                )

            client.upsert(
                collection_name=COLLECTION_NAME,
                points=points
            )

            total_points += len(points)

        # ----------------------------------
        # Response
        # ----------------------------------

        return {
            "status": "success",
            "pdfs_found": len(files),
            "pdfs_indexed": indexed_papers,
            "pdfs_skipped": skipped_papers,
            "already_indexed": len(already_indexed),
            "total_pages": total_pages,
            "chunks_indexed": len(all_chunks),
            "points_uploaded": total_points,
            "page_level_citations": True,
            "paper_ids_enabled": True,
        }

    except Exception as e:

        print(f"Indexing Error: {str(e)}")

        return {
            "status": "error",
            "message": str(e)
        }


from app.services.text_cleaner import clean_text

from app.services.metadata_extractor import (
    extract_authors,
    extract_abstract,
    extract_keywords
)

from app.rag.chunker import create_chunks
from app.rag.embedder import create_embeddings

from app.rag.vector_store import (
    create_collection,
    client
)

router = APIRouter()

COLLECTION_NAME = "researchmind"

EMBEDDING_BATCH_SIZE = 50

UPLOAD_DIR = "uploads/papers"


@router.get("/index-document")
def index_document():

    try:

        # ----------------------------------
        # Reset Collection
        # ----------------------------------

        try:

            client.delete_collection(
                collection_name=COLLECTION_NAME
            )

            print(
                f"Deleted collection: {COLLECTION_NAME}"
            )

        except Exception:
            pass

        create_collection()

        # ----------------------------------
        # Validate Upload Directory
        # ----------------------------------

        if not os.path.exists(
            UPLOAD_DIR
        ):

            return {

                "status":
                "error",

                "message":
                "uploads/papers folder not found"
            }

        # ----------------------------------
        # Load PDFs
        # ----------------------------------

        files = [

            file

            for file in os.listdir(
                UPLOAD_DIR
            )

            if file.lower().endswith(".pdf")
        ]

        if not files:

            return {

                "status":
                "error",

                "message":
                "No PDFs found"
            }

        all_chunks = []

        indexed_papers = 0

        skipped_papers = 0

        total_pages = 0

        # ----------------------------------
        # Process PDFs
        # ----------------------------------

        for pdf_file in files:

            pdf_path = os.path.join(
                UPLOAD_DIR,
                pdf_file
            )

            print(
                f"\nProcessing: {pdf_file}"
            )

            pages = extract_pdf_pages(
                pdf_path
            )

            if not pages:

                print(
                    f"Skipped unreadable PDF: {pdf_file}"
                )

                skipped_papers += 1

                continue

            full_text = "\n".join(

                page.get(
                    "text",
                    ""
                )

                for page in pages
            )

            full_text = clean_text(
                full_text
            )

            if len(full_text) < 1000:

                print(
                    f"Skipped tiny PDF: {pdf_file}"
                )

                skipped_papers += 1

                continue

            # ----------------------------------
            # Metadata
            # ----------------------------------

            paper_title = os.path.splitext(
                pdf_file
            )[0]

            paper_id = str(
                uuid.uuid4()
            )

            authors = extract_authors(
                full_text
            )

            abstract = extract_abstract(
                full_text
            )

            keywords = extract_keywords(
                full_text
            )

            indexed_papers += 1

            total_pages += len(
                pages
            )

            total_chunks_for_pdf = 0

            print(
                f"Pages: {len(pages)}"
            )

            # ----------------------------------
            # Process Pages
            # ----------------------------------

            for page_data in pages:

                page_number = page_data.get(
                    "page",
                    0
                )

                page_text = clean_text(

                    page_data.get(
                        "text",
                        ""
                    )
                )

                if len(
                    page_text.strip()
                ) < 100:

                    continue

                chunks = create_chunks(
                    page_text
                )

                total_chunks_for_pdf += len(
                    chunks
                )

                for chunk_index, chunk in enumerate(
                    chunks
                ):

                    all_chunks.append(

                        {

                            "text":
                            chunk,

                            "source":
                            pdf_file,

                            "paper":
                            paper_title,

                            "paper_id":
                            paper_id,

                            "authors":
                            authors,

                            "abstract":
                            abstract,

                            "keywords":
                            keywords,

                            "page":
                            page_number,

                            "total_pages":
                            len(pages),

                            "chunk_id":
                            chunk_index,

                            "chunk_count":
                            len(chunks)
                        }
                    )

            print(
                f"Chunks: {total_chunks_for_pdf}"
            )

        # ----------------------------------
        # No Chunks
        # ----------------------------------

        if not all_chunks:

            return {

                "status":
                "error",

                "message":
                "No valid text extracted from PDFs"
            }

        # ----------------------------------
        # Create Embeddings
        # ----------------------------------

        total_points = 0

        for i in range(

            0,
            len(all_chunks),
            EMBEDDING_BATCH_SIZE

        ):

            batch = all_chunks[
                i:i + EMBEDDING_BATCH_SIZE
            ]

            texts = [

                item["text"]

                for item in batch
            ]

            embeddings = create_embeddings(
                texts
            )

            points = []

            for item, embedding in zip(
                batch,
                embeddings
            ):

                points.append(

                    PointStruct(

                        id=str(
                            uuid.uuid4()
                        ),

                        vector=embedding.tolist(),

                        payload={

                            "text":
                            item["text"],

                            "source":
                            item["source"],

                            "paper":
                            item["paper"],

                            "paper_id":
                            item["paper_id"],

                            "authors":
                            item["authors"],

                            "abstract":
                            item["abstract"],

                            "keywords":
                            item["keywords"],

                            "page":
                            item["page"],

                            "total_pages":
                            item["total_pages"],

                            "chunk_id":
                            item["chunk_id"],

                            "chunk_count":
                            item["chunk_count"]
                        }
                    )
                )

            client.upsert(

                collection_name=COLLECTION_NAME,

                points=points
            )

            total_points += len(
                points
            )

        # ----------------------------------
        # Response
        # ----------------------------------

        return {

            "status":
            "success",

            "pdfs_found":
            len(files),

            "pdfs_indexed":
            indexed_papers,

            "pdfs_skipped":
            skipped_papers,

            "total_pages":
            total_pages,

            "chunks_indexed":
            len(all_chunks),

            "points_uploaded":
            total_points,

            "page_level_citations":
            True,

            "paper_ids_enabled":
            True
        }

    except Exception as e:

        print(
            f"Indexing Error: {str(e)}"
        )

        return {

            "status":
            "error",

            "message":
            str(e)
        }