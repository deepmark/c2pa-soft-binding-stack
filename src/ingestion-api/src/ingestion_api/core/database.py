"""
Async MongoDB connection management for ingestion-api.

Two collections live here:

- ``ingestions`` — one doc per successful ``IngestionRecord``.
- ``failed_ingestions`` — one doc per ``FailedIngestion`` (pipeline failures captured for ops/forensics).

No GridFS — the signed asset and manifest bytes stay on the local
filesystem (see ``services.artifact_store``).
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
FAILED_INGESTIONS_COLLECTION = "failed_ingestions"


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
        await cls._ensure_ingestions_indexes(cls.db[INGESTIONS_COLLECTION])
        await cls._ensure_failed_ingestions_indexes(cls.db[FAILED_INGESTIONS_COLLECTION])

    @staticmethod
    async def _ensure_ingestions_indexes(col: AsyncIOMotorCollection) -> None:
        # Reconciliation worker — find pushes by status, ordered by age.
        await col.create_index(
            [("resolutionPushStatus", ASCENDING), ("createdAt", ASCENDING)],
            name="push_status_created_idx",
        )
        # Reconciler retry filter — "stale" records not touched recently.
        await col.create_index(
            [("resolutionPushStatus", ASCENDING), ("updatedAt", ASCENDING)],
            name="push_status_updated_idx",
        )
        # Listing endpoint will want most-recent-first.
        await col.create_index(
            [("createdAt", DESCENDING)],
            name="created_desc_idx",
        )
        # Cross-ingest dedup / "have I seen this exact upload before?".
        await col.create_index(
            [("uploadSha256", ASCENDING)],
            name="upload_sha_idx",
        )
        # Lookup by canonical signed-asset identity (e.g. for verification flows).
        await col.create_index(
            [("assetSha256", ASCENDING)],
            name="asset_sha_idx",
        )
        # Foreign-key into resolution-api — given a manifestId, find which
        # ingestion produced it.
        await col.create_index(
            [("manifestId", ASCENDING)],
            name="manifest_id_idx",
        )

    @staticmethod
    async def _ensure_failed_ingestions_indexes(col: AsyncIOMotorCollection) -> None:
        # Ops dashboard — recent failures.
        await col.create_index(
            [("createdAt", DESCENDING)],
            name="failed_created_desc_idx",
        )
        # Drill-down by failure stage.
        await col.create_index(
            [("failureStage", ASCENDING), ("createdAt", DESCENDING)],
            name="failed_stage_created_idx",
        )
        # Cross-correlate with successful records on the same upload.
        await col.create_index(
            [("uploadSha256", ASCENDING)],
            name="failed_upload_sha_idx",
        )


def get_ingestions_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db[INGESTIONS_COLLECTION]


def get_failed_ingestions_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db[FAILED_INGESTIONS_COLLECTION]
