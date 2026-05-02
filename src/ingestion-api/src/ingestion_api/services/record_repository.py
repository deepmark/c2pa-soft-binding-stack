"""
Repository for ``IngestionRecord`` entities.

Two implementations:

- ``MongoIngestionRecordRepository`` — production. Backed by the
  ``ingestions`` collection in ingestion-api's own MongoDB.
- ``InMemoryIngestionRecordRepository`` — for tests, so the unit suite
  can exercise the orchestrator + routes without spinning up Mongo.

Both expose the same duck-typed surface (``write`` / ``get`` /
``delete``). The orchestrator and routes type-hint with the production
class for documentation; tests pass the in-memory variant. Python's
runtime doesn't care.

The split between this module (``record_repository``) and
``artifact_store`` is intentional: "repository" = typed records in a
database, "store" = opaque binary files on disk.

A future failed-push reconciliation worker will live alongside this
module and query Mongo directly via ``get_ingestions_collection()``;
the indexes in ``core.database`` are sized for that workload.
"""
from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorCollection

from ingestion_api.models.ingestion import IngestionRecord


def _to_doc(record: IngestionRecord) -> dict:
    # _id mirrors ingestionId so lookups are O(log n) on the primary key
    # without an additional index. Pydantic handles enum/datetime
    # serialisation; mode="python" keeps datetimes as datetime so Mongo
    # stores them natively (queryable as dates).
    doc = record.model_dump(mode="python")
    doc["_id"] = record.ingestionId
    return doc


def _from_doc(doc: dict) -> IngestionRecord:
    doc = dict(doc)
    doc.pop("_id", None)
    return IngestionRecord.model_validate(doc)


class MongoIngestionRecordRepository:
    """Mongo-backed repository. One doc per ingestion in the ``ingestions`` collection."""

    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._col = collection

    async def write(self, record: IngestionRecord) -> None:
        doc = _to_doc(record)
        await self._col.replace_one({"_id": record.ingestionId}, doc, upsert=True)

    async def get(self, ingestion_id: str) -> IngestionRecord | None:
        doc = await self._col.find_one({"_id": ingestion_id})
        return _from_doc(doc) if doc else None

    async def delete(self, ingestion_id: str) -> None:
        await self._col.delete_one({"_id": ingestion_id})


class InMemoryIngestionRecordRepository:
    """Process-local dict-backed repository. Test-only — never persists."""

    def __init__(self) -> None:
        self._records: dict[str, IngestionRecord] = {}

    async def write(self, record: IngestionRecord) -> None:
        self._records[record.ingestionId] = record

    async def get(self, ingestion_id: str) -> IngestionRecord | None:
        return self._records.get(ingestion_id)

    async def delete(self, ingestion_id: str) -> None:
        self._records.pop(ingestion_id, None)
