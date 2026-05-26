"""
Repositories for persisted ingest records.

``MongoIngestionRecordRepository`` is backed by the ``ingestions``
collection. ``InMemoryIngestionRecordRepository`` is test-only.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import DESCENDING

from ingestion_api.models.enums import IngestionStatus, ResolutionPushStatus
from ingestion_api.models.ingestion import IngestionRecord


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
        ingestion_status: IngestionStatus | None = None,
        resolution_push_status: ResolutionPushStatus | None = None,
    ) -> IngestionListPage: ...


def _to_doc(record: IngestionRecord) -> dict:
    # _id := ingestionId so the primary-key index doubles as the
    # ingestionId index. mode="python" keeps datetimes as datetime;
    # mode="json" would store ISO strings and break range queries.
    doc = record.model_dump(mode="python")
    doc["_id"] = record.ingestionId
    return doc


def _from_doc(doc: dict) -> IngestionRecord:
    doc = dict(doc)
    doc.pop("_id", None)
    return IngestionRecord.model_validate(doc)


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


def _apply_filters(
    query: dict,
    *,
    ingestion_status: IngestionStatus | None,
    resolution_push_status: ResolutionPushStatus | None,
) -> None:
    if ingestion_status is not None:
        query["status"] = ingestion_status.value
    if resolution_push_status is not None:
        query["resolutionPushStatus"] = resolution_push_status.value


class MongoIngestionRecordRepository:
    """Mongo-backed repository. One doc per ingestion in ``ingestions``."""

    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._col = collection

    async def write(self, record: IngestionRecord) -> None:
        doc = _to_doc(record)
        await self._col.replace_one({"_id": record.ingestionId}, doc, upsert=True)

    async def get(self, ingestion_id: str) -> IngestionRecord | None:
        doc = await self._col.find_one({"_id": ingestion_id})
        return _from_doc(doc) if doc else None

    async def delete(self, ingestion_id: str) -> bool:
        result = await self._col.delete_one({"_id": ingestion_id})
        return result.deleted_count > 0

    async def list(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        ingestion_status: IngestionStatus | None = None,
        resolution_push_status: ResolutionPushStatus | None = None,
    ) -> IngestionListPage:
        if limit <= 0:
            return IngestionListPage(items=[], next_cursor=None)

        query: dict = {}
        _apply_filters(
            query,
            ingestion_status=ingestion_status,
            resolution_push_status=resolution_push_status,
        )
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
        docs = await (
            self._col.find(query).sort(sort).limit(limit + 1).to_list(limit + 1)
        )
        has_more = len(docs) > limit
        docs = docs[:limit]
        items = [_from_doc(d) for d in docs]
        next_cursor = None
        if has_more and docs:
            tail = docs[-1]
            next_cursor = _encode_cursor(tail["createdAt"], tail["_id"])
        return IngestionListPage(items=items, next_cursor=next_cursor)


class InMemoryIngestionRecordRepository:
    """Process-local dict-backed repository. Test-only."""

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
        ingestion_status: IngestionStatus | None = None,
        resolution_push_status: ResolutionPushStatus | None = None,
    ) -> IngestionListPage:
        if limit <= 0:
            return IngestionListPage(items=[], next_cursor=None)
        all_records = sorted(
            self._records.values(),
            key=lambda r: (r.createdAt, r.ingestionId),
            reverse=True,
        )
        if ingestion_status is not None:
            all_records = [r for r in all_records if r.status is ingestion_status]
        if resolution_push_status is not None:
            all_records = [
                r for r in all_records
                if r.resolutionPushStatus is resolution_push_status
            ]
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
        next_cursor = None
        if len(all_records) > limit and page:
            tail = page[-1]
            next_cursor = _encode_cursor(tail.createdAt, tail.ingestionId)
        return IngestionListPage(items=page, next_cursor=next_cursor)
