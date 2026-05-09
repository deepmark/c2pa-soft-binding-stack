"""
Plugin pass orchestration + per-pass safety checks.

Runs every requested algorithm's plugin in order, threading mutated bytes
through watermark passes and leaving them untouched on fingerprint passes
(see ``adapters.dispatcher`` for the HTTP contract). Watermark passes
are gated by a media-type-specific format-preservation check so a
plugin that re-samples / mono-mixes / otherwise mutates the container
is rejected before the bytes reach the manifest signer.

Stateless module-level functions (no class, no DI) — this is a
pipeline step, not an application service. The IngestionService calls
``run_plugin_passes`` once per ingest and ``capture_plugin_versions``
once per ingest for the persisted record.
"""
from __future__ import annotations

from collections.abc import Sequence

from ingestion_api.adapters.dispatcher import PluginDispatcher
from ingestion_api.contracts.plugin import PluginEntry
from ingestion_api.contracts.plugin import PluginPass
from ingestion_api.core.logging import get_logger
from ingestion_api.core.errors import IngestionError
from ingestion_api.models.enums import FailureStage, MediaType
from ingestion_api.utils.media import read_audio_format

logger = get_logger(__name__)


def run_plugin_passes(
    entries: Sequence[PluginEntry],
    initial_bytes: bytes,
    media_type: MediaType,
    mime_type: str,
    *,
    request_id: str | None = None,
) -> list[PluginPass]:
    """Run every alg's plugin, threading mutated bytes through.

    Watermark plugins replace ``current_bytes`` with their /embed
    output; fingerprint plugins return only a binding value and leave
    the bytes untouched. ``mime_type`` is forwarded to every plugin so
    it can pick the right decoder. ``media_type`` selects the watermark
    format-preservation check (audio-only today).

    Raises ``IngestionError(stage=PLUGIN_PASS)`` for plugin contract
    violations (empty payload, unsupported alg type, format mutation)
    and re-raises ``PluginUnavailableError`` from the HTTP layer so the
    caller can distinguish transport failures from contract failures.
    """
    passes: list[PluginPass] = []
    current_bytes = initial_bytes
    for entry in entries:
        with PluginDispatcher(entry, request_id=request_id) as plugin:
            if entry.type == "watermark":
                in_format = _snapshot_format(media_type, current_bytes)
                embed = plugin.embed(
                    media_bytes=current_bytes, mime_type=mime_type,
                )
                if not embed.watermarked_bytes:
                    raise IngestionError(
                        f"Plugin {entry.alg!r} returned an empty watermarked payload",
                        stage=FailureStage.PLUGIN_PASS,
                    )
                _verify_format_preserved(
                    entry.alg, media_type, in_format, embed.watermarked_bytes,
                )
                current_bytes = embed.watermarked_bytes
                binding_value = embed.binding_value
            elif entry.type == "fingerprint":
                binding_value = plugin.compute(
                    media_bytes=current_bytes, mime_type=mime_type,
                )
            else:
                raise IngestionError(
                    f"Unsupported alg type {entry.type!r} for {entry.alg!r}",
                    stage=FailureStage.PLUGIN_PASS,
                )
        passes.append(
            PluginPass(
                entry=entry,
                binding_value=binding_value,
                output_bytes=current_bytes,
            ),
        )
    return passes


def capture_plugin_versions(
    entries: Sequence[PluginEntry],
) -> dict[str, dict] | None:
    """Snapshot ``/info`` per alg via the cached PluginDispatcher.info_cached().

    Returns None if every plugin probe fails — keeps the persisted
    record clean (no ``{}``) when plugin metadata isn't available.
    """
    snapshots: dict[str, dict] = {}
    for entry in entries:
        try:
            with PluginDispatcher(entry) as plugin:
                snapshots[entry.alg] = plugin.info_cached()
        except Exception:
            logger.debug(
                "Could not capture /info for alg=%s", entry.alg, exc_info=True,
            )
    return snapshots or None


def _snapshot_format(media_type: MediaType, data: bytes):
    """Return a media-type-specific format snapshot used for the
    watermark format-preservation check, or ``None`` when no inspector
    is implemented for ``media_type`` (the verifier then logs + skips).

    Add a new branch here when image / video plugins land — keep the
    inspector dispatch table in one place rather than scattered in the
    pipeline.
    """
    if media_type is MediaType.AUDIO:
        return read_audio_format(data)
    return None


def _verify_format_preserved(
    alg: str, media_type: MediaType, in_format, out_bytes: bytes,
) -> None:
    """Reject watermark plugins that change a media-type-specific
    format invariant (audio: sample rate + channel count).

    Dispatches by ``media_type``. For media types without an
    implemented inspector the check is skipped with a loud warning so
    adding a new MIME doesn't silently turn the safeguard off.

    Skipped silently when either side is unparseable — we don't want a
    header-parser quirk to nuke an otherwise-valid ingest. Real format
    mismatches (a plugin that mono-mixed a stereo input, re-sampled to
    a different rate, etc.) raise ``IngestionError``.
    """
    if media_type is not MediaType.AUDIO:
        logger.warning(
            "No format-preservation inspector for media_type=%s; "
            "watermark plugin %r ran without a preservation check.",
            media_type.value, alg,
        )
        return
    if in_format is None:
        return
    out_format = read_audio_format(out_bytes)
    if out_format is None:
        logger.warning(
            "Format-preservation check skipped for alg=%s "
            "(could not parse plugin output bytes)", alg,
        )
        return
    if in_format != out_format:
        raise IngestionError(
            f"Plugin {alg!r} changed audio format: "
            f"in=(rate={in_format.sample_rate}, ch={in_format.channels}) "
            f"out=(rate={out_format.sample_rate}, ch={out_format.channels})",
            stage=FailureStage.PLUGIN_PASS,
        )
