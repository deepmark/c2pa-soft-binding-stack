# C2PA Soft Binding Resolution API

Implementation of the [C2PA Soft Binding Resolution API v2.4](https://spec.c2pa.org/specifications/specifications/2.4/softbinding/Decoupled.html) using FastAPI, async MongoDB (motor), and GridFS for manifest blob storage.

This API enables matching soft bindings (watermarks and fingerprints) to C2PA Manifests, providing content provenance verification for digital assets.

## Endpoints

### Query (`src/soft_binding_api/routers/query.py`) — §1.4.1.1
- `GET  /matches/byBinding` — Query manifests by soft binding value
- `POST /matches/byBinding` — Same, for binding values too large for a URL
- `POST /matches/byContent` — Find manifests by uploading an asset file
- `POST /matches/byReference` — Find manifests by asset URL (with SSRF protection)

### Store (`src/soft_binding_api/routers/store.py`) — §1.4.1.2
- `POST   /manifests` — Ingest a C2PA Manifest Store
- `DELETE /manifests/{manifestId}` — Remove a Manifest Store (and its bindings)
- `POST   /bindings` — Associate a manifest with a soft binding value
- `PUT    /bindings` — Update the manifest associated with a binding

### Fetch (`src/soft_binding_api/routers/fetch.py`) — §1.4.1.3
- `GET  /manifests/{manifestId}` — Retrieve a Manifest Store (or just the active manifest)
- `GET  /manifests/{manifestId}/receipts` — Get a verified ingestion receipt
- `POST /manifests/{manifestId}/receipts` — Verify a supplied receipt

### Service (`src/soft_binding_api/routers/service.py`) — §1.4.1.4
- `GET /services/supportedAlgorithms` — List supported watermark / fingerprint algorithms

## Prerequisites

- Python 3.10+
- MongoDB 6.0+ running on port 27017

### Install MongoDB (if not already installed)

**macOS:**
```bash
brew tap mongodb/brew
brew install mongodb-community
brew services start mongodb-community
```

**Linux:**
```bash
curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc | sudo gpg -o /usr/share/keyrings/mongodb-server-8.0.gpg --dearmor
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-8.0.list
sudo apt update && sudo apt install -y mongodb-org
sudo systemctl enable --now mongod
```

**Windows:**
Download from https://www.mongodb.com/try/download/community

**Docker (any platform):**
```bash
docker run -d -p 27017:27017 --name mongo mongo:latest
```

## Setup

1. **Install dependencies:**
```bash
python -m venv venv
source venv/bin/activate
pip install -e .            # runtime deps
pip install -e ".[dev]"     # + pytest, ruff, mypy
```

Initialize the database with sample data + indexes:

```bash
softbinding-init-db          # console script
# or:
python -m soft_binding_api.init_db
```

Run the API server:

```bash
uvicorn soft_binding_api.main:app --reload   # dev
# or:
softbinding-api                              # production-ish (no reload)
```

The API will be available at http://localhost:8000.

## API Documentation

- **Swagger UI:** http://localhost:8000/docs (endpoints are grouped by the four spec tags: `query`, `store`, `fetch`, `service`)
- **ReDoc:** http://localhost:8000/redoc

## Testing the Endpoints

The sample data inserted by `init_db.py` uses these IDs:

- Manifest A: `urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4`
- Manifest B: `urn:c2pa:A1B2C3D4-E5F6-7890-ABCD-EF1234567890`

### 1. List Supported Algorithms
```bash
curl http://localhost:8000/services/supportedAlgorithms
```

### 2. Query by Soft Binding (GET)
```bash
curl "http://localhost:8000/matches/byBinding?alg=example.watermark.v1&value=d2F0ZXJtYXJrX3ZhbHVlXzEyMw%3D%3D&maxResults=10"
```

### 3. Query by Large Soft Binding (POST)
```bash
curl -X POST "http://localhost:8000/matches/byBinding?maxResults=10" \
  -H "Content-Type: application/json" \
  -d '{
    "alg": "example.watermark.v1",
    "value": "d2F0ZXJtYXJrX3ZhbHVlXzEyMw=="
  }'
```

### 4. Query by Content (file upload)
```bash
curl -X POST "http://localhost:8000/matches/byContent?alg=example.watermark.v1&maxResults=10" \
  -F "file=@/path/to/asset.mp3"
```

### 5. Query by Reference URL
```bash
curl -X POST "http://localhost:8000/matches/byReference?alg=example.watermark.v1" \
  -H "Content-Type: application/json" \
  -d '{
    "referenceUrl": "https://example.com/asset.mp3",
    "assetLength": 1048576,
    "assetType": "audio/mpeg"
  }'
```

### 6. Store a Manifest
```bash
curl -X POST "http://localhost:8000/manifests?returnReceipt=true" \
  -H "Content-Type: application/c2pa" \
  --data-binary "@manifest.c2pa"
```

### 7. Get Manifest by ID
```bash
curl http://localhost:8000/manifests/urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4
```

Add `?returnActiveManifest=true` to fetch only the active manifest instead of the full store.

### 8. Create Soft Binding Association
```bash
curl -X POST http://localhost:8000/bindings \
  -H "Content-Type: application/json" \
  -d '{
    "alg": "example.watermark.v1",
    "bindingValue": "d2F0ZXJtYXJrX3ZhbHVlXzEyMw==",
    "manifestId": "urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4"
  }'
```

### 9. Update Soft Binding Association
```bash
curl -X PUT http://localhost:8000/bindings \
  -H "Content-Type: application/json" \
  -d '{
    "alg": "example.watermark.v1",
    "bindingValue": "d2F0ZXJtYXJrX3ZhbHVlXzEyMw==",
    "manifestId": "urn:c2pa:A1B2C3D4-E5F6-7890-ABCD-EF1234567890"
  }'
```

### 10. Get Verification Receipt
```bash
curl http://localhost:8000/manifests/urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4/receipts
```

### 11. Verify Receipt
```bash
curl -X POST http://localhost:8000/manifests/urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4/receipts \
  -H "Content-Type: application/json" \
  -d '{
    "@context": {
      "c2pa": "https://c2pa.org/ns/",
      "receipt": "https://c2pa.org/ns/manifest-receipt#"
    },
    "@type": "org.c2pa.manifest-receipt",
    "repository": {
      "uri": "https://repo.example.org",
      "manifestId": "urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4"
    },
    "anchor": {
      "uri": "https://repo.example.org/anchors/123456",
      "proof": { "alg": "ES256", "value": "BASE64URL_PROOF_VALUE" }
    }
  }'
```

### 12. Delete Manifest
```bash
curl -X DELETE http://localhost:8000/manifests/urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4
```

## Project Structure

```
soft-binding-resolution-api/
├── pyproject.toml             # Build system, deps, entry points
├── README.md
├── .gitignore
├── specification.json         # Vendored OpenAPI spec from C2PA
├── src/
│   └── soft_binding_api/
│       ├── __init__.py
│       ├── __main__.py        # `python -m soft_binding_api` / `softbinding-api`
│       ├── main.py            # FastAPI app, lifespan, router wiring
│       ├── config.py          # Pydantic-settings configuration
│       ├── database.py        # Async Mongo connection, indexes, GridFS bucket
│       ├── init_db.py         # Sample data + index seeding (`softbinding-init-db`)
│       ├── models.py          # Pydantic request/response models
│       ├── routers/           # One module per C2PA spec route group
│       │   ├── query.py       # §1.4.1.1 — /matches/*
│       │   ├── store.py       # §1.4.1.2 — POST/PUT /bindings, POST /manifests, DELETE /manifests/{id}
│       │   ├── fetch.py       # §1.4.1.3 — GET /manifests/{id}, /receipts
│       │   └── service.py     # §1.4.1.4 — /services/supportedAlgorithms
│       └── services/          # Algorithm implementations (out-of-spec plug points)
│           ├── registry.py    # `register_watermark` / `register_fingerprint`
│           ├── watermark/
│           │   ├── embed.py   # publisher-side embedder placeholder
│           │   └── detect.py  # verifier-side detector placeholder
│           └── fingerprint/
│               └── compute.py # fingerprint computer placeholder
├── tests/
│   ├── test_routes.py         # smoke: required spec routes are registered
│   └── test_registry.py       # algorithm registry behaviour
└── docs/
```

## Database

The service uses three collections plus a GridFS bucket:

- **`manifests`** — `_id` is the C2PA `manifestId` (`urn:c2pa:<UUID>`); doc holds GridFS file references for the active manifest and full manifest store.
- **`soft_bindings`** — `{alg, value, manifestId, similarityScore}`. Indexed on `(alg, value)` for the hot lookup path, on `manifestId` for cascade deletes, and uniquely on the `(alg, value, manifestId)` triple to prevent duplicates.
- **`supported_algorithms`** — one document per algorithm: `{type: "watermark"|"fingerprint", alg: "<id>"}`. Unique on `(type, alg)`.
- **`manifest_blobs.*`** (GridFS) — manifest payloads. Used because C2PA Manifest Stores can exceed MongoDB's 16 MB document limit.

Indexes are created automatically at startup (`MongoDB._ensure_indexes`) and re-asserted by `init_db.py`.

## Configuration

Create a `.env` file to override defaults:
```env
MONGODB_URL=mongodb://localhost:27017
DATABASE_NAME=c2pa_soft_bindings
API_TITLE=C2PA Soft Binding Resolution API
API_VERSION=1.1.0
```

## Error Handling

Standard HTTP status codes per the C2PA Decoupled spec:
- **200** — Successful operation
- **204** — Successful operation with no content
- **400** — Invalid request (bad parameters, invalid format)
- **403** — Client not allowed to perform operation
- **404** — Resource not found
- **414** — URI too long (use POST instead of GET)
- **415** — Unsupported media type
- **500** — Internal server error

## Implementation Notes

### Placeholder Functionality

Some endpoints contain placeholders that require an algorithm-specific implementation or the `c2pa-python` SDK to be plumbed in:

1. **Soft binding extraction** (`/matches/byContent`, `/matches/byReference`)
   - Currently returns empty results.
   - Each registered algorithm (`/services/supportedAlgorithms`) needs an extractor function that maps `bytes -> base64 value`.
   - Wire them into a registry, dispatch by `alg`, then reuse the same query as `/matches/byBinding`.

2. **C2PA manifest parsing** (`POST /manifests`)
   - Currently stores the request body as-is and mints a placeholder `urn:c2pa:<uuid4>` ID.
   - Should call `c2pa-python` to parse the manifest store, extract the actual active manifest label (already in `urn:c2pa:` form per the C2PA Technical Spec), and store the active manifest blob separately.

3. **Receipt verification** (`POST /manifests/{manifestId}/receipts`)
   - Currently performs a simplified comparison.
   - Should cryptographically verify the receipt's proof against the anchor / repository signing key.

### Architecture: where does the watermark itself come from?

The C2PA Decoupled spec **only** covers the lookup API. Embedding the watermark in an asset and extracting it back out are out of scope:

- **Embedding** is done by publisher tooling (a separate ingest service / CLI) before content ships. That tool generates a binding `value`, embeds it into the audio/video, builds a signed C2PA manifest containing a soft-binding assertion, and `POST`s the manifest + binding here.
- **Extraction** happens either client-side (preferred — clients run the algorithm locally and call `/matches/byBinding`) or server-side via `/matches/byContent`.

The `src/soft_binding_api/services/` package holds plug points for these:

- `services/watermark/embed.py` — publisher-side embedder (used by ingest tooling)
- `services/watermark/detect.py` — verifier-side detector (used by `/matches/byContent`)
- `services/fingerprint/compute.py` — fingerprint computer (both sides)
- `services/registry.py` — `register_watermark(alg, embed=..., detect=...)` and `register_fingerprint(alg, compute=...)` to wire concrete implementations in by their algorithm identifier.

All three modules raise `NotImplementedError` until something is registered. See https://github.com/c2pa-org/softbinding-algorithm-list for the registry of standardised algorithm identifiers.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The bundled tests verify the spec-required routes are registered and that the algorithm registry round-trips. They don't require MongoDB.

## Next Steps

To make this production-ready:
- Integrate `c2pa-python` for manifest parsing, `manifestId` extraction, and active-manifest separation in `POST /manifests`.
- Implement / register at least one real soft binding algorithm and wire its extractor into `/matches/byContent`.
- Add OAuth2 / API-key authentication as specified in the OpenAPI spec.
- Enhance SSRF protection (domain allowlist, cloud metadata endpoint blocking, IP-resolution checks).
- Add rate limiting, structured logging, and metrics.
- Implement real receipt storage with cryptographic proofs (e.g. signed COSE / JWS).
