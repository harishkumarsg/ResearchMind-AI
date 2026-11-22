import logging
import os
from qdrant_client import QdrantClient

from qdrant_client.models import (
    Distance,
    VectorParams,
    PayloadSchemaType
)

from dotenv import load_dotenv
load_dotenv()

COLLECTION_NAME = "researchmind_v2"

logger = logging.getLogger(__name__)

_qdrant_url = os.environ.get("QDRANT_URL", "")
_qdrant_api_key = os.environ.get("QDRANT_API_KEY", "")

#: Timeout for the startup reachability probe. Short on purpose: the probe
#: only reports, and must not hold up startup when the cluster is down.
REACHABILITY_TIMEOUT_SECONDS = 5


def _build_client(timeout):
    """A Qdrant client for the configured cluster.

    check_compatibility=False: the client's own version check sends a
    request while the client is being constructed, which here is at import
    time, and logs an opaque "Failed to obtain server version" warning
    whenever the cluster is slow, suspended or unreachable. Reachability is
    checked explicitly at startup instead (check_qdrant_reachable).
    """
    if _qdrant_url:
        # Qdrant Cloud (production, with the backend on Railway)
        return QdrantClient(
            url=_qdrant_url,
            api_key=_qdrant_api_key,
            timeout=timeout,
            check_compatibility=False,
        )
    # Local development
    return QdrantClient(
        host="localhost",
        port=6333,
        timeout=timeout,
        check_compatibility=False,
    )


client = _build_client(timeout=120)


def check_qdrant_reachable(timeout=REACHABILITY_TIMEOUT_SECONDS):
    """Makes one request to Qdrant and logs the outcome. Never raises.

    Uses its own short-timeout client rather than the shared one, whose
    120 s timeout is sized for indexing. The log carries the exception
    type and any HTTP status, never the URL, key or error text.
    """
    probe = None
    try:
        probe = _build_client(timeout=timeout)
        exists = probe.collection_exists(COLLECTION_NAME)
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        logger.warning(
            "Qdrant unreachable at startup: %s%s. Search and indexing will "
            "fail until it responds; check whether the cluster is suspended.",
            type(exc).__name__,
            f" (HTTP {status})" if status else "",
        )
        return False
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass

    if exists:
        logger.info("Qdrant reachable; collection %s exists.", COLLECTION_NAME)
    else:
        logger.warning(
            "Qdrant reachable, but collection %s does not exist yet; "
            "it is created on the first indexing run.",
            COLLECTION_NAME,
        )
    return True


def create_collection():

    collections = client.get_collections()

    collection_names = [
        collection.name
        for collection in collections.collections
    ]

    if COLLECTION_NAME not in collection_names:

        print(
            f"Creating Collection: {COLLECTION_NAME}"
        )

        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=1024,
                distance=Distance.COSINE
            )
        )

        try:

            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name="paper",
                field_schema=PayloadSchemaType.KEYWORD
            )

            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name="source",
                field_schema=PayloadSchemaType.KEYWORD
            )

            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name="paper_id",
                field_schema=PayloadSchemaType.KEYWORD
            )

            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name="owner_id",
                field_schema=PayloadSchemaType.KEYWORD
            )

            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name="authors",
                field_schema=PayloadSchemaType.KEYWORD
            )

            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name="keywords",
                field_schema=PayloadSchemaType.KEYWORD
            )

            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name="page",
                field_schema=PayloadSchemaType.INTEGER
            )

            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name="chunk_id",
                field_schema=PayloadSchemaType.INTEGER
            )

            print(
                "Payload indexes created successfully."
            )

        except Exception as e:

            print(
                f"Payload index warning: {e}"
            )

    else:

        print(
            f"Collection already exists: {COLLECTION_NAME}"
        )

    return client