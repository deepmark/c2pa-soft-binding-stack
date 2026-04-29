# Fingerprint plugins

This is the home for `type: fingerprint` C2PA soft-binding algorithm
plugins. Each plugin is its own folder with its own Dockerfile + FastAPI
app, mirroring the watermark plugin pattern.

## Adding a new fingerprint plugin

1. Create a folder `plugins/fingerprint/<name>/`.
2. Implement `app.py` exposing the **fingerprint plugin contract**:

   ### `GET /info`
   ```
   { "alg": "<reverse-DNS alg id>", "type": "fingerprint",
     "valueBits": <int>, "mediaTypes": ["audio/wav", ...] }
   ```

   ### `GET /health`
   ```
   { "status": "ok", "alg": "<alg id>" }
   ```

   ### `POST /compute`
   Request:
   ```
   { "input_path": "/shared/<id>/audio.wav" }
   ```
   Response:
   ```
   { "bindingValue": "<base64 fingerprint value>" }
   ```

   Bytes are exchanged via the shared Docker volume — `input_path` is an
   absolute path on that volume.

3. Add `requirements.txt` and a `Dockerfile` (copy from
   `plugins/watermark/vigil-128/` and adapt).

4. Register the plugin in the root `algorithms.yaml`:
   ```yaml
   - alg: org.example.audiofp.v1
     type: fingerprint
     valueBits: 128
     mediaTypes: ["audio/wav", "audio/mpeg"]
     url: http://fingerprint-example:8000
   ```

5. Add a service block to the root `docker-compose.yml` mirroring
   `watermark-vigil-128` — same shared-volume mount, port mapping,
   container hostname matching the YAML `url`.

Fingerprint plugins do **not** expose `/embed` or `/detect`. They only
compute a value from the bytes; the resolution API and ingestion API
treat them differently from watermarks (no embedding step in the
ingest pipeline; matching happens server-side by recomputing the
fingerprint over an uploaded asset).
