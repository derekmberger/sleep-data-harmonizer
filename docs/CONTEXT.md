# Sleep Data Harmonizer — Implementation Context

> **Purpose:** Orients implementation tools (Codex, Claude, etc.) to the project design. This is a map — the detailed artifacts (code, schemas, mapping tables, ADRs) live in the study docs linked below.

---

## 1. Project Overview

**What:** A mock project (3–4 days) that ingests sleep data from Oura Ring and Withings, normalizes it into a canonical `SleepDay` model, and exposes product APIs. Demonstrates backend/platform engineering competence for a healthcare data company (Blue).

**Tech stack:** Python 3.12+, FastAPI, PostgreSQL, SQLAlchemy, Pydantic v2, pytest, OpenAPI 3.1.0

**What Blue evaluates:**

| Axis | Signal |
|------|--------|
| Architecture + API/Data Design | Canonical model, intersection-first field selection, JSONB extras, cursor pagination, RFC 9457 errors |
| Implementation Quality | Idempotent pipeline, quarantine pattern, two-layer validation, golden fixture tests |
| Impact/Ownership/Autonomy | 5 ADRs with alternatives, tradeoffs, and explicit deferrals |
| Domain Experience (healthcare) | FHIR R4 Observation output serializer with LOINC codes |

---

## 2. Architecture Summary

### DDD Alignment

| DDD Concept | Implementation |
|-------------|---------------|
| Bounded Context | Sleep domain owns: canonical model, mappers, validation, quarantine, API, repository, FHIR serializer |
| Ubiquitous Language | `SleepDay`, `source`, `effective_date`, `raw_payload`, `source_record_id` — one name everywhere |
| Entity | `SleepDay` — UUID identity, persists over time |
| Value Object | `SleepSource.OURA`, `effective_date`, fingerprint |
| Aggregate Root | `SleepDay` — owns all its fields; loaded/saved atomically via upsert |
| Repository | `SleepDayRepository` encapsulates all DB access |
| Anti-Corruption Layer | Inbound: vendor mappers. Outbound: FHIR serializer |
| Domain Events | Intentionally deferred — synchronous pipeline sufficient for MVP |

> **Deep dive:** `study_domain_modeling.md` Section 2.1 (DDD concepts, bounded context diagram, interview talking points)

### Hexagonal Architecture (Ports and Adapters)

```
                  ┌────────────────────────────┐
                  │       DOMAIN CORE           │
                  │  SleepDay, validation,      │
                  │  fingerprint computation    │
                  └─────────┬──────────────────┘
                            │
           ┌────────────────┼────────────────┐
           │                │                │
  ┌────────▼────────┐ ┌────▼─────┐ ┌────────▼────────┐
  │ INBOUND ADAPTERS│ │   REPO   │ │OUTBOUND ADAPTERS│
  │ OuraMapper      │ │ SleepDay │ │ FHIRSerializer  │
  │ WithingsMapper  │ │ Repo     │ │                 │
  │ OuraFixture     │ │          │ │                 │
  │ WithingsFixture │ │          │ │                 │
  └─────────────────┘ └──────────┘ └─────────────────┘
```

### Bounded Context Boundary

**Inside (sleep domain controls):** SleepDay model, mappers, validation, quarantine, API endpoints, repository, FHIR serializer

**Outside (referenced by ID only):** Patient identity (`patient_id: UUID`), authentication (middleware), device management, clinical interpretation

---

## 3. Data Model at a Glance

### Canonical Fields (intersection of both vendors)

9 typed columns — fields both Oura and Withings provide:

| Canonical Field | Oura Source | Withings Source | Transform |
|----------------|-------------|-----------------|-----------|
| `total_sleep_minutes` | `total_sleep_duration` (sec) | `total_sleep_time` (sec) | `// 60` |
| `deep_sleep_minutes` | `deep_sleep_duration` (sec) | `deepsleepduration` (sec) | `// 60` |
| `light_sleep_minutes` | `light_sleep_duration` (sec) | `lightsleepduration` (sec) | `// 60` |
| `rem_sleep_minutes` | `rem_sleep_duration` (sec) | `remsleepduration` (sec) | `// 60` |
| `awake_minutes` | `awake_time` (sec) | `wakeupduration` (sec) | `// 60` |
| `sleep_efficiency` | `efficiency` (0-100 int) | `sleep_efficiency` (0.0-1.0) | Normalize to 0.0-1.0 |
| `sleep_onset` | `bedtime_start` (ISO 8601) | `startdate` (Unix epoch) | Parse to timestamptz |
| `sleep_offset` | `bedtime_end` (ISO 8601) | `enddate` (Unix epoch) | Parse to timestamptz |
| `effective_date` | `day` (YYYY-MM-DD) | `date` (YYYY-MM-DD) | Parse to DATE |

Everything else goes in `extra` JSONB. Top promotion candidate: **sleep latency** (both vendors provide it).

> **Full model:** `study_domain_modeling.md` Section 5.1 (Pydantic), 5.2 (SQL DDL), 5.3 (SQLAlchemy), 5.4-5.5 (mapping tables with 25+ fields each), 5.6 (nullable strategy), 5.8 (extra JSONB pattern), 5.9 (PostgreSQL timezone handling)

### Nullable Strategy

- **NOT NULL:** identity (`id`, `patient_id`), provenance (`source`, `source_record_id`, `raw_payload`, `fingerprint`), temporal (`effective_date`, `ingested_at`, `updated_at`), extension (`extra`, defaults to `{}`)
- **NULLABLE:** all sleep metrics and times. `NULL` = "vendor did not provide", never "zero"

### Three Tables

| Table | Role | Mutability |
|-------|------|------------|
| `sleep_days` | Canonical normalized data (silver layer) | Upserted via fingerprint |
| `quarantine_records` | Failed records with raw payload + error details | Append; resolved flag toggled |
| `raw_vendor_responses` | Exact vendor API responses (bronze layer) | Append-only, immutable |

> **Schemas:** `study_domain_modeling.md` Section 5.2 (sleep_days), `study_data_pipelines.md` Section 5.4 (quarantine + raw)

---

## 4. Key Decisions (ADR Summary)

Full ADRs with context, alternatives, consequences, and deferrals in `study_adr_decision_log.md` Section 5.

| ADR | Decision | Key Tradeoff |
|-----|----------|-------------|
| 001 | Intersection-first canonical model with JSONB `extra` | Stable schema + no data loss, but `extra` fields lack type enforcement |
| 002 | Two-layer idempotency: fingerprint `sha256(source:record_id:date)` for data dedup + HTTP `Idempotency-Key` for transport | Retry-safe on any path, but can't distinguish re-delivery from correction |
| 003 | Separate quarantine table (not inline flag) | Zero risk of bad data in API, but two tables to manage |
| 004 | Protocol interface for fixture/live adapter swap via DI | Deterministic tests + demo mode, but fixtures can drift from real API |
| 005 | Cursor pagination + RFC 9457 errors | Stable + performant pagination, but no "jump to page N" |

---

## 5. Pipeline Flow

```
Vendor API → Fetch → Mapper (ACL) → Shared Validation → Upsert (ON CONFLICT)
                                          ↓ fail
                                    Quarantine Table
```

- **Two-layer validation:** Layer 1 implicit in mapper (vendor-specific parsing). Layer 2 shared canonical rules (9 rules: ranges, consistency, temporal, source).
- **Idempotency (two-layer):** Data layer: `fingerprint = sha256(source:source_record_id:effective_date)`, UNIQUE constraint, `INSERT ... ON CONFLICT DO UPDATE`. Transport layer: `Idempotency-Key` header on HTTP POST short-circuits redundant pipeline runs.
- **Replay:** Read from `raw_vendor_responses` → same pipeline → upsert overwrites. No special replay mode needed.

> **Detail:** `study_data_pipelines.md` Section 2 (concepts), Section 5 (code: fingerprint function, upsert SQL, validation rules, quarantine schema, pipeline diagram, replay function)

---

## 6. API Surface

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/ingest/{source}/sleep` | POST | Ingest vendor data (requires `Idempotency-Key` header) |
| `/api/v1/patients/{id}/sleep/timeline` | GET | Paginated sleep records (cursor pagination, date range filter) |
| `/api/v1/patients/{id}/sleep/summary` | GET | Aggregated metrics for a period |
| `/api/v1/patients/{id}/sleep/provenance` | GET | Data lineage and source tracing |

- **Pagination:** Cursor-based, encodes `(effective_date, id)` as opaque base64 token
- **Errors:** RFC 9457 Problem Details with `violations` extension for field-level errors
- **Envelope:** `{ data, meta: { request_id, timestamp, api_version }, pagination }`

> **Detail:** `study_api_design.md` Section 2.1-2.7 (concepts incl. content negotiation), 5.1 (full OpenAPI 3.1.0 spec YAML), 5.2 (error examples), 5.3 (query param contract), 5.4 (idempotency key pattern), 5.5 (response envelope pattern)

---

## 7. FHIR R4 Output

The FHIR serializer converts `SleepDay` → FHIR R4 Observation with LOINC-coded components. It's an **outbound ACL** — FHIR is an output format, not storage.

**LOINC codes used:** 93832-4 (total sleep), 93831-6 (deep), 93830-8 (light), 93829-0 (REM). Source: HL7 PHR IG Sleep Observation ValueSet. Sleep efficiency has no LOINC — uses text-only code.

> **Detail:** `study_domain_modeling.md` Section 5.7 (LOINC mapping table, serializer function, example output JSON, design decisions)

---

## 8. Vendor Integration Notes

### Oura V2
- **Auth:** OAuth 2.0 only (PATs deprecated Dec 2025). 8 scopes; `daily` covers sleep.
- **Rate limit:** 5,000 req / 5-min rolling window. Webhooks strongly recommended.
- **Caveat:** Data requires user to open app to sync. App limited to 10 users pre-approval.
- **Type enum:** 5 values (`deleted`, `sleep`, `long_sleep`, `late_nap`, `rest`). Ingest `long_sleep` with `period==0` only (primary overnight sleep).
- **Pervasive nullability:** Nearly every numeric field is `type|null`.

### Withings
- **Auth:** Non-standard OAuth 2.0 (requires `action=requesttoken`, HMAC `signature`, `nonce`).
- **Rate limit:** 120 req/min. Access tokens expire in 3 hours.
- **Response shape:** `{ "status": 0, "body": { "series": [...] } }`
- **Field aliases:** `durationtosleep` / `sleep_latency`, `durationtowakeup` / `wakeup_latency`.

> **Detail:** `study_oura_withings_integration.md` — example JSONs (5.1-5.2), field mapping tables (5.3-5.4), adapter protocol (5.5), fixture adapter impl (5.6), live adapter impl (5.7), rate limiter impl (5.8), realistic fixture files (5.9), adapter factory/DI wiring (5.10), OAuth flows (2.3), rate limit strategies (2.4)

---

## 9. Document Map

| File | What It Contains | Key Sections to Read |
|------|-----------------|---------------------|
| `study_domain_modeling.md` | Canonical model design, DDD concepts, Pydantic/SQL/SQLAlchemy models, field mapping tables, FHIR serializer | 2.0 (glossary), 2.1 (DDD deep dive), 5.1-5.3 (model code), 5.4-5.5 (mapping tables), 5.6 (nullable strategy), 5.7 (FHIR serializer), 5.8 (extra JSONB pattern), 5.9 (PG timezone handling) |
| `study_data_pipelines.md` | Idempotency, dedup, validation, quarantine, replay | 2.1-2.8 (concepts), 5.1-5.6 (code: fingerprint, upsert, validation rules, quarantine schema, pipeline diagram, replay) |
| `study_api_design.md` | Contract-first OpenAPI, endpoint design, pagination, RFC 9457, idempotency keys, response envelopes | 2.1-2.7 (concepts incl. content negotiation), 5.1 (full OpenAPI spec), 5.2 (error examples), 5.3 (query contract), 5.4 (idempotency), 5.5 (response envelope) |
| `study_oura_withings_integration.md` | Vendor API details, OAuth, rate limits, field-by-field mapping, adapter pattern, fixture/live mode, adapter factory | 2.1-2.6 (adapter pattern, OAuth, rate limits, webhooks, sync lag), 5.1-5.10 (example JSONs, mapping tables, protocol, fixture/live adapters, rate limiter, adapter factory) |
| `study_adr_decision_log.md` | ADR format, 5 complete ADRs, interview talking points | 2.1-2.6 (ADR concepts), 5 (all 5 ADRs), 7 (talking points with Y-statements) |
| `study_healthcare_interoperability.md` | FHIR basics, HL7 context, SNOMED/LOINC awareness, terminology layer | Background knowledge for domain experience axis |
| `study_implementation_quality.md` | Test pyramid, structured logging, metrics, retry patterns, integration testing | Test plan, logging conventions, observability hooks |

---

## 10. Cross-Cutting Conventions

### Ubiquitous Language

| Concept | Name | NOT This |
|---------|------|----------|
| One night of sleep | `SleepDay` | sleep_record, SleepEntry, sleep_session |
| Data origin | `source` | vendor, provider, origin |
| Vendor's record ID | `source_record_id` | vendor_id, external_id |
| Which night | `effective_date` | sleep_date, night_of |
| Raw vendor JSON | `raw_payload` | raw_data, original_response |

### Temporal Columns

| Column | Question It Answers | Type |
|--------|-------------------|------|
| `effective_date` | When did this sleep happen? | DATE |
| `ingested_at` | When did our system receive it? | TIMESTAMPTZ |
| `updated_at` | When was it last modified? | TIMESTAMPTZ |

### Idempotency Formula

```
fingerprint = sha256(source + ":" + source_record_id + ":" + effective_date)
```

DB unique constraint on `fingerprint`. `INSERT ... ON CONFLICT (fingerprint) DO UPDATE` for last-write-wins.

### Idempotency Reconciliation

Two layers, complementary not redundant:

| Layer | Mechanism | Scope | What it prevents |
|-------|-----------|-------|-----------------|
| Transport | `Idempotency-Key` HTTP header | POST ingest endpoint only | Redundant pipeline execution on client retries |
| Data | `fingerprint` UNIQUE constraint + upsert | All ingestion paths (HTTP, polling, batch, replay) | Duplicate canonical rows regardless of entry point |

The fingerprint is the **authoritative** dedup mechanism. The Idempotency-Key is a performance optimization that short-circuits before the pipeline runs. See ADR-002 for full rationale.

---

## 11. Folder Structure

```
sleep_harmonizer/
├── sleep/                        # Sleep bounded context
│   ├── domain/
│   │   ├── models.py             # SleepDay, SleepSource
│   │   └── validation.py         # 9 canonical validation rules
│   ├── adapters/
│   │   ├── protocol.py           # SleepDataSource Protocol
│   │   ├── oura_mapper.py        # Inbound ACL: Oura → SleepDay
│   │   ├── withings_mapper.py    # Inbound ACL: Withings → SleepDay
│   │   ├── oura_fixture.py       # Fixture adapter
│   │   ├── withings_fixture.py   # Fixture adapter
│   │   └── fhir_serializer.py    # Outbound ACL: SleepDay → FHIR R4
│   ├── repository.py             # DB access (upsert, timeline query)
│   ├── api.py                    # FastAPI router
│   └── pipeline.py               # raw payload → map → validate → upsert/quarantine
├── shared/
│   ├── database.py               # Engine, session factory
│   ├── middleware.py              # Auth, request ID, error handling
│   └── exceptions.py             # RFC 9457 helpers
├── fixtures/                     # Static JSON test data
├── migrations/                   # Alembic
├── tests/
├── docs/adr/                     # ADR-001 through ADR-005
├── openapi.yaml                  # Contract-first API spec
└── pyproject.toml
```
