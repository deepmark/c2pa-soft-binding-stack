"""
``me.deepmark.audio.aware.20`` — AWARE soft-binding watermark plugin.

Standard plugin contract:
- ``GET  /info``    metadata about this plugin
- ``POST /embed``   embed a 20-bit binding into an audio asset
- ``POST /detect``  extract the binding from an audio asset
- ``GET  /health``  liveness probe

Bytes are exchanged over HTTP. ``/embed`` and ``/detect`` accept
``application/octet-stream`` request bodies (the audio bytes). ``/embed``
returns the watermarked bytes as ``application/octet-stream`` and the
binding value in the ``X-Binding-Value`` response header. ``/detect``
returns JSON.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import sys

import librosa
import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from aware.service import embed_watermark, detect_watermark
from aware.utils.models import load

logger = logging.getLogger(__name__)

ALG = "me.deepmark.audio.aware.20"
TYPE = "watermark"
VALUE_BITS = 20
MEDIA_TYPES = (
    "audio/wav",
    "audio/wave",
    "audio/x-wav",
    "audio/mpeg",
    "audio/flac",
    "audio/x-flac",
    "audio/ogg",
)
TARGET_SR = 16000
BINDING_VALUE_HEADER = "X-Binding-Value"

try:
    logger.info("Loading AWARE models...")
    embedder, detector = load(name="AWARE(20bps)")
    logger.info("AWARE models loaded successfully")
except Exception as e:
    logger.critical("Failed to load AWARE models: %s", e)
    sys.exit(1)


app = FastAPI(
    title=f"C2PA watermark plugin: {ALG}",
    version="0.1.0",
    description="AWARE soft-binding watermark plugin (20-bit). Bytes exchanged over HTTP.",
)


# ---------------------------------------------------------------------------
# Audio / binding helpers
# ---------------------------------------------------------------------------

def _decode_audio(raw: bytes) -> tuple[np.ndarray, int]:
    audio, sr = librosa.load(io.BytesIO(raw), sr=TARGET_SR, mono=True)
    return audio, sr


def _encode_audio(audio: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV")
    return buf.getvalue()


def _bits_to_binding_value(bits: np.ndarray) -> str:
    packed = np.packbits(bits.astype(np.uint8))
    return base64.b64encode(packed.tobytes()).decode("ascii")


def _binding_value_to_bits(value_b64: str) -> np.ndarray:
    packed = np.frombuffer(base64.b64decode(value_b64), dtype=np.uint8)
    return np.unpackbits(packed)[:VALUE_BITS].astype(np.int32)


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

    audio, sr = _decode_audio(data)

    override = request.headers.get(BINDING_VALUE_HEADER) or None
    if override:
        watermark_data = _binding_value_to_bits(override)
    else:
        watermark_data = np.random.randint(0, 2, size=VALUE_BITS, dtype=np.int32)

    watermarked = embed_watermark(audio, sr, watermark_data, embedder)
    watermarked = np.nan_to_num(watermarked, nan=0.0, posinf=0.0, neginf=0.0)

    value = override or _bits_to_binding_value(watermark_data)
    watermarked_bytes = _encode_audio(watermarked, sr)

    return Response(
        content=watermarked_bytes,
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

    audio, sr = _decode_audio(data)
    detected, confidence = detect_watermark(audio, sr, detector)

    if detected is not None and confidence > 0.5:
        detected = np.nan_to_num(detected, nan=0.0, posinf=1.0, neginf=0.0)
        return DetectResponse(bindingValue=_bits_to_binding_value(detected))

    return DetectResponse(bindingValue=None)


if __name__ == "__main__":
    app_port = int(os.getenv("APP_PORT", 9004))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=app_port)
