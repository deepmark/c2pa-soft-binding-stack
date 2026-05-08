"""Enums + constants shared across the persisted and wire models."""
from __future__ import annotations

from enum import Enum
from typing import Literal

# Stamped onto every persisted doc (IngestionRecord, FailedIngestion) via
# ``schemaVersion``. Bump on any breaking change to either model so
# read-side code / migrations can branch on the version of the doc in
# hand.
SCHEMA_VERSION = 1


SoftBindingKind = Literal["watermark", "fingerprint"]


class MediaType(str, Enum):
    """Top-level media category an ingest belongs to.

    Derived from the upload's MIME at ingest time and persisted on the
    record so ops queries can filter by media without parsing MIME
    strings.

    Add a new value when adding support for a new media family.
    """
    AUDIO = "audio"
    VIDEO = "video"
    IMAGE = "image"


class FailureStage(str, Enum):
    """Where in the pipeline a failure landed. Set by ``_run_pipeline``."""
    PLUGIN_PASS = "plugin_pass"                  # /embed or /compute failed
    MANIFEST_SIGN = "manifest_sign"              # Builder.sign blew up
    MANIFEST_ID_EXTRACT = "manifest_id_extract"  # Reader returned no active_manifest
    UNKNOWN = "unknown"                          # caught everything else


class ResolutionPushStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"  # when RESOLUTION_PUSH_ENABLED=false (standalone deploy)
