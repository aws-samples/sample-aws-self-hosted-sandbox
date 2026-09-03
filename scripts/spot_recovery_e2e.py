#!/usr/bin/env python3
"""Full-load Firecracker checkpoint and same-AZ EBS recovery test.

The script creates sandboxes in a selected capacity pool and optional hard
placement group, waits for their Full base snapshots, dirty-fills guest
memory, triggers the node-agent reclaim path, optionally terminates the
isolated Spot instance, verifies automatic EBS takeover and memory markers,
and removes the sandboxes.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import hashlib
import hmac
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_json(command: list[str]) -> Any:
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout or "{}")


class HttpClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        node_agent_auth_secret: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.node_agent_auth_secret = node_agent_auth_secret

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        timeout_s: float = 180,
    ) -> tuple[int, dict]:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.node_agent_auth_secret:
            timestamp = str(int(time.time()))
            nonce = uuid.uuid4().hex
            content_hash = hashlib.sha256(payload or b"").hexdigest()
            canonical = "\n".join((
                "v1",
                timestamp,
                nonce,
                method.upper(),
                path,
                content_hash,
            )).encode()
            signature = hmac.new(
                self.node_agent_auth_secret.encode(),
                canonical,
                hashlib.sha256,
            ).hexdigest()
            headers.update({
                "X-SBX-Auth-Version": "v1",
                "X-SBX-Timestamp": timestamp,
                "X-SBX-Nonce": nonce,
                "X-SBX-Content-SHA256": content_hash,
                "X-SBX-Signature": signature,
            })
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read()
                return response.status, json.loads(raw or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                parsed = {"error": raw.decode(errors="replace")}
            return exc.code, parsed

    def require(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        expected: tuple[int, ...] = (200,),
        timeout_s: float = 180,
    ) -> dict:
        code, result = self.request(method, path, body, timeout_s)
        if code not in expected:
            raise RuntimeError(
                f"{method} {path} returned {code}: {json.dumps(result)}"
            )
        return result


class PortForward:
    def __init__(
        self,
        namespace: str,
        resource: str,
        remote_port: int,
    ):
        self.namespace = namespace
        self.resource = resource
        self.remote_port = remote_port
        self.local_port = free_port()
        self.process: subprocess.Popen | None = None
        self.log = tempfile.NamedTemporaryFile(
            prefix="spot-recovery-port-forward-",
            suffix=".log",
            delete=False,
        )

    def __enter__(self) -> str:
        self.process = subprocess.Popen(
            [
                "kubectl",
                "-n",
                self.namespace,
                "port-forward",
                self.resource,
                f"{self.local_port}:{self.remote_port}",
            ],
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                with socket.create_connection(
                    ("127.0.0.1", self.local_port),
                    timeout=0.2,
                ):
                    return f"http://127.0.0.1:{self.local_port}"
            except OSError:
                time.sleep(0.2)
        self.close()
        log = Path(self.log.name).read_text(errors="replace")
        raise RuntimeError(
            f"kubectl port-forward failed for {self.resource}: {log}"
        )

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.log.close()

    def __exit__(self, *_args) -> None:
        self.close()


def parallel_map(
    values: list[Any],
    worker: Callable[[Any], Any],
    concurrency: int,
) -> list[Any]:
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(concurrency, len(values) or 1))
    ) as executor:
        futures = {executor.submit(worker, value): value for value in values}
        results = []
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
        return results


def wait_until(
    description: str,
    timeout_s: float,
    probe: Callable[[], Any],
    *,
    interval_s: float = 2,
) -> Any:
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(interval_s)
    raise TimeoutError(f"timed out waiting for {description}; last={last!r}")


def find_node_agent_pod(namespace: str, source_node: str) -> str:
    pods = run_json([
        "kubectl",
        "-n",
        namespace,
        "get",
        "pods",
        "-l",
        "app=node-agent",
        "-o",
        "json",
    ])
    for item in pods.get("items", []):
        status = item.get("status", {})
        spec = item.get("spec", {})
        if source_node in {
            status.get("hostIP"),
            status.get("podIP"),
            spec.get("nodeName"),
        }:
            return item["metadata"]["name"]
    raise RuntimeError(f"node-agent pod not found for source node {source_node}")


def read_host_writeback_kib(namespace: str, pod: str) -> dict[str, int]:
    result = subprocess.run(
        [
            "kubectl",
            "-n",
            namespace,
            "exec",
            pod,
            "--",
            "cat",
            "/proc/meminfo",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    values: dict[str, int] = {}
    for line in result.stdout.splitlines():
        key, separator, raw = line.partition(":")
        if not separator or key not in {
            "Dirty",
            "Writeback",
            "WritebackTmp",
        }:
            continue
        values[key] = int(raw.strip().split()[0])
    return {
        "dirty_kib": values.get("Dirty", 0),
        "writeback_kib": values.get("Writeback", 0),
        "writeback_tmp_kib": values.get("WritebackTmp", 0),
    }


def quiesce_host_writeback(
    namespace: str,
    pod: str,
    *,
    timeout_s: int,
    threshold_mib: int,
) -> dict[str, Any]:
    started = time.monotonic()
    before = read_host_writeback_kib(namespace, pod)
    sync_started = time.monotonic()
    subprocess.run(
        ["kubectl", "-n", namespace, "exec", pod, "--", "sync"],
        check=True,
    )
    sync_elapsed_s = time.monotonic() - sync_started
    threshold_kib = threshold_mib * 1024
    deadline = time.monotonic() + timeout_s
    after = read_host_writeback_kib(namespace, pod)
    while (
        after["dirty_kib"]
        + after["writeback_kib"]
        + after["writeback_tmp_kib"]
        > threshold_kib
    ):
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "host writeback did not quiesce before checkpoint: "
                f"{after}, threshold_mib={threshold_mib}"
            )
        time.sleep(2)
        after = read_host_writeback_kib(namespace, pod)
    return {
        "threshold_mib": threshold_mib,
        "before": before,
        "after": after,
        "sync_elapsed_s": round(sync_elapsed_s, 3),
        "elapsed_s": round(time.monotonic() - started, 3),
    }


def verify_isolated_instance(
    instance_id: str,
    *,
    region: str,
    test_id: str,
) -> dict:
    response = run_json([
        "aws",
        "ec2",
        "describe-instances",
        "--region",
        region,
        "--instance-ids",
        instance_id,
        "--output",
        "json",
    ])
    reservations = response.get("Reservations", [])
    instances = [
        instance
        for reservation in reservations
        for instance in reservation.get("Instances", [])
    ]
    if len(instances) != 1:
        raise RuntimeError(f"instance {instance_id} not found")
    instance = instances[0]
    tags = {
        tag["Key"]: tag["Value"]
        for tag in instance.get("Tags", [])
    }
    if tags.get("SpotRecoveryTest") != test_id:
        raise RuntimeError(
            f"refusing to terminate untagged instance {instance_id}; "
            f"SpotRecoveryTest={tags.get('SpotRecoveryTest')!r}"
        )
    if instance.get("InstanceLifecycle") != "spot":
        raise RuntimeError(
            f"refusing to terminate non-Spot instance {instance_id}"
        )
    return instance


def terminate_instance(instance_id: str, region: str) -> None:
    subprocess.run(
        [
            "aws",
            "ec2",
            "terminate-instances",
            "--region",
            region,
            "--instance-ids",
            instance_id,
        ],
        check=True,
    )


def discover_node_agent_auth_secret(namespace: str) -> str:
    try:
        result = subprocess.run(
            [
                "kubectl",
                "-n",
                namespace,
                "get",
                "secret",
                "node-agent-auth",
                "-o",
                "jsonpath={.data.NODE_AGENT_AUTH_SECRET}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        encoded = result.stdout.strip()
        return base64.b64decode(encoded).decode() if encoded else ""
    except Exception:
        # Backwards-compatible with clusters that have not enabled signed
        # node-agent requests yet.
        return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="")
    parser.add_argument("--api-key", default=os.environ.get("API_KEY", ""))
    parser.add_argument("--namespace", default="sandbox-system")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--test-id", required=True)
    parser.add_argument(
        "--run-id",
        default="",
        help=(
            "Unique execution suffix for idempotency keys; defaults to a "
            "random value so a rerun cannot reuse a deleted sandbox."
        ),
    )
    parser.add_argument(
        "--pool",
        choices=("spot", "protected"),
        default="spot",
    )
    parser.add_argument(
        "--placement-group",
        default="",
        help=(
            "Hard recovery-group constraint; prevents fallback to unrelated "
            "production nodes."
        ),
    )
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--memory-mib", type=int, default=2048)
    parser.add_argument("--dirty-mib", type=int, default=1200)
    parser.add_argument("--create-concurrency", type=int, default=8)
    parser.add_argument("--exec-concurrency", type=int, default=12)
    parser.add_argument("--base-timeout-s", type=int, default=1200)
    parser.add_argument("--checkpoint-timeout-s", type=int, default=150)
    parser.add_argument("--recovery-timeout-s", type=int, default=600)
    parser.add_argument(
        "--pre-checkpoint-quiesce-s",
        type=int,
        default=0,
        help=(
            "When positive, run host sync after dirty-fill and wait this many "
            "seconds for Dirty+Writeback to fall below the configured limit."
        ),
    )
    parser.add_argument(
        "--pre-checkpoint-dirty-limit-mib",
        type=int,
        default=64,
        help="Dirty+Writeback limit used by --pre-checkpoint-quiesce-s.",
    )
    parser.add_argument("--node-agent-url", default="")
    parser.add_argument(
        "--node-agent-auth-secret",
        default=os.environ.get("NODE_AGENT_AUTH_SECRET", ""),
        help=(
            "HMAC secret for direct node-agent calls. Prefer the "
            "NODE_AGENT_AUTH_SECRET environment variable."
        ),
    )
    parser.add_argument(
        "--trigger-mode",
        choices=("simulate", "fis"),
        default="simulate",
        help=(
            "simulate calls the node-agent test endpoint; fis starts an AWS "
            "FIS experiment and waits for the IMDS watcher to detect it."
        ),
    )
    parser.add_argument(
        "--fis-template-id",
        default="",
        help="Required with --trigger-mode=fis.",
    )
    parser.add_argument(
        "--terminate-source",
        action="store_true",
        help=(
            "With simulate mode, terminate the isolated tagged Spot source "
            "after checkpointing. FIS mode always performs a real termination."
        ),
    )
    parser.add_argument("--keep-sandboxes", action="store_true")
    parser.add_argument(
        "--output",
        default="",
        help="JSON result path; defaults to /tmp/spot-recovery-<test-id>.json",
    )
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be positive")
    if args.dirty_mib >= args.memory_mib:
        parser.error("--dirty-mib must be less than --memory-mib")
    if args.pre_checkpoint_quiesce_s < 0:
        parser.error("--pre-checkpoint-quiesce-s must be non-negative")
    if args.pre_checkpoint_dirty_limit_mib < 0:
        parser.error("--pre-checkpoint-dirty-limit-mib must be non-negative")
    if args.terminate_source and not args.test_id:
        parser.error("--terminate-source requires --test-id")
    if args.trigger_mode == "fis" and not args.fis_template_id:
        parser.error("--trigger-mode=fis requires --fis-template-id")
    if args.trigger_mode == "fis" and args.pool != "spot":
        parser.error("--trigger-mode=fis requires --pool=spot")
    if not args.node_agent_auth_secret:
        args.node_agent_auth_secret = discover_node_agent_auth_secret(
            args.namespace
        )

    source_will_terminate = (
        args.trigger_mode == "fis" or args.terminate_source
    )
    run_id = args.run_id or uuid.uuid4().hex[:8]

    report: dict[str, Any] = {
        "test_id": args.test_id,
        "run_id": run_id,
        "started_at": utcnow(),
        "config": {
            **vars(args),
            "api_key": "<redacted>" if args.api_key else "",
            "node_agent_auth_secret": (
                "<redacted>" if args.node_agent_auth_secret else ""
            ),
        },
        "sandboxes": [],
    }
    output = Path(
        args.output or f"/tmp/spot-recovery-{args.test_id}.json"
    )
    sandbox_ids: list[str] = []
    node: HttpClient | None = None

    with contextlib.ExitStack() as stack:
        api_url = args.api_url
        if not api_url:
            api_url = stack.enter_context(
                PortForward(
                    args.namespace,
                    "svc/sandbox-control-plane",
                    80,
                )
            )
        api = HttpClient(api_url, args.api_key)
        api.require("GET", "/", expected=(200,))

        def create(index: int) -> dict:
            meta = (
                {"placement_group": args.placement_group}
                if args.placement_group else {}
            )
            code, result = api.request(
                "POST",
                "/sandboxes",
                {
                    "tenant_id": f"spot-recovery-{args.test_id}",
                    "image": "min",
                    "cpu": 1,
                    "mem_mib": args.memory_mib,
                    "pool": args.pool,
                    "meta": meta,
                    "idempotency_key": (
                        f"spot-recovery-{args.test_id}-{run_id}-{index}"
                    ),
                },
                timeout_s=300,
            )
            result["_create_http_code"] = code
            result["_create_index"] = index
            return result

        try:
            created = parallel_map(
                list(range(args.count)),
                create,
                args.create_concurrency,
            )
            sandbox_ids = sorted(
                str(item["id"])
                for item in created
                if item.get("id")
            )
            report["sandbox_ids"] = sandbox_ids
            create_failures = [
                {
                    "index": item.get("_create_index"),
                    "id": item.get("id"),
                    "code": item.get("_create_http_code"),
                    "state": item.get("state"),
                    "error": (
                        item.get("operation_error")
                        or item.get("error")
                    ),
                }
                for item in created
                if (
                    item.get("_create_http_code") not in {200, 201}
                    or not item.get("id")
                    or item.get("state") == "failed"
                )
            ]
            if create_failures:
                report["create_failures"] = create_failures
                raise RuntimeError(
                    "sandbox creation failures: "
                    + json.dumps(create_failures)
                )

            def wait_running(sid: str) -> dict:
                def probe() -> dict | None:
                    record = api.require(
                        "GET", f"/sandboxes/{sid}", expected=(200,)
                    )
                    if record.get("state") in {
                        "failed",
                        "recovery_failed",
                    }:
                        raise RuntimeError(
                            f"{sid} entered {record.get('state')}: "
                            f"{record.get('error') or record.get('recovery_error')}"
                        )
                    return (
                        record
                        if record.get("state") == "running"
                        else None
                    )

                return wait_until(
                    f"{sid} running",
                    300,
                    probe,
                )

            running = parallel_map(
                sandbox_ids,
                wait_running,
                args.create_concurrency,
            )
            source_nodes = {record.get("node", "") for record in running}
            if len(source_nodes) != 1 or "" in source_nodes:
                raise RuntimeError(
                    f"test sandboxes span source nodes: {source_nodes}"
                )
            source_node = next(iter(source_nodes))
            report["source_node"] = source_node

            def base_ready(sid: str) -> bool:
                record = api.require(
                    "GET", f"/sandboxes/{sid}", expected=(200,)
                )
                if record.get("state") != "running":
                    raise RuntimeError(
                        f"{sid} left running while waiting for base: "
                        f"{record.get('state')}"
                    )
                result = api.require(
                    "GET",
                    f"/admin/events?id={sid}&limit=100",
                    expected=(200,),
                )
                return any(
                    event.get("event") == "base_snapshot"
                    for event in result.get("events", [])
                )

            base_started = time.monotonic()
            for sid in sandbox_ids:
                wait_until(
                    f"{sid} base snapshot",
                    args.base_timeout_s,
                    lambda sid=sid: base_ready(sid),
                    interval_s=5,
                )
            report["base_wait_s"] = round(
                time.monotonic() - base_started, 3
            )

            markers = {
                sid: f"MARK-{args.test_id}-{index}-{uuid.uuid4().hex[:8]}"
                for index, sid in enumerate(sandbox_ids)
            }

            def dirty(sid: str) -> dict:
                marker = markers[sid]
                shm_size_mib = args.dirty_mib + 128
                command = (
                    "set -eu; "
                    "mkdir -p /dev/shm; "
                    f"mount -t tmpfs -o size={shm_size_mib}M "
                    "tmpfs /dev/shm; "
                    "mkdir -p /dev/shm/spot-recovery; "
                    f"dd if=/dev/zero of=/dev/shm/spot-recovery/payload "
                    f"bs=1M count={args.dirty_mib} status=none; "
                    f"printf '%s' '{marker}' "
                    "> /dev/shm/spot-recovery/marker; "
                    "sync; cat /dev/shm/spot-recovery/marker"
                )
                result = api.require(
                    "POST",
                    f"/sandboxes/{sid}/exec",
                    {"cmd": command},
                    expected=(200,),
                    timeout_s=180,
                )
                if marker not in result.get("stdout", ""):
                    raise RuntimeError(f"memory marker failed for {sid}")
                return result

            dirty_started = time.monotonic()
            parallel_map(sandbox_ids, dirty, args.exec_concurrency)
            report["dirty_fill_s"] = round(
                time.monotonic() - dirty_started, 3
            )
            report["dirty_total_mib"] = args.dirty_mib * args.count

            pod = find_node_agent_pod(args.namespace, source_node)
            report["source_node_agent_pod"] = pod
            if args.pre_checkpoint_quiesce_s > 0:
                report["pre_checkpoint_quiesce"] = quiesce_host_writeback(
                    args.namespace,
                    pod,
                    timeout_s=args.pre_checkpoint_quiesce_s,
                    threshold_mib=args.pre_checkpoint_dirty_limit_mib,
                )

            node_url = args.node_agent_url
            if not node_url:
                node_url = stack.enter_context(
                    PortForward(args.namespace, f"pod/{pod}", 8002)
                )
            node = HttpClient(
                node_url,
                node_agent_auth_secret=args.node_agent_auth_secret,
            )
            node.require("POST", "/reclaim/reset", {}, expected=(200,))

            trigger_at = time.monotonic()
            fis_experiment_id = ""
            expected_source_instance = ""
            if args.trigger_mode == "simulate":
                initial = node.require(
                    "POST",
                    "/reclaim/simulate",
                    {
                        "type": "spot-termination",
                        "action": "terminate",
                        "checkpoint_only": not args.terminate_source,
                    },
                    expected=(200,),
                    timeout_s=args.checkpoint_timeout_s,
                )
            else:
                node_identity = node.require(
                    "GET", "/recovery/status", expected=(200,)
                )
                expected_source_instance = node_identity.get(
                    "instance_id", ""
                )
                if not expected_source_instance:
                    raise RuntimeError(
                        "node-agent did not report its EC2 instance id"
                    )
                verify_isolated_instance(
                    expected_source_instance,
                    region=args.region,
                    test_id=args.test_id,
                )
                experiment = run_json([
                    "aws",
                    "fis",
                    "start-experiment",
                    "--region",
                    args.region,
                    "--experiment-template-id",
                    args.fis_template_id,
                    "--output",
                    "json",
                ])
                fis_experiment_id = experiment.get("experiment", {}).get(
                    "id", ""
                )
                if not fis_experiment_id:
                    raise RuntimeError(
                        "AWS FIS did not return an experiment id"
                    )
                report["fis_experiment_id"] = fis_experiment_id
                report["fis_started_at"] = utcnow()
                initial = {}

            def checkpoint_complete() -> dict | None:
                value = node.require(
                    "GET", "/reclaim/status", expected=(200,)
                )
                current_plan = value.get("plan") or {}
                if current_plan.get("phase") in {
                    "checkpointed",
                    "partial",
                }:
                    return current_plan
                if fis_experiment_id:
                    experiment = run_json([
                        "aws",
                        "fis",
                        "get-experiment",
                        "--region",
                        args.region,
                        "--id",
                        fis_experiment_id,
                        "--output",
                        "json",
                    ]).get("experiment", {})
                    fis_status = experiment.get("state", {}).get(
                        "status", ""
                    )
                    report["fis_status_during_checkpoint"] = fis_status
                    if (
                        fis_status
                        in {"failed", "cancelled", "stopped"}
                        and not value.get("detected")
                    ):
                        reason = experiment.get("state", {}).get(
                            "reason", ""
                        )
                        raise RuntimeError(
                            f"FIS experiment {fis_status}: {reason}"
                        )
                return None

            plan = wait_until(
                "checkpoint completion",
                args.checkpoint_timeout_s,
                checkpoint_complete,
            )
            session_id = (
                plan.get("session_id")
                or initial.get("session_id", "")
            )
            report["recovery_session_id"] = session_id
            report["checkpoint_plan"] = plan
            report["checkpoint_elapsed_from_trigger_s"] = round(
                time.monotonic() - trigger_at, 3
            )
            if plan.get("failed", 0):
                raise RuntimeError(
                    f"checkpoint failures: {plan.get('results')}"
                )
            if (
                source_will_terminate
                and not plan.get(
                    "volume_preservation", {}
                ).get("preserved")
            ):
                raise RuntimeError(
                    "source state volume was not preserved: "
                    f"{plan.get('volume_preservation')}"
                )

            source_instance = plan.get("instance_id", "")
            report["source_instance_id"] = source_instance
            report["source_volume_id"] = plan.get("state_volume_id", "")
            if (
                expected_source_instance
                and source_instance != expected_source_instance
            ):
                raise RuntimeError(
                    "FIS source instance mismatch: "
                    f"watcher={source_instance}, "
                    f"expected={expected_source_instance}"
                )

            if args.trigger_mode == "simulate" and args.terminate_source:
                verify_isolated_instance(
                    source_instance,
                    region=args.region,
                    test_id=args.test_id,
                )
                terminate_instance(source_instance, args.region)
                report["source_terminated_at"] = utcnow()
            elif not source_will_terminate:
                report["source_reset"] = node.require(
                    "POST",
                    "/reclaim/reset",
                    {},
                    expected=(200,),
                )

            if source_will_terminate:
                def recovery_complete() -> list[dict] | None:
                    records = [
                        api.require(
                            "GET", f"/sandboxes/{sid}", expected=(200,)
                        )
                        for sid in sandbox_ids
                    ]
                    current = [
                        record
                        for record in records
                        if record.get("recovery_session_id") == session_id
                    ]
                    if len(current) != len(records):
                        return None
                    terminal = {"running", "recovery_failed"}
                    if all(
                        record.get("state") in terminal
                        for record in current
                    ):
                        return current
                    return None

                recovered = wait_until(
                    "all sandboxes recovered",
                    args.recovery_timeout_s,
                    recovery_complete,
                    interval_s=2,
                )
                report["recovery_elapsed_from_trigger_s"] = round(
                    time.monotonic() - trigger_at, 3
                )
                failures = [
                    record for record in recovered
                    if record.get("state") != "running"
                ]
                if failures:
                    raise RuntimeError(
                        "recovery failures: "
                        + json.dumps(
                            [
                                {
                                    "id": item["id"],
                                    "state": item.get("state"),
                                    "phase": item.get("recovery_phase"),
                                    "error": item.get("recovery_error"),
                                }
                                for item in failures
                            ]
                        )
                    )

                target_nodes = {
                    record.get("node", "") for record in recovered
                }
                report["target_nodes"] = sorted(target_nodes)
                report["recovered_records"] = recovered

                def verify_marker(sid: str) -> dict:
                    marker = markers[sid]
                    result = api.require(
                        "POST",
                        f"/sandboxes/{sid}/exec",
                        {
                            "cmd": (
                                "cat /dev/shm/spot-recovery/marker"
                            )
                        },
                        expected=(200,),
                        timeout_s=120,
                    )
                    if marker not in result.get("stdout", ""):
                        raise RuntimeError(
                            f"recovered marker mismatch for {sid}"
                        )
                    return result

                verify_started = time.monotonic()
                parallel_map(
                    sandbox_ids,
                    verify_marker,
                    args.exec_concurrency,
                )
                report["marker_verify_s"] = round(
                    time.monotonic() - verify_started, 3
                )
                if fis_experiment_id:
                    report["fis_experiment"] = run_json([
                        "aws",
                        "fis",
                        "get-experiment",
                        "--region",
                        args.region,
                        "--id",
                        fis_experiment_id,
                        "--output",
                        "json",
                    ]).get("experiment", {})

            report["status"] = "passed"
        except Exception as exc:
            report["status"] = "failed"
            report["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            report["finished_at"] = utcnow()
            if (
                not source_will_terminate
                and node is not None
                and report.get("checkpoint_plan")
                and not report.get("source_reset")
            ):
                try:
                    report["source_reset"] = node.require(
                        "POST",
                        "/reclaim/reset",
                        {},
                        expected=(200,),
                    )
                except Exception as exc:
                    report["source_reset_error"] = str(exc)
            cleanup_allowed = (
                not args.keep_sandboxes
                and (
                    report.get("status") == "passed"
                    or not report.get("fis_experiment_id")
                )
            )
            if sandbox_ids and cleanup_allowed:
                cleanup_errors = []
                for sid in sandbox_ids:
                    try:
                        code, result = api.request(
                            "DELETE",
                            f"/sandboxes/{sid}",
                            timeout_s=180,
                        )
                        if code not in {200, 404}:
                            cleanup_errors.append({
                                "id": sid,
                                "code": code,
                                "result": result,
                            })
                    except Exception as exc:
                        cleanup_errors.append({
                            "id": sid,
                            "error": str(exc),
                        })
                report["sandbox_cleanup_errors"] = cleanup_errors
            elif sandbox_ids and not args.keep_sandboxes:
                # Once a real FIS interruption has started, deleting the test
                # sandbox on a local harness exception can race the node
                # checkpoint and destroy the evidence needed to diagnose the
                # recovery path. Leave it in the isolated test placement group
                # and require explicit cleanup after inspection.
                report["sandbox_cleanup_deferred"] = True
            output.write_text(
                json.dumps(report, indent=2, default=str) + "\n"
            )
            print(f"result: {output}")

    print(json.dumps({
        "status": report.get("status"),
        "checkpoint_s": report.get(
            "checkpoint_plan", {}
        ).get("wall_clock_s"),
        "effective_write_mib_s": report.get(
            "checkpoint_plan", {}
        ).get("effective_write_mib_s"),
        "recovery_s": report.get("recovery_elapsed_from_trigger_s"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
