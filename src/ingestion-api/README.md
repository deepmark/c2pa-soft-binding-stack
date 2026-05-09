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
- a watermark plugin reachable at the `url` in `<repo>/plugins.yaml`
  (default: `http://watermark-vigil-128:8000` from inside the docker
  network),
- an ES256 cert chain + private key under `<repo>/credentials/`,
- a reachable resolution-api at `RESOLUTION_API_URL` (or
  `RESOLUTION_PUSH_ENABLED=false` to opt out of auto-push entirely).

## Config

Settings live in `src/ingestion_api/core/config.py`. Common overrides:

Soft-binding algs are now passed per request via the `algs` form field
on `POST /ingest` (repeated form fields, e.g.
`-F 'algs=me.deepmark.audio.vigil.128'`). The catalog
(`plugins.yaml`) is the source of truth for which algs this
deployment can serve; there is no service-wide default.

| Var | Default | What |
| --- | --- | --- |
| `MONGODB_URL` | **required** | Ingestion-api's own Mongo cluster |
| `DATABASE_NAME` | **required** | Database for the `ingestions` collection |
| `PLUGINS_CATALOG_PATH` | **required** | Shared catalog (compose mounts `/catalog/plugins.yaml`) |
| `STORAGE_ROOT` | **required** | Local artifact store directory |
| `CREDENTIALS_DIR` | **required** | ES256 cert + key root |
| `SIGNING_ALG` | `ES256` | C2PA signing algorithm |
| `TA_URL` | _(unset)_ | RFC 3161 timestamp authority |
| `RESOLUTION_PUSH_ENABLED` | `true` | Auto-push to resolution-api after sign |
| `RESOLUTION_API_URL` | _(required when push enabled)_ | Auto-push target |
| `RESOLUTION_MAX_RETRIES` | `1` | Per-HTTP-call retries on transient failure (5xx, timeout, network). 4xx never retries. |
| `RESOLUTION_RETRY_BACKOFF_S` | `0.5` | Initial backoff between retries (doubles each attempt) |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `false` | Logging |

## Tests

```bash
pytest -q
```

Tests use `httpx.MockTransport` to stub the plugin and the
resolution-api. Real C2PA signing is exercised end-to-end against the
ES256 test cert chain — drop one in `<repo>/credentials/` (see the
[top-level README](../../README.md)) or those tests will skip.
