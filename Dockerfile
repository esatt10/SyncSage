FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="SyncSage" \
      org.opencontainers.image.description="Docker-first MCP knowledge graph indexer and agentic retrieval layer" \
      org.opencontainers.image.source="https://github.com/esatt10/SyncSage" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    SYNCSAGE_CONFIG=/config/syncsage.yaml

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

# Install from pyproject so the image's dependencies never drift from the
# package's declared deps (a hand-maintained list previously omitted core deps
# added later — numpy (21.4) and zstandard (21.6a) — breaking the smoke test).
# ".[mcp]" = core deps + the MCP server extra the container serves.
COPY pyproject.toml README.md LICENSE /app/
COPY src /app/src
RUN pip install --no-cache-dir ".[mcp]"

COPY syncsage.example.yaml /config/syncsage.yaml

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/health || exit 1

CMD ["python", "-m", "syncsage", "serve", "--config", "/config/syncsage.yaml"]
