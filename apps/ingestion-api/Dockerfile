# ---------- native C2PA reference support ----------
FROM rust:1.99.1-bookworm AS c2pa-native

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl patch pkg-config libssl-dev \
 && rm -rf /var/lib/apt/lists/*
COPY native /native
RUN bash /native/build-c2pa.sh /build /opt/c2pa

# ---------- build stage ----------
FROM python:3.14-slim AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install deps first — cached until package metadata changes
COPY pyproject.toml README.md ./
RUN pip install --upgrade pip \
 && mkdir -p src/ingestion_api \
 && touch src/ingestion_api/__init__.py \
 && pip install .

# Copy actual source and reinstall (deps already cached)
COPY src ./src
RUN pip install . --no-deps

# ---------- runtime stage ----------
FROM python:3.14-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    C2PA_LIBRARY_NAME=/opt/c2pa/libc2pa_c.so

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser \
 && mkdir -p /var/lib/ingestion-api/storage /var/lib/ingestion-api/credentials \
 && chown -R appuser:appuser /var/lib/ingestion-api

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=c2pa-native /opt/c2pa /opt/c2pa
COPY docker-entrypoint.sh /usr/local/bin/ingestion-entrypoint

RUN chmod 0755 /usr/local/bin/ingestion-entrypoint
RUN python -c "import c2pa; assert c2pa.sdk_version() == '0.91.0+deepmark.1', c2pa.sdk_version()"

# Volume will be owned by appuser via the init container / entrypoint fix
VOLUME /var/lib/ingestion-api/storage

USER appuser

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.environ.get('PORT', '8001'); urllib.request.urlopen(f'http://localhost:{port}/health')" || exit 1

ENTRYPOINT ["ingestion-entrypoint"]
CMD ["sh", "-c", "uvicorn ingestion_api.__main__:app --host ${HOST:-0.0.0.0} --port ${PORT:-8001}"]
