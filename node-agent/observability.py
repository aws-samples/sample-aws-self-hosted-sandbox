"""Node-agent metrics, structured logs, and request correlation."""
from __future__ import annotations

import contextvars
import json
import os
import sys
import time
import uuid
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
    "HTTP requests handled by node-agent.",
    ("route", "method", "status_class"),
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "Node-agent HTTP request duration.",
    ("route", "method"),
)
FC_OPERATION_DURATION = Histogram(
    "fc_operation_duration_seconds",
    "Firecracker operation duration.",
    ("operation", "result", "snapshot_type"),
)
FC_RESUME_STAGE_DURATION = Histogram(
    "fc_resume_stage_duration_seconds",
    "Measured Firecracker resume stages.",
    ("stage", "result"),
)
FC_RESUME_INFLIGHT = Gauge(
    "fc_resume_inflight",
    "Resume operations currently executing on this node-agent.",
)
FC_RESTORE_MODE = Counter(
    "fc_restore_mode_total",
    "Successful restores by snapshot source.",
    ("mode",),
)
FC_SNAPSHOT_TRANSFER_DURATION = Histogram(
    "fc_snapshot_transfer_duration_seconds",
    "Snapshot transfer duration.",
    ("direction", "result"),
)
FC_SNAPSHOT_VERIFY = Counter(
    "fc_snapshot_verify_total",
    "Snapshot integrity verification attempts.",
    ("result",),
)
FC_SNAPSHOT_VERIFY_DURATION = Histogram(
    "fc_snapshot_verify_duration_seconds",
    "Snapshot integrity verification duration.",
    ("result",),
)
FC_SNAPSHOT_ERRORS = Counter(
    "fc_snapshot_errors_total",
    "Snapshot failures by phase.",
    ("phase",),
)
FC_SNAPSHOT_LEGACY_MIGRATIONS = Counter(
    "fc_snapshot_legacy_migrations_total",
    "Legacy snapshots migrated to an integrity manifest.",
    ("result",),
)
FC_VMS = Gauge(
    "fc_vms",
    "Firecracker VMs managed by this node.",
    ("node", "state"),
)
FCNODE_FREE_MEMORY = Gauge(
    "fcnode_free_memory_bytes",
    "Node memory currently available.",
    ("node",),
)
FCNODE_SCRATCH_FREE = Gauge(
    "fcnode_scratch_free_bytes",
    "Bytes available on the sandbox state filesystem.",
    ("node",),
)
FCNODE_SCRATCH = Gauge(
    "fcnode_scratch_bytes",
    "Sandbox state filesystem bytes by capacity kind.",
    ("node", "kind"),
)
NODE_HEARTBEAT_ERRORS = Counter(
    "node_heartbeat_errors_total",
    "Failed node heartbeat writes.",
    ("node",),
)


def _initialize_snapshot_metric_series() -> None:
    # Alerts use increase(); pre-create bounded label sets so the first failure
    # has a zero sample to compare against.
    for mode in ("local", "s3", "unknown"):
        FC_RESTORE_MODE.labels(mode).inc(0)
    for direction in ("upload", "download"):
        for result in ("success", "error"):
            FC_SNAPSHOT_TRANSFER_DURATION.labels(direction, result)
    for result in ("success", "error"):
        FC_SNAPSHOT_VERIFY.labels(result).inc(0)
        FC_SNAPSHOT_VERIFY_DURATION.labels(result)
    for phase in ("upload", "download", "verify"):
        FC_SNAPSHOT_ERRORS.labels(phase).inc(0)
    for result in ("success", "error"):
        FC_SNAPSHOT_LEGACY_MIGRATIONS.labels(result).inc(0)


_initialize_snapshot_metric_series()

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)


def _configure_tracing() -> trace.Tracer:
    provider = TracerProvider(
        resource=Resource.create({
            "service.name": os.environ.get("OTEL_SERVICE_NAME", "sandbox-node-agent")
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
    if parts[0] == "vm":
        if len(parts) == 2 and parts[1] not in {
            "create", "destroy", "snapshot_base", "suspend", "resume", "exec"
        }:
            return "/vm/{id}"
        return "/" + "/".join(parts[:2])
    if parts[0] == "proxy":
        return "/proxy/{id}/{port}/{path}"
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


def record_fc_operation(
    operation: str, result: str, duration: float, snapshot_type: str = ""
) -> None:
    FC_OPERATION_DURATION.labels(operation, result, snapshot_type or "none").observe(duration)


def record_resume_stage(stage: str, result: str, duration: float) -> None:
    FC_RESUME_STAGE_DURATION.labels(stage, result).observe(duration)


def record_restore_mode(mode: str) -> None:
    FC_RESTORE_MODE.labels(mode if mode in {"local", "s3"} else "unknown").inc()


def record_snapshot_transfer(direction: str, result: str, duration: float) -> None:
    FC_SNAPSHOT_TRANSFER_DURATION.labels(direction, result).observe(duration)


def record_snapshot_verify(result: str, duration: float) -> None:
    FC_SNAPSHOT_VERIFY.labels(result).inc()
    FC_SNAPSHOT_VERIFY_DURATION.labels(result).observe(duration)


def record_snapshot_error(phase: str) -> None:
    FC_SNAPSHOT_ERRORS.labels(phase).inc()


def record_snapshot_legacy_migration(result: str) -> None:
    FC_SNAPSHOT_LEGACY_MIGRATIONS.labels(result).inc()


def refresh_node_metrics(
    node: str,
    vm_states: dict[str, int],
    free_memory_bytes: int,
    scratch_free_bytes: int,
    scratch_total_bytes: int,
) -> None:
    FC_VMS.clear()
    for state, count in vm_states.items():
        FC_VMS.labels(node, state).set(count)
    FCNODE_FREE_MEMORY.labels(node).set(free_memory_bytes)
    FCNODE_SCRATCH_FREE.labels(node).set(scratch_free_bytes)
    FCNODE_SCRATCH.labels(node, "free").set(scratch_free_bytes)
    FCNODE_SCRATCH.labels(node, "used").set(
        max(0, scratch_total_bytes - scratch_free_bytes)
    )
    FCNODE_SCRATCH.labels(node, "total").set(scratch_total_bytes)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
