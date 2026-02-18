"""Prometheus metrics for pipeline observability.

Counters and histograms at each pipeline stage.
Exposed via /metrics endpoint.
"""

from prometheus_client import Counter, Histogram, make_asgi_app

# Pipeline counters
ingestion_records_total = Counter(
    "ingestion_records_total",
    "Total records processed by the ingestion pipeline",
    ["source", "status"],  # status: created, deduplicated, quarantined
)

validation_failures_total = Counter(
    "validation_failures_total",
    "Total validation failures by rule",
    ["source", "rule"],
)

# API counters
api_requests_total = Counter(
    "api_requests_total",
    "Total API requests",
    ["endpoint", "method", "status_code"],
)

# Histograms
vendor_api_duration_seconds = Histogram(
    "vendor_api_duration_seconds",
    "Duration of vendor API calls",
    ["source"],
)

pipeline_duration_seconds = Histogram(
    "pipeline_duration_seconds",
    "Duration of the full ingestion pipeline",
    ["source"],
)

api_response_duration_seconds = Histogram(
    "api_response_duration_seconds",
    "Duration of API responses",
    ["endpoint"],
)


def create_metrics_app():
    """Create ASGI app for /metrics endpoint."""
    return make_asgi_app()
