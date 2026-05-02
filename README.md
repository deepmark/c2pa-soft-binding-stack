# C2PA Soft-Binding Stack

Multi-service repo implementing the [C2PA Soft Binding Resolution API
v2.4](https://spec.c2pa.org/specifications/specifications/2.4/softbinding/Decoupled.html)
plus an audio ingest pipeline that produces signed manifests with
soft-binding watermarks. Inspired by the
[deepmarkpy-benchmark](https://github.com/deepmark/deepmarkpy-benchmark)
plugin-per-container architecture.

## Services

```
soft-binding-resolution-api/
├── algorithms.yaml                     ← shared catalog (mounted into both APIs)
├── docker-compose.yml
├── .env.example
├── credentials/                        ← cert + key, mounted only into ingestion-api
└── src/
    ├── resolution-api/                 ← lookup half of the spec
    │   └── src/resolution_api/         ← /matches, /manifests, /bindings, /services
    ├── ingestion-api/                  ← orchestrator: upload → embed → sign → store → push
    │   └── src/ingestion_api/          ← POST /ingest, /ready, …
    └── plugins/
        ├── watermark/
        │   └── vigil-128/              ← me.deepmark.audio.vigil.128 (FastAPI plugin)
        └── fingerprint/                ← drop new fingerprint plugins here
```

Four containers wired together via `docker-compose`:

- **`resolution-api`** (port 8000) — read/write of the C2PA Manifest
  Store and the soft-binding lookup table. Reads supported algorithms
  from `algorithms.yaml`.
- **`ingestion-api`** (port 8001) — accepts audio uploads, calls the
  watermark plugin, builds + signs the C2PA manifest, persists artifacts,
  auto-pushes the resulting manifest store + binding to
  `resolution-api`.
- **`watermark-vigil-128`** (port 8101) — soft-binding watermark plugin
  for `me.deepmark.audio.vigil.128`. Currently a deterministic SHA-256[:16]
  dummy — drop in real DSP without touching anything else (see
  [src/plugins/watermark/vigil-128/README.md](src/plugins/watermark/vigil-128/README.md)).
- **`mongo`** — backing store for `resolution-api`. The ingestion
  service is filesystem-only; it does not touch Mongo.

### Data flow (`POST /ingest`)

```
Client
  │ multipart upload (audio file)
  ▼
ingestion-api (8001)
  │ POST http://watermark-vigil-128:8000/embed
  │   Content-Type: application/octet-stream
  │   <raw audio bytes>
  ▼
watermark-vigil-128 (8000 internal)
  │ reads body, embeds, returns:
  │   Content-Type: application/octet-stream
  │   X-Binding-Value: <base64 128-bit>
  │   <watermarked audio bytes>
  ▼
ingestion-api
  │ builds C2PA manifest (EDIT intent: parent ingredient + c2pa.opened
  │   auto-injected; we add c2pa.watermarked.bound + c2pa.soft-binding)
  │ signs with cert/key from /credentials
  │ writes /var/lib/ingestion-api/storage/ingestions/<id>/{signed.wav,
  │   manifest.c2pa, metadata.json}
  │
  │ POST http://resolution-api:8000/manifests   (raw c2pa bytes)
  │ POST http://resolution-api:8000/bindings    ({alg, value, manifestId})
  ▼
resolution-api (8000)
  │ persists into Mongo. /matches/byBinding now resolves the new asset.

Response back to client:
  { ingestionId, manifestId, alg, bindingValue,
    outputAssetUrl, manifestUrl,
    resolutionPush: { status, error? }, ... }
```

Bytes flow ingestion-api ⇄ plugins purely over HTTP, so plugin
containers don't need to share a filesystem with ingestion-api and can
run on a different host or behind a load balancer.

## Quick start with `docker compose`

```bash
cp .env.example .env

# Drop ES256 test certs in (cert chain + private key for local dev)
mkdir -p credentials
curl -fsSLo credentials/es256_certs.pem \
  https://raw.githubusercontent.com/contentauth/c2pa-rs/main/sdk/tests/fixtures/certs/es256.pub
curl -fsSLo credentials/es256_private.key \
  https://raw.githubusercontent.com/contentauth/c2pa-rs/main/sdk/tests/fixtures/certs/es256.pem

docker compose build
docker compose up -d

# Seed resolution-api Mongo with the sample manifests + indexes.
docker compose exec resolution-api resolution-init-db

# Sanity checks:
curl http://localhost:8000/health
curl http://localhost:8001/ready | jq .
curl http://localhost:8101/info  | jq .
curl http://localhost:8000/services/supportedAlgorithms | jq .
```

Ingest an audio asset end-to-end:

```bash
curl -X POST http://localhost:8001/ingest \
  -F "file=@/path/to/audio.wav;type=audio/wav" \
  -F "title=Hello world" | jq .
```

The response carries `outputAssetUrl`, `manifestUrl`, and
`resolutionPush.status` so you can confirm the auto-push succeeded.
Then verify the lookup side resolves the new asset:

```bash
curl "http://localhost:8000/matches/byBinding?alg=me.deepmark.audio.vigil.128&value=<bindingValue>"
```

## Local development without docker

Each service is a `pip install -e .`-able package. Run them from one
shared venv (or one per service — your call):

```bash
python -m venv venv && source venv/bin/activate

pip install -e "./src/resolution-api[dev]"
pip install -e "./src/ingestion-api[dev]"
pip install -r ./src/plugins/watermark/vigil-128/requirements.txt
```

Then in three terminals (with mongo running locally on 27017):

```bash
# 1. plugin
cd src/plugins/watermark/vigil-128 && uvicorn app:app --port 8101

# 2. resolution-api
cd src/resolution-api && resolution-api          # PORT=8000

# 3. ingestion-api
cd src/ingestion-api && ingestion-api            # PORT=8001
```

Tweak per-service config via the env vars described in each service's
README + `.env.example` at the repo root. Default settings assume a
Docker network, so for bare-metal dev you'll likely want:

```bash
export ALGORITHMS_CATALOG_PATH=$PWD/algorithms.yaml
export STORAGE_ROOT=$PWD/.storage
export CREDENTIALS_DIR=$PWD/credentials
export MONGODB_URL=mongodb://127.0.0.1:27017
export DATABASE_NAME=c2pa_ingestions
export RESOLUTION_API_URL=http://127.0.0.1:8000   # or RESOLUTION_PUSH_ENABLED=false
# point ingestion-api at the local plugin instead of the docker hostname
sed -i 's|http://watermark-vigil-128:8000|http://127.0.0.1:8101|' algorithms.yaml
```

## Tests

Each service ships its own pytest suite. From the repo root:

```bash
( cd src/resolution-api && pytest -q )
( cd src/ingestion-api  && pytest -q )
( cd src/plugins/watermark/vigil-128 && PYTHONPATH=. pytest -q )
```

- `src/resolution-api` — route smoke + algorithms catalog loader. No Mongo needed.
- `src/ingestion-api` — end-to-end orchestrator with a stubbed plugin client
  + stubbed resolution-api auto-push. Needs `credentials/` for real
  C2PA signing (auto-skips otherwise). Catalog + plugin client + push
  client are tested separately with `httpx.MockTransport`.
- `src/plugins/watermark/vigil-128` — algorithm unit tests + FastAPI
  TestClient over `/info`, `/health`, `/embed`, `/detect`.

Total: 39 tests, no external services, all pass on a fresh checkout
once `credentials/` is populated.

## Adding a new plugin

1. Create `src/plugins/<watermark|fingerprint>/<name>/{app.py,requirements.txt,Dockerfile}`.
   Mirror `src/plugins/watermark/vigil-128/` for the shape — watermark
   plugins expose `/info`, `/embed`, `/detect`, `/health`; fingerprint
   plugins expose `/info`, `/compute`, `/health`. All endpoints take
   raw audio bytes in the request body and return either bytes (with
   `X-Binding-Value` header) or JSON.
2. Register the plugin in `algorithms.yaml`.
3. Add a service block to `docker-compose.yml` with a hostname matching
   the YAML `url`.
4. Restart with `docker compose up -d --build`.

ingestion-api will resolve the new alg from the catalog at request time;
resolution-api will surface it on the next call to
`/services/supportedAlgorithms` (no restart needed — the YAML is reread
per request).

## Configuration reference

See:
- `src/resolution-api/src/resolution_api/core/config.py`
- `src/ingestion-api/src/ingestion_api/core/config.py`
- `credentials/README.md` — supported signing algorithms + how to drop in
  test certs.

Common knobs:

| Env var | Service | Default | What |
| --- | --- | --- | --- |
| `MONGODB_URL` | both (independent) | resolution: `mongodb://localhost:27017` / ingestion: **required** | Each service has its own Mongo cluster |
| `DATABASE_NAME` | both (independent) | resolution: `c2pa_soft_bindings` / ingestion: **required** | Each service has its own DB |
| `ALGORITHMS_CATALOG_PATH` | both | **required** | Shared YAML catalog path |
| `AUDIO_ALGS` | ingestion-api | `["me.deepmark.audio.vigil.128"]` | JSON list of soft-binding algs per ingest (watermarks first, fingerprints last) |
| `STORAGE_ROOT` | ingestion-api | **required** | Where signed assets + manifest bytes land (records live in Mongo) |
| `CREDENTIALS_DIR` | ingestion-api | **required** | Cert + key root |
| `SIGNING_ALG` | ingestion-api | `ES256` | C2PA signing algorithm |
| `TA_URL` | ingestion-api | _(unset)_ | RFC 3161 timestamp authority |
| `RESOLUTION_PUSH_ENABLED` | ingestion-api | `true` | Auto-push to resolution-api after sign |
| `RESOLUTION_API_URL` | ingestion-api | _(required when push enabled)_ | Auto-push target |
| `RESOLUTION_MAX_RETRIES` | ingestion-api | `1` | Per-HTTP-call retries on transient failure (5xx, timeout, network); 4xx never retries |
| `RESOLUTION_RETRY_BACKOFF_S` | ingestion-api | `0.5` | Initial backoff between retries (doubles each attempt) |

## Known limitations / next steps

- `Signer.from_info` is broken for ES256 in c2pa-python 0.32.3; we use
  `Signer.from_callback` + `cryptography` in ingestion-api. Drop the
  explicit `cryptography` dep when the upstream bug is fixed.
- vigil-128 is a dummy embedder. Replace `_embed_bytes` and
  `_detect_bytes` in `src/plugins/watermark/vigil-128/app.py` to wire in
  real Vigil-128 DSP.
- Fingerprint plugins: not implemented; see
  `src/plugins/fingerprint/README.md` for the contract.
- Ingestion-api stores artifacts on a Docker volume only; for shared
  multi-host deployments, point `STORAGE_ROOT` at network-attached
  storage or extend `services/storage.py` with an S3 backend.
- The `/matches/byContent` and `/matches/byReference` endpoints in
  resolution-api still return empty results — they don't yet call
  back into the plugin containers to recompute bindings.
