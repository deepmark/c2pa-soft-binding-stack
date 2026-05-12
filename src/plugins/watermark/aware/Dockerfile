FROM python:3.11-slim

ARG APP_PORT=9004
ENV APP_PORT=${APP_PORT}

ARG HOST="0.0.0.0"
ENV HOST=${HOST}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        gcc \
        g++ \
        ffmpeg \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone and install AWARE package
RUN git clone https://github.com/deepmarkpy/aware.git aware && \
    cd aware && pip install --no-cache-dir -e .

# Install service-specific dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .

EXPOSE ${APP_PORT}

CMD ["sh", "-c", "uvicorn app:app --host=$HOST --port=$APP_PORT"]
