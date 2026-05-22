# resolution-api

Lookup half of the [C2PA Soft Binding Resolution API
v2.4](https://spec.c2pa.org/specifications/specifications/2.4/softbinding/Decoupled.html)
— `GET /matches/byBinding`, `GET /manifests/{id}`, `POST /bindings`,
`GET /services/supportedAlgorithms`, etc.

For the full picture (compose stack, ingestion-api, plugins) see the
[top-level README](../../README.md).

## Run

```bash
pip install -e ".[dev]"
resolution-init-db          # seed Mongo with sample manifests + indexes
resolution-api              # uvicorn on port 8000
```

## Docker

Easiest path is the repo-root `docker-compose.yml`, which wires this
service together with Mongo, ingestion-api, and the watermark plugin:

```bash
# from the repo root
docker compose up -d resolution-api
docker compose exec resolution-api resolution-init-db
```

To build and run just this service standalone:

```bash
# from src/resolution-api/
docker build -t resolution-api .
docker run --rm -p 8000:8000 \
  -e MONGODB_URL=mongodb://host.docker.internal:27017 \
  -e PLUGINS_CATALOG_PATH=/catalog/plugins.yaml \
  -v "$PWD/../../plugins.yaml:/catalog/plugins.yaml:ro" \
  resolution-api
```

The image is multi-stage (builder + slim runtime) and runs as a
non-root `appuser`. See [Dockerfile](Dockerfile) for the full build.

## Config

Settings live in `src/resolution_api/core/config.py`. Override via env
vars (or `.env`):

| Var | Default | What |
| --- | --- | --- |
| `MONGODB_URL` | `mongodb://localhost:27017` | Mongo URL |
| `DATABASE_NAME` | `c2pa_soft_bindings` | Mongo DB |
| `PLUGINS_CATALOG_PATH` | `<repo>/plugins.yaml` | Shared catalog |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `false` | Logging |

## Tests

```bash
pytest -q
```

### Test Coverage

Most tests use mocked dependencies (no real MongoDB required):

- **test_routes.py** - Route registration smoke tests
- **test_service.py** - `/services/supportedAlgorithms` endpoint
- **test_store.py** - `POST /bindings`, `POST /manifests`, `DELETE /manifests/{id}`
- **test_query.py** - `/matches/byBinding`, `/matches/byContent`, `/matches/byReference`
- **test_fetch.py** - `GET /manifests/{id}`, manifest receipts
