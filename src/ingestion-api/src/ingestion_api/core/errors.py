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
    """4xx-class — MIME isn't in the supported registry."""


class InvalidAlgRequestError(IngestionError):
    """4xx-class — caller asked for an empty / unknown / MIME-incompatible alg."""


class MissingSigningMaterialError(IngestionError):
    """Cert/key files vanished or became unreadable at sign time."""


class PluginUnavailableError(IngestionError):
    """The plugin container couldn't be reached, or returned a non-2xx response."""


class PluginNotFoundError(IngestionError):
    """The catalog has no plugin entry for the requested ``alg`` identifier."""
