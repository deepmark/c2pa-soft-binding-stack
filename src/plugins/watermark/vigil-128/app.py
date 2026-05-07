"""
``me.deepmark.audio.vigil.128`` — soft-binding watermark plugin.

Single-file FastAPI service exposing the watermark plugin contract:

- ``GET  /info``    metadata about this plugin
- ``POST /embed``   embed a 128-bit binding into an audio asset
- ``POST /detect``  extract the binding from an audio asset
- ``GET  /health``  liveness probe

Header contract on the binary endpoints:
- ``Content-Type: application/octet-stream`` — opaque bytes on the wire.
- ``X-Media-Type: <mime>`` — what the bytes actually are (e.g. ``audio/wav``).
- ``X-Binding-Value`` — REQUIRED on ``/embed``. The caller (ingestion-api)
  mints the binding value and tells the plugin what to embed. The plugin
  echoes the same value in the response header.

``/embed`` returns the watermarked bytes as ``application/octet-stream``
plus the binding value in ``X-Binding-Value``. ``/detect`` returns JSON.

Current implementation is a deterministic dummy:
- ``_embed_bytes`` is a passthrough (output bytes == input bytes) but
  records ``sha256(bytes) -> value`` in a process-local map so detect
  can recover what was embedded. Real Vigil-128 DSP would modulate the
  audio signal here so detection is byte-derived rather than map-derived.
- ``_detect_bytes`` looks the value up in that map, returning ``None``
  if these bytes were never embedded by this process.

When the real DSP lands, replace ``_embed_bytes`` and ``_detect_bytes``;
the FastAPI surface stays as-is.
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

ALG = "me.deepmark.audio.vigil.128"
TYPE = "watermark"
BINDING_BITS = 128
MEDIA_TYPES = (
    "audio/wav",
    "audio/wave",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp3",
    "audio/flac",
    "audio/x-flac",
    "audio/mp4",
)

BINDING_VALUE_HEADER = "X-Binding-Value"
MEDIA_TYPE_HEADER = "X-Media-Type"

# Cap on the dummy embed/detect side-channel map. Per-process, per-worker.
# Plenty for tests and local dev; real DSP doesn't need this at all.
_DUMMY_MAP_CAP = 1024
_DUMMY_EMBED_MAP: "OrderedDict[str, str]" = OrderedDict()


# ---------------------------------------------------------------------------
# Core algorithm (swap these two functions to plug in real DSP).
# ---------------------------------------------------------------------------


def _embed_bytes(asset_bytes: bytes, value_b64: str) -> bytes:
    """Dummy embedder: passthrough bytes, stash the value in a side map.

    Passthrough is required because the watermarked output is fed to the
    C2PA SDK for signing, and the SDK needs a valid container (WAV/MP3/...).
    A real DSP would modulate the audio signal so the value lives inside
    the bytes; the dummy fakes it via a process-local lookup.
    """
    key = hashlib.sha256(asset_bytes).hexdigest()
    _DUMMY_EMBED_MAP[key] = value_b64
    _DUMMY_EMBED_MAP.move_to_end(key)
    while len(_DUMMY_EMBED_MAP) > _DUMMY_MAP_CAP:
        _DUMMY_EMBED_MAP.popitem(last=False)
    return asset_bytes


def _detect_bytes(asset_bytes: bytes) -> str | None:
    """Dummy detector: look up what the dummy embedder stashed."""
    key = hashlib.sha256(asset_bytes).hexdigest()
    return _DUMMY_EMBED_MAP.get(key)


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class DetectResponse(BaseModel):
    bindingValue: str | None


class InfoResponse(BaseModel):
    alg: str
    type: str
    bindingBits: int
    mediaTypes: list[str]


app = FastAPI(
    title=f"C2PA watermark plugin: {ALG}",
    version="0.1.0",
    description=(
        "Soft-binding watermark plugin (dummy 128-bit). "
        "Bytes exchanged over HTTP (octet-stream)."
    ),
)


@app.get("/info", response_model=InfoResponse, summary="Plugin metadata")
def info() -> InfoResponse:
    return InfoResponse(
        alg=ALG,
        type=TYPE,
        bindingBits=BINDING_BITS,
        mediaTypes=list(MEDIA_TYPES),
    )


@app.get("/health", summary="Liveness probe")
def health() -> dict:
    return {"status": "ok", "alg": ALG}


@app.post(
    "/embed",
    summary="Embed a soft-binding watermark",
    responses={
        200: {
            "content": {"application/octet-stream": {}},
            "description": (
                "Watermarked asset bytes. The binding value is echoed in "
                f"the ``{BINDING_VALUE_HEADER}`` response header."
            ),
        },
        400: {"description": "Empty body or missing required header"},
    },
)
async def embed_endpoint(request: Request) -> Response:
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty request body")
    value = request.headers.get(BINDING_VALUE_HEADER)
    if not value:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required header {BINDING_VALUE_HEADER!r}",
        )
    watermarked = _embed_bytes(data, value)
    return Response(
        content=watermarked,
        media_type="application/octet-stream",
        headers={BINDING_VALUE_HEADER: value},
    )


@app.post(
    "/detect",
    response_model=DetectResponse,
    summary="Detect a soft-binding watermark",
    responses={400: {"description": "Empty request body"}},
)
async def detect_endpoint(request: Request) -> DetectResponse:
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty request body")
    return DetectResponse(bindingValue=_detect_bytes(data))
