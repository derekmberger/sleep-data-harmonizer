# Sleep Data Harmonizer — Implementation Context

> **Purpose:** Orients contributors and implementation tools to the project design.

---

## 1. Project Overview

**What:** Ingests sleep data from Oura Ring and Withings, normalizes it into a canonical `SleepDay` model, and exposes product APIs.

**Tech stack:** Python 3.12+, FastAPI, PostgreSQL, SQLAlchemy 2.0, Pydantic v2, structlog, Prometheus

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

### Nullable Strategy

- **NOT NULL:** identity (`id`, `patient_id`), provenance (`source`, `source_record_id`, `raw_payload`, `fingerprint`), temporal (`effective_date`, `ingested_at`, `updated_at`), extension (`extra`, defaults to `{}`)
- **NULLABLE:** all sleep metrics and times. `NULL` = "vendor did not provide", never "zero"

### Tables

| Table | Role | Mutability |
|-------|------|------------|
| `sleep_days` | Canonical normalized data (silver layer) | Upserted via fingerprint |
| `raw_vendor_responses` | Exact vendor API responses (bronze layer) | Append-only, immutable |
| `quarantine_records` | Failed records with raw payload + error details | Append; resolved flag toggled |
| `idempotency_keys` | Transport-layer dedup for HTTP POST | TTL-based, 24h expiry |

---

## 4. Key Decisions (ADR Summary)

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

---

## 6. API Surface

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/ingest/{source}/sleep` | POST | Ingest vendor data (requires `Idempotency-Key` header) |
| `/api/v1/patients/{id}/sleep/timeline` | GET | Paginated sleep records (cursor pagination, date range filter) |
| `/api/v1/patients/{id}/sleep/summary` | GET | Aggregated metrics for a period |
| `/api/v1/patients/{id}/sleep/provenance` | GET | Data lineage and source tracing |
| `/health` | GET | Health check |
| `/metrics` | GET | Prometheus metrics |

- **Pagination:** Cursor-based, encodes `(effective_date, id)` as opaque base64 token
- **Errors:** RFC 9457 Problem Details with `violations` extension for field-level errors
- **Envelope:** `{ data, meta: { request_id, timestamp, api_version }, pagination }`

---

## 7. FHIR R4 Output

The FHIR serializer converts `SleepDay` → FHIR R4 Observation with LOINC-coded components. It's an **outbound ACL** — FHIR is an output format, not storage.

**LOINC codes used:** 93832-4 (total sleep), 93831-6 (deep), 93830-8 (light), 93829-0 (REM). Sleep efficiency has no LOINC — uses text-only code. Each Observation includes `meta.lastUpdated`, `issued`, and `identifier` elements for versioning and cross-system correlation.

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

---

## 9. Cross-Cutting Conventions

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
