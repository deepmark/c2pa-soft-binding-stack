"""
``me.deepmark.audio.vigil.128`` — soft-binding watermark plugin.

Single-file FastAPI service exposing the watermark plugin contract:

- ``GET  /info``    metadata about this plugin
- ``POST /embed``   embed a 128-bit binding into the input audio
- ``POST /detect``  extract the binding from an input
- ``GET  /health``  liveness probe

Bytes are exchanged via a Docker volume shared with ingestion-api. The
caller specifies absolute ``input_path`` / ``output_path`` under that
volume; this plugin reads/writes those paths directly. We never accept
audio over the wire.

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
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

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


class EmbedRequest(BaseModel):
    input_path: str = Field(..., description="Absolute path to source audio on the shared volume")
    output_path: str = Field(..., description="Absolute path where watermarked audio should be written")
    value: str | None = Field(
        None,
        description=(
            "Optional caller-provided binding value. When omitted, the plugin "
            "derives it deterministically from the input bytes."
        ),
    )


class EmbedResponse(BaseModel):
    bindingValue: str
    outputPath: str


class DetectRequest(BaseModel):
    input_path: str = Field(..., description="Absolute path to audio on the shared volume")


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
    description="Soft-binding watermark plugin (dummy 128-bit). Bytes via shared volume.",
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


@app.post("/embed", response_model=EmbedResponse, summary="Embed a soft-binding watermark")
def embed_endpoint(req: EmbedRequest) -> EmbedResponse:
    src = Path(req.input_path)
    dst = Path(req.output_path)
    if not src.is_file():
        raise HTTPException(status_code=400, detail=f"input_path not found: {src}")
    try:
        data = src.read_bytes()
        value = req.value or compute_binding_value(data)
        watermarked = _embed_bytes(data, value)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if watermarked is data:
            # Passthrough fast path; avoid a redundant copy if src == dst.
            if src.resolve() != dst.resolve():
                shutil.copyfile(src, dst)
        else:
            dst.write_bytes(watermarked)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"I/O error: {exc}") from exc
    return EmbedResponse(bindingValue=value, outputPath=str(dst))


@app.post("/detect", response_model=DetectResponse, summary="Detect a soft-binding watermark")
def detect_endpoint(req: DetectRequest) -> DetectResponse:
    src = Path(req.input_path)
    if not src.is_file():
        raise HTTPException(status_code=400, detail=f"input_path not found: {src}")
    try:
        data = src.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"I/O error: {exc}") from exc
    return DetectResponse(bindingValue=_detect_bytes(data))
