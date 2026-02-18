"""API E2E integration tests: HTTP requests against real Postgres.

Tests the full stack: httpx → FastAPI middleware → route handler →
pipeline → repository → Postgres → response serialization.

Requires Docker to be running (testcontainers).
"""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

OURA_PAYLOAD = {
    "data": [
        {
            "id": "e2e-oura-001",
            "day": "2024-03-14",
            "type": "long_sleep",
            "period": 0,
            "total_sleep_duration": 28800,
            "deep_sleep_duration": 5400,
            "light_sleep_duration": 14400,
            "rem_sleep_duration": 7200,
            "awake_time": 1800,
            "efficiency": 88,
            "bedtime_start": "2024-03-14T23:00:00-05:00",
            "bedtime_end": "2024-03-15T07:00:00-05:00",
            "time_in_bed": 28800,
        }
    ]
}

WITHINGS_PAYLOAD = {
    "status": 0,
    "body": {
        "series": [
            {
                "startdate": 1710468000,
                "enddate": 1710496800,
                "date": "2024-03-15",
                "model": 32,
                "model_id": 93,
                "data": {
                    "wakeupduration": 1800,
                    "lightsleepduration": 12600,
                    "deepsleepduration": 5400,
                    "remsleepduration": 7200,
                    "total_sleep_time": 27000,
                    "sleep_efficiency": 0.85,
                    "total_timeinbed": 28800,
                    "wakeupcount": 3,
                    "durationtosleep": 480,
                    "durationtowakeup": 120,
                    "out_of_bed_count": 1,
                },
            }
        ]
    },
}

# Oura payload with a future date — passes model parsing but fails
# canonical validation rule 5 (no future dates).
QUARANTINE_OURA_PAYLOAD = {
    "data": [
        {
            "id": "e2e-quarantine-001",
            "day": "2099-01-01",
            "type": "long_sleep",
            "period": 0,
            "total_sleep_duration": 28800,
            "deep_sleep_duration": 5400,
            "light_sleep_duration": 14400,
            "rem_sleep_duration": 7200,
            "awake_time": 1800,
            "efficiency": 88,
            "bedtime_start": "2099-01-01T23:00:00-05:00",
            "bedtime_end": "2099-01-02T07:00:00-05:00",
            "time_in_bed": 28800,
        }
    ]
}


def _idem_key() -> str:
    return f"e2e-{uuid4()}"


# ── Ingest tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_oura_creates_record(api_client):
    pid = str(uuid4())
    resp = await api_client.post(
        "/api/v1/ingest/oura/sleep",
        json={"patient_id": pid, "data": OURA_PAYLOAD},
        headers={"Idempotency-Key": _idem_key()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["data"]["status"] == "created"
    assert body["data"]["sleep_day_id"] is not None
    assert body["meta"]["api_version"] == "v1"


@pytest.mark.asyncio
async def test_ingest_withings_creates_record(api_client):
    pid = str(uuid4())
    resp = await api_client.post(
        "/api/v1/ingest/withings/sleep",
        json={"patient_id": pid, "data": WITHINGS_PAYLOAD},
        headers={"Idempotency-Key": _idem_key()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["data"]["status"] == "created"
    assert body["data"]["sleep_day_id"] is not None


@pytest.mark.asyncio
async def test_ingest_idempotency_replay(api_client):
    pid = str(uuid4())
    key = _idem_key()
    payload = {"patient_id": pid, "data": OURA_PAYLOAD}

    resp1 = await api_client.post(
        "/api/v1/ingest/oura/sleep",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert resp1.status_code == 201

    resp2 = await api_client.post(
        "/api/v1/ingest/oura/sleep",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    # Replay returns the cached status code (201 because original had inserts)
    assert resp2.status_code == resp1.status_code
    assert resp2.json()["data"] == resp1.json()["data"]


@pytest.mark.asyncio
async def test_ingest_idempotency_conflict(api_client):
    pid = str(uuid4())
    key = _idem_key()

    resp1 = await api_client.post(
        "/api/v1/ingest/oura/sleep",
        json={"patient_id": pid, "data": OURA_PAYLOAD},
        headers={"Idempotency-Key": key},
    )
    assert resp1.status_code == 201

    resp2 = await api_client.post(
        "/api/v1/ingest/oura/sleep",
        json={"patient_id": str(uuid4()), "data": OURA_PAYLOAD},
        headers={"Idempotency-Key": key},
    )
    assert resp2.status_code == 409
    body = resp2.json()
    assert "idempotency-conflict" in body["type"]
    assert resp2.headers["content-type"] == "application/problem+json"


# ── Quarantine ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_quarantine_future_date(api_client):
    pid = str(uuid4())
    resp = await api_client.post(
        "/api/v1/ingest/oura/sleep",
        json={"patient_id": pid, "data": QUARANTINE_OURA_PAYLOAD},
        headers={"Idempotency-Key": _idem_key()},
    )
    assert resp.status_code == 200  # no inserts → 200
    body = resp.json()
    assert body["data"]["status"] == "quarantined"
    assert body["data"]["sleep_day_id"] is None

    timeline = await api_client.get(f"/api/v1/patients/{pid}/sleep/timeline")
    assert timeline.status_code == 200
    assert len(timeline.json()["data"]) == 0


# ── Bronze-layer persistence ────────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_raw_response_stored(api_client, pg_url):
    pid = str(uuid4())
    resp = await api_client.post(
        "/api/v1/ingest/oura/sleep",
        json={"patient_id": pid, "data": OURA_PAYLOAD},
        headers={"Idempotency-Key": _idem_key()},
    )
    assert resp.status_code == 201

    engine = create_async_engine(pg_url, echo=False)
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT source, patient_id::text, response_body "
                "FROM raw_vendor_responses WHERE patient_id = :pid"
            ),
            {"pid": pid},
        )
        rows = result.all()
    await engine.dispose()

    assert len(rows) == 1, f"expected 1 raw row, got {len(rows)}"
    assert rows[0][0] == "oura"
    assert rows[0][1] == pid
    assert rows[0][2]["data"] == OURA_PAYLOAD["data"]


# ── Read endpoints ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeline_read_after_write(api_client):
    pid = str(uuid4())
    await api_client.post(
        "/api/v1/ingest/oura/sleep",
        json={"patient_id": pid, "data": OURA_PAYLOAD},
        headers={"Idempotency-Key": _idem_key()},
    )

    resp = await api_client.get(f"/api/v1/patients/{pid}/sleep/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1

    record = body["data"][0]
    assert record["effective_date"] == "2024-03-14"
    assert record["total_sleep_minutes"] == 480
    assert record["sleep_efficiency"] == 0.88
    assert "deep_sleep_minutes" in record["stages"]
    assert "light_sleep_minutes" in record["stages"]
    assert "rem_sleep_minutes" in record["stages"]
    assert "awake_minutes" in record["stages"]
    assert record["sleep_onset"] is not None
    assert record["sleep_offset"] is not None

    assert "request_id" in body["meta"]
    assert body["meta"]["api_version"] == "v1"
    assert "pagination" in body


@pytest.mark.asyncio
async def test_timeline_date_range_filter(api_client):
    pid = str(uuid4())

    # Oura record on 2024-03-14
    await api_client.post(
        "/api/v1/ingest/oura/sleep",
        json={"patient_id": pid, "data": OURA_PAYLOAD},
        headers={"Idempotency-Key": _idem_key()},
    )
    # Withings record on 2024-03-15
    await api_client.post(
        "/api/v1/ingest/withings/sleep",
        json={"patient_id": pid, "data": WITHINGS_PAYLOAD},
        headers={"Idempotency-Key": _idem_key()},
    )

    # Filter to only 2024-03-14
    resp = await api_client.get(
        f"/api/v1/patients/{pid}/sleep/timeline",
        params={"start": "2024-03-14", "end": "2024-03-15"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["effective_date"] == "2024-03-14"


@pytest.mark.asyncio
async def test_timeline_cursor_pagination(api_client):
    pid = str(uuid4())

    # Insert 3 records on different dates using Oura payloads with distinct IDs.
    # bedtime_end must be next day (07:00 > 23:00 same-day triggers rule 9).
    days = [
        ("2024-03-12", "2024-03-13"),
        ("2024-03-13", "2024-03-14"),
        ("2024-03-14", "2024-03-15"),
    ]
    for i, (day, next_day) in enumerate(days):
        payload = {
            "data": [
                {
                    "id": f"e2e-page-{i}",
                    "day": day,
                    "type": "long_sleep",
                    "period": 0,
                    "total_sleep_duration": 28800,
                    "deep_sleep_duration": 5400,
                    "light_sleep_duration": 14400,
                    "rem_sleep_duration": 7200,
                    "awake_time": 1800,
                    "efficiency": 88,
                    "bedtime_start": f"{day}T23:00:00-05:00",
                    "bedtime_end": f"{next_day}T07:00:00-05:00",
                    "time_in_bed": 28800,
                }
            ]
        }
        resp = await api_client.post(
            "/api/v1/ingest/oura/sleep",
            json={"patient_id": pid, "data": payload},
            headers={"Idempotency-Key": _idem_key()},
        )
        assert resp.status_code == 201

    # Page 1: limit=1
    resp1 = await api_client.get(
        f"/api/v1/patients/{pid}/sleep/timeline", params={"limit": 1}
    )
    body1 = resp1.json()
    assert len(body1["data"]) == 1
    assert body1["pagination"]["has_more"] is True
    cursor1 = body1["pagination"]["next_cursor"]
    assert cursor1 is not None

    # Page 2
    resp2 = await api_client.get(
        f"/api/v1/patients/{pid}/sleep/timeline",
        params={"limit": 1, "cursor": cursor1},
    )
    body2 = resp2.json()
    assert len(body2["data"]) == 1
    assert body2["pagination"]["has_more"] is True
    cursor2 = body2["pagination"]["next_cursor"]

    # Page 3 (last)
    resp3 = await api_client.get(
        f"/api/v1/patients/{pid}/sleep/timeline",
        params={"limit": 1, "cursor": cursor2},
    )
    body3 = resp3.json()
    assert len(body3["data"]) == 1
    assert body3["pagination"]["has_more"] is False
    assert body3["pagination"]["next_cursor"] is None

    # All three dates should be distinct
    dates = [
        body1["data"][0]["effective_date"],
        body2["data"][0]["effective_date"],
        body3["data"][0]["effective_date"],
    ]
    assert len(set(dates)) == 3


@pytest.mark.asyncio
async def test_summary_aggregation(api_client):
    pid = str(uuid4())

    await api_client.post(
        "/api/v1/ingest/oura/sleep",
        json={"patient_id": pid, "data": OURA_PAYLOAD},
        headers={"Idempotency-Key": _idem_key()},
    )
    await api_client.post(
        "/api/v1/ingest/withings/sleep",
        json={"patient_id": pid, "data": WITHINGS_PAYLOAD},
        headers={"Idempotency-Key": _idem_key()},
    )

    resp = await api_client.get(
        f"/api/v1/patients/{pid}/sleep/summary",
        params={"start": "2024-03-01", "end": "2024-03-31"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["record_count"] == 2
    assert data["avg_total_sleep_minutes"] > 0
    assert len(data["sources"]) == 2
    assert set(data["sources"]) == {"oura", "withings"}


@pytest.mark.asyncio
async def test_provenance_read_after_write(api_client):
    pid = str(uuid4())
    await api_client.post(
        "/api/v1/ingest/oura/sleep",
        json={"patient_id": pid, "data": OURA_PAYLOAD},
        headers={"Idempotency-Key": _idem_key()},
    )

    resp = await api_client.get(f"/api/v1/patients/{pid}/sleep/provenance")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1

    record = body["data"][0]
    assert record["source"] == "oura"
    assert record["source_record_id"] == "e2e-oura-001"
    assert record["fingerprint"] is not None
    assert record["ingested_at"] is not None
    assert record["updated_at"] is not None


# ── Error contracts ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_error_contracts_rfc9457(api_client):
    pid = str(uuid4())
    rfc9457_fields = {"type", "title", "status", "detail", "instance"}

    # Missing idempotency key → 400
    resp = await api_client.post(
        "/api/v1/ingest/oura/sleep",
        json={"patient_id": pid, "data": {}},
    )
    assert resp.status_code == 400
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert rfc9457_fields <= body.keys()

    # Unsupported source → 422
    resp = await api_client.post(
        "/api/v1/ingest/fitbit/sleep",
        json={"patient_id": pid, "data": {}},
        headers={"Idempotency-Key": _idem_key()},
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert rfc9457_fields <= body.keys()
    assert "fitbit" in body["detail"]

    # Invalid date range → 400
    resp = await api_client.get(
        f"/api/v1/patients/{pid}/sleep/summary",
        params={"start": "2024-03-15", "end": "2024-03-01"},
    )
    assert resp.status_code == 400
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert rfc9457_fields <= body.keys()

    # Invalid sort → 400
    resp = await api_client.get(
        f"/api/v1/patients/{pid}/sleep/timeline",
        params={"sort": "created_at"},
    )
    assert resp.status_code == 400
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert rfc9457_fields <= body.keys()
    assert "created_at" in body["detail"]
