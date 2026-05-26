# resolution-api

C2PA Soft Binding Resolution API — lookup, store, and manage C2PA manifests and their soft-binding associations. Implements the [C2PA Soft Binding Resolution API spec (v2.4)](https://spec.c2pa.org/specifications/specifications/2.4/softbinding/Decoupled.html).

For the full stack (compose, ingestion-api, plugins) see the [top-level README](../../README.md).

## Run

```bash
pip install -e ".[dev]"
resolution-api              # uvicorn on port 8000
```

Requires MongoDB running on `MONGODB_URL` (defaults to `mongodb://localhost:27017`).

Swagger UI: `http://localhost:8000/docs`

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/matches/byBinding` | Look up manifests by soft binding value (base64) |
| POST | `/matches/byBinding` | Same, for large binding values |
| POST | `/matches/byContent` | Upload file, detect watermark, return matches |
| POST | `/matches/byReference` | Fetch file by HTTPS URL, detect, return matches |
| GET | `/manifests/{id}` | Download a C2PA manifest store |
| POST | `/manifests` | Store a manifest (body: raw `application/c2pa` bytes) |
| DELETE | `/manifests/{id}` | Delete manifest + associated bindings |
| POST | `/bindings` | Associate a binding value with a manifest |
| PUT | `/bindings` | Update an existing binding association |
| DELETE | `/bindings` | Remove a binding association |
| GET | `/services/supportedAlgorithms` | List registered algorithms |
| GET | `/health` | Liveness probe |
| GET | `/ready` | Readiness probe (checks MongoDB + algorithm count) |

## MongoDB collections

All in the `c2pa_soft_bindings` database (configurable via `DATABASE_NAME`):

| Collection | Purpose |
|------------|---------|
| `manifests` | One doc per stored manifest (ID + GridFS file refs) |
| `soft_bindings` | Binding lookups: `{alg, value, manifestId}` |
| `supported_algorithms` | Algorithm catalog: `{alg, type, bindingBits, mediaTypes, url}` |
| `manifest_blobs.*` | GridFS bucket for raw manifest bytes |

## Config

Settings: `src/resolution_api/core/config.py`. Override via env vars or `.env`.

| Variable | Default | Description |
|----------|---------|-------------|
| `MONGODB_URL` | `mongodb://localhost:27017` | MongoDB connection string |
| `DATABASE_NAME` | `c2pa_soft_bindings` | Database name |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LOG_JSON` | `false` | JSON-formatted log output |

## Scripts

```bash
# Seed sample manifests + bindings for local testing (destructive — wipes existing data)
ALLOW_DESTRUCTIVE_INIT=true resolution-init-db
```

## Tests

```bash
pytest -q
```

All tests use mocked MongoDB (no live connection needed):

- `test_query.py` — `/matches/byBinding`, `/matches/byContent`, `/matches/byReference`
- `test_store.py` — `POST /bindings`, `POST /manifests`, `DELETE /manifests/{id}`
- `test_fetch.py` — `GET /manifests/{id}`, receipt endpoints
- `test_service.py` — `/services/supportedAlgorithms`
- `test_routes.py` — route registration smoke tests
