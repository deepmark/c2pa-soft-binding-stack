"""
Async MongoDB connection management for ingestion-api.

This Mongo cluster is independent of resolution-api's. The two services
have separate ``MONGODB_URL`` + ``DATABASE_NAME`` config and may run on
entirely different instances; sharing a container in the dev compose
stack is a deployment detail, not a code coupling.

What lives here: the ``ingestions`` collection (one doc per
``IngestionRecord``). No GridFS — the signed asset and manifest bytes
stay on the local filesystem (see ``services.artifact_store``).
"""
from __future__ import annotations

from typing import Optional

from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
)
from pymongo import ASCENDING, DESCENDING

from ingestion_api.core.config import settings

INGESTIONS_COLLECTION = "ingestions"


class MongoDB:
    client: Optional[AsyncIOMotorClient] = None
    db: Optional[AsyncIOMotorDatabase] = None

    @classmethod
    async def connect(cls) -> None:
        cls.client = AsyncIOMotorClient(
            settings.mongodb_url,
            serverSelectionTimeoutMS=5_000,
            maxPoolSize=50,
            retryWrites=True,
            uuidRepresentation="standard",
        )
        cls.db = cls.client[settings.database_name]
        await cls.client.admin.command("ping")
        await cls._ensure_indexes()

    @classmethod
    async def close(cls) -> None:
        if cls.client is not None:
            cls.client.close()
            cls.client = None
            cls.db = None

    @classmethod
    async def _ensure_indexes(cls) -> None:
        assert cls.db is not None
        col = cls.db[INGESTIONS_COLLECTION]
        # Future reconciliation worker: cheap to scan failed pushes by
        # status + creation time without a collscan.
        await col.create_index(
            [("resolutionPushStatus", ASCENDING), ("createdAt", ASCENDING)],
            name="push_status_created_idx",
        )
        # Lookup by manifestId (e.g. when resolution-api retries a push
        # we may want to find the originating ingestion).
        await col.create_index(
            [("manifestId", ASCENDING)],
            name="manifest_id_idx",
            sparse=True,
        )
        # Listing endpoint (when added) will want most-recent-first.
        await col.create_index(
            [("createdAt", DESCENDING)],
            name="created_desc_idx",
        )


def get_ingestions_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db[INGESTIONS_COLLECTION]
