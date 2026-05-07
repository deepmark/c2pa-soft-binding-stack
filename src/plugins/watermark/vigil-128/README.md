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
  "bindingBits": 128, "mediaTypes": ["audio/wav", ...] }
```

### `GET /health`
```
{ "status": "ok", "alg": "me.deepmark.audio.vigil.128" }
```

### `POST /embed`
Request:
```
Content-Type: application/octet-stream
X-Media-Type: <source MIME, e.g. audio/wav>
X-Binding-Value: <REQUIRED — the urlsafe-base64 value to embed>

<raw audio bytes>
```
Response:
```
Content-Type: application/octet-stream
X-Binding-Value: <echoes the request value>

<watermarked audio bytes>
```

The caller (ingestion-api) mints the binding value; this plugin only
embeds what it's told. The response header MUST equal the request
header — ingestion-api verifies the echo to catch buggy plugins.

### `POST /detect`
Request:
```
Content-Type: application/octet-stream

<raw audio bytes>
```
Response:
```
{ "bindingValue": "<urlsafe-base64 128-bit>" }   # null if no watermark detected
```

## Status: dummy

Current implementation:
- `_embed_bytes` is a passthrough (output bytes == input bytes) but
  records `sha256(bytes) -> value` in a process-local map so detect
  can recover what was embedded.
- `_detect_bytes` looks the value up in that map; returns `null` for
  bytes the dummy never embedded in this process.

Caveat: the side-channel map is per-worker, so embed and detect must
land on the same process for the round-trip to work. Real Vigil-128 DSP
will modulate the audio signal directly, removing this constraint.

Replace `_embed_bytes` and `_detect_bytes` in `app.py` to wire in real
Vigil-128 DSP. The HTTP surface stays the same.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

…or use docker-compose at the repo root (`docker-compose up watermark-vigil-128`).
