"""
Async MongoDB connection management, indexes, and the manifest GridFS bucket.

Owned by the resolution API only. Ingestion has been split into a separate
service (``ingestion-api``) which keeps its artifacts on the filesystem;
the only piece that reaches Mongo is the eventual ``POST /manifests`` +
``POST /bindings`` calls back into this service.
"""
from __future__ import annotations

from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
    AsyncIOMotorGridFSBucket,
)
from pymongo import ASCENDING

from resolution_api.core.config import settings


class MongoDB:
    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None
    fs: AsyncIOMotorGridFSBucket | None = None

    @classmethod
    async def connect(cls) -> None:
        cls.client = AsyncIOMotorClient(
            settings.mongodb_url,
            serverSelectionTimeoutMS=5_000,
            maxPoolSize=50,
            retryWrites=True,
            uuidRepresentation="standard",
        )
        cls.db = cls.client[settings.mongodb_database]
        cls.fs = AsyncIOMotorGridFSBucket(cls.db, bucket_name=settings.manifest_blobs_bucket)

        await cls.client.admin.command("ping")
        await cls._ensure_indexes()

    @classmethod
    async def close(cls) -> None:
        if cls.client is not None:
            cls.client.close()

    @classmethod
    async def _ensure_indexes(cls) -> None:
        assert cls.db is not None

        soft_bindings = cls.db[settings.soft_bindings_collection]
        supported_algorithms = cls.db[settings.supported_algorithms_collection]

        await soft_bindings.create_index(
            [("alg", ASCENDING), ("value", ASCENDING)],
            name="alg_value_idx",
        )
        await soft_bindings.create_index(
            [("manifestId", ASCENDING)],
            name="manifestId_idx",
        )
        await soft_bindings.create_index(
            [("alg", ASCENDING), ("value", ASCENDING), ("manifestId", ASCENDING)],
            name="binding_unique",
            unique=True,
        )
        await supported_algorithms.create_index(
            [("alg", ASCENDING)],
            name="alg_unique",
            unique=True,
        )


def get_manifests_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db[settings.manifests_collection]


def get_soft_bindings_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db[settings.soft_bindings_collection]


def get_supported_algorithms_collection() -> AsyncIOMotorCollection:
    assert MongoDB.db is not None, "MongoDB not connected"
    return MongoDB.db[settings.supported_algorithms_collection]


def get_manifest_blobs_bucket() -> AsyncIOMotorGridFSBucket:
    assert MongoDB.fs is not None, "MongoDB not connected"
    return MongoDB.fs
