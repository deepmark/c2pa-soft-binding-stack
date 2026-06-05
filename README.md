# C2PA Soft-Binding Stack

Multi-service implementation of the [C2PA Soft Binding Resolution API spec (v2.4)](https://spec.c2pa.org/specifications/specifications/2.4/softbinding/Decoupled.html) with an audio ingestion pipeline that watermarks media, builds signed C2PA manifests, and publishes them to a resolution service for later lookup.

**Supported media formats:** `.wav` and `.flac` audio. Lossy formats (MP3, M4A) are temporarily disabled.

⚠️ **This repository is for testing and experimentation only.** It is not intended for production use and should not be used to issue real C2PA manifests or perform real soft-binding ingestion / resolution. All routes are unauthenticated — do not upload sensitive data.

A hosted demo instance is available at:
 - Ingestion API: https://ingestion-api.deepmark.me
 - Resolution API: https://resolution-api.deepmark.me

## Architecture

```
soft-binding-resolution-api/
├── docker-compose.yml
├── mongo-init/                         <- seeds supported_algorithms on first boot
├── credentials/                        <- cert + key for C2PA signing (not committed)
└── apps/
    ├── resolution-api/                 <- lookup + store: /matches, /manifests, /bindings
    ├── ingestion-api/                  <- orchestrator: upload -> embed -> sign -> store -> push
    └── plugins/
        └── watermark/
            └── aware/                  <- me.deepmark.audio.aware.20 (AWARE watermark)
```

Four containers via `docker compose`:

| Service | Port | Role |
|---------|------|------|
| `resolution-api` | 8000 | C2PA manifest store + soft-binding lookup. Reads algorithm catalog from MongoDB. |
| `ingestion-api` | 8001 | Accepts media uploads, calls watermark plugins, signs manifests, pushes to resolution-api. |
| `watermark-aware-20` | 8102 | Audio watermark plugin (AWARE). Embeds/detects binding values in audio. |
| `mongo` | 27017 | Shared MongoDB instance. Each API uses its own database. |

### Data flow

```
Client
  | POST /ingest (multipart: file + algs)
  v
ingestion-api
  | 1. Mints random binding value (sized to plugin's bindingBits)
  | 2. POST http://watermark-aware-20:9004/embed
  |      X-Binding-Value: <base64>
  |      body: raw audio bytes
  | 3. Receives watermarked bytes (plugin echoes binding value)
  | 4. Builds C2PA manifest with soft-binding assertion
  | 5. Signs manifest with configured cert/key
  | 6. Stores signed asset + manifest to disk
  | 7. POST http://resolution-api:8000/manifests  (manifest bytes)
  |    POST http://resolution-api:8000/bindings   (alg + value + manifestId)
  v
resolution-api
  | Persists to MongoDB. GET /matches/byBinding now resolves the asset.
```

## Quick start

```bash
# 1. Get signing credentials (test certs for local dev)
mkdir -p credentials
curl -fsSLo credentials/es256_certs.pem \
  https://raw.githubusercontent.com/contentauth/c2pa-python/main/tests/fixtures/es256_certs.pem
curl -fsSLo credentials/es256_private.key \
  https://raw.githubusercontent.com/contentauth/c2pa-python/main/tests/fixtures/es256_private.key

# 2. Start everything
docker compose up -d --build

# 3. Verify
curl http://localhost:8000/health
curl http://localhost:8001/health/deep
curl http://localhost:8000/services/supportedAlgorithms
```

### Ingest audio

```bash
curl -X POST http://localhost:8001/ingest \
  -F "file=@audio.wav;type=audio/wav" \
  -F "algs=me.deepmark.audio.aware.20"
```

Response includes `ingestionId`, `manifestId`, `softBindings`, `outputAssetUrl`, and `resolutionPush.status`.

### Look up by binding

```bash
curl "http://localhost:8000/matches/byBinding?alg=me.deepmark.audio.aware.20&value=<bindingValue>"
```

### Detect from audio

```bash
curl -X POST http://localhost:8000/matches/byContent \
  -F "file=@watermarked.wav;type=audio/wav"
```

## API endpoints

### Resolution API (port 8000)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/matches/byBinding` | Look up manifests by soft binding value |
| POST | `/matches/byBinding` | Same, for large binding values |
| POST | `/matches/byContent` | Upload file, detect watermark, look up manifests |
| POST | `/matches/byReference` | Fetch file by URL, detect watermark, look up manifests |
| GET | `/manifests/{id}` | Download C2PA manifest store |
| POST | `/manifests` | Store a manifest (used by ingestion-api push) |
| POST | `/bindings` | Associate a binding with a manifest |
| PUT | `/bindings` | Update a binding association |
| DELETE | `/bindings` | Remove a binding association |
| GET | `/services/supportedAlgorithms` | List registered algorithms |
| GET | `/health` | Liveness probe |
| GET | `/ready` | Readiness probe (checks MongoDB) |

Swagger UI: `http://localhost:8000/docs`

### Ingestion API (port 8001)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/ingest` | Ingest media: watermark + sign + store + push |
| GET | `/ingestions` | List ingestions (cursor pagination, filterable by lifecycle and push status) |
| GET | `/ingest/{id}` | Get ingestion record |
| GET | `/ingest/{id}/asset` | Download signed asset |
| GET | `/ingest/{id}/manifest` | Download raw manifest bytes |
| DELETE | `/ingest/{id}` | Delete ingestion (local only) |
| GET | `/health` | Liveness probe |
| GET | `/ready` | Readiness probe |
| GET | `/health/deep` | Full diagnostics (plugins + resolution-api + cert expiry) |

Swagger UI: `http://localhost:8001/docs`

### Watermark plugin (port 8102)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/embed` | Embed a binding value into audio |
| POST | `/detect` | Detect a binding value from audio |
| GET | `/info` | Plugin metadata (alg, bindingBits, mediaTypes) |
| GET | `/health` | Liveness probe |

## Environment variables

### Required (ingestion-api)

| Variable | Description |
|----------|-------------|
| `MONGODB_URL` | MongoDB connection string |
| `MONGODB_DATABASE` | MongoDB database containing ingestion and plugin catalog collections |
| `STORAGE_ROOT` | Path for signed asset + manifest storage |
| `CREDENTIALS_DIR` | Directory containing `<alg>_certs.pem` + `<alg>_private.key` |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `SIGNING_ALG` | `ES256` | C2PA signing algorithm |
| `TA_URL` | _(empty)_ | RFC 3161 timestamp authority URL |
| `FETCH_SIGNING_CREDENTIALS` | `true` | Download signing credentials at ingestion startup when files are missing |
| `SIGNING_CREDENTIALS_BASE_URL` | C2PA fixture URL | Base URL for `<alg>_certs.pem` and `<alg>_private.key` |
| `RESOLUTION_PUSH_ENABLED` | `true` | Auto-push to resolution-api after signing |
| `RESOLUTION_API_URL` | _(required when push enabled)_ | Resolution-api base URL |
| `INGESTIONS_COLLECTION` | `ingestions` | Ingestion lifecycle records collection |
| `SUPPORTED_ALGORITHMS_COLLECTION` | `supported_algorithms` | Shared plugin catalog collection |
| `PLUGIN_REQUEST_TIMEOUT_S` | `60` | HTTP timeout for plugin calls |
| `MAX_ALGS_PER_INGEST` | `8` | Max algorithms per ingest request |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LOG_JSON` | `false` | JSON-formatted logs |

### Resolution-api

| Variable | Default | Description |
|----------|---------|-------------|
| `MONGODB_URL` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGODB_DATABASE` | `c2pa` | MongoDB database name |
| `MANIFESTS_COLLECTION` | `manifests` | Stored manifest metadata collection |
| `SOFT_BINDINGS_COLLECTION` | `soft_bindings` | Binding lookup collection |
| `SUPPORTED_ALGORITHMS_COLLECTION` | `supported_algorithms` | Shared plugin catalog collection |
| `MANIFEST_BLOBS_BUCKET` | `manifest_blobs` | GridFS bucket for manifest bytes |
| `LOG_LEVEL` | `INFO` | Logging level |

## Algorithm catalog

Supported algorithms are stored in the `c2pa.supported_algorithms` MongoDB collection by default. Both services read from this collection. The `mongo-init/seed-algorithms.js` script seeds the default AWARE algorithm on first container boot.

To register a new algorithm, insert a document:

```javascript
db.supported_algorithms.insertOne({
  alg: "me.example.audio.newalg.64",
  type: "watermark",
  bindingBits: 64,
  mediaTypes: ["audio/wav", "audio/mpeg"],
  url: "http://new-plugin-container:9000"
})
```

Both services pick up new algorithms immediately (no restart required).

## Adding a new plugin

1. Create `apps/plugins/<watermark|fingerprint>/<name>/` with `app.py`, `Dockerfile`, and `requirements.txt`.
2. Implement the plugin HTTP contract:
   - Watermark: `POST /embed`, `POST /detect`, `GET /info`, `GET /health`
   - Fingerprint: `POST /compute`, `GET /info`, `GET /health`
3. Add a service block to `docker-compose.yml`.
4. Insert the algorithm into `supported_algorithms` (or add it to `mongo-init/seed-algorithms.js`).
5. `docker compose up -d --build`

Binary endpoints use `Content-Type: application/octet-stream` for the body and `X-Media-Type` to describe the actual format. Watermark `/embed` requires an `X-Binding-Value` header (the API mints the value; the plugin embeds and echoes it back).

## Tests

```bash
cd apps/resolution-api && pip install -e ".[dev]" && pytest
cd apps/ingestion-api && pip install -e ".[dev]" && pytest
```

Tests run with no external services (MongoDB and plugins are mocked). Ingestion-api tests that exercise C2PA signing require test credentials in `credentials/` (auto-skipped otherwise).

## Signing credentials

See [`credentials/README.md`](credentials/README.md) for supported algorithms, how to obtain test certs, and how to generate your own.

Self-signed certs work for local development. Production deployments need certificates from a CA on a recognized [C2PA trust list](https://contentcredentials.org/trust-list).

## License

[MIT](LICENSE)
