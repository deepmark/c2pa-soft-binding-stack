# ingestion-api

Audio ingestion pipeline — accepts media uploads, applies watermark soft-bindings via plugin containers, builds and signs C2PA manifests, persists artifacts, and auto-pushes to resolution-api.

For the full stack (compose, resolution-api, plugins) see the [top-level README](../../README.md).

## Run

The Dockerfile builds the required patched C2PA SDK (`0.91.0+deepmark.1`) automatically. For local Linux development, install Rust 1.96.0, a C compiler, OpenSSL development headers, `pkg-config`, `curl`, and `patch`, then build the dependency before starting Python:

```bash
pip install -e ".[dev]"
rustup toolchain install 1.96.0 --profile minimal
C2PA_BUILD_DIR="$(mktemp -d)"
bash native/build-c2pa.sh "$C2PA_BUILD_DIR" "$C2PA_BUILD_DIR/lib"
export C2PA_LIBRARY_NAME="$C2PA_BUILD_DIR/lib/libc2pa_c.so"
ingestion-api              # uvicorn on port 8001
```

The pinned patch in `native/` resolves assertion references using SDK-generated hashes before signing. The service rejects an unpatched library; keep `C2PA_LIBRARY_NAME` set when running locally.

Requires:
- MongoDB running at `MONGODB_URL`
- Signing credentials in `CREDENTIALS_DIR` (see [`credentials/README.md`](../../credentials/README.md))
- A watermark plugin reachable (URL comes from `supported_algorithms` collection)
- `RESOLUTION_API_URL` set (or `RESOLUTION_PUSH_ENABLED=false` to skip auto-push)

Swagger UI: `http://localhost:8001/docs`

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/ingest` | Ingest media: watermark + sign + store + push |
| GET | `/ingestions` | List ingestions (cursor pagination, filterable by lifecycle and push status) |
| GET | `/ingest/{id}` | Get ingestion record |
| GET | `/ingest/{id}/asset` | Download signed asset |
| HEAD | `/ingest/{id}/asset` | Check signed asset existence |
| GET | `/ingest/{id}/manifest` | Download raw manifest bytes |
| HEAD | `/ingest/{id}/manifest` | Check manifest existence |
| DELETE | `/ingest/{id}` | Delete ingestion (artifacts + record, local only) |
| GET | `/health` | Liveness probe |
| GET | `/ready` | Readiness probe (MongoDB + credentials + signing) |
| GET | `/health/deep` | Full diagnostics (plugins + resolution-api + cert expiry) |

## Ingest request

```bash
curl -X POST http://localhost:8001/ingest \
  -F "file=@audio.wav;type=audio/wav" \
  -F "algs=me.deepmark.audio.aware.20" \
  -F "title=Optional title"
```

- `file` — media asset (supported: `audio/wav`, `audio/flac`)
- `algs` — algorithm IDs to apply (repeat for multiple: `-F "algs=a" -F "algs=b"`)
- `title` — optional manifest title

## Config

Settings: `src/ingestion_api/core/config.py`. Override via env vars or `.env`.

### Required

| Variable | Description |
|----------|-------------|
| `MONGODB_URL` | MongoDB connection string |
| `MONGODB_DATABASE` | MongoDB database containing ingestion and plugin catalog collections |
| `STORAGE_ROOT` | Directory for signed assets + manifest bytes |
| `CREDENTIALS_DIR` | Directory containing `<alg>_certs.pem` + `<alg>_private.key` |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `INGESTIONS_COLLECTION` | `ingestions` | Ingestion lifecycle records collection |
| `SUPPORTED_ALGORITHMS_COLLECTION` | `supported_algorithms` | Shared plugin catalog collection |
| `SIGNING_ALG` | `ES256` | C2PA signing algorithm |
| `TA_URL` | _(empty)_ | RFC 3161 timestamp authority |
| `FETCH_SIGNING_CREDENTIALS` | `true` | Download signing credentials at container startup when files are missing |
| `SIGNING_CREDENTIALS_BASE_URL` | C2PA fixture URL | Base URL for `<alg>_certs.pem` and `<alg>_private.key` |
| `RESOLUTION_PUSH_ENABLED` | `true` | Auto-push to resolution-api |
| `RESOLUTION_API_URL` | _(required when push enabled)_ | Resolution-api base URL |
| `RESOLUTION_MAX_RETRIES` | `1` | Retries on transient failure (5xx/timeout) |
| `RESOLUTION_RETRY_BACKOFF_S` | `0.5` | Initial backoff (doubles each retry) |
| `RESOLUTION_REQUEST_TIMEOUT_S` | `10.0` | HTTP timeout for push calls |
| `PLUGIN_REQUEST_TIMEOUT_S` | `60.0` | HTTP timeout for plugin calls |
| `MAX_ALGS_PER_INGEST` | `8` | Max algorithms per request |
| `MAX_TITLE_LENGTH` | `128` | Max title characters |
| `PUBLIC_BASE_URL` | _(empty)_ | Pin absolute URLs in responses (otherwise uses request host) |
| `FORWARDED_ALLOW_IPS` | `*` | Trusted proxy CIDRs for X-Forwarded-* |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LOG_JSON` | `false` | JSON-formatted log output |

## MongoDB collections

In the `c2pa` database by default:

| Collection | Purpose |
|------------|---------|
| `ingestions` | One doc per ingest lifecycle state: pending, succeeded, or failed |

Algorithm catalog is read from `c2pa.supported_algorithms` by default and is shared with resolution-api.

## Signing credentials in containers

The Docker image fetches the expected files into `CREDENTIALS_DIR`
before the API starts when they are missing. By default it downloads
the public C2PA fixtures described in `credentials/README.md`:

```
curl -fsSLo ${CREDENTIALS_DIR}/es256_certs.pem \
  https://raw.githubusercontent.com/contentauth/c2pa-python/main/tests/fixtures/es256_certs.pem
curl -fsSLo ${CREDENTIALS_DIR}/es256_private.key \
  https://raw.githubusercontent.com/contentauth/c2pa-python/main/tests/fixtures/es256_private.key
```

Set `SIGNING_CREDENTIALS_BASE_URL` to fetch the same file names from a
different host. Set `FETCH_SIGNING_CREDENTIALS=false` to disable startup
fetching and require the files to already exist.

## Tests

```bash
pytest -q
```

Set `C2PA_LIBRARY_NAME` as above before running the tests. Tests use mocked plugins and resolution-api (no live services needed). Manifest tests generate temporary ES256 certificates and validate completed WAV and FLAC assets, exact salted assertion hashes, duplicate labels, parent provenance, and audio tampering. The temporary certificate authority is untrusted; signature and asset-binding failures remain test failures. Existing ingestion orchestrator tests still require `credentials/es256_certs.pem` + `es256_private.key` and skip when those files are absent.
