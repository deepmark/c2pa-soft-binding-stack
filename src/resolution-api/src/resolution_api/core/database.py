"""
Async MongoDB connection management, indexes, and the manifest GridFS bucket.

Owned by the resolution API only. Ingestion has been split into a separate
service (``ingestion-api``) which keeps its artifacts on the filesystem;
the only piece that reaches Mongo is the eventual ``POST /manifests`` +
``POST /bindings`` calls back into this service.
"""
from __future__ import annotations

from typing import Optional

from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
    AsyncIOMotorGridFSBucket,
)
from pymongo import ASCENDING

from resolution_api.core.config import settings


class MongoDB:
    client: Optional[AsyncIOMotorClient] = None
    db: Optional[AsyncIOMotorDatabase] = None
    fs: Optional[AsyncIOMotorGridFSBucket] = None

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
        cls.fs = AsyncIOMotorGridFSBucket(cls.db, bucket_name="manifest_blobs")

        await cls.client.admin.command("ping")
        await cls._ensure_indexes()

    @classmethod
    async def close(cls) -> None:
        if cls.client is not None:
            cls.client.close()

    @classmethod
    async def _ensure_indexes(cls) -> None:
        assert cls.db is not None

        await cls.db.soft_bindings.create_index(
            [("alg", ASCENDING), ("value", ASCENDING)],
            name="alg_value_idx",
        )
        await cls.db.soft_bindings.create_index(
            [("manifestId", ASCENDING)],
            name="manifestId_idx",
        )
        await cls.db.soft_bindings.create_index(
            [("alg", ASCENDING), ("value", ASCENDING), ("manifestId", ASCENDING)],
            name="binding_unique",
            unique=True,
        )


def get_manifests_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db["manifests"]


def get_soft_bindings_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db["soft_bindings"]


def get_manifest_blobs_bucket() -> AsyncIOMotorGridFSBucket:
    assert MongoDB.fs is not None, "MongoDB not connected"
    return MongoDB.fs
