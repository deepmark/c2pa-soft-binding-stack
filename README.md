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
├── plugins.yaml                        ← shared catalog (mounted into both APIs)
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
        │   └── aware/                  ← me.deepmark.audio.aware.20 (FastAPI plugin, default)
        └── fingerprint/                ← drop new fingerprint plugins here
```

Four containers wired together via `docker-compose`:

- **`resolution-api`** (port 8000) — read/write of the C2PA Manifest
  Store and the soft-binding lookup table. Reads supported algorithms
  from `plugins.yaml`.
- **`ingestion-api`** (port 8001) — accepts audio uploads, calls the
  watermark plugin, builds + signs the C2PA manifest, persists artifacts,
  auto-pushes the resulting manifest store + binding to
  `resolution-api`.
- **`watermark-aware-20`** (host port 8102, container port 9004) —
  soft-binding watermark plugin for `me.deepmark.audio.aware.20`. Real
  20-bit AWARE DSP: decodes the upload, embeds the binding value the
  ingestion-api minted (handed in via the `X-Binding-Value` request
  header), and returns watermarked bytes. This is the default plugin
  shipped in `docker-compose.yml`.

  A second proprietary plugin (`me.deepmark.audio.vigil.128`) is
  declared in `plugins.yaml` for internal deployments. It is not open
  sourced and not wired into this repo's `docker-compose.yml`.
- **`mongo`** — backing store for **both** APIs. Each owns an
  independent database on the same dev cluster (`c2pa_ingestions` for
  ingestion-api; `c2pa_soft_bindings` for resolution-api). In
  production they can point at completely separate clusters via
  `MONGODB_URL`. Ingestion-api additionally writes signed assets and
  raw manifest bytes to a filesystem volume (`STORAGE_ROOT`) — the
  binary artifacts don't fit cleanly into Mongo and live separately
  from the indexed `IngestionRecord`s.

### Data flow (`POST /ingest`)

```
Client
  │ multipart upload (audio file)
  ▼
ingestion-api (8001)
  │ mints a random binding value (per-plugin bindingBits — e.g. 20 for aware-20)
  │ POST http://watermark-aware-20:9004/embed
  │   Content-Type: application/octet-stream     (wire encoding)
  │   X-Media-Type:    audio/wav                 (semantic format)
  │   X-Binding-Value: <base64 binding value>    (API-minted; plugin embeds this)
  │   <raw audio bytes>
  ▼
watermark-aware-20 (9004 internal)
  │ reads body, embeds, returns:
  │   Content-Type:    application/octet-stream
  │   X-Binding-Value: <echo of request value — verified by ingestion-api>
  │   <watermarked audio bytes>
  ▼
ingestion-api
  │ builds C2PA manifest (EDIT intent: c2pa.opened auto-injected against
  │   an explicit parent ingredient built from the ORIGINAL upload bytes;
  │   we add c2pa.watermarked.bound + one c2pa.soft-binding assertion per
  │   alg)
  │ signs with cert/key from /credentials
  │ writes /var/lib/ingestion-api/storage/ingestions/<id>/{signed.wav,
  │   manifest.c2pa}  (binaries on disk; the IngestionRecord lands in
  │   the c2pa_ingestions Mongo DB)
  │
  │ POST http://resolution-api:8000/manifests   (raw c2pa bytes)
  │ POST http://resolution-api:8000/bindings    ({alg, value, manifestId})
  ▼
resolution-api (8000)
  │ persists into Mongo. /matches/byBinding now resolves the new asset.

Response back to client (IngestionResponse):
  { ingestionId, manifestId,
    softBindings: [ { alg, kind, bindingValue }, ... ],
    mimeType, mediaType, assetSha256,
    outputAssetUrl, manifestUrl,
    signingAlg, taUrl, signedAt, createdAt,
    resolutionPush: { status, error? } }
```

Fingerprint plugins (`type: fingerprint`) follow the same wire shape but
hit `POST /compute` instead of `/embed`. They do not mutate bytes (so
later passes still see the previous output), and they return the binding
value as JSON `{bindingValue}` rather than echoing a header — the plugin
computes it from the bytes rather than embedding a value the API minted.

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
curl http://localhost:8102/info  | jq .
curl http://localhost:8000/services/supportedAlgorithms | jq .
```

Ingest a media asset end-to-end (caller picks the algs, which must
exist in `plugins.yaml` and declare the upload's MIME):

```bash
curl -X POST http://localhost:8001/ingest \
  -F "file=@/path/to/audio.wav;type=audio/wav" \
  -F "algs=me.deepmark.audio.aware.20" \
  -F "title=Hello world" | jq .
```

Pass multiple algs by repeating the form field
(`-F 'algs=foo' -F 'algs=bar'`); order matters (watermark passes
mutate bytes for subsequent passes — put watermarks first).

The response carries `outputAssetUrl`, `manifestUrl`, and
`resolutionPush.status` so you can confirm the auto-push succeeded.
Then verify the lookup side resolves the new asset:

```bash
curl "http://localhost:8000/matches/byBinding?alg=me.deepmark.audio.aware.20&value=<bindingValue>"
```

## Local development without docker

The two APIs are `pip install -e .`-able packages and run fine on bare
metal. The aware plugin is **not** — it depends on a git-installed
AWARE model package, CPU PyTorch from a custom index, and system
libraries (`ffmpeg`, `libsndfile1`); see
[src/plugins/watermark/aware/Dockerfile](src/plugins/watermark/aware/Dockerfile)
for the full install. The path of least resistance is to keep aware in
Docker and run only the APIs locally.

```bash
python -m venv venv && source venv/bin/activate

pip install -e "./src/resolution-api[dev]"
pip install -e "./src/ingestion-api[dev]"
```

Then (with mongo running locally on 27017):

```bash
# 1. plugin (via docker — exposed on host port 8102)
docker compose up -d watermark-aware-20

# 2. resolution-api
cd src/resolution-api && resolution-api          # PORT=8000

# 3. ingestion-api
cd src/ingestion-api && ingestion-api            # PORT=8001
```

Tweak per-service config via the env vars described in each service's
README + `.env.example` at the repo root. Default settings assume a
Docker network, so for bare-metal dev you'll likely want:

```bash
# Shared by both APIs.
export PLUGINS_CATALOG_PATH=$PWD/plugins.yaml

# Ingestion-api required vars (Mongo + storage + credentials + push target).
export STORAGE_ROOT=$PWD/.storage
export CREDENTIALS_DIR=$PWD/credentials
export MONGODB_URL=mongodb://127.0.0.1:27017
export DATABASE_NAME=c2pa_ingestions
export RESOLUTION_API_URL=http://127.0.0.1:8000   # or RESOLUTION_PUSH_ENABLED=false

# point ingestion-api at the local plugin instead of the docker hostname
sed -i 's|http://watermark-aware-20:9004|http://127.0.0.1:8102|' plugins.yaml
```

resolution-api ships defaults for `MONGODB_URL`
(`mongodb://localhost:27017`) and `DATABASE_NAME` (`c2pa_soft_bindings`)
that work out of the box on a local Mongo, so it only *requires*
`PLUGINS_CATALOG_PATH`. Override either env var if you want it to share
ingestion-api's DB or point at a separate cluster.

## Tests

The two APIs ship pytest suites. From the repo root:

```bash
( cd src/resolution-api && pytest -q )
( cd src/ingestion-api  && pytest -q )
```

- `src/resolution-api` — route smoke, store, query, catalog loader. No
  Mongo needed (uses in-memory repos).
- `src/ingestion-api` — end-to-end orchestrator with a stubbed plugin
  client + stubbed resolution-api auto-push. Needs `credentials/` for
  real C2PA signing (auto-skips otherwise). Catalog + plugin client +
  push client are tested separately with `httpx.MockTransport`.

Both suites run with no external services (Mongo, plugin containers,
and resolution-api are stubbed or use in-memory repos). Pass on a
fresh checkout once `credentials/` is populated.

## Adding a new plugin

1. Create `src/plugins/<watermark|fingerprint>/<name>/{app.py,requirements.txt,Dockerfile}`.
   Mirror `src/plugins/watermark/aware/` for the shape — watermark
   plugins expose `/info`, `/embed`, `/detect`, `/health`; fingerprint
   plugins expose `/info`, `/compute`, `/health`. The binary endpoints
   (`/embed`, `/detect`, `/compute`) take raw asset bytes in the request
   body with `Content-Type: application/octet-stream` plus an
   `X-Media-Type: <real mime>` header. `/embed` additionally requires
   `X-Binding-Value` (the API-minted value the plugin must embed) and
   echoes it on the response; `/detect` and `/compute` return JSON.
2. Register the plugin in `plugins.yaml`. The `url` field supports
   `${ENV_VAR}` substitution, so internal-only plugins can be declared
   in the catalog without hard-coding their location.
3. Add a service block to `docker-compose.yml` with a hostname matching
   the YAML `url`.
4. Restart with `docker compose up -d --build`.

resolution-api will surface the new alg on the next call to
`/services/supportedAlgorithms` without a restart — it re-reads
`plugins.yaml` per request. ingestion-api loads the catalog **once at
startup** and threads it through DI, so it needs the restart triggered
by step 4 above before the new alg is routable.

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
| `PLUGINS_CATALOG_PATH` | both | **required** | Shared YAML catalog path |
| `STORAGE_ROOT` | ingestion-api | **required** | Where signed assets + manifest bytes land (records live in Mongo) |
| `CREDENTIALS_DIR` | ingestion-api | **required** | Cert + key root |
| `SIGNING_ALG` | ingestion-api | `ES256` | C2PA signing algorithm |
| `DEFAULT_AUDIO_ALG` | ingestion-api | `me.deepmark.audio.aware.20` | Alg used when `POST /ingest` doesn't pass an explicit `algs` field |
| `TA_URL` | ingestion-api | _(unset)_ | RFC 3161 timestamp authority |
| `RESOLUTION_PUSH_ENABLED` | ingestion-api | `true` | Auto-push to resolution-api after sign |
| `RESOLUTION_API_URL` | ingestion-api | _(required when push enabled)_ | Auto-push target |
| `RESOLUTION_MAX_RETRIES` | ingestion-api | `1` | Per-HTTP-call retries on transient failure (5xx, timeout, network); 4xx never retries |
| `RESOLUTION_RETRY_BACKOFF_S` | ingestion-api | `0.5` | Initial backoff between retries (doubles each attempt) |
| `RESOLUTION_REQUEST_TIMEOUT_S` | ingestion-api | `10.0` | Per-call HTTP timeout for `POST /manifests` and `POST /bindings` |
| `PLUGIN_REQUEST_TIMEOUT_S` | ingestion-api | `60.0` | Per-call HTTP timeout for plugin `/embed` and `/compute` |
| `MAX_ALGS_PER_INGEST` | ingestion-api | `8` | Cap on `algs` list per `POST /ingest` (rejects with 400 above this) |
| `MAX_TITLE_LENGTH` | ingestion-api | `128` | Cap on the optional `title` form field |
| `PUBLIC_BASE_URL` | ingestion-api | _(unset)_ | Pin the absolute base URL used for `outputAssetUrl` / `manifestUrl`. When unset, derived from `request.base_url` (rewritten by the proxy-headers middleware) |
| `FORWARDED_ALLOW_IPS` | ingestion-api | `*` | CIDRs trusted to send `X-Forwarded-*`. Lock to the proxy CIDR in hardened deployments |
| `MONGO_SERVER_SELECTION_TIMEOUT_S` | ingestion-api | `5.0` | Motor client server-selection deadline |
| `READY_MONGO_TIMEOUT_S` | ingestion-api | `1.5` | `/ready`'s Mongo ping deadline. **Must be < `MONGO_SERVER_SELECTION_TIMEOUT_S`** (validated at startup) so the readiness probe returns before the client's own deadline |
| `HEALTH_DEEP_PROBE_TIMEOUT_S` | ingestion-api | `2.0` | Per-probe timeout for `/health/deep`'s plugin `/health` fan-out |
| `CLAIM_GENERATOR_NAME` / `_VERSION` | ingestion-api | `Deepmark Inc.` / `0.1.0` | Embedded in every C2PA manifest's claim_generator |
| `GIT_SHA` / `IMAGE_TAG` | ingestion-api | _(empty)_ | Build metadata surfaced in `/health/deep`. Set by CI/deploy |
| `LOG_LEVEL` | both | `INFO` | stdlib logging level |
| `LOG_JSON` | both | `false` | Switch logs to single-line JSON for ingestion into Loki/CloudWatch/etc |

## Known limitations / next steps

- `Signer.from_info` is broken for ES256 in c2pa-python 0.32.3; we use
  `Signer.from_callback` + `cryptography` in ingestion-api. Drop the
  explicit `cryptography` dep when the upstream bug is fixed.
- The default audio plugin is `me.deepmark.audio.aware.20`. A second
  proprietary plugin is referenced in `plugins.yaml` for internal
  deployments and is not part of this open-source repo.
- Fingerprint plugins: not implemented; see
  `src/plugins/fingerprint/README.md` for the contract.
- Ingestion-api stores binary artifacts on a Docker volume only; for
  shared multi-host deployments, point `STORAGE_ROOT` at network-
  attached storage or extend `repositories/artifacts.py` with an S3
  backend.
- The `/matches/byContent` and `/matches/byReference` endpoints in
  resolution-api still return empty results — they don't yet call
  back into the plugin containers to recompute bindings.
