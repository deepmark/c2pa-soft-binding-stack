# `me.deepmark.audio.vigil.128` plugin

Soft-binding watermark plugin for the C2PA ingest pipeline. Single-file
FastAPI service. Bytes are exchanged over HTTP — the caller POSTs the
audio body as `application/octet-stream` and the plugin returns the
watermarked bytes plus the binding value in a response header. No
shared filesystem required.

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
Content-Type: application/octet-stream
X-Binding-Value: <optional caller-supplied base64 value>

<raw audio bytes>
```
Response:
```
Content-Type: application/octet-stream
X-Binding-Value: <base64 128-bit value the plugin used>

<watermarked audio bytes>
```

### `POST /detect`
Request:
```
Content-Type: application/octet-stream

<raw audio bytes>
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
