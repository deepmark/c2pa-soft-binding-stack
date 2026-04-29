# `me.deepmark.audio.vigil.128` plugin

Soft-binding watermark plugin for the C2PA ingest pipeline. Single-file
FastAPI service. Bytes are exchanged via a Docker volume shared with
ingestion-api — the caller passes absolute paths in `/embed` /
`/detect`, the plugin reads/writes those files directly.

## HTTP contract

### `GET /info`
```
{ "alg": "me.deepmark.audio.vigil.128", "type": "watermark",
  "valueBits": 128, "mediaTypes": ["audio/wav", ...] }
```

### `GET /health`
```
{ "status": "ok", "alg": "me.deepmark.audio.vigil.128" }
```

### `POST /embed`
Request:
```
{ "input_path": "/shared/<id>/input.wav",
  "output_path": "/shared/<id>/watermarked.wav",
  "value": null }                  # optional; plugin derives if omitted
```
Response:
```
{ "bindingValue": "<base64 128-bit>", "outputPath": "/shared/<id>/watermarked.wav" }
```

### `POST /detect`
Request:
```
{ "input_path": "/shared/<id>/audio.wav" }
```
Response:
```
{ "bindingValue": "<base64 128-bit>" }   # null if no watermark detected
```

## Status: dummy

Current implementation:
- `compute_binding_value` = `base64(SHA-256(bytes)[:16])` — deterministic 128 bits.
- `_embed_bytes` is a passthrough; output bytes equal input bytes.
- `_detect_bytes` re-runs the value derivation, so embed/detect round-trip cleanly.

Replace `_embed_bytes` and `_detect_bytes` in `app.py` to wire in real
Vigil-128 DSP. The HTTP surface and `compute_binding_value` stay the
same.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

…or use docker-compose at the repo root (`docker-compose up watermark-vigil-128`).
