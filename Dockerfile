# syntax=docker/dockerfile:1.7
# ---------- Stage 1: builder ----------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System build deps for wheels (CBC solver ships as wheel, but keep build-essential
# available for any source-only transitive dep).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies into a prefix we copy into the runtime stage.
# README.md is referenced by pyproject.toml (readme = "README.md") so it must
# be present at metadata-generation time.
COPY pyproject.toml README.md ./
COPY src ./src

# Use pip's "install --prefix" into /install then copy the prefix into runtime.
RUN pip install --prefix=/install --no-cache-dir .

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/usr/local/bin:${PATH}" \
    PYTHONPATH=/app/src \
    PORT=8080 \
    GRIDWISE_WORKERS=1 \
    GRIDWISE_LOG_LEVEL=info

# Minimal runtime system packages: tini for signal handling, curl for healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin gridwise

WORKDIR /app

# Pull the installed Python packages from the builder.
COPY --from=builder /install /usr/local

# Copy application source (kept small — system prompt is required at runtime).
COPY --chown=gridwise:gridwise src ./src

USER gridwise

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

# tini reaps zombies and forwards signals (SIGTERM triggers graceful shutdown).
ENTRYPOINT ["/usr/bin/tini", "--"]

# Single uvicorn worker — scaling happens horizontally at the ECS task level.
# worker-class=uvloop for lower-latency event loop; --proxy-headers for ALB X-Forwarded-* parsing.
CMD ["sh", "-c", "exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port ${PORT} \
    --workers ${GRIDWISE_WORKERS} \
    --loop uvloop \
    --http httptools \
    --proxy-headers \
    --forwarded-allow-ips='*' \
    --access-log \
    --log-level ${GRIDWISE_LOG_LEVEL}"]
