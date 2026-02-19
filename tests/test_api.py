"""API endpoint tests using FastAPI TestClient.

These tests use dependency overrides to provide a mock database session,
testing the API layer in isolation from the actual database.
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from main import app
from shared.database import get_session
from tests.conftest import PATIENT_ID


@pytest.fixture
def client():
    """FastAPI test client with mocked DB session."""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    async def override_session():
        yield mock_session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as c:
        yield c, mock_session
    app.dependency_overrides.clear()


class TestHealthEndpoint:
    def test_health(self):
        with TestClient(app) as c:
            resp = c.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}


class TestIngestEndpoint:
    def test_missing_idempotency_key_returns_400(self, client):
        c, _ = client
        resp = c.post(
            "/api/v1/ingest/oura/sleep",
            json={"patient_id": str(PATIENT_ID), "data": {}},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["title"] == "Missing Idempotency Key"
        assert resp.headers["content-type"] == "application/problem+json"

    def test_unsupported_source_returns_422(self, client):
        c, _ = client
        resp = c.post(
            "/api/v1/ingest/fitbit/sleep",
            json={"patient_id": str(PATIENT_ID), "data": {}},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert "fitbit" in body["detail"]

    def test_missing_patient_id_returns_422(self, client):
        """Pydantic request model enforces patient_id is required."""
        c, _ = client
        resp = c.post(
            "/api/v1/ingest/oura/sleep",
            json={"data": {}},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert resp.status_code == 422
        body = resp.json()
        # RFC 9457 format from request_validation_handler
        assert body["title"] == "Validation Error"
        assert "violations" in body
        assert resp.headers["content-type"] == "application/problem+json"

    def test_invalid_patient_id_returns_422(self, client):
        """Pydantic validates patient_id as UUID."""
        c, _ = client
        resp = c.post(
            "/api/v1/ingest/oura/sleep",
            json={"patient_id": "not-a-uuid", "data": {}},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["title"] == "Validation Error"
        assert "violations" in body

    def test_missing_data_field_returns_422(self, client):
        """Pydantic request model enforces data field is required."""
        c, _ = client
        resp = c.post(
            "/api/v1/ingest/oura/sleep",
            json={"patient_id": str(PATIENT_ID)},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["title"] == "Validation Error"


class TestTimelineEndpoint:
    def test_invalid_date_range_returns_400(self, client):
        c, _ = client
        resp = c.get(
            f"/api/v1/patients/{PATIENT_ID}/sleep/timeline",
            params={"start": "2024-03-15", "end": "2024-03-01"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["title"] == "Invalid Date Range"

    def test_invalid_limit_returns_422(self, client):
        c, _ = client
        resp = c.get(
            f"/api/v1/patients/{PATIENT_ID}/sleep/timeline",
            params={"limit": 0},
        )
        assert resp.status_code == 422

    def test_invalid_sort_returns_400(self, client):
        c, _ = client
        resp = c.get(
            f"/api/v1/patients/{PATIENT_ID}/sleep/timeline",
            params={"sort": "created_at"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["title"] == "Invalid Sort Parameter"
        assert "created_at" in body["detail"]
        assert resp.headers["content-type"] == "application/problem+json"

    def test_valid_sort_values_accepted(self):
        """Both effective_date and -effective_date should not trigger sort error."""
        # Use raise_server_exceptions=False since mock DB can't execute queries
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        async def override_session():
            yield mock_session

        app.dependency_overrides[get_session] = override_session
        with TestClient(app, raise_server_exceptions=False) as c:
            for sort_val in ("effective_date", "-effective_date"):
                resp = c.get(
                    f"/api/v1/patients/{PATIENT_ID}/sleep/timeline",
                    params={"sort": sort_val},
                )
                # Should NOT be a sort validation error (may be 500 from mock DB)
                if resp.status_code == 400:
                    body = resp.json()
                    assert body.get("title") != "Invalid Sort Parameter"
        app.dependency_overrides.clear()


class TestSummaryEndpoint:
    def test_invalid_date_range_returns_400(self, client):
        c, _ = client
        resp = c.get(
            f"/api/v1/patients/{PATIENT_ID}/sleep/summary",
            params={"start": "2024-03-15", "end": "2024-03-01"},
        )
        assert resp.status_code == 400


class TestProvenanceEndpoint:
    def test_invalid_date_range_returns_400(self, client):
        c, _ = client
        resp = c.get(
            f"/api/v1/patients/{PATIENT_ID}/sleep/provenance",
            params={"start": "2024-03-15", "end": "2024-03-01"},
        )
        assert resp.status_code == 400


class TestRFC9457ErrorFormat:
    def test_error_has_required_fields(self, client):
        """All error responses include RFC 9457 required fields."""
        c, _ = client
        resp = c.post(
            "/api/v1/ingest/oura/sleep",
            json={"patient_id": str(PATIENT_ID)},
        )
        body = resp.json()
        assert "type" in body
        assert "title" in body
        assert "status" in body
        assert "detail" in body
        assert "instance" in body

    def test_pydantic_422_uses_problem_json_format(self, client):
        """FastAPI native validation errors are wrapped in RFC 9457 format."""
        c, _ = client
        resp = c.post(
            "/api/v1/ingest/oura/sleep",
            json={"not_valid": True},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert resp.status_code == 422
        body = resp.json()
        # Must have RFC 9457 structure, not FastAPI default {detail: [...]}
        assert body["type"] == "https://api.blue.health/problems/validation-error"
        assert body["title"] == "Validation Error"
        assert "violations" in body
        assert resp.headers["content-type"] == "application/problem+json"

    def test_request_id_in_response_header(self):
        """X-Request-ID header is set on all responses."""
        with TestClient(app) as c:
            resp = c.get("/health")
            assert "X-Request-ID" in resp.headers
