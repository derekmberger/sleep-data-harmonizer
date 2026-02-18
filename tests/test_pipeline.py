"""Tests for the pipeline module (unit-level, no DB)."""

from sleep.pipeline import compute_request_hash


class TestRequestHash:
    def test_deterministic(self):
        body = {"patient_id": "abc", "data": {"total_sleep_duration": 26000}}
        h1 = compute_request_hash(body)
        h2 = compute_request_hash(body)
        assert h1 == h2

    def test_different_body_different_hash(self):
        h1 = compute_request_hash({"a": 1})
        h2 = compute_request_hash({"a": 2})
        assert h1 != h2

    def test_key_order_irrelevant(self):
        h1 = compute_request_hash({"a": 1, "b": 2})
        h2 = compute_request_hash({"b": 2, "a": 1})
        assert h1 == h2
