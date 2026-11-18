"""Prometheus metrics. Label values are bounded enums or route templates, never raw input."""

import logging
import threading

from prometheus_client import Counter, Gauge, Histogram, start_http_server

logger = logging.getLogger("relay.metrics")

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["route", "status"])
HTTP_DURATION = Histogram("http_request_duration_seconds", "HTTP request duration", ["route"])
EVENTS_ACCEPTED = Counter("events_accepted_total", "New events accepted through the API")
DELIVERY_ATTEMPTS = Counter("delivery_attempts_total", "Delivery attempts", ["outcome"])
DELIVERIES_TERMINAL = Counter("deliveries_terminal_total", "Deliveries finished", ["status"])
ATTEMPT_DURATION = Histogram("delivery_attempt_duration_seconds", "Delivery attempt duration")
WORKER_INFLIGHT = Gauge("worker_inflight", "Delivery attempts currently in flight")
DEMO_RUNS = Counter("demo_runs_created_total", "Demo runs created", ["scenario"])
QUOTA_REJECTIONS = Counter("quota_rejections_total", "Sandbox quota rejections", ["reason"])

_started = threading.Lock()
_ports: set[int] = set()


def serve(port):
    """Start the internal metrics listener once per process; port 0 disables it."""
    if not port:
        return
    with _started:
        if port in _ports:
            return
        try:
            start_http_server(port)
        except OSError:
            logger.warning("metrics_listener_unavailable", extra={"port": port})
            return
        _ports.add(port)
