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

## Config

Settings live in `src/resolution_api/core/config.py`. Override via env
vars (or `.env`):

| Var | Default | What |
| --- | --- | --- |
| `MONGODB_URL` | `mongodb://localhost:27017` | Mongo URL |
| `DATABASE_NAME` | `c2pa_soft_bindings` | Mongo DB |
| `ALGORITHMS_CATALOG_PATH` | `<repo>/algorithms.yaml` | Shared catalog |
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

### Integration Tests

**test_aware_plugin.py** requires the AWARE watermark container running:

```bash
docker compose up watermark-aware-20
python tests/test_aware_plugin.py
```

This test validates the full watermark embed/detect round-trip with a complex audio signal.
