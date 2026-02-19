# ── Stage 1: Builder ─────────────────────────────────────────────
FROM python:3.13.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv

WORKDIR /app

# Layer cache: deps change less often than source
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Source
COPY sleep/ sleep/
COPY shared/ shared/
COPY main.py alembic.ini ./
COPY migrations/ migrations/

# ── Stage 2: Runtime ─────────────────────────────────────────────
FROM python:3.13.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app

LABEL org.opencontainers.image.title="sleep-harmonizer" \
      org.opencontainers.image.description="Sleep Data Harmonizer — Oura + Withings wearable ingestion API" \
      org.opencontainers.image.source="https://github.com/derekmberger/sleep-data-harmonizer"

COPY --from=builder /app /app

RUN groupadd -g 1000 appuser && \
    useradd --no-log-init -r -u 1000 -g appuser appuser && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

ENTRYPOINT ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
