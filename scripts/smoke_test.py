#!/usr/bin/env python3
"""Smoke test for a running Sleep Harmonizer instance.

Works against any target: Docker Compose, K8s port-forward, deployed environment.
Uses httpx (project dependency) for HTTP calls.

Usage:
    python scripts/smoke_test.py                                   # default localhost:8000
    python scripts/smoke_test.py --base-url http://10.0.0.5:8000   # custom target
    python scripts/smoke_test.py --wait 120 --verbose              # longer wait, verbose
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid

import httpx

# ── Payloads ─────────────────────────────────────────────────────

def _oura_payload(run_id: str) -> dict:
    return {
        "data": [
            {
                "id": f"smoke-oura-{run_id}",
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


def _withings_payload(run_id: str) -> dict:
    # Use run_id-derived epoch offsets so fingerprint is unique per run.
    # Base epochs: 1710468000 (2024-03-15 02:00 UTC), 1710496800 (2024-03-15 10:00 UTC)
    offset = int(run_id, 16) % 100_000
    start_epoch = 1710468000 + offset
    end_epoch = 1710496800 + offset
    return {
        "status": 0,
        "body": {
            "series": [
                {
                    "startdate": start_epoch,
                    "enddate": end_epoch,
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


def _quarantine_oura_payload(run_id: str) -> dict:
    return {
        "data": [
            {
                "id": f"smoke-quarantine-{run_id}",
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


# ── Test infrastructure ─────────────────────────────────────────


class TestResult:
    def __init__(self, name: str, passed: bool, detail: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail


class SmokeRunner:
    def __init__(self, base_url: str, verbose: bool = False):
        self.client = httpx.Client(base_url=base_url, timeout=30.0)
        self.verbose = verbose
        self.results: list[TestResult] = []
        self.run_id = uuid.uuid4().hex[:8]
        self.patient_id = str(uuid.uuid4())
        self.quarantine_patient_id = str(uuid.uuid4())
        self.oura_key = f"smoke-{self.run_id}-oura"
        self.withings_key = f"smoke-{self.run_id}-withings"
        self.quarantine_key = f"smoke-{self.run_id}-quarantine"
        self.oura_payload = _oura_payload(self.run_id)
        self.withings_payload = _withings_payload(self.run_id)
        self.quarantine_payload = _quarantine_oura_payload(self.run_id)
        self.all_responses: list[httpx.Response] = []
        self.oura_ingest_data: dict | None = None

    def _record(self, name: str, passed: bool, detail: str = "") -> TestResult:
        result = TestResult(name, passed, detail)
        self.results.append(result)
        status = "PASS" if passed else "FAIL"
        line = f" [{status}] {name}"
        if detail and (not passed or self.verbose):
            line += f"  ({detail})"
        print(line)
        return result

    def _check(
        self,
        name: str,
        resp: httpx.Response,
        expected_status: int,
        checks: dict[str, object] | None = None,
    ) -> TestResult:
        self.all_responses.append(resp)
        if resp.status_code != expected_status:
            return self._record(
                name,
                False,
                f"expected {expected_status}, got {resp.status_code}: {resp.text[:300]}",
            )
        if checks:
            try:
                body = resp.json()
                for path, expected in checks.items():
                    value = body
                    for key in path.split("."):
                        value = value[key]
                    assert value == expected, f"{path}: expected {expected!r}, got {value!r}"
            except (KeyError, AssertionError, Exception) as e:
                return self._record(name, False, str(e))
        return self._record(name, True)

    # ── Individual checks ────────────────────────────────────────

    def check_health(self) -> None:
        resp = self.client.get("/health")
        self._check("Health check", resp, 200, {"status": "ok"})

    def check_metrics_baseline(self) -> None:
        resp = self.client.get("/metrics/")
        self.all_responses.append(resp)
        ok = resp.status_code == 200
        has_ingestion = "ingestion_records_total" in resp.text
        has_pipeline = "pipeline_duration_seconds" in resp.text
        self._record(
            "Metrics endpoint available",
            ok and has_ingestion and has_pipeline,
            "" if ok else f"status={resp.status_code}",
        )

    def check_swagger(self) -> None:
        resp = self.client.get("/docs")
        self.all_responses.append(resp)
        self._record("Swagger UI accessible", resp.status_code == 200)

    def check_ingest_oura(self) -> None:
        resp = self.client.post(
            "/api/v1/ingest/oura/sleep",
            json={"patient_id": self.patient_id, "data": self.oura_payload},
            headers={"Idempotency-Key": self.oura_key},
        )
        self.all_responses.append(resp)
        ok = resp.status_code == 201
        detail = ""
        if ok:
            body = resp.json()
            data = body.get("data", {})
            ok = data.get("status") == "created" and data.get("sleep_day_id") is not None
            self.oura_ingest_data = data
            detail = f"201, sleep_day_id={data.get('sleep_day_id', '?')}"
        else:
            detail = f"expected 201, got {resp.status_code}: {resp.text[:200]}"
        self._record("Ingest Oura sleep", ok, detail)

    def check_ingest_withings(self) -> None:
        resp = self.client.post(
            "/api/v1/ingest/withings/sleep",
            json={"patient_id": self.patient_id, "data": self.withings_payload},
            headers={"Idempotency-Key": self.withings_key},
        )
        self.all_responses.append(resp)
        ok = resp.status_code == 201
        detail = ""
        if ok:
            data = resp.json().get("data", {})
            ok = data.get("status") == "created" and data.get("sleep_day_id") is not None
            detail = f"201, sleep_day_id={data.get('sleep_day_id', '?')}"
        else:
            detail = f"expected 201, got {resp.status_code}: {resp.text[:200]}"
        self._record("Ingest Withings sleep", ok, detail)

    def check_idempotency_replay(self) -> None:
        resp = self.client.post(
            "/api/v1/ingest/oura/sleep",
            json={"patient_id": self.patient_id, "data": self.oura_payload},
            headers={"Idempotency-Key": self.oura_key},
        )
        self.all_responses.append(resp)
        # Replay returns the cached status code (201 when original had inserts)
        ok = resp.status_code == 201
        detail = ""
        if ok and self.oura_ingest_data:
            cached = resp.json().get("data", {})
            ok = cached == self.oura_ingest_data
            if not ok:
                detail = "cached response does not match original"
        elif not ok:
            detail = f"expected 201 (cached), got {resp.status_code}"
        self._record("Idempotency replay", ok, detail)

    def check_timeline_read_after_write(self) -> None:
        resp = self.client.get(f"/api/v1/patients/{self.patient_id}/sleep/timeline")
        self.all_responses.append(resp)
        ok = resp.status_code == 200
        detail = ""
        if ok:
            body = resp.json()
            data = body.get("data", [])
            ok = len(data) >= 2
            if ok:
                record = data[0]
                required = ["effective_date", "total_sleep_minutes", "stages",
                            "sleep_onset", "sleep_offset"]
                missing = [f for f in required if f not in record]
                ok = len(missing) == 0
                if not ok:
                    detail = f"missing fields: {missing}"
                else:
                    meta_ok = "request_id" in body.get("meta", {})
                    pagination_ok = "pagination" in body
                    api_version_ok = body.get("meta", {}).get("api_version") == "v1"
                    ok = meta_ok and pagination_ok and api_version_ok
                    detail = f"{len(data)} records"
            else:
                detail = f"expected >= 2 records, got {len(data)}"
        self._record("Timeline read-after-write", ok, detail)

    def check_timeline_date_filter(self) -> None:
        resp = self.client.get(
            f"/api/v1/patients/{self.patient_id}/sleep/timeline",
            params={"start": "2024-03-14", "end": "2024-03-15"},
        )
        self.all_responses.append(resp)
        ok = resp.status_code == 200
        detail = ""
        if ok:
            data = resp.json().get("data", [])
            dates = [r["effective_date"] for r in data]
            ok = all(d >= "2024-03-14" and d < "2024-03-15" for d in dates)
            detail = f"dates={dates}"
        self._record("Timeline date filter", ok, detail)

    def check_summary(self) -> None:
        resp = self.client.get(
            f"/api/v1/patients/{self.patient_id}/sleep/summary",
            params={"start": "2024-03-01", "end": "2024-03-31"},
        )
        self.all_responses.append(resp)
        ok = resp.status_code == 200
        detail = ""
        if ok:
            data = resp.json().get("data", {})
            count = data.get("record_count", 0)
            avg = data.get("avg_total_sleep_minutes")
            sources = data.get("sources", [])
            ok = count >= 2 and avg is not None and avg > 0 and len(sources) > 0
            detail = f"record_count={count}"
        self._record("Summary aggregation", ok, detail)

    def check_provenance(self) -> None:
        resp = self.client.get(f"/api/v1/patients/{self.patient_id}/sleep/provenance")
        self.all_responses.append(resp)
        ok = resp.status_code == 200
        detail = ""
        if ok:
            data = resp.json().get("data", [])
            ok = len(data) >= 2
            if ok:
                required = ["source", "source_record_id", "fingerprint", "ingested_at"]
                for r in data:
                    missing = [f for f in required if f not in r]
                    if missing:
                        ok = False
                        detail = f"missing fields: {missing}"
                        break
            detail = detail or f"{len(data)} records"
        self._record("Provenance tracking", ok, detail)

    def check_error_missing_idem_key(self) -> None:
        resp = self.client.post(
            "/api/v1/ingest/oura/sleep",
            json={"patient_id": self.patient_id, "data": {}},
        )
        self.all_responses.append(resp)
        ok = resp.status_code == 400
        if ok:
            body = resp.json()
            rfc_fields = {"type", "title", "status", "detail", "instance"}
            ok = rfc_fields <= body.keys()
            ct = resp.headers.get("content-type", "")
            ok = ok and "application/problem+json" in ct
        self._record("Error: missing idempotency key", ok, f"{resp.status_code}")

    def check_error_unsupported_source(self) -> None:
        resp = self.client.post(
            "/api/v1/ingest/fitbit/sleep",
            json={"patient_id": self.patient_id, "data": {}},
            headers={"Idempotency-Key": f"smoke-{self.run_id}-fitbit"},
        )
        self.all_responses.append(resp)
        ok = resp.status_code == 422
        if ok:
            body = resp.json()
            ok = "fitbit" in body.get("detail", "")
            ok = ok and "application/problem+json" in resp.headers.get("content-type", "")
        self._record("Error: unsupported source", ok, f"{resp.status_code}")

    def check_error_invalid_date_range(self) -> None:
        resp = self.client.get(
            f"/api/v1/patients/{self.patient_id}/sleep/summary",
            params={"start": "2024-03-15", "end": "2024-03-01"},
        )
        self.all_responses.append(resp)
        ok = resp.status_code == 400
        if ok:
            body = resp.json()
            rfc_fields = {"type", "title", "status", "detail", "instance"}
            ok = rfc_fields <= body.keys()
        self._record("Error: invalid date range", ok, f"{resp.status_code}")

    def check_quarantine_excluded(self) -> None:
        # Ingest with future date — should be quarantined
        resp = self.client.post(
            "/api/v1/ingest/oura/sleep",
            json={
                "patient_id": self.quarantine_patient_id,
                "data": self.quarantine_payload,
            },
            headers={"Idempotency-Key": self.quarantine_key},
        )
        self.all_responses.append(resp)
        ok = resp.status_code == 200  # no inserts → 200
        detail = ""
        if ok:
            data = resp.json().get("data", {})
            ok = data.get("status") == "quarantined" and data.get("sleep_day_id") is None
            if not ok:
                detail = f"data={data}"
        else:
            detail = f"expected 200, got {resp.status_code}"

        # Timeline for quarantine patient should be empty
        if ok:
            tl = self.client.get(
                f"/api/v1/patients/{self.quarantine_patient_id}/sleep/timeline"
            )
            self.all_responses.append(tl)
            tl_data = tl.json().get("data", [])
            ok = len(tl_data) == 0
            detail = f"{len(tl_data)} timeline records"

        self._record("Quarantine: bad data excluded", ok, detail)

    def check_metrics_increment(self) -> None:
        resp = self.client.get("/metrics/")
        self.all_responses.append(resp)
        ok = resp.status_code == 200
        detail = ""
        if ok:
            text = resp.text
            # Check that created counter has non-zero value
            has_created = 'status="created"' in text
            has_pipeline = "pipeline_duration_seconds_count" in text
            ok = has_created and has_pipeline
            if not ok:
                detail = f"created={has_created}, pipeline={has_pipeline}"
        self._record("Metrics increment after ingest", ok, detail)

    def check_request_id_header(self) -> None:
        missing = []
        for resp in self.all_responses:
            if "X-Request-ID" not in resp.headers:
                missing.append(f"{resp.request.method} {resp.request.url.path}")
        ok = len(missing) == 0
        detail = ""
        if not ok:
            detail = f"missing on: {missing[:3]}"
        self._record("X-Request-ID present on all responses", ok, detail)

    def run_all(self) -> int:
        self.check_health()
        self.check_metrics_baseline()
        self.check_swagger()
        self.check_ingest_oura()
        self.check_ingest_withings()
        self.check_idempotency_replay()
        self.check_timeline_read_after_write()
        self.check_timeline_date_filter()
        self.check_summary()
        self.check_provenance()
        self.check_error_missing_idem_key()
        self.check_error_unsupported_source()
        self.check_error_invalid_date_range()
        self.check_quarantine_excluded()
        self.check_metrics_increment()
        self.check_request_id_header()

        passed = sum(1 for r in self.results if r.passed)
        failed = len(self.results) - passed
        print(f"\n{passed}/{len(self.results)} passed")
        return failed


# ── Entry point ──────────────────────────────────────────────────


def wait_for_health(client: httpx.Client, timeout: int) -> None:
    """Poll /health until it returns 200 or timeout expires."""
    start = time.monotonic()
    print("Waiting for /health...", end=" ", flush=True)
    while time.monotonic() - start < timeout:
        try:
            resp = client.get("/health")
            if resp.status_code == 200:
                elapsed = time.monotonic() - start
                print(f"OK ({elapsed:.1f}s)")
                return
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
            pass
        time.sleep(2)
    elapsed = time.monotonic() - start
    print(f"TIMEOUT ({elapsed:.0f}s)")
    print("ERROR: app did not become healthy in time")
    sys.exit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke test for Sleep Harmonizer API"
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Target URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=60,
        help="Max seconds to wait for /health (default: 60)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print response details on success (default: only on failure)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print("Sleep Harmonizer Smoke Test")
    print(f"Target: {args.base_url}")
    print()

    client = httpx.Client(base_url=args.base_url, timeout=30.0)
    wait_for_health(client, timeout=args.wait)
    client.close()

    print()
    runner = SmokeRunner(args.base_url, verbose=args.verbose)
    failed = runner.run_all()
    sys.exit(failed)


if __name__ == "__main__":
    main()
