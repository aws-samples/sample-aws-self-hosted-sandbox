"""Low-cardinality metrics, structured logs, and background-loop watchdogs."""
from __future__ import annotations

import contextvars
import functools
import json
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from urllib.parse import urlparse

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled by the control plane.",
    ("route", "method", "status_class"),
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "Control-plane HTTP request duration.",
    ("route", "method"),
)
SANDBOX_OPERATIONS = Counter(
    "sandbox_operations_total",
    "Sandbox lifecycle operations.",
    ("operation", "result"),
)
SANDBOX_OPERATION_DURATION = Histogram(
    "sandbox_operation_duration_seconds",
    "Sandbox lifecycle operation duration.",
    ("operation", "result"),
)
RESUME_QUEUE_WAIT = Histogram(
    "resume_queue_wait_seconds",
    "Time spent waiting for the resume concurrency limiter.",
)
RESUME_INFLIGHT = Gauge(
    "resume_inflight",
    "Resume operations currently executing in the driver.",
)
WAKE_RPC_DURATION = Histogram(
    "wake_rpc_duration_seconds",
    "End-to-end time for a proxy request to wake a sleeping sandbox.",
    ("result",),
)
BACKGROUND_LOOP_RUNS = Counter(
    "background_loop_runs_total",
    "Background-loop iterations.",
    ("loop", "result"),
)
BACKGROUND_LOOP_LAST_SUCCESS = Gauge(
    "background_loop_last_success_unixtime",
    "Unix time of the last successful background-loop iteration.",
    ("loop",),
)
LEADER_STATUS = Gauge(
    "leader_status",
    "Whether this control-plane replica currently holds the leader lock.",
)
LEADER_TRANSITIONS = Counter(
    "leader_transitions_total",
    "Leader lock state transitions.",
    ("state",),
)
RECONCILE_ACTIONS = Counter(
    "reconcile_actions_total",
    "State corrections made by reconcile.",
    ("action", "reason"),
)

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)
_loop_lock = threading.Lock()
_loops: dict[str, dict[str, float]] = {}


def _configure_tracing() -> trace.Tracer:
    provider = TracerProvider(
        resource=Resource.create({
            "service.name": os.environ.get(
                "OTEL_SERVICE_NAME", "sandbox-control-plane"
            )
        })
    )
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return trace.get_tracer("sandbox-platform")


_tracer = _configure_tracing()


def new_request_id(value: str | None = None) -> str:
    request_id = (value or "").strip()[:128] or uuid.uuid4().hex
    _request_id.set(request_id)
    return request_id


def current_request_id() -> str:
    return _request_id.get()


def log_event(level: str, event: str, **fields) -> None:
    span_context = trace.get_current_span().get_span_context()
    record = {
        "ts": time.time(),
        "level": level,
        "event": event,
        "request_id": current_request_id() or None,
        "trace_id": (
            format(span_context.trace_id, "032x") if span_context.is_valid else None
        ),
        "span_id": (
            format(span_context.span_id, "016x") if span_context.is_valid else None
        ),
        **fields,
    }
    print(
        json.dumps(record, ensure_ascii=True, separators=(",", ":"), default=str),
        file=sys.stderr,
        flush=True,
    )


def normalize_route(path: str) -> str:
    parts = urlparse(path).path.strip("/").split("/")
    if not parts or parts == [""]:
        return "/"
    if parts[0] == "sandboxes":
        if len(parts) == 1:
            return "/sandboxes"
        suffix = f"/{parts[2]}" if len(parts) >= 3 else ""
        return f"/sandboxes/{{id}}{suffix}"
    if parts[0] == "s":
        return "/s/{id}/{port}/{path}"
    if parts[0] == "admin":
        return "/" + "/".join(parts[:2])
    return "/" + "/".join(parts)


def record_http(method: str, path: str, status: int, duration: float) -> str:
    route = normalize_route(path)
    HTTP_REQUESTS.labels(route, method, f"{status // 100}xx").inc()
    HTTP_REQUEST_DURATION.labels(route, method).observe(duration)
    return route


def start_server_span(headers, method: str, path: str):
    carrier = {key.lower(): value for key, value in headers.items()}
    parent = propagate.extract(carrier)
    route = normalize_route(path)
    span = _tracer.start_span(
        f"{method} {route}",
        context=parent,
        kind=SpanKind.SERVER,
        attributes={"http.request.method": method, "http.route": route},
    )
    token = otel_context.attach(trace.set_span_in_context(span, parent))
    return span, token


def finish_server_span(span, token, status: int) -> None:
    if span is None:
        return
    span.set_attribute("http.response.status_code", status)
    if status >= 500:
        span.set_status(Status(StatusCode.ERROR))
    span.end()
    otel_context.detach(token)


def inject_trace_headers(headers: dict[str, str]) -> None:
    propagate.inject(headers)


@contextmanager
def traced_client_request(method: str, target: str):
    with _tracer.start_as_current_span(
        f"{method} {target}",
        kind=SpanKind.CLIENT,
        attributes={
            "http.request.method": method,
            "server.address": target,
        },
    ):
        yield


def record_operation(operation: str, result: str, duration: float) -> None:
    SANDBOX_OPERATIONS.labels(operation, result).inc()
    SANDBOX_OPERATION_DURATION.labels(operation, result).observe(duration)


def observed_operation(operation: str):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            started = time.monotonic()
            result = "error"
            try:
                response = fn(*args, **kwargs)
                code = response[0] if isinstance(response, tuple) else 200
                result = "success" if code < 400 else ("rejected" if code < 500 else "error")
                return response
            finally:
                record_operation(operation, result, time.monotonic() - started)
        return wrapped
    return decorate


def register_loop(name: str, interval_s: float) -> None:
    with _loop_lock:
        _loops[name] = {
            "interval": max(float(interval_s), 1.0),
            "last_iteration": time.monotonic(),
        }


def record_loop(name: str, result: str) -> None:
    now = time.monotonic()
    with _loop_lock:
        state = _loops.setdefault(name, {"interval": 30.0})
        state["last_iteration"] = now
    BACKGROUND_LOOP_RUNS.labels(name, result).inc()
    if result == "success":
        BACKGROUND_LOOP_LAST_SUCCESS.labels(name).set(time.time())


def stale_loops() -> list[str]:
    now = time.monotonic()
    with _loop_lock:
        return sorted(
            name
            for name, state in _loops.items()
            if now - state["last_iteration"] > max(30.0, state["interval"] * 3)
        )


def set_leader(is_leader: bool, changed: bool) -> None:
    LEADER_STATUS.set(1 if is_leader else 0)
    if changed:
        LEADER_TRANSITIONS.labels("acquired" if is_leader else "lost").inc()


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
