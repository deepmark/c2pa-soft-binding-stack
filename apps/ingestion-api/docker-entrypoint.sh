#!/bin/sh
set -eu

alg="${SIGNING_ALG:-ES256}"
alg_lower="$(printf '%s' "$alg" | tr '[:upper:]' '[:lower:]')"
base_url="${SIGNING_CREDENTIALS_BASE_URL:-https://raw.githubusercontent.com/contentauth/c2pa-python/main/tests/fixtures}"

should_fetch_credentials() {
  case "${FETCH_SIGNING_CREDENTIALS:-true}" in
    true|TRUE|1|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

fetch_credentials() {
  if ! should_fetch_credentials; then
    return 0
  fi
  if [ -z "${CREDENTIALS_DIR:-}" ]; then
    echo "CREDENTIALS_DIR must be set when FETCH_SIGNING_CREDENTIALS=true" >&2
    exit 1
  fi

  mkdir -p "$CREDENTIALS_DIR"

  cert_path="$CREDENTIALS_DIR/${alg_lower}_certs.pem"
  key_path="$CREDENTIALS_DIR/${alg_lower}_private.key"

  if [ -s "$cert_path" ] && [ -s "$key_path" ]; then
    return 0
  fi

  cert_tmp="${cert_path}.tmp"
  key_tmp="${key_path}.tmp"
  trap 'rm -f "$cert_tmp" "$key_tmp"' EXIT HUP INT TERM

  echo "Fetching ${alg} signing credentials into ${CREDENTIALS_DIR}" >&2
  curl -fsSLo "$cert_tmp" "${base_url}/${alg_lower}_certs.pem"
  curl -fsSLo "$key_tmp" "${base_url}/${alg_lower}_private.key"

  mv "$cert_tmp" "$cert_path"
  mv "$key_tmp" "$key_path"
  chmod 0644 "$cert_path"
  chmod 0600 "$key_path"
}

fetch_credentials

exec "$@"
