#!/usr/bin/env bash
# Build the pinned native library used by c2pa-python. No publication occurs.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 BUILD_DIRECTORY OUTPUT_DIRECTORY" >&2
  exit 2
fi

C2PA_NATIVE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p -- "$1" "$2"
C2PA_BUILD_DIR="$(cd -- "$1" && pwd)"
C2PA_OUTPUT_DIR="$(cd -- "$2" && pwd)"
C2PA_REVISION=e7cc666c3409bdaf4cea8e49046dfbf8290dff89
C2PA_ARCHIVE_SHA256=2597e7a98823f290032ba45b949e729c2cc6deee875961e0b843641a13ef5db1
C2PA_ARCHIVE="$C2PA_BUILD_DIR/$C2PA_REVISION.tar.gz"

if [[ ! -f "$C2PA_ARCHIVE" ]]; then
  curl --fail --location --retry 3 \
    "https://codeload.github.com/contentauth/c2pa-rs/tar.gz/$C2PA_REVISION" \
    --output "$C2PA_ARCHIVE.part"
  mv -- "$C2PA_ARCHIVE.part" "$C2PA_ARCHIVE"
fi
printf '%s  %s\n' "$C2PA_ARCHIVE_SHA256" "$C2PA_ARCHIVE" | sha256sum --check --status

# A fresh extraction prevents a previous patch or local source edit being reused.
C2PA_SOURCE_DIR="$(mktemp -d "$C2PA_BUILD_DIR/source.XXXXXXXX")"
tar -xzf "$C2PA_ARCHIVE" --strip-components=1 -C "$C2PA_SOURCE_DIR"
patch --batch --fuzz=0 --directory="$C2PA_SOURCE_DIR" -p1 \
  < "$C2PA_NATIVE_DIR/related-assertions.patch"
cp -- "$C2PA_NATIVE_DIR/Cargo.lock" "$C2PA_SOURCE_DIR/Cargo.lock"

CARGO_TARGET_DIR="$C2PA_BUILD_DIR/target" cargo +1.96.0 build \
  --manifest-path "$C2PA_SOURCE_DIR/Cargo.toml" \
  --locked --release -p c2pa-c-ffi --jobs "${C2PA_BUILD_JOBS:-4}"
install -m 0755 "$C2PA_BUILD_DIR/target/release/libc2pa_c.so" \
  "$C2PA_OUTPUT_DIR/libc2pa_c.so"
printf 'Built native SDK 0.91.0+deepmark.1: %s/libc2pa_c.so\n' "$C2PA_OUTPUT_DIR"
