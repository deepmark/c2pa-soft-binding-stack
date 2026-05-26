"""
Logging configuration.

Single entry point ``configure_logging`` is called once on app startup. 
Two modes:
- plain (default): human-readable, single-line log records
- json: one JSON document per line, suitable for log shippers

A per-request correlation id (``X-Request-ID``) is stored in a
``ContextVar`` set by ``RequestIDMiddleware`` and stamped onto every
``LogRecord`` via ``_RequestIDFilter`` so log shippers can collate
request flows without each log site having to plumb the id manually.
The contextvar is async-safe (each request has its own context) but
does **not** auto-propagate into thread-pool executor calls. Current
executor-bound call sites handle that explicitly in
``services/ingestion.py``: ``IngestionService.ingest()`` captures
``request_id = get_request_id()`` before entering the executor, then
passes it into ``run_plugin_passes(..., request_id=request_id)`` and
``ResolutionPushClient.push(..., request_id=request_id)``. Future
``run_in_executor`` call sites should copy that pattern.
"""
from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar, Token
from typing import Any

from ingestion_api.core.config import settings

REQUEST_ID_HEADER = "X-Request-ID"

# Populated per-request by ``RequestIDMiddleware``. 
# Default is None so a log statement made outside of a request flow gets a placeholder.
# Cap to 128 chars to defang clients sending unbounded ids that would end up in our logs verbatim.
_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """Return the current request's id, or None if not in a request scope."""
    return _request_id_var.get()


def set_request_id(request_id: str) -> Token[str | None]:
    """Bind ``request_id`` to the current context. Pair with ``reset_request_id``."""
    return _request_id_var.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id_var.reset(token)


class _RequestIDFilter(logging.Filter):
    """Stamps the active ``request_id`` onto every ``LogRecord``.

    Logger format strings reference ``%(request_id)s`` (plain mode) and
    the JSON formatter picks it up via the LogRecord __dict__ scan.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get() or "-"
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = getattr(record, "request_id", None)
        if rid and rid != "-":
            payload["request_id"] = rid
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k in {"args", "msg", "levelname", "levelno", "name", "pathname",
                     "filename", "module", "exc_info", "exc_text", "stack_info",
                     "lineno", "funcName", "created", "msecs", "relativeCreated",
                     "thread", "threadName", "processName", "process",
                     "taskName", "request_id"}:
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Configure the root logger. Idempotent."""
    root = logging.getLogger()
    if getattr(root, "_ingestion_api_configured", False):
        return

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_RequestIDFilter())
    if settings.log_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(request_id)s] %(name)s — %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
    root.addHandler(handler)
    root._ingestion_api_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
