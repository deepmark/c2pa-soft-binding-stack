"""
Repositories for persisted ingest records.

Two collections, each with a Mongo-backed and an in-memory variant:

- ``MongoIngestionRecordRepository`` — production. Backed by the
  ``ingestions`` collection.
- ``MongoFailedIngestionRepository`` — production. Backed by the
  ``failed_ingestions`` collection.
- ``InMemoryIngestionRecordRepository`` / ``InMemoryFailedIngestionRepository``
  — test-only.

Both surfaces expose ``write`` / ``get`` / ``delete`` and are duck-typed
across mem/Mongo so the orchestrator and routes don't care which they
got. Writes always stamp ``updatedAt = utcnow()`` so the reconciler
can find stale records cheaply.

The split between this module (``record_repository``) and
``artifact_store`` is intentional: "repository" = typed records in a
database, "store" = opaque binary files on disk.

A future failed-push reconciliation worker will live alongside this
module and query Mongo directly via ``get_ingestions_collection()``;
the indexes in ``core.database`` are sized for that workload.
"""
from __future__ import annotations

from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorCollection

from ingestion_api.models.ingestion import FailedIngestion, IngestionRecord


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_doc_ingestion(record: IngestionRecord) -> dict:
    # _id mirrors ingestionId so lookups are O(log n) on the primary key
    # without an additional index. Pydantic handles enum/datetime
    # serialisation; mode="python" keeps datetimes as datetime so Mongo
    # stores them natively (queryable as dates).
    doc = record.model_dump(mode="python")
    doc["_id"] = record.ingestionId
    return doc


def _from_doc_ingestion(doc: dict) -> IngestionRecord:
    doc = dict(doc)
    doc.pop("_id", None)
    return IngestionRecord.model_validate(doc)


def _to_doc_failed(record: FailedIngestion) -> dict:
    doc = record.model_dump(mode="python")
    doc["_id"] = record.ingestionId
    return doc


def _from_doc_failed(doc: dict) -> FailedIngestion:
    doc = dict(doc)
    doc.pop("_id", None)
    return FailedIngestion.model_validate(doc)


# ---------------------------------------------------------------------------
# IngestionRecord repositories (the success collection).
# ---------------------------------------------------------------------------


class MongoIngestionRecordRepository:
    """Mongo-backed repository. One doc per ingestion in ``ingestions``."""

    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._col = collection

    async def write(self, record: IngestionRecord) -> None:
        # Auto-stamp updatedAt so callers don't have to remember.
        # model_copy(update=...) keeps the original immutable, which is
        # nice for tests asserting on the input record.
        record = record.model_copy(update={"updatedAt": _now_utc()})
        doc = _to_doc_ingestion(record)
        await self._col.replace_one({"_id": record.ingestionId}, doc, upsert=True)

    async def get(self, ingestion_id: str) -> IngestionRecord | None:
        doc = await self._col.find_one({"_id": ingestion_id})
        return _from_doc_ingestion(doc) if doc else None

    async def delete(self, ingestion_id: str) -> None:
        await self._col.delete_one({"_id": ingestion_id})


class InMemoryIngestionRecordRepository:
    """Process-local dict-backed repository. Test-only — never persists."""

    def __init__(self) -> None:
        self._records: dict[str, IngestionRecord] = {}

    async def write(self, record: IngestionRecord) -> None:
        self._records[record.ingestionId] = record.model_copy(
            update={"updatedAt": _now_utc()},
        )

    async def get(self, ingestion_id: str) -> IngestionRecord | None:
        return self._records.get(ingestion_id)

    async def delete(self, ingestion_id: str) -> None:
        self._records.pop(ingestion_id, None)


# ---------------------------------------------------------------------------
# FailedIngestion repositories (the failure collection).
# ---------------------------------------------------------------------------


class MongoFailedIngestionRepository:
    """Mongo-backed repository for pipeline failures (``failed_ingestions``)."""

    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._col = collection

    async def write(self, record: FailedIngestion) -> None:
        record = record.model_copy(update={"updatedAt": _now_utc()})
        doc = _to_doc_failed(record)
        await self._col.replace_one({"_id": record.ingestionId}, doc, upsert=True)

    async def get(self, ingestion_id: str) -> FailedIngestion | None:
        doc = await self._col.find_one({"_id": ingestion_id})
        return _from_doc_failed(doc) if doc else None

    async def delete(self, ingestion_id: str) -> None:
        await self._col.delete_one({"_id": ingestion_id})


class InMemoryFailedIngestionRepository:
    """Process-local dict-backed failure repository. Test-only."""

    def __init__(self) -> None:
        self._records: dict[str, FailedIngestion] = {}

    async def write(self, record: FailedIngestion) -> None:
        self._records[record.ingestionId] = record.model_copy(
            update={"updatedAt": _now_utc()},
        )

    async def get(self, ingestion_id: str) -> FailedIngestion | None:
        return self._records.get(ingestion_id)

    async def delete(self, ingestion_id: str) -> None:
        self._records.pop(ingestion_id, None)
