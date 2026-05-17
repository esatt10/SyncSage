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

RUN pip install --no-cache-dir \
    "fastapi>=0.111" \
    "uvicorn[standard]>=0.30" \
    "pydantic>=2.7" \
    "PyYAML>=6.0" \
    "networkx>=3.3" \
    "typer>=0.12" \
    "watchdog>=4.0" \
    "markdown-it-py>=3.0" \
    "beautifulsoup4>=4.12" \
    "python-docx>=1.1" \
    "pymupdf>=1.24" \
    "mcp>=1.27,<2"

COPY pyproject.toml README.md LICENSE /app/
COPY src /app/src
COPY syncsage.example.yaml /config/syncsage.yaml

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/health || exit 1

CMD ["python", "-m", "syncsage", "serve", "--config", "/config/syncsage.yaml"]
