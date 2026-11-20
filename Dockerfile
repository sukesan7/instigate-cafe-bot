# ==============================================================================
# Instigate Cafe Bot — Minimal Non-Root Production Container
# ==============================================================================
# Multi-stage build ensuring minimal image attack surface, zero baked secrets,
# locked dependency resolution via uv, and dedicated non-root execution.
#
# Discord gateway connections are purely outbound over HTTPS/WSS; no inbound ports
# are exposed. Persistent SQLite database and local state are bind-mounted at /data.
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: Dependency builder
# ------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# Install uv binary from official image
COPY --from=ghcr.io/astral-sh/uv:0.5.26 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Cache dependency resolution layer
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application source and build wheels
COPY icbot ./icbot
COPY tools ./tools
COPY main.py ./

RUN uv sync --frozen --no-dev

# ------------------------------------------------------------------------------
# Stage 2: Minimal non-root runtime
# ------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# Install runtime SQLite3 CLI for backup and healthcheck diagnostics
RUN apt-get update && \
    apt-get install -y --no-install-recommends sqlite3 ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

# Create dedicated non-root user and group (UID/GID 10001)
RUN groupadd -g 10001 campusbot && \
    useradd -u 10001 -g campusbot -s /bin/bash -m campusbot

WORKDIR /app

# Copy isolated virtual environment and application code from builder
COPY --from=builder --chown=campusbot:campusbot /app/.venv /app/.venv
COPY --chown=campusbot:campusbot icbot /app/icbot
COPY --chown=campusbot:campusbot tools /app/tools
COPY --chown=campusbot:campusbot main.py /app/main.py

# Create persistent state directory with campusbot ownership
RUN mkdir -p /data && chown -R campusbot:campusbot /data

# Outbound networking only: Discord connects via outgoing websocket/HTTPS; no EXPOSE ports needed
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SQLITE_PATH="/data/campus.db" \
    ICBOT_ALLOW_GATEWAY="true"

USER campusbot

# Healthcheck ensures Python runtime and configuration loader execute cleanly
HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD python3 -c "import icbot; from icbot.config import Settings; Settings.from_env()" || exit 1

# Process signal handling: Discord.py cleanly cancels tasks and closes DB on SIGTERM
STOPSIGNAL SIGTERM

ENTRYPOINT ["python3", "main.py", "--allow-gateway"]

