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
got.

A future failed-push reconciliation worker will live alongside this
module and query Mongo directly via ``get_ingestions_collection()``.
The ``push_retry_idx`` index in ``core.database`` is sized for that.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeVar

from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import DESCENDING

from ingestion_api.models.enums import ResolutionPushStatus
from ingestion_api.models.ingestion import FailedIngestion, IngestionRecord

_RecordT = TypeVar("_RecordT", IngestionRecord, FailedIngestion)


# ---------------------------------------------------------------------------
# Repository protocols.
#
# Defined so consumers (IngestionService, routers) can declare the
# narrowest surface they need without coupling to a concrete backend
# (Mongo in production, InMemory in tests). Both Mongo* and InMemory*
# implementations below structurally satisfy these Protocols — there's
# no explicit ``class X(Protocol)`` inheritance, just shape compliance,
# which is the whole point of Protocols.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IngestionListPage:
    """Page of ingestions returned by ``list()``."""
    items: list[IngestionRecord]
    next_cursor: str | None


class IngestionRecordRepository(Protocol):
    """Repository surface for the ``ingestions`` collection."""

    async def write(self, record: IngestionRecord) -> None: ...

    async def get(self, ingestion_id: str) -> IngestionRecord | None: ...

    async def delete(self, ingestion_id: str) -> bool: ...

    async def list(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: ResolutionPushStatus | None = None,
    ) -> IngestionListPage: ...


class FailedIngestionRepository(Protocol):
    """Repository surface for the ``failed_ingestions`` collection."""

    async def write(self, record: FailedIngestion) -> None: ...

    async def get(self, ingestion_id: str) -> FailedIngestion | None: ...

    async def delete(self, ingestion_id: str) -> None: ...


def _to_doc(record: IngestionRecord | FailedIngestion) -> dict:
    # _id := ingestionId so the primary-key index doubles as the
    # ingestionId index (no extra index/storage). 
    # mode="python" keeps datetimes as datetime 
    # mode="json" would store ISO strings and break range queries / push_retry_idx ordering.
    doc = record.model_dump(mode="python")
    doc["_id"] = record.ingestionId
    return doc


def _from_doc(model: type[_RecordT], doc: dict) -> _RecordT:
    doc = dict(doc)
    doc.pop("_id", None)
    return model.model_validate(doc)


# ---------------------------------------------------------------------------
# Cursor pagination helpers (opaque base64 of (createdAt iso, ingestionId)).
# Tuple is monotone in (createdAt desc, _id desc), giving stable ordering
# even when two records share createdAt to the millisecond.
# ---------------------------------------------------------------------------


def _encode_cursor(created_at: datetime, ingestion_id: str) -> str:
    payload = json.dumps([created_at.isoformat(), ingestion_id]).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(token: str) -> tuple[datetime, str]:
    pad = "=" * (-len(token) % 4)
    raw = base64.urlsafe_b64decode((token + pad).encode("ascii"))
    parsed = json.loads(raw.decode("utf-8"))
    if not (isinstance(parsed, list) and len(parsed) == 2):
        raise ValueError("malformed cursor")
    created_at = datetime.fromisoformat(parsed[0])
    return created_at, str(parsed[1])


# ---------------------------------------------------------------------------
# IngestionRecord repositories (the success collection).
# ---------------------------------------------------------------------------


class MongoIngestionRecordRepository:
    """Mongo-backed repository. One doc per ingestion in ``ingestions``."""

    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._col = collection

    async def write(self, record: IngestionRecord) -> None:
        doc = _to_doc(record)
        await self._col.replace_one({"_id": record.ingestionId}, doc, upsert=True)

    async def get(self, ingestion_id: str) -> IngestionRecord | None:
        doc = await self._col.find_one({"_id": ingestion_id})
        return _from_doc(IngestionRecord, doc) if doc else None

    async def delete(self, ingestion_id: str) -> bool:
        """Remove the record. Returns True if a doc was actually deleted."""
        result = await self._col.delete_one({"_id": ingestion_id})
        return result.deleted_count > 0

    async def list(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: ResolutionPushStatus | None = None,
    ) -> IngestionListPage:
        """Paginate ingestions, newest-first.

        ``cursor`` is an opaque token returned as ``next_cursor`` in the
        previous page. Stable across same-millisecond ties because the
        sort is (``createdAt`` desc, ``_id`` desc).
        """
        if limit <= 0:
            return IngestionListPage(items=[], next_cursor=None)

        query: dict = {}
        if status is not None:
            query["resolutionPushStatus"] = status.value
        if cursor:
            try:
                last_created, last_id = _decode_cursor(cursor)
            except (ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid cursor: {exc}") from exc
            query["$or"] = [
                {"createdAt": {"$lt": last_created}},
                {"createdAt": last_created, "_id": {"$lt": last_id}},
            ]

        sort = [("createdAt", DESCENDING), ("_id", DESCENDING)]
        # Fetch limit+1 to detect whether there's a next page without
        # an extra round-trip.
        docs = await (
            self._col.find(query).sort(sort).limit(limit + 1).to_list(limit + 1)
        )
        has_more = len(docs) > limit
        docs = docs[:limit]
        items = [_from_doc(IngestionRecord, d) for d in docs]
        next_cursor: str | None = None
        if has_more and docs:
            tail = docs[-1]
            next_cursor = _encode_cursor(tail["createdAt"], tail["_id"])
        return IngestionListPage(items=items, next_cursor=next_cursor)


class InMemoryIngestionRecordRepository:
    """Process-local dict-backed repository. Test-only — never persists."""

    def __init__(self) -> None:
        self._records: dict[str, IngestionRecord] = {}

    async def write(self, record: IngestionRecord) -> None:
        self._records[record.ingestionId] = record

    async def get(self, ingestion_id: str) -> IngestionRecord | None:
        return self._records.get(ingestion_id)

    async def delete(self, ingestion_id: str) -> bool:
        return self._records.pop(ingestion_id, None) is not None

    async def list(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: ResolutionPushStatus | None = None,
    ) -> IngestionListPage:
        if limit <= 0:
            return IngestionListPage(items=[], next_cursor=None)
        all_records = sorted(
            self._records.values(),
            key=lambda r: (r.createdAt, r.ingestionId),
            reverse=True,
        )
        if status is not None:
            all_records = [r for r in all_records if r.resolutionPushStatus is status]
        if cursor:
            try:
                last_created, last_id = _decode_cursor(cursor)
            except (ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid cursor: {exc}") from exc
            all_records = [
                r for r in all_records
                if (r.createdAt, r.ingestionId) < (last_created, last_id)
            ]
        page = all_records[:limit]
        next_cursor: str | None = None
        if len(all_records) > limit and page:
            tail = page[-1]
            next_cursor = _encode_cursor(tail.createdAt, tail.ingestionId)
        return IngestionListPage(items=page, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# FailedIngestion repositories (the failure collection).
# ---------------------------------------------------------------------------


class MongoFailedIngestionRepository:
    """Mongo-backed repository for pipeline failures (``failed_ingestions``)."""

    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._col = collection

    async def write(self, record: FailedIngestion) -> None:
        doc = _to_doc(record)
        await self._col.replace_one({"_id": record.ingestionId}, doc, upsert=True)

    async def get(self, ingestion_id: str) -> FailedIngestion | None:
        doc = await self._col.find_one({"_id": ingestion_id})
        return _from_doc(FailedIngestion, doc) if doc else None

    async def delete(self, ingestion_id: str) -> None:
        await self._col.delete_one({"_id": ingestion_id})


class InMemoryFailedIngestionRepository:
    """Process-local dict-backed failure repository. Test-only."""

    def __init__(self) -> None:
        self._records: dict[str, FailedIngestion] = {}

    async def write(self, record: FailedIngestion) -> None:
        self._records[record.ingestionId] = record

    async def get(self, ingestion_id: str) -> FailedIngestion | None:
        return self._records.get(ingestion_id)

    async def delete(self, ingestion_id: str) -> None:
        self._records.pop(ingestion_id, None)
