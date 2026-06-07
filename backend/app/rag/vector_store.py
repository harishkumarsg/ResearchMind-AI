import os
from qdrant_client import QdrantClient

from qdrant_client.models import (
    Distance,
    VectorParams,
    PayloadSchemaType
)

from dotenv import load_dotenv
load_dotenv()

COLLECTION_NAME = "researchmind"

_qdrant_url = os.environ.get("QDRANT_URL", "")
_qdrant_api_key = os.environ.get("QDRANT_API_KEY", "")

if _qdrant_url:
    # Cloud / Render deployment
    client = QdrantClient(
        url=_qdrant_url,
        api_key=_qdrant_api_key,
        timeout=120,
    )
else:
    # Local development
    client = QdrantClient(
        host="localhost",
        port=6333,
        timeout=120,
    )


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
                size=384,
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