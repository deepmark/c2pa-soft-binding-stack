# ingestion-api

Audio ingest pipeline: takes an upload, POSTs the audio bytes to a
watermark plugin over HTTP, builds + signs a C2PA manifest, persists
artifacts, and (optionally) auto-pushes the manifest store + binding
to `resolution-api`.

For the full picture (compose stack, plugins, resolution-api) see the
[top-level README](../../README.md).

## Run

```bash
pip install -e ".[dev]"
ingestion-api                # uvicorn on port 8001
```

`POST /ingest` requires:
- a watermark plugin reachable at the `url` in `<repo>/algorithms.yaml`
  (default: `http://watermark-vigil-128:8000` from inside the docker
  network),
- an ES256 cert chain + private key under `<repo>/credentials/`,
- (optional) the resolution API URL via `RESOLUTION_API_URL=` to enable
  auto-push.

## Config

Settings live in `src/ingestion_api/core/config.py`. Common overrides:

| Var | Default | What |
| --- | --- | --- |
| `DEFAULT_AUDIO_ALG` | `me.deepmark.audio.vigil.128` | Default plugin to call from `POST /ingest` |
| `ALGORITHMS_CATALOG_PATH` | `<repo>/algorithms.yaml` | Shared catalog |
| `STORAGE_ROOT` | `<service>/storage` | Local artifact store |
| `CREDENTIALS_DIR` | `<repo>/credentials` | ES256 cert + key root |
| `SIGNING_ALG` | `ES256` | C2PA signing algorithm |
| `TA_URL` | _(unset)_ | RFC 3161 timestamp authority |
| `RESOLUTION_API_URL` | _(unset)_ → SKIP | Auto-push target |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `false` | Logging |

## Tests

```bash
pytest -q
```

Tests use `httpx.MockTransport` to stub the plugin and the
resolution-api. Real C2PA signing is exercised end-to-end against the
ES256 test cert chain — drop one in `<repo>/credentials/` (see the
[top-level README](../../README.md)) or those tests will skip.
