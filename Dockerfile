# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# The token exists only for this RUN. Without a secret, public sources fetch anonymously.
RUN --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
        export GIT_CONFIG_COUNT=1 \
          GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
          GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi \
    && uv sync --no-install-project --no-dev

COPY src/ src/
RUN --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
        export GIT_CONFIG_COUNT=1 \
          GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
          GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi \
    && uv sync --no-dev

# The data directory is created, owned and locked down *before* the volume is declared:
# docker seeds a named volume from the image path, so the ownership set here is what the
# volume gets. Without it the volume arrives root-owned, the unprivileged user cannot
# create memory.db, and the service starts happily and then answers /ready with a
# database OperationalError -- healthy to docker's healthcheck, useless to a caller.
RUN useradd --create-home --uid 10001 memory \
    && mkdir -p /var/lib/memory \
    && chown -R memory:memory /var/lib/memory /app \
    && chmod 700 /var/lib/memory
VOLUME ["/var/lib/memory"]
USER memory

ENV MEMORY_HOST=0.0.0.0 \
    MEMORY_PORT=8009 \
    MEMORY_DATABASE_PATH=/var/lib/memory/memory.db \
    MEMORY_LOG_FORMAT=json

EXPOSE 8009

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8009/healthy', timeout=4).status == 200 else 1)"

CMD ["memory-api"]
