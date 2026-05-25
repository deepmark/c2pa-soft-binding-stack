# ---------- build stage ----------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        gcc \
        g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only PyTorch (skips ~1.5 GB of NVIDIA libs)
RUN pip install --timeout=300 \
    torch==2.7.0+cpu torchaudio==2.7.0 \
    --extra-index-url https://download.pytorch.org/whl/cpu

ARG AWARE_VERSION=v1.0.0
RUN git clone https://github.com/deepmark/aware.git aware \
 && cd aware && git checkout ${AWARE_VERSION} \
 && pip install --timeout=300 --no-deps -e . \
 && pip install --timeout=300 \
    numpy==1.26.4 librosa==0.9.2 soundfile==0.12.1 pydantic==2.5.0 \
    matplotlib==3.7.2 scikit-learn==1.5.0 numba==0.59.0 resampy==0.4.2 \
    tqdm==4.66.1 pyyaml==6.0.1

# Service-specific deps
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------- runtime stage ----------
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG APP_PORT=9004
ENV APP_PORT=${APP_PORT}
ENV HOST="0.0.0.0"

RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app/aware /app/aware

COPY app.py .

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE ${APP_PORT}

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${APP_PORT}/health')" || exit 1

CMD ["sh", "-c", "uvicorn app:app --host=$HOST --port=$APP_PORT"]
