# Test signing credentials

The ingest pipeline needs a cert chain + matching private key for local
C2PA signing. The directory is configurable via `CREDENTIALS_DIR`; the
file names inside it are derived from `SIGNING_ALG` so multiple
algorithms' fixtures can coexist in one directory:

```
${CREDENTIALS_DIR}/
  ${signing_alg_lower}_certs.pem      # full chain (leaf + intermediates), PEM
  ${signing_alg_lower}_private.key    # private key, PKCS#8 PEM
```

For the default ES256 setup that's `es256_certs.pem` + `es256_private.key`.

Configure via env var:

```
CREDENTIALS_DIR=/abs/path/to/credentials
SIGNING_ALG=ES256                                    # see table above
TA_URL=https://your-rfc3161-tsa.example/timestamp    # optional, RFC 3161 TSA
```

## Container startup automation

The ingestion-api Docker image has an entrypoint that fetches these
files before the app starts when they are missing:

```
CREDENTIALS_DIR=/var/lib/ingestion-api/credentials
SIGNING_ALG=ES256
FETCH_SIGNING_CREDENTIALS=true
```

By default the entrypoint runs the equivalent of:

```
curl -fsSLo ${CREDENTIALS_DIR}/es256_certs.pem \
  https://raw.githubusercontent.com/contentauth/c2pa-python/main/tests/fixtures/es256_certs.pem
curl -fsSLo ${CREDENTIALS_DIR}/es256_private.key \
  https://raw.githubusercontent.com/contentauth/c2pa-python/main/tests/fixtures/es256_private.key
```

Override `SIGNING_CREDENTIALS_BASE_URL` to fetch the same file names
from another location.

## Where to get test certs

The C2PA project ships ready-made test certs for each supported algorithm.
See:

- https://opensource.contentauthenticity.org/docs/signing/test-certs
- https://github.com/contentauth/c2pa-rs/tree/main/sdk/tests/fixtures/certs

ES256 (default):

```
curl -fsSLo es256_certs.pem \
  https://raw.githubusercontent.com/contentauth/c2pa-python/main/tests/fixtures/es256_certs.pem
curl -fsSLo es256_private.key \
  https://raw.githubusercontent.com/contentauth/c2pa-python/main/tests/fixtures/es256_private.key
```

ES384 / ES512 / PS256 / PS384 / PS512 / ED25519: the same upstream repo
has equivalent fixtures (`es384_*`, `ps256_*`, `ed25519_*`, …). Drop
them in here and set `SIGNING_ALG` to match — ingestion-api looks for
`<signing_alg_lower>_certs.pem` and `<signing_alg_lower>_private.key`
automatically.

## Generating your own (optional)

ES256:

```
openssl ecparam -name prime256v1 -genkey -noout -out es256_private.key
openssl req -new -x509 -key es256_private.key -out es256_certs.pem -days 365 \
  -subj "/CN=Soft Binding API Local Test"
```

ES384 / ES512:

```
openssl ecparam -name secp384r1 -genkey -noout -out es384_private.key
openssl ecparam -name secp521r1 -genkey -noout -out es512_private.key
```

PS256 (RSA-PSS, SHA-256):

```
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out ps256_private.key
openssl req -new -x509 -key ps256_private.key -out ps256_certs.pem -days 365 \
  -subj "/CN=Soft Binding API Local Test"
```

ED25519:

```
openssl genpkey -algorithm ED25519 -out ed25519_private.key
openssl req -new -x509 -key ed25519_private.key -out ed25519_certs.pem -days 365 \
  -subj "/CN=Soft Binding API Local Test"
```

Self-signed certs satisfy the SDK for local development, but real C2PA
verifiers will reject them. They expect a chain rooted in a C2PA-aware
trust list. For production, get certs from a CA that publishes its root
on a recognised C2PA trust list.

## Don't commit these files

`credentials/*.pem`, `*.key`, `*.crt`, `*.cer`, `*.p12`, `*.pfx` are in
`.gitignore`. Keep them out of version control.
