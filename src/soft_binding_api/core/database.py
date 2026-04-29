"""
Async MongoDB connection management, indexes, and GridFS buckets.

Two GridFS buckets are exposed:
- ``manifest_blobs``: C2PA Manifest Stores accepted via ``POST /manifests``
  (lookup-API side).
- ``ingest_assets``: optional copy of watermarked output bytes produced by
  the ingest pipeline. The local filesystem store is the source of truth;
  Mongo is for cross-host distribution. Falling back to filesystem-only is
  fine — assets just won't be in Mongo.
"""
from __future__ import annotations

from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
    AsyncIOMotorGridFSBucket,
)
from pymongo import ASCENDING, DESCENDING

from soft_binding_api.core.config import settings


class MongoDB:
    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None
    fs: AsyncIOMotorGridFSBucket | None = None
    asset_fs: AsyncIOMotorGridFSBucket | None = None

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
        cls.asset_fs = AsyncIOMotorGridFSBucket(cls.db, bucket_name="ingest_assets")

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
        await cls.db.supported_algorithms.create_index(
            [("type", ASCENDING), ("alg", ASCENDING)],
            name="type_alg_unique",
            unique=True,
        )

        # Ingestion pipeline records.
        await cls.db.ingestions.create_index(
            [("ingestionId", ASCENDING)],
            name="ingestionId_unique",
            unique=True,
        )
        await cls.db.ingestions.create_index(
            [("alg", ASCENDING), ("bindingValue", ASCENDING)],
            name="alg_value_idx",
        )
        await cls.db.ingestions.create_index(
            [("manifestId", ASCENDING)],
            name="manifestId_idx",
        )
        await cls.db.ingestions.create_index(
            [("createdAt", DESCENDING)],
            name="createdAt_idx",
        )


def get_manifests_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db["manifests"]


def get_soft_bindings_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db["soft_bindings"]


def get_supported_algorithms_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db["supported_algorithms"]


def get_ingestions_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db["ingestions"]


def get_manifest_blobs_bucket() -> AsyncIOMotorGridFSBucket:
    assert MongoDB.fs is not None, "MongoDB not connected"
    return MongoDB.fs


def get_asset_blobs_bucket() -> AsyncIOMotorGridFSBucket:
    assert MongoDB.asset_fs is not None, "MongoDB not connected"
    return MongoDB.asset_fs
