"""
Async MongoDB connection management for ingestion-api.

What lives here: the ``ingestions`` collection (one doc per ``IngestionRecord``). 
No GridFS - the signed asset and manifest bytes stay on the local filesystem (see ``services.artifact_store``).
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
            connectTimeoutMS=5_000,
            socketTimeoutMS=30_000,
            heartbeatFrequencyMS=10_000,
            minPoolSize=2,
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
        # Reconciliation worker — find failed pushes ordered by age.
        await col.create_index(
            [("resolutionPushStatus", ASCENDING), ("createdAt", ASCENDING)],
            name="push_status_created_idx",
        )
        # Ops/admin — find pipeline-failed ingestions ordered by age.
        # Distinct from push status: status==FAILED means the pipeline
        # itself blew up (signing, plugin), not just the downstream push.
        await col.create_index(
            [("status", ASCENDING), ("createdAt", ASCENDING)],
            name="status_created_idx",
        )
        # Listing endpoint will want most-recent-first.
        await col.create_index(
            [("createdAt", DESCENDING)],
            name="created_desc_idx",
        )


def get_ingestions_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db[INGESTIONS_COLLECTION]
