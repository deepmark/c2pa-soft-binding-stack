# ---------- build stage ----------
FROM python:3.12-slim AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install deps first — this layer is cached until package metadata changes
COPY pyproject.toml README.md ./
RUN pip install --upgrade pip \
 && mkdir -p src/resolution_api \
 && touch src/resolution_api/__init__.py \
 && pip install .

# Copy actual source and reinstall (only rebuilds the app, deps cached)
COPY src ./src
RUN pip install . --no-deps

# ---------- runtime stage ----------
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.environ.get('PORT', '8000'); urllib.request.urlopen(f'http://localhost:{port}/health')" || exit 1

CMD ["sh", "-c", "uvicorn resolution_api.__main__:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000}"]
