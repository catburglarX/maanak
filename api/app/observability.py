"""Structured logging, request correlation and metrics.

Logs are JSON lines so they can be shipped without a parser. A request id is
generated (or taken from an inbound ``X-Request-ID``) and bound to a context
variable, so every log line produced while handling a request carries it, and the
same id is returned to the client and stored on audit events.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog
from prometheus_client import Counter, Gauge, Histogram

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
actor_id_var: ContextVar[str | None] = ContextVar("actor_id", default=None)

#: Header names whose values must never be logged.
SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "set-cookie", "x-api-key"})
#: Keys scrubbed from any log event.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "new_password",
        "current_password",
        "password_hash",
        "token",
        "access_token",
        "refresh_token",
        "token_hash",
        "secret",
        "jwt_secret",
        "s3_secret_key",
        "mfa_secret",
        "authorization",
        "cookie",
    }
)


def _scrub(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = "[redacted]"
    return event_dict


def _bind_context(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    request_id = request_id_var.get()
    if request_id and "request_id" not in event_dict:
        event_dict["request_id"] = request_id
    actor_id = actor_id_var.get()
    if actor_id and "actor_id" not in event_dict:
        event_dict["actor_id"] = actor_id
    return event_dict


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Install the logging configuration. Safe to call more than once."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    # uvicorn's access log duplicates the request log emitted by the middleware.
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = False

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _bind_context,
        _scrub,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def new_request_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def request_context(request_id: str, actor_id: str | None = None) -> Iterator[None]:
    request_token = request_id_var.set(request_id)
    actor_token = actor_id_var.set(actor_id)
    try:
        yield
    finally:
        request_id_var.reset(request_token)
        actor_id_var.reset(actor_token)


def current_request_id() -> str | None:
    return request_id_var.get()


def set_actor(actor_id: str | None) -> None:
    actor_id_var.set(actor_id)


def safe_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: ("[redacted]" if key.lower() in SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
HTTP_REQUESTS = Counter(
    "maanak_http_requests_total",
    "HTTP requests handled",
    labelnames=("method", "route", "status"),
)
HTTP_LATENCY = Histogram(
    "maanak_http_request_seconds",
    "HTTP request duration",
    labelnames=("method", "route"),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
JOBS_ENQUEUED = Counter(
    "maanak_jobs_enqueued_total", "Analysis jobs enqueued", labelnames=("job_type",)
)
JOBS_COMPLETED = Counter(
    "maanak_jobs_completed_total",
    "Analysis jobs finished",
    labelnames=("job_type", "outcome"),
)
JOB_DURATION = Histogram(
    "maanak_job_seconds",
    "Analysis job duration",
    labelnames=("job_type",),
    buckets=(0.5, 1, 2, 5, 10, 20, 40, 80, 160),
)
OCR_DURATION = Histogram(
    "maanak_ocr_seconds",
    "OCR duration per evidence file",
    buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 32),
)
QUEUE_DEPTH = Gauge("maanak_queue_depth", "Jobs waiting in the queue")
STORAGE_OPERATIONS = Counter(
    "maanak_storage_operations_total",
    "Object storage operations",
    labelnames=("operation", "outcome"),
)
REPORTS_ISSUED = Counter("maanak_reports_issued_total", "Reports issued", labelnames=("format",))
LOGIN_ATTEMPTS = Counter("maanak_login_attempts_total", "Sign-in attempts", labelnames=("outcome",))
