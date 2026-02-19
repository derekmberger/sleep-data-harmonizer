# Architecture Decision Records

This directory contains the Architecture Decision Records (ADRs) for the Sleep Data Harmonizer. Each ADR captures a single architectural decision, its context, considered alternatives, and consequences.

**Format:** Hybrid Nygard/MADR — Nygard's conciseness with MADR's explicit alternatives and structured consequences, plus a "What We Defer" section for scope boundaries.

| ADR | Decision | Status |
|-----|----------|--------|
| 001 | Intersection-first canonical model + JSONB extras | Accepted |
| 002 | Natural-key fingerprint for idempotency | Accepted |
| 003 | Separate quarantine table, store-and-flag | Accepted |
| 004 | Protocol interface with runtime adapter swap | Accepted |
| 005 | Cursor pagination + RFC 9457 errors | Accepted |

---

## ADR-001: Canonical Model Design — Intersection-First with JSONB Extras

**Status:** Accepted

Problem:
The Sleep Data Harmonizer must unify sleep data from multiple wearable vendors (initially Oura and Withings), each of which uses different field names, units, and schemas. This creates a challenge: how do we represent this diverse data as a single internal model without sacrificing either data fidelity or schema stability?

**Context:**

The Sleep Data Harmonizer ingests sleep data from multiple wearable vendors (initially Oura and Withings) that use different field names, units, and schemas. We need a single internal representation: a canonical `SleepDay` model that downstream APIs can query without knowing the source vendor. The core tension is between data loss and schema stability. Capturing every vendor-specific field requires either a wide, sparse schema or a flexible overflow mechanism. Keeping the schema tight risks discarding potentially valuable data.

**Decision Drivers:**

- API consumers need a stable, predictable response shape that does not change when a new vendor is added.
- We must not silently discard vendor data — healthcare data provenance requires preservability.
- New vendors should be addable with one new mapper, not a schema migration.
- Query performance matters for the common case (filtering and aggregating on canonical fields like sleep duration, efficiency, and date).

**Options Considered:**

1. **Union model** — Every field from every vendor gets a typed column. `oura_movement_30_sec`, `withings_snoring`, etc. The canonical table is the superset of all vendor fields.
   - *Pro:* Every field is queryable with native SQL types and indexes.
   - *Con:* Table grows wider with every vendor. Most columns are NULL for most rows. Schema migration is required for every new vendor. Column names embed vendor semantics in the canonical layer.

2. **Per-source tables with a view** — Each vendor keeps its own table (`oura_sleep`, `withings_sleep`). A SQL view or materialized view unions them into a common shape.
   - *Pro:* Each table is compact and vendor-specific. No NULL sprawl.
   - *Con:* The unioning logic is the hard part, and it lives in SQL (harder to test, harder to version). Adding computed fields (e.g., normalized efficiency) requires updating the view. Product APIs must query the view, inserting a layer of indirection and potential performance issues.

3. Intersection-first with JSONB extras (chosen): To maximize extensibility without migrations, we use typed columns for fields present in all sources (total sleep, deep/light/REM/awake minutes, efficiency, onset/offset, effective date). A single extra JSONB column stores vendor-specific overflow.
   - *Pro:* Stable schema — adding a new vendor requires only a new mapper, no migration. Common-case queries use typed, indexed columns. No data loss — vendor-specific fields live in `extra`. JSONB is queryable (`->`, `->>`, `@>`) when needed.
   - *Con:* Vendor-specific fields in `extra` lack type enforcement and are not directly indexable (require expression indexes). Reporting on `extra` fields requires JSON path queries, which are less ergonomic.

**Decision:**

We will use an intersection-first canonical model with a JSONB `extra` column. The `SleepDay` table has typed columns for the nine fields both Oura and Withings provide (total sleep, stage durations, efficiency, onset/offset, effective date) plus provenance columns (source, source_record_id, fingerprint, raw_payload, ingested_at, updated_at). All other vendor-specific fields are stored in `extra`.

**Consequences:**

- *Good:* Adding the third vendor (e.g., Apple Health, WHOOP) requires writing one mapper function and zero schema migrations.
- *Good:* Product API response shape is fixed. Consumers never see a new nullable field appear because a new vendor was added.
- *Good:* `raw_payload` preserves the complete vendor response for audit and reprocessing. No data is ever lost.
- *Bad:* If a field in `extra` becomes commonly queried (e.g., Oura's `readiness_score` becomes a product feature), we must promote it to a typed column via migration. This is intentional friction — it forces a conscious decision about what belongs in the canonical schema.
- *Bad:* JSONB `extra` has no schema enforcement. A mapper bug could cause malformed JSON to be written to the DB without detection at the DB layer.

**What We Defer:**

- **JSONB schema validation:** We do not enforce a JSON Schema on the `extra` column at the database level. This can be added as a CHECK constraint or application-layer validation if malformed `extra` data becomes a problem. Revisit when a third vendor is added.
- **Field promotion workflow:** We do not have a formal process for promoting `extra` fields to typed columns. For now, this is a manual migration decision. Revisit when any `extra` field is requested in more than two product API queries.
- **Materialized views for analytics:** We do not pre-aggregate data. Raw canonical records are sufficient for the MVP query patterns. Revisit when query latency on timeline endpoints exceeds 200ms at the projected data volume.

---

## ADR-002: Idempotency Strategy — Natural-Key Fingerprint

**Status:** Accepted

**Context:**

Wearable data ingestion is inherently retry-prone. Vendors retry failed webhook deliveries. Our polling jobs may re-fetch overlapping date ranges. Network timeouts cause clients to re-submit. Without idempotency, each retry creates a duplicate `SleepDay` record, corrupting timeline queries and aggregations. For example, if a user's sleep for April 1 is ingested twice, their monthly average sleep is artificially inflated, and charts or averages seen by both users and clinicians no longer reflect reality. We need a mechanism that guarantees processing the same vendor record twice produces exactly one canonical row.

**Decision Drivers:**

- Idempotency must work regardless of ingestion path (webhook push, polling pull, manual re-import).
- The mechanism should be deterministic: the same input always produces the same dedup key, without requiring the caller to supply a key.
- We must handle the case where a vendor updates a record (same source_record_id, different payload) — this is an update, not a duplicate.
- The solution must be enforceable at the database level (not just application logic) as a safety net.

**Options Considered:**

1. **Idempotency-Key header (client-supplied)** — The caller provides a unique key in the HTTP header. The server stores and checks it.
   - *Pro:* Standard pattern (Stripe, AWS). Gives the caller control.
   - *Con:* Only works for HTTP ingestion, not for polling jobs or batch imports. Requires the caller to generate and manage keys. If a caller retries without the same key, dedup fails silently. Adds a separate idempotency-key storage table.

2. **Full-payload hash** — `sha256(json.dumps(entire_payload, sort_keys=True))`. Any difference in payload produces a different hash.
   - *Pro:* Detects even trivial payload differences (e.g., a vendor adds a new field).
   - *Con:* Legitimate updates (vendor corrects a value) produce a different hash, so they are treated as new records instead of updates. Payload serialization order is fragile — different JSON serializers may produce different hashes for the same data. Vendor API responses often include volatile fields (timestamps, request IDs) that change on every call.

3. **Natural-key fingerprint (chosen)** — `sha256(source + ":" + source_record_id + ":" + effective_date)`. The fingerprint is derived from the record's identity, not its content.
   - *Pro:* Deterministic from the record's natural key. The same vendor record on the same date always produces the same fingerprint. Supports upsert: if the fingerprint matches, we update the existing row (picking up corrected values from the vendor). Works for any ingestion path.
   - *Con:* Does not detect same-key records with different payloads at the dedup layer. This is managed by the upsert (overwrite) strategy, meaning only the most recent payload is retained and prior values are lost in the canonical table. If a vendor reuses `source_record_id` among different record types (unlikely but possible), collisions could occur. Importantly, this approach sacrifices granular history in favor of simplicity: we do not retain a complete audit trail of every change, only the latest state per natural key. If audit requirements ever mandate full version history or the need to reconstruct prior states (for example, compliance with stricter regulatory standards or detailed forensic review), a dedicated version-history table recording all changes would be required. For now, upsert/last-write-wins is justifiable, but should audit or compliance needs become more stringent, we would need to transition to a history table with full record versioning.

**Decision:**

Two-layer idempotency:

1. **Data layer (primary):** Compute a fingerprint as `sha256(source + ":" + source_record_id + ":" + effective_date)` and store it in a `fingerprint` column with a UNIQUE constraint. On ingestion, perform INSERT ... ON CONFLICT (fingerprint) DO UPDATE. This guarantees exactly-once semantics for new records and last-write-wins for updates, regardless of ingestion path.

2. **Transport layer (supplementary):** Require an `Idempotency-Key` header on HTTP POST ingest endpoints. This provides an additional safeguard at the API boundary — if a client retries the same HTTP request, the server can short-circuit before re-running the pipeline. This layer is additive; the fingerprint remains the authoritative dedup mechanism.

Option 1 was rejected as the *sole* mechanism (it doesn't cover polling/batch paths), but it adds value as a transport-level optimization atop the fingerprint.

**Consequences:**

- *Good:* Retry-safe by default. Any ingestion path (HTTP, cron, manual) benefits from the fingerprint. Zero caller burden for non-HTTP paths.
- *Good:* The UNIQUE constraint on `fingerprint` provides a database-level safety net even if application code has a bug.
- *Good:* Upsert behavior means vendor corrections (same record, updated values) are automatically applied.
- *Good:* HTTP Idempotency-Key prevents redundant pipeline execution on client retries (performance optimization, not correctness dependency).
- *Bad:* We cannot distinguish "identical re-delivery" from "vendor updated the record" at the fingerprint level. Both trigger the same upsert. If we needed to track vendor corrections, we would need an event log or a history table.
- *Bad:* The fingerprint assumes `source_record_id` is stable and unique per vendor. If a vendor changes their ID scheme, we would need a migration or a new fingerprint algorithm.

**What We Defer:**

- **Correction history:** We do not track the history of upserts (what the previous values were before the vendor correction). Revisit when audit requirements demand a full change log rather than just the current state.
- **Cross-vendor dedup:** We do not detect if Oura and Withings report data for the same night. The fingerprint is per-source. Cross-source reconciliation is a separate, much harder problem. Revisit when product requirements demand a single "best" record per night.
- **Fingerprint versioning:** If we change the fingerprint algorithm (e.g., add `patient_id` to the key), existing records would not match. We do not have a migration strategy for this. Revisit before adding multi-patient support.

---

## ADR-003: Quarantine Policy — Separate Table, Store-and-Flag

**Status:** Accepted

**Context:**

Vendor payloads can fail validation for many reasons: missing required fields, values outside expected ranges (negative sleep duration, efficiency > 1.0), unparseable date formats, and unknown schema versions. We need a policy for handling records that fail validation. The core tension is between data preservation (never lose data) and data quality (never serve bad data to consumers).

**Decision Drivers:**

- Healthcare data auditability: We must be able to explain what happened to every record we received, including the failures.
- Bad records must never appear in product API responses.
- Failed records must be inspectable for debugging (what did the vendor send? what validation rule failed?).
- Recovery must be possible: if a validation bug in our code incorrectly rejects a good record, we need to reprocess it without re-fetching from the vendor.

**Options Considered:**

1. **Reject and log** — Failed records are logged to application logs and discarded. No persistence.
   - *Pro:* Simplest implementation. No storage overhead for bad data.
   - *Con:* Recovery is impossible without re-fetching from the vendor (which may be rate-limited, paginated, or time-windowed). Logs are ephemeral and hard to query. No structured metadata about *why* the record failed.

2. **Inline status column** — Bad records go into the same `sleep_days` table with a `status` column (`valid`, `quarantined`). Product APIs filter `WHERE status = 'valid'.
   - *Pro:* Single table, simpler schema. Easy to "promote" a quarantined record by flipping the status.
   - *Con:* Every product query must include the `WHERE status = 'valid'` filter. If any query forgets this filter, bad data leaks into API responses. The `sleep_days` table mixes concerns (canonical data and error storage). Index performance is affected by rows that will rarely be read.

3. **Dead letter queue (DLQ)** — Failed records go to a message queue (SQS, RabbitMQ) for async reprocessing.
   - *Pro:* Standard pattern for async pipelines. Built-in retry semantics.
   - *Con:* Adds infrastructure dependency (message broker). DLQ messages expire. Not queryable for debugging without tooling. Overengineered for a synchronous ingest API.

4. **Separate quarantine table (chosen)** — Failed records go to a `quarantine_records` table with the raw payload, error details, and metadata.
   - *Pro:* Clean separation — product queries never touch quarantine data, so no forgotten-filter risk. Quarantine table can have its own schema optimized for debugging (error_message, error_field, failed_at, retry_count). Reprocessing reads from quarantine, re-validates, and inserts into `sleep_days` if valid.
   - *Con:* Two tables to manage. Reprocessing logic must be built separately. Cannot trivially join quarantine records with canonical records (different table).

**Decision:**

We will use a separate `quarantine_records` table. When a vendor payload fails validation, we store the full raw payload, the source, validation error details (which field, which rule, and the actual value), and a timestamp. Quarantined records are never visible to product APIs. A separate reprocessing flow can re-validate quarantined records (for example, after a validation bug fix) and promote them to `sleep_days`.

**Consequences:**

- *Good:* Zero risk of bad data leaking into product APIs. The separation is structural, not a filter that someone might forget to use.
- *Good:* Full debuggability: the quarantine table stores the raw payload, the specific error, and the timestamp. An on-call engineer can query `SELECT * FROM quarantine_records WHERE source = 'oura' AND failed_at > '2025-03-15'` to see what went wrong.
- *Good:* Reprocessing is possible without vendor re-fetch. If our validation code had a bug, we would fix the code and re-run the quarantined records.
- *Bad:* Two tables means two sets of queries, two migration files, two sets of indexes. Schema maintenance cost is higher.
- *Bad:* The reprocessing flow is not trivial — it must re-validate, compute the fingerprint, handle the upsert, and remove from quarantine atomically. This is a separate piece of work.

**What We Defer:**

- **Automatic retry/reprocessing:** We do not build an automatic retry loop for quarantined records. Reprocessing is manual (triggered via an admin endpoint or a script). Revisit when quarantine volume is high enough to warrant automation.
- **Quarantine retention policy:** We do not auto-purge old quarantine records. Revisit when the storage cost of quarantine data becomes non-trivial (unlikely in the near term, given expected volume).
- **Alerting on quarantine rate:** We do not alert when the quarantine rate exceeds a threshold (e.g., >5% of ingested records). Revisit when monitoring infrastructure is in place.

---

## ADR-004: Fixture + Live Adapter Mode — Protocol Interface with Runtime Swap

**Status:** Accepted

**Context:**

The Sleep Data Harmonizer fetches data from external vendor APIs (Oura, Withings). During development and testing, we cannot or should not call live vendor APIs. They are rate-limited, require OAuth credentials, return non-deterministic data, and are slow. We need a way to swap between live mode (real API calls) and fixture mode (deterministic test data) without changing application code.

**Decision Drivers:**

- Tests must be deterministic: the same input produces the same output, every run.
- The fixture/live boundary needs be explicit in the code — not hidden in environment variables that silently change behavior.
- Adding a new vendor adapter has to follow the same pattern whether it is live or fixture.
- Fixture mode must be usable in local development, CI, and integration tests.
- The swap mechanism must not leak into business logic. The ingest pipeline should not know or care whether data came from a live API or a fixture.

**Options Considered:**

1. **Environment-based mocking (monkeypatch / mock.patch)** — Use `unittest.mock.patch` or pytest monkeypatch to replace API client methods in tests.
   - *Pro:* Zero production code changes. Standard pytest pattern.
   - *Con:* Mock setup is scattered throughout test files. Mocks can drift from real API behavior (the mock says the response has field X, but the real API renamed it). No way to run the full application in fixture mode for manual testing or demos. Mock-heavy tests are brittle and tightly coupled to implementation details.

2. **Test-only fixture files with conditional imports** — `if os.environ["MODE"] == "test": from fixtures import data else: from api_client import data`.
   - *Pro:* Simple to understand.
   - *Con:* Conditional imports based on environment variables are a code smell. The "test" and "live" paths differ structurally, so bugs in one may not manifest in the other. Cannot run a demo server with fixture data without deploying in "test" mode.

3. Protocol interface with runtime swap (chosen): Define a SleepDataSource Protocol (Python typing.Protocol) with a fetch_sleep_data(patient_id, start_date, end_date) -> list[RawSleepRecord] method. Implement OuraLiveSource, WithingsLiveSource, OuraFixtureSource, and WithingsFixtureSource. Dependency injection at application startup selects the implementation. Adapter swap is performed at application startup, and the DI wiring latency is expected to be negligible (typically well under 100ms, even as the number of vendors grows), which is acceptable for both API services and CLI tools. If the number of adapters increases significantly or initialization logic becomes non-trivial, we will revisit these performance boundaries to ensure startup time remains acceptable for all workflows.
   - *Pro:* The interface contract is explicit and type-checkable. Live and fixture adapters implement the same Protocol, so they are structurally interchangeable. Business logic depends only on the Protocol, never on a concrete adapter. Fixture mode works for tests, local dev, and demos. Adding a new vendor means implementing one Protocol for live and one for fixture.
   - *Con:* More code upfront (Protocol definition, two implementations per vendor). Slightly more complex wiring at application startup (dependency injection or factory function). Over-engineering risk if we only ever have two vendors.

**Decision:**

We will define a `SleepDataSource` Protocol and implement live and fixture adapters for each vendor. The application startup (FastAPI's lifespan or dependency injection) selects the adapter based on configuration. Business logic (the ingest pipeline, validation, and canonical mapping) receives a `SleepDataSource` and is agnostic to the implementation.

**Consequences:**

- *Good:* Tests are deterministic, fast, and do not require network access or vendor credentials.
- *Good:* Fixture adapters serve as living documentation of the vendor API response shape. If a test fixture does not match the Protocol, mypy catches it.
- *Good:* A new developer can run the full application locally with `ADAPTER_MODE=fixture` and see realistic data without configuring OAuth tokens.
- *Good:* Demo mode is free — the same fixture adapters power both tests and product demos.
- *Bad:* Two implementations per vendor means fixture adapters must be kept in sync with real API response shapes. If the vendor changes their API and we update the live adapter but not the fixture, tests pass, but production breaks.
- *Bad:* The Protocol abstraction adds indirection. A developer debugging an ingest failure must trace through the DI wiring to find which adapter is active.

**What We Defer:**

- **Record/replay mode:** We do not build a mechanism to capture live API responses and replay them as fixtures. This would keep fixtures automatically in sync with real API changes. Revisit when vendor API changes cause fixture drift.
- **Contract testing:** We do not run the fixture adapters against a schema derived from the live API. Revisit when we have more than two vendors and fixture drift becomes a maintenance burden.
- **Per-test fixture customization:** Fixture adapters return a fixed dataset. We do not support parameterized fixtures (e.g., "give me a record with missing REM data"). Revisit when test scenarios require more variety.

---

## ADR-005: API Query Contract — Date-Range Filtering, Cursor Pagination, RFC 9457 Errors

**Status:** Accepted

**Context:**

The Sleep Data Harmonizer exposes a `GET /patients/{id}/sleep/timeline` endpoint that returns canonical sleep records. Product consumers (frontend dashboards, analytics services, mobile apps) need to query sleep data by date range, paginate results, and handle errors in a structured way. The API contract determines how consumers integrate, how we handle large result sets, and how we communicate failures.

**Decision Drivers:**

- Sleep data is inherently time-series: consumers almost always filter by date range ("show me the last 30 days").
- Result sets can be large (years of nightly data). Pagination is required.
- Pagination must be stable: adding a new record should not cause a consumer to skip or duplicate records during pagination.
- Error responses must be machine-parseable and carry sufficient information for the caller to take action (which field was invalid, what the constraint was).
- The API should follow established standards where they exist, rather than inventing custom conventions.

**Options Considered — Pagination:**

1. **Offset pagination** — `?offset=20&limit=10`. Skip the first 20 rows, return 10.
   - *Pro:* Simple to implement (`OFFSET` / `LIMIT` in SQL). Consumers can jump to any page.
   - *Con:* Unstable under concurrent writes: if a record is inserted at the beginning, all subsequent offsets shift by one, causing consumers to see duplicates or skip records. Performance degrades on large offsets (`OFFSET 10000` requires scanning and discarding 10000 rows).

2. **Page-number pagination** — `?page=3&per_page=10`.
   - *Pro:* Human-readable. Easy for frontend "page 1, 2, 3" UI.
   - *Con:* Same instability and performance problems as offset pagination (it is offset pagination with different syntax).

3. **Cursor pagination (chosen)** — `?cursor=<opaque_token>&limit=10`. The cursor encodes the position of the last returned record (e.g., base64-encoded `effective_date` + `id`).
   - *Pro:* Stable under concurrent writes: the cursor points to a specific record, not a numeric position. Efficient: the query uses `WHERE (effective_date, id) > (cursor_date, cursor_id) ORDER BY effective_date, id LIMIT 10`, which uses an index seek, not a scan. Standard pattern (used by Stripe, GitHub, Slack APIs).
   - *Con:* Cannot jump to an arbitrary page (no "go to page 5"). The cursor is opaque — consumers cannot construct or manipulate it. Slightly more complex server implementation (cursor encoding/decoding).

**Options Considered — Error Format:**

1. **Custom error JSON** — `{"error": "Invalid date range", "code": "INVALID_DATE_RANGE"}`.
   - *Pro:* Full control over shape.
   - *Con:* Every API invents its own error format. Consumers must learn a new convention for every service. No standard for field-level validation errors.

2. RFC 9457 Problem Details (chosen) — {"type": "...", "title": "...", "status": 422, "detail": "...", "instance": "..."} with Content-Type: application/problem+json. Our FastAPI endpoints will leverage the openapi-schema-pydantic and fastapi-problems libraries to automatically generate and format Problem Details error responses following RFC 9457. This integrates cleanly with our existing API schemas and is compatible with common OpenAPI code generators and client SDKs (including openapi-generator and Swagger Codegen), ensuring downstream consumers can natively parse and utilize errors without custom parsing logic.
   - *Pro:* Internet standard (RFC 9457, published July 2023, supersedes RFC 7807). Machine-parseable, extensible (add custom fields like `violations: [{field, message, constraint}]`). Consumers who integrate with any RFC 9457 API can reuse their error-handling code.
   - *Con:* Slightly more verbose than a minimal custom format. Less familiar to developers who have not encountered it (though this is decreasing).

**Decision:**

We will use cursor pagination on all list endpoints, with the cursor encoding `(effective_date, id)` as a base64-encoded opaque token. Date-range filtering is via `?start=YYYY-MM-DD&end=YYYY-MM-DD` query parameters (both optional; omitting both returns all records). Error responses follow RFC 9457 Problem Details with a `violations` extension array for field-level validation failures.

**Consequences:**

- *Good:* Cursor pagination is stable and performant at any scale. A consumer paginating through 3 years of data will never skip or duplicate a record due to concurrent ingestion.
- *Good:* RFC 9457 errors give consumers a predictable structure for every error. The `violations` extension array (`[{field: "start_date", message: "must be before end_date", constraint: "date_range"}]`) enables field-level UI error display.
- *Good:* Date-range parameters are optional — consumers can request a specific window or omit both to get the full history. Pagination keeps result sets manageable regardless.
- *Bad:* Cursor pagination does not support "jump to page N" — consumers who need random access pagination (rare for time-series data) are not well served.
- *Bad:* The cursor encoding is an implementation detail that we must not break. If we change the cursor format (e.g., by adding a field), old cursors will not be decoded. We need a versioning or graceful-fallback strategy.

**What We Defer:**

- **Cursor versioning:** We do not version the cursor encoding. If we change it, old cursors will fail. Revisit before any change to the sort order or cursor fields.
- **Rate limiting:** We do not apply rate limiting on the API. Revisit before exposing the API to external consumers or untrusted clients.
- **Bulk export endpoint:** For analytics use cases that need all data (not paginated), we do not provide a bulk CSV/Parquet export. Revisit when analytics consumers request it.
- **GraphQL or field selection:** We return the full `SleepDay` object on every request. We do not support `?fields=total_sleep_minutes,efficiency` sparse fieldsets. Revisit if the response payload size becomes a concern for mobile clients.
