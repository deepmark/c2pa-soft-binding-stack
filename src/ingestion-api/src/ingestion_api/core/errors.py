"""
Pipeline error hierarchy for the ingestion service.
"""
from __future__ import annotations

from ingestion_api.models.enums import FailureStage


class IngestionError(RuntimeError):
    """Generic ingestion failure with a stable HTTP-friendly message.

    Carries a ``stage`` so the orchestrator's failure-persistence layer
    can record where in the pipeline things blew up.
    """

    def __init__(self, msg: str, *, stage: FailureStage = FailureStage.UNKNOWN) -> None:
        super().__init__(msg)
        self.stage = stage


class UnsupportedMediaError(IngestionError):
    """4xx-class — MIME isn't in the supported registry. Pre-allocate, never persisted."""


class InvalidAlgRequestError(IngestionError):
    """4xx-class — caller asked for an empty / unknown / MIME-incompatible alg.

    Raised before allocating an ingestion id (where possible) so the
    failure isn't persisted as a pipeline error.
    """


class MissingSigningMaterialError(IngestionError):
    """Cert/key files vanished or became unreadable at sign time.

    Distinct type so the ingest router can map it to a 503 cleanly,
    rather than catching a bare ``FileNotFoundError``
    (which would swallow unrelated FS failures from anywhere in the call tree).
    """


class PluginUnavailableError(IngestionError):
    """The plugin container couldn't be reached, or returned a non-2xx."""


class PluginNotFoundError(IngestionError):
    """The catalog has no plugin entry for the requested ``alg`` identifier."""
