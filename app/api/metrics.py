"""Prometheus-style metrics endpoint."""
import time
from typing import Callable

from fastapi import APIRouter, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from app.processing import get_circuit_breaker, get_processing_queue, get_rate_limiter

router = APIRouter()

# Request metrics
REQUEST_LATENCY = Histogram(
    "request_latency_seconds",
    "Request latency in seconds",
    ["method", "endpoint", "status"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

REQUEST_COUNT = Counter(
    "request_count_total",
    "Total request count",
    ["method", "endpoint", "status"],
)

# Grok API metrics
GROK_REQUESTS = Counter(
    "grok_requests_total",
    "Total Grok API requests",
    ["status"],  # success, error, rate_limited
)

GROK_TOKENS = Counter(
    "grok_tokens_total",
    "Total tokens consumed",
    ["type"],  # input, output
)

GROK_COST = Counter(
    "grok_cost_dollars_total",
    "Estimated total cost in dollars",
)

GROK_LATENCY = Histogram(
    "grok_latency_seconds",
    "Grok API call latency",
    buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

# Queue metrics
QUEUE_DEPTH = Gauge(
    "queue_depth",
    "Current processing queue depth",
)

BACKPRESSURE_EVENTS = Counter(
    "backpressure_events_total",
    "Total backpressure events (queue full rejections)",
)

# Circuit breaker metrics
CIRCUIT_BREAKER_STATE = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=open, 2=half_open)",
)

# Rate limiter metrics
RATE_LIMITER_CURRENT_RATE = Gauge(
    "rate_limiter_current_rate",
    "Current rate limiter rate (requests/second)",
)

# Cache metrics
CACHE_HITS = Counter(
    "cache_hits_total",
    "Total cache hits",
)

CACHE_MISSES = Counter(
    "cache_misses_total",
    "Total cache misses",
)

# Analysis metrics
ANALYSIS_FILTERED = Counter(
    "analysis_filtered_total",
    "Conversations filtered out by pre-filter",
    ["reason"],
)

ANALYSIS_COMPLETED = Counter(
    "analysis_completed_total",
    "Conversations successfully analyzed",
)


def update_processing_metrics() -> None:
    """Update gauges with current processing state."""
    # Queue metrics
    queue = get_processing_queue()
    QUEUE_DEPTH.set(queue.depth)

    # Circuit breaker state
    circuit = get_circuit_breaker()
    state_map = {"closed": 0, "open": 1, "half_open": 2}
    CIRCUIT_BREAKER_STATE.set(state_map.get(circuit.state.value, 0))

    # Rate limiter
    rate_limiter = get_rate_limiter()
    RATE_LIMITER_CURRENT_RATE.set(rate_limiter.current_rate)


@router.get("/metrics")
async def metrics() -> Response:
    """
    Prometheus-compatible metrics endpoint.

    Exposes:
    - request_latency_seconds (histogram: p50/p95/p99)
    - grok_requests_total (counter: success/error labels)
    - grok_tokens_total (counter)
    - grok_cost_dollars_total (counter)
    - queue_depth (gauge)
    - backpressure_events_total (counter)
    - circuit_breaker_state (gauge: 0=closed, 1=open, 2=half_open)
    - cache_hits_total, cache_misses_total (counters)
    """
    # Update dynamic metrics
    update_processing_metrics()

    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


def create_metrics_middleware() -> Callable:
    """Create middleware for tracking request metrics."""

    async def metrics_middleware(request: Request, call_next: Callable) -> Response:
        """Track request latency and count."""
        start_time = time.monotonic()

        response = await call_next(request)

        # Calculate latency
        latency = time.monotonic() - start_time

        # Extract endpoint (simplified)
        endpoint = request.url.path
        method = request.method
        status = str(response.status_code)

        # Record metrics
        REQUEST_LATENCY.labels(
            method=method, endpoint=endpoint, status=status
        ).observe(latency)
        REQUEST_COUNT.labels(
            method=method, endpoint=endpoint, status=status
        ).inc()

        return response

    return metrics_middleware


# Helper functions for recording metrics from services


def record_grok_success(tokens_in: int, tokens_out: int, cost: float, latency: float) -> None:
    """Record successful Grok API call metrics."""
    GROK_REQUESTS.labels(status="success").inc()
    GROK_TOKENS.labels(type="input").inc(tokens_in)
    GROK_TOKENS.labels(type="output").inc(tokens_out)
    GROK_COST.inc(cost)
    GROK_LATENCY.observe(latency)


def record_grok_error(error_type: str = "error") -> None:
    """Record Grok API error."""
    GROK_REQUESTS.labels(status=error_type).inc()


def record_cache_hit() -> None:
    """Record cache hit."""
    CACHE_HITS.inc()


def record_cache_miss() -> None:
    """Record cache miss."""
    CACHE_MISSES.inc()


def record_analysis_filtered(reason: str) -> None:
    """Record conversation filtered by pre-filter."""
    ANALYSIS_FILTERED.labels(reason=reason).inc()


def record_analysis_completed() -> None:
    """Record successful analysis."""
    ANALYSIS_COMPLETED.inc()


def record_backpressure_event() -> None:
    """Record backpressure event."""
    BACKPRESSURE_EVENTS.inc()
