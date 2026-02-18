# Sleep Data Harmonizer

Ingests sleep data from wearable devices (Oura Ring, Withings), normalizes it into a canonical domain model, and exposes product APIs.

**Tech stack:** Python 3.12+, FastAPI, PostgreSQL, SQLAlchemy 2.0, Pydantic v2, structlog, Prometheus

---

## Architecture

The system follows hexagonal architecture (ports and adapters) within a single bounded context around the sleep domain.

```
              ┌──────────────────────────┐
              │       DOMAIN CORE        │
              │  SleepDay, validation,   │
              │  fingerprint computation │
              └────────────┬─────────────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
 ┌────────▼────────┐ ┌────▼─────┐ ┌────────▼────────┐
 │ INBOUND ADAPTERS│ │   REPO   │ │OUTBOUND ADAPTERS│
 │ OuraMapper      │ │ SleepDay │ │ FHIRSerializer  │
 │ WithingsMapper  │ │ Repo     │ │                 │
 └─────────────────┘ └──────────┘ └─────────────────┘
```

Vendor-specific mappers act as an anti-corruption layer — they translate Oura/Withings field names, units, and types into the canonical `SleepDay` model at the boundary. The domain layer never sees vendor-specific data structures. Adding a third vendor (Fitbit, WHOOP, Apple Health) requires writing one mapper and zero schema migrations.

---

## Data Model

### Canonical fields (intersection of both vendors)

9 typed columns representing the fields both Oura and Withings provide:

| Canonical Field | Oura Source | Withings Source | Transform |
|----------------|-------------|-----------------|-----------|
| `total_sleep_minutes` | `total_sleep_duration` (sec) | `total_sleep_time` (sec) | `// 60` |
| `deep_sleep_minutes` | `deep_sleep_duration` (sec) | `deepsleepduration` (sec) | `// 60` |
| `light_sleep_minutes` | `light_sleep_duration` (sec) | `lightsleepduration` (sec) | `// 60` |
| `rem_sleep_minutes` | `rem_sleep_duration` (sec) | `remsleepduration` (sec) | `// 60` |
| `awake_minutes` | `awake_time` (sec) | `wakeupduration` (sec) | `// 60` |
| `sleep_efficiency` | `efficiency` (0-100 int) | `sleep_efficiency` (0.0-1.0) | Normalize to 0.0-1.0 |
| `sleep_onset` | `bedtime_start` (ISO 8601) | `startdate` (Unix epoch) | Parse to `TIMESTAMPTZ` |
| `sleep_offset` | `bedtime_end` (ISO 8601) | `enddate` (Unix epoch) | Parse to `TIMESTAMPTZ` |
| `effective_date` | `day` (YYYY-MM-DD) | `date` (YYYY-MM-DD) | Parse to `DATE` |

Vendor-specific fields (Oura's `movement_30_sec`, Withings' `snoring`, etc.) are stored in the `extra` JSONB column — no data is discarded.

### Nullable strategy

`NULL` means "vendor did not provide this value", never "zero". Identity and provenance fields (`id`, `patient_id`, `source`, `source_record_id`, `fingerprint`, `raw_payload`, `effective_date`) are NOT NULL. All sleep metrics are nullable.

### Tables

| Table | Role | Mutability |
|-------|------|------------|
| `sleep_days` | Canonical normalized data (silver layer) | Upserted via fingerprint |
| `raw_vendor_responses` | Exact vendor API responses (bronze layer) | Append-only, immutable |
| `quarantine_records` | Failed records with raw payload + error details | Append; resolved flag toggled |
| `idempotency_keys` | Transport-layer dedup for HTTP POST | TTL-based, 24h expiry |

---

## Pipeline

```
Raw Payload → Mapper (ACL) → Validate (9 rules) → Upsert (ON CONFLICT)
                                    ↓ fail
                              Quarantine Table
```

**Two-layer validation:** Layer 1 is implicit in the mapper (vendor-specific parsing failures). Layer 2 is a shared set of 9 canonical rules applied after mapping: required effective_date, sleep duration range [0, 1440], non-negative stages, stage sum consistency (5% tolerance), no future dates, efficiency range [0.0, 1.0], timezone on timestamps, known source, and sleep window ordering.

**Two-layer idempotency:**

| Layer | Mechanism | Scope |
|-------|-----------|-------|
| Data (authoritative) | `fingerprint = sha256(source:source_record_id:effective_date)` with UNIQUE constraint + upsert | All ingestion paths |
| Transport (optimization) | `Idempotency-Key` HTTP header, stored with TTL | POST ingest endpoint only |

The fingerprint is the authoritative dedup mechanism — it works for HTTP, polling, batch, and replay. The Idempotency-Key short-circuits before the pipeline runs on client retries.

**Replay** is free: read from `raw_vendor_responses`, run through the same pipeline, upsert handles the rest. No special replay mode needed.

---

## API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/ingest/{source}/sleep` | POST | Ingest vendor data (requires `Idempotency-Key` header) |
| `/api/v1/patients/{id}/sleep/timeline` | GET | Paginated sleep records with date range filter |
| `/api/v1/patients/{id}/sleep/summary` | GET | Aggregated metrics for a date range |
| `/api/v1/patients/{id}/sleep/provenance` | GET | Data lineage and source tracing |
| `/health` | GET | Health check |
| `/metrics` | GET | Prometheus metrics |

**Pagination:** Cursor-based on `(effective_date, id)`, encoded as an opaque base64 token. Stable under concurrent writes.

**Errors:** All error responses use [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) Problem Details (`application/problem+json`) with a `violations` extension array for field-level errors.

**Response envelope:**
```json
{
  "data": { },
  "meta": { "request_id": "...", "timestamp": "...", "api_version": "v1" },
  "pagination": { "next_cursor": "...", "has_more": true, "limit": 25 }
}
```

### Ingest status contract

The POST ingest endpoint returns per-record status:

| Status | Meaning |
|--------|---------|
| `created` | New record inserted into `sleep_days` |
| `deduplicated` | Record matched existing fingerprint (upsert, no new row) |
| `quarantined` | Record failed validation (stored in quarantine, `sleep_day_id` is null) |

HTTP 201 if at least one record was created; HTTP 200 if all records were deduplicated or quarantined.

---

## FHIR R4 Output

An outbound serializer converts `SleepDay` into FHIR R4 Observation resources with LOINC-coded components. FHIR is an output format — the canonical model remains the internal source of truth.

| LOINC Code | Display | Canonical Field |
|------------|---------|-----------------|
| 93832-4 | Sleep duration | `total_sleep_minutes` |
| 93831-6 | Deep sleep duration | `deep_sleep_minutes` |
| 93830-8 | Light sleep duration | `light_sleep_minutes` |
| 93829-0 | REM sleep duration | `rem_sleep_minutes` |

Sleep efficiency and awake minutes use text-only codes (no standard LOINC exists). Observation category is `activity` (wearable-generated wellness data).

---

## Key Decisions

| ADR | Decision | Tradeoff |
|-----|----------|----------|
| 001 | Intersection-first canonical model with JSONB `extra` | Stable schema + no data loss, but `extra` fields lack type enforcement |
| 002 | Two-layer idempotency: fingerprint for data dedup + Idempotency-Key for transport | Retry-safe on any ingestion path, but can't distinguish re-delivery from correction at the dedup layer |
| 003 | Separate quarantine table (not inline flag) | Zero risk of bad data in API responses, but two tables to manage |
| 004 | Protocol interface for fixture/live adapter swap | Deterministic tests + demo mode without credentials, but fixtures can drift from real API |
| 005 | Cursor pagination + RFC 9457 errors | Stable and performant pagination, but no "jump to page N" |

---

## Project Structure

```
main.py                          # FastAPI application entry point
pyproject.toml                   # Dependencies, tool config (ruff, pytest)
Makefile                         # Common commands (lint, test, fmt)
alembic.ini                      # Alembic migration config
migrations/
├── env.py                       # Async migration runner
└── versions/
    └── 001_initial_schema.py    # All 4 tables, indexes, constraints, triggers
sleep/                           # Sleep bounded context
├── domain/
│   ├── models.py                # SleepDay (Pydantic), SleepSource
│   ├── orm.py                   # SQLAlchemy models (4 tables)
│   └── validation.py            # 9 canonical validation rules
├── adapters/
│   ├── protocol.py              # SleepAdapter protocol
│   ├── oura_mapper.py           # Oura V2 → SleepDay
│   ├── withings_mapper.py       # Withings Sleep V2 → SleepDay
│   ├── oura_fixture.py          # Fixture adapter (no HTTP)
│   ├── oura_live.py             # Live adapter (fetch + parse)
│   ├── withings_fixture.py      # Fixture adapter (no HTTP)
│   ├── withings_live.py         # Live adapter (fetch + parse)
│   ├── http_client.py           # Retry policy (transient-only)
│   ├── factory.py               # Adapter selection by config
│   └── fhir_serializer.py       # SleepDay → FHIR R4 Observation
├── api.py                       # FastAPI router (4 endpoints)
├── pipeline.py                  # Ingestion pipeline
└── repository.py                # All DB access
shared/
├── config.py                    # pydantic-settings, startup validation
├── database.py                  # Engine, async session factory
├── exceptions.py                # RFC 9457 exception hierarchy
├── logging.py                   # structlog configuration
├── metrics.py                   # Prometheus counters + histograms
└── middleware.py                # Request ID injection, error handlers
scripts/
└── smoke_test.py                # 16-check E2E smoke test (httpx, works against any target)
tests/
├── fixtures/                    # Vendor response JSON fixtures
├── integration/
│   ├── conftest.py              # Testcontainers Postgres + api_client fixture
│   ├── test_api_e2e.py          # 12 full-stack API integration tests
│   ├── test_ingest_roundtrip.py # Pipeline-level roundtrip
│   └── test_upsert_idempotency.py # Repository-level dedup
├── test_adapters.py
├── test_api.py
├── test_fhir.py
├── test_models.py
├── test_pipeline.py
└── test_validation.py
```

---

## Getting Started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)
- PostgreSQL 16+ (for running the app; tests use testcontainers)
- Docker (for integration tests)

### Setup

```bash
uv sync --all-groups
```

### Database

Create the database and run migrations:

```bash
createdb sleep_harmonizer
uv run alembic upgrade head
```

The migration creates all 4 tables, indexes, check constraints, and an `updated_at` trigger. Connection string defaults to `postgresql+asyncpg://postgres:postgres@localhost:5432/sleep_harmonizer` and can be overridden with `SH_DATABASE_URL`.

### Running

```bash
uv run uvicorn main:app --reload
```

The API is available at `http://localhost:8000`. Interactive docs at `/docs`.

### Adapter modes

The system runs in two modes controlled by `SH_ADAPTER_MODE`:

- **`fixture`** (default): Adapters parse raw payloads passed directly. No vendor API credentials needed. Use for development and testing.
- **`live`**: Adapters fetch from vendor APIs before parsing. Requires `SH_OURA_ACCESS_TOKEN` and `SH_WITHINGS_ACCESS_TOKEN`.

---

## Testing

```bash
make check              # lint + unit tests + OpenAPI drift check
make test-unit          # unit tests only (88 tests, <1s)
make test-integration   # integration tests (requires Docker, ~3s)
make test               # all tests
make lint               # ruff check
make fmt                # ruff format
make openapi-check      # verify openapi.json matches code
```

### Test pyramid

| Layer | What it covers | Count |
|-------|---------------|-------|
| Unit | Mapper parsing, validation rules, fingerprint generation, FHIR serialization, API routing, adapter factory | 88 |
| Integration (data) | Upsert idempotency, ingest-to-timeline roundtrip with real PostgreSQL | 4 |
| Integration (API) | Full HTTP stack: httpx → FastAPI → pipeline → repository → real Postgres. Covers ingest, idempotency, quarantine, bronze-layer persistence, timeline, pagination, summary, provenance, RFC 9457 errors | 12 |
| Smoke (E2E) | 16 checks against a running instance (Docker Compose or K8s) | 16 checks |

Integration tests use [testcontainers](https://testcontainers.com/) to spin up a real PostgreSQL instance — no mocks for database behavior.

### Smoke tests

The smoke test is a standalone Python script that validates a running instance end-to-end. It uses unique IDs per run, so it's safe to run repeatedly against persistent environments.

```bash
# Against an already-running instance (default: http://localhost:8000)
make smoke

# Full lifecycle: Docker Compose up → smoke test → tear down
make smoke-local

# K8s: port-forward → smoke test → cleanup (requires helm install first)
make smoke-k8s
```

Options:

```bash
# Custom target URL and longer readiness wait
uv run python scripts/smoke_test.py --base-url http://10.0.0.5:8000 --wait 120 --verbose
```

The smoke test exits with the number of failed checks (0 = all passed).

---

## Observability

**Structured logging** via structlog. Every log entry is JSON with `request_id` correlation (injected by middleware), `source`, `patient_id`, and domain-specific context.

**Prometheus metrics** exposed at `/metrics`:

| Metric | Type | Labels |
|--------|------|--------|
| `ingestion_records_total` | Counter | `source`, `status` |
| `validation_failures_total` | Counter | `source`, `rule` |
| `api_requests_total` | Counter | `endpoint`, `method`, `status_code` |
| `pipeline_duration_seconds` | Histogram | `source` |
| `vendor_api_duration_seconds` | Histogram | `source` |
| `api_response_duration_seconds` | Histogram | `endpoint` |
