"""
``me.deepmark.audio.vigil.128`` — soft-binding watermark plugin.

Single-file FastAPI service exposing the watermark plugin contract:

- ``GET  /info``    metadata about this plugin
- ``POST /embed``   embed a 128-bit binding into an audio asset
- ``POST /detect``  extract the binding from an audio asset
- ``GET  /health``  liveness probe

Bytes are exchanged over HTTP. ``/embed`` and ``/detect`` accept
``application/octet-stream`` request bodies (the audio bytes). ``/embed``
returns the watermarked bytes as ``application/octet-stream`` and the
binding value in the ``X-Binding-Value`` response header. ``/detect``
returns JSON.

Current implementation is a deterministic dummy:
- ``compute_binding_value`` = base64(SHA-256(bytes)[:16]) — 128 bits.
- ``embed`` is a passthrough (output bytes == input bytes). Real
  Vigil-128 DSP would modulate the audio signal here.
- ``detect`` re-runs ``compute_binding_value`` on the input, so the
  embed/detect pair round-trips cleanly.

When the real DSP lands, replace ``_embed_bytes`` and ``_detect_bytes``;
the FastAPI surface stays as-is.
"""
from __future__ import annotations

import base64
import hashlib

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

ALG = "me.deepmark.audio.vigil.128"
TYPE = "watermark"
VALUE_BITS = 128
MEDIA_TYPES = (
    "audio/wav",
    "audio/wave",
    "audio/x-wav",
    "audio/mpeg",
    "audio/flac",
    "audio/x-flac",
    "audio/ogg",
)

BINDING_VALUE_HEADER = "X-Binding-Value"


# ---------------------------------------------------------------------------
# Core algorithm (swap these two functions to plug in real DSP).
# ---------------------------------------------------------------------------


def compute_binding_value(asset_bytes: bytes) -> str:
    """Deterministic 128-bit binding value, base64-encoded."""
    digest = hashlib.sha256(asset_bytes).digest()[:16]  # 128 bits
    return base64.b64encode(digest).decode("ascii")


def _embed_bytes(asset_bytes: bytes, value_b64: str) -> bytes:  # noqa: ARG001
    """Dummy embedder: passthrough. Replace with real DSP."""
    return asset_bytes


def _detect_bytes(asset_bytes: bytes) -> str | None:
    """Dummy detector: round-trips the dummy embedder."""
    return compute_binding_value(asset_bytes)


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class DetectResponse(BaseModel):
    bindingValue: str | None


class InfoResponse(BaseModel):
    alg: str
    type: str
    valueBits: int
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
        valueBits=VALUE_BITS,
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
                "Watermarked asset bytes. The binding value is returned in "
                f"the ``{BINDING_VALUE_HEADER}`` response header."
            ),
        },
        400: {"description": "Empty request body"},
    },
)
async def embed_endpoint(request: Request) -> Response:
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty request body")
    override = request.headers.get(BINDING_VALUE_HEADER) or None
    value = override or compute_binding_value(data)
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
