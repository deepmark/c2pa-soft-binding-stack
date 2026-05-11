"""
Async MongoDB connection management for ingestion-api.

Two collections live here:

- ``ingestions`` — one doc per successful ``IngestionRecord``.
- ``failed_ingestions`` — one doc per ``FailedIngestion`` (pipeline failures captured for ops/forensics).

No GridFS — the signed asset and manifest bytes stay on the local filesystem 
(see ``repositories.artifacts``).
"""
from __future__ import annotations

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
    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None

    @classmethod
    async def connect(cls) -> None:
        # motor / pymongo take milliseconds for *_MS args; settings
        # expose seconds (consistent with every other timeout in the
        # config), so convert at the call site.
        cls.client = AsyncIOMotorClient(
            settings.mongodb_url,
            serverSelectionTimeoutMS=int(settings.mongo_server_selection_timeout_s * 1000),
            connectTimeoutMS=int(settings.mongo_connect_timeout_s * 1000),
            socketTimeoutMS=int(settings.mongo_socket_timeout_s * 1000),
            heartbeatFrequencyMS=int(settings.mongo_heartbeat_frequency_s * 1000),
            minPoolSize=settings.mongo_min_pool_size,
            maxPoolSize=settings.mongo_max_pool_size,
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
        # Reconciliation worker will need to find FAILED pushes that haven't been retried recently. 
        # Sort key is ``lastPushAttemptAt`` (not createdAt) because the reconciler
        # bumps that field on each retry, naturally drifting the record to the back of the queue.
        await col.create_index(
            [("resolutionPushStatus", ASCENDING), ("lastPushAttemptAt", ASCENDING)],
            name="push_retry_idx",
        )
        # Backs ``GET /ingestions`` cursor pagination
        # (sort: createdAt desc, _id desc).
        # Indexed on (createdAt, _id) so identical createdAt values
        # still tie-break deterministically without a collection scan.
        await col.create_index(
            [("createdAt", DESCENDING), ("_id", DESCENDING)],
            name="created_at_id_desc_idx",
        )

    @staticmethod
    async def _ensure_failed_ingestions_indexes(col: AsyncIOMotorCollection) -> None:
        await col.create_index(
            [("createdAt", DESCENDING), ("_id", DESCENDING)],
            name="failed_created_at_id_desc_idx",
        )


def get_ingestions_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db[INGESTIONS_COLLECTION]


def get_failed_ingestions_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db[FAILED_INGESTIONS_COLLECTION]
