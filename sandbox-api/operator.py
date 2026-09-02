"""
FirecrackerSandbox Operator (Route A).

This process watches FirecrackerSandbox CRs and reconciles their desired
lifecycle state by reusing the existing FirecrackerDriver. The driver still
talks to the unchanged node-agent HTTP API; CRDs do not replace node-agent or
Firecracker's local API.

DynamoDB is retained as the backwards-compatible REST/Portal projection and
for idempotency, events, activity, node discovery, warm-pool indexing, tap
allocation, and operation leases.
"""
from __future__ import annotations

import os
import queue
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from botocore.exceptions import ClientError
from kubernetes import watch

from sandbox_api import db
from sandbox_api.crd import (
    FINALIZER,
    FirecrackerSandboxStore,
)
from sandbox_api.driver import SandboxSpec, ServiceSpec
from sandbox_api.drivers.firecracker import (
    FirecrackerDriver,
    normalize_image,
    requested_placement_group,
)
from sandbox_api.idle_detection import IdleDetector
from sandbox_api.observability import log_event
from sandbox_api.recovery import SpotRecoveryManager
from sandbox_api.warm_pool import WarmPool


DRIVER_NAME = "firecracker"
OPERATION_LEASE_S = int(os.environ.get("CRD_OPERATION_LEASE_S", "900"))
WORKERS = max(1, int(os.environ.get("CRD_OPERATOR_WORKERS", "8")))
RESYNC_S = max(5, int(os.environ.get("CRD_RESYNC_S", "20")))
LEADER_TTL_S = max(10, int(os.environ.get("LEADER_TTL_S", "30")))
LEADER_LOCK_ID = os.environ.get(
    "CRD_OPERATOR_LEADER_LOCK_ID", "firecracker-operator"
)
AUTO_SLEEP_ENABLED = os.environ.get(
    "AUTO_SLEEP_ENABLED", "1"
).lower() in ("1", "true")
WARM_REFILL_S = max(5, int(os.environ.get("WARM_POOL_REFILL_S", "60")))
BASE_SNAPSHOT_DELAY_S = float(
    os.environ.get("BASE_SNAPSHOT_DELAY_S", "20")
)
AUTO_BASE = os.environ.get(
    "AUTO_SNAPSHOT_BASE", "1"
).lower() in ("1", "true")
BASE_CONCURRENCY = max(
    1, int(os.environ.get("BASE_SNAPSHOT_CONCURRENCY", "2"))
)
RESUME_CONCURRENCY = max(1, int(os.environ.get("RESUME_CONCURRENCY", "12")))
SPOT_RECOVERY_POLL_S = max(
    1, int(os.environ.get("SPOT_RECOVERY_POLL_S", "2"))
)

_ACTIVE_STATES = [
    "creating",
    "running",
    "suspending",
    "suspended",
    "slept",
    "resuming",
    "failed",
    "orphaned",
    "needs_reschedule",
    "checkpointing",
    "checkpointed",
    "attaching",
    "recovering",
    "recovery_failed",
]


class FirecrackerSandboxOperator:
    def __init__(
        self,
        store: FirecrackerSandboxStore | Any | None = None,
        driver: FirecrackerDriver | Any | None = None,
    ):
        self.store = store or FirecrackerSandboxStore()
        self.driver = driver or FirecrackerDriver()
        self.warm_pool = WarmPool(DRIVER_NAME, self.driver)
        self.spot_recovery = SpotRecoveryManager(self.driver)
        self.idle_detector = IdleDetector(
            lambda sid: db.force_update(
                sid, {"last_active_at": db._utcnow()}
            )
        )
        self.owner = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.is_leader = False
        self._queue: queue.Queue[str] = queue.Queue()
        self._queued: set[str] = set()
        self._dirty: set[str] = set()
        self._queued_lock = threading.Lock()
        self._stop = threading.Event()
        self._base_sem = threading.Semaphore(BASE_CONCURRENCY)
        self._base_executor = ThreadPoolExecutor(
            max_workers=BASE_CONCURRENCY,
            thread_name_prefix="base-snapshot",
        )
        self._resume_sem = threading.Semaphore(RESUME_CONCURRENCY)
        self._last_warm_refill = 0.0
        self._warm_refill_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        self.store.ready()
        self.adopt_legacy_records()

        for idx in range(WORKERS):
            threading.Thread(
                target=self._worker,
                name=f"crd-worker-{idx}",
                daemon=True,
            ).start()
        threading.Thread(
            target=self._leadership_loop,
            name="crd-leadership",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._maintenance_loop,
            name="crd-maintenance",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._spot_recovery_loop,
            name="spot-recovery",
            daemon=True,
        ).start()

        self.enqueue_all()
        log_event(
            "info",
            "firecracker_operator_started",
            workers=WORKERS,
            namespace=os.environ.get("CRD_NAMESPACE", "sandbox-system"),
        )

        while not self._stop.is_set():
            watcher = watch.Watch()
            try:
                for event in watcher.stream(
                    self.store.api.list_namespaced_custom_object,
                    **self.store.watch_kwargs(),
                    timeout_seconds=30,
                ):
                    resource = event.get("object") or {}
                    sid = resource.get("metadata", {}).get("name")
                    if sid:
                        self.enqueue(sid)
                    if self._stop.is_set():
                        watcher.stop()
                        break
            except Exception as exc:
                log_event(
                    "error",
                    "crd_watch_failed",
                    error_type=type(exc).__name__,
                )
                self._stop.wait(2)

    def stop(self) -> None:
        self._stop.set()

    def reconcile(self, resource: dict) -> None:
        metadata = resource.get("metadata", {})
        sid = metadata.get("name")
        if not sid:
            return

        if metadata.get("deletionTimestamp"):
            self._reconcile_delete(sid, resource)
            return

        if FINALIZER not in (metadata.get("finalizers") or []):
            self.store.ensure_finalizer(sid, resource)

        record = self._ensure_record(resource)
        spec = resource.get("spec", {})
        status = resource.get("status", {})
        desired = spec.get("desiredState", "Running")
        operation_id = str(spec.get("operationId", ""))
        observed_operation = str(status.get("observedOperationId", ""))
        generation = int(metadata.get("generation", 0) or 0)
        state = record.get("state", "creating")

        if _terminal_operation_failed(
            status, operation_id, generation
        ):
            return

        if desired == "Running":
            if state == "running":
                self._reconcile_running_drift(
                    sid, resource, record, generation, operation_id
                )
                return
            if state == "failed" and operation_id == observed_operation:
                self._publish(
                    sid, record, generation, operation_id,
                    conditions=_conditions(
                        False, "OperationFailed",
                        record.get("error", "operation failed"),
                    ),
                )
                return
            if state in {
                "checkpointing",
                "checkpointed",
                "attaching",
                "recovering",
            }:
                self._publish(
                    sid,
                    record,
                    generation,
                    operation_id,
                    conditions=_conditions(
                        False,
                        "SpotRecoveryInProgress",
                        record.get("recovery_phase", state),
                    ),
                )
                return
            if state == "recovery_failed":
                self._publish(
                    sid,
                    record,
                    generation,
                    operation_id,
                    conditions=_conditions(
                        False,
                        "SpotRecoveryFailed",
                        record.get(
                            "recovery_error",
                            "spot checkpoint or recovery failed",
                        ),
                    ),
                )
                return
            if state in {
                "suspended", "slept", "resuming",
                "needs_reschedule",
            } or (
                state == "orphaned" and record.get("snapshot_s3")
            ):
                self._resume(
                    sid, resource, record, generation, operation_id
                )
                return
            if state == "orphaned":
                self._fail_without_action(
                    sid,
                    record,
                    generation,
                    operation_id,
                    "NoRecoverableSnapshot",
                    "runtime disappeared and no durable snapshot is available",
                )
                return
            self._create(sid, resource, record, generation, operation_id)
            return

        if desired == "Suspended":
            reason = spec.get("suspendReason", "manual")
            target = "slept" if reason == "idle" else "suspended"
            if state == target:
                self._publish(
                    sid, record, generation, operation_id,
                    conditions=_conditions(True, "Reconciled", target),
                )
                return
            if state == "suspended" and target == "slept":
                db.force_update(sid, {"state": "slept"})
                record = db.get(sid) or {**record, "state": "slept"}
                self._publish(
                    sid, record, generation, operation_id,
                    conditions=_conditions(True, "Reconciled", "slept"),
                )
                return
            if state == "suspending":
                self._recover_suspending(
                    sid,
                    record,
                    generation,
                    operation_id,
                    target,
                )
                return
            if state != "running":
                self._publish(
                    sid, record, generation, operation_id,
                    conditions=_conditions(
                        False, "WaitingForRunning",
                        f"cannot suspend from {state}",
                    ),
                )
                return
            self._suspend(
                sid,
                resource,
                record,
                generation,
                operation_id,
                target,
                reason,
            )

    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------

    def _create(
        self,
        sid: str,
        resource: dict,
        record: dict,
        generation: int,
        operation_id: str,
    ) -> None:
        lease = self._lease(sid)
        if not lease:
            return
        renewer = _LeaseRenewer(sid, lease)
        renewer.start()
        try:
            fresh = db.get(sid) or record
            if fresh.get("state") == "running":
                self._publish(
                    sid, fresh, generation, operation_id,
                    conditions=_conditions(True, "Reconciled", "running"),
                )
                return
            db.force_update(
                sid,
                {
                    "state": "creating",
                    "error": "",
                    "operation_error": "",
                    "failed_operation_id": "",
                },
            )
            fresh = db.get(sid) or {**fresh, "state": "creating"}
            self._publish(
                sid, fresh, generation, operation_id,
                conditions=_conditions(False, "Creating", "creating microVM"),
            )

            spec = _sandbox_spec(resource)
            pool = resource.get("spec", {}).get("pool") or None
            wants_default = normalize_image(spec.image) == "min"
            placement_group = requested_placement_group(spec)
            claimed = (
                self.warm_pool.claim(sid, spec, pool=pool)
                if (
                    wants_default
                    and placement_group is None
                    and self.warm_pool.can_claim(pool)
                )
                else False
            )
            if not claimed:
                fields = self.driver.create(sid, spec, pool=pool)
                db.force_update(sid, {**fields, "state": "running"})

            current = db.get(sid) or fresh
            db.write_event(sid, "created", "creating")
            self._publish(
                sid, current, generation, operation_id,
                conditions=_conditions(True, "Reconciled", "running"),
            )
            self._snapshot_base_async(sid)
        except Exception as exc:
            self._operation_failed(
                sid, generation, operation_id, "CreateFailed", exc
            )
        finally:
            renewer.stop()
            db.release_lease(sid, lease)

    def _recover_suspending(
        self,
        sid: str,
        record: dict,
        generation: int,
        operation_id: str,
        target: str,
    ) -> None:
        """Recover an abandoned suspend without racing the active owner.

        A node-agent marks the VM suspended before a durable S3 upload has
        necessarily returned. Another operator replica must therefore not
        infer completion from runtime state while the original lifecycle
        lease is still held.
        """
        lease = self._lease(sid)
        if not lease:
            return
        renewer = _LeaseRenewer(sid, lease)
        renewer.start()
        retry = False
        try:
            fresh = db.get(sid) or record
            if fresh.get("state") != "suspending":
                return
            runtime = self.driver.get_runtime_state(sid, fresh)
            if runtime == "running":
                db.force_update(sid, {"state": "running"})
                retry = True
                return
            if runtime in {"suspended", "stopped"}:
                db.force_update(sid, {"state": target})
                current = db.get(sid) or {**fresh, "state": target}
                self._publish(
                    sid,
                    current,
                    generation,
                    operation_id,
                    conditions=_conditions(
                        True, "RecoveredTransition", target
                    ),
                )
                return
            self._publish(
                sid,
                fresh,
                generation,
                operation_id,
                conditions=_conditions(
                    False,
                    "SuspendRecoveryPending",
                    f"runtime state is {runtime or 'unknown'}",
                ),
            )
        finally:
            renewer.stop()
            db.release_lease(sid, lease)
            if retry:
                self.enqueue(sid)

    def _suspend(
        self,
        sid: str,
        resource: dict,
        record: dict,
        generation: int,
        operation_id: str,
        target: str,
        reason: str,
    ) -> None:
        lease = self._lease(sid)
        if not lease:
            return
        renewer = _LeaseRenewer(sid, lease)
        renewer.start()
        try:
            fresh = db.get(sid) or record
            if fresh.get("state") != "running":
                return
            if reason == "idle":
                decision = self.idle_detector.decide(fresh)
                if not decision.idle:
                    # Activity arrived after the maintenance scan but before
                    # the operation lease. Cancel only this stale idle intent;
                    # a concurrent manual spec update wins via resourceVersion.
                    try:
                        self.store.request_state(
                            sid,
                            "Running",
                            uuid.uuid4().hex,
                            resource_version=str(
                                resource.get("metadata", {}).get(
                                    "resourceVersion", ""
                                )
                            ),
                        )
                    except Exception:
                        pass
                    self._publish(
                        sid,
                        fresh,
                        generation,
                        operation_id,
                        conditions=_conditions(
                            True,
                            "IdleCancelled",
                            "activity observed before suspend",
                        ),
                    )
                    return
            db.update_state(
                sid,
                "suspending",
                "running",
                {
                    "operation_error": "",
                    "failed_operation_id": "",
                },
            )
            self._publish(
                sid,
                db.get(sid) or {**fresh, "state": "suspending"},
                generation,
                operation_id,
                conditions=_conditions(
                    False, "Suspending", f"suspend reason={reason}"
                ),
            )
            snap_info = self.driver.suspend(sid, fresh)
            db.update_state(sid, target, "suspending", snap_info)
            detail = dict(snap_info)
            if reason == "idle":
                detail["reason"] = "idle"
            db.write_event(
                sid,
                "slept" if target == "slept" else "suspended",
                "running",
                detail,
            )
            self.idle_detector.forget(sid)
            current = db.get(sid) or {**fresh, **snap_info, "state": target}
            self._publish(
                sid, current, generation, operation_id,
                conditions=_conditions(True, "Reconciled", target),
            )
        except Exception as exc:
            # node-agent makes a best effort to resume a paused VM when the
            # snapshot fails. Preserve the legacy rollback-to-running rule.
            db.force_update(
                sid,
                {
                    "state": "running",
                    "error": str(exc)[:2048],
                    "operation_error": str(exc)[:2048],
                    "failed_operation_id": operation_id,
                },
            )
            current = db.get(sid) or record
            self._publish(
                sid, current, generation, operation_id,
                conditions=_conditions(False, "SuspendFailed", str(exc)),
            )
        finally:
            renewer.stop()
            db.release_lease(sid, lease)

    def _resume(
        self,
        sid: str,
        resource: dict,
        record: dict,
        generation: int,
        operation_id: str,
    ) -> None:
        lease = self._lease(sid)
        if not lease:
            return
        renewer = _LeaseRenewer(sid, lease)
        renewer.start()
        prev = record.get("state", "")
        try:
            fresh = db.get(sid) or record
            prev = fresh.get("state", prev)
            if prev == "running":
                self._publish(
                    sid, fresh, generation, operation_id,
                    conditions=_conditions(True, "Reconciled", "running"),
                )
                return
            if prev not in {
                "suspended", "slept", "resuming",
                "needs_reschedule", "orphaned",
            }:
                raise RuntimeError(f"sandbox is not resumable from {prev}")
            if prev == "resuming":
                runtime = self.driver.get_runtime_state(sid, fresh)
                if runtime == "running":
                    db.force_update(
                        sid,
                        {
                            "state": "running",
                            "error": "",
                            "operation_error": "",
                            "failed_operation_id": "",
                        },
                    )
                    current = db.get(sid) or {
                        **fresh,
                        "state": "running",
                    }
                    self._publish(
                        sid, current, generation, operation_id,
                        conditions=_conditions(
                            True, "RecoveredTransition", "running"
                        ),
                    )
                    return
            db.force_update(
                sid,
                {
                    "state": "resuming",
                    "error": "",
                    "operation_error": "",
                    "failed_operation_id": "",
                },
            )
            self._publish(
                sid,
                db.get(sid) or {**fresh, "state": "resuming"},
                generation,
                operation_id,
                conditions=_conditions(
                    False, "Resuming", "loading Firecracker snapshot"
                ),
            )

            queued_at = time.monotonic()
            with self._resume_sem:
                queue_wait = time.monotonic() - queued_at
                started = time.monotonic()
                fields = self.driver.resume(sid, fresh)
                restore_time = time.monotonic() - started
            self.idle_detector.forget(sid)
            db.force_update(
                sid,
                {
                    **fields,
                    "state": "running",
                    "restore_time_s": str(round(restore_time, 4)),
                    "resume_queue_wait_s": str(round(queue_wait, 4)),
                    "last_active_at": db._utcnow(),
                    "error": "",
                    "operation_error": "",
                    "failed_operation_id": "",
                },
            )
            db.write_event(
                sid,
                "resumed",
                prev,
                {
                    "restore_time_s": round(restore_time, 4),
                    "resume_queue_wait_s": round(queue_wait, 4),
                },
            )
            current = db.get(sid) or {**fresh, **fields, "state": "running"}
            self._publish(
                sid, current, generation, operation_id,
                conditions=_conditions(True, "Reconciled", "running"),
            )
        except Exception as exc:
            self._operation_failed(
                sid, generation, operation_id, "ResumeFailed", exc
            )
        finally:
            renewer.stop()
            db.release_lease(sid, lease)

    def _reconcile_delete(self, sid: str, resource: dict) -> None:
        finalizers = resource.get("metadata", {}).get("finalizers") or []
        if FINALIZER not in finalizers:
            return
        record = db.get(sid)
        if record is None:
            self.store.remove_finalizer(sid, resource)
            return
        lease = self._lease(sid)
        if not lease:
            return
        renewer = _LeaseRenewer(sid, lease)
        renewer.start()
        prev = record.get("state", "unknown")
        projection_deleted = False
        try:
            db.force_update(sid, {"state": "destroying", "error": ""})
            self._publish(
                sid,
                db.get(sid) or {**record, "state": "destroying"},
                int(resource.get("metadata", {}).get("generation", 0) or 0),
                str(resource.get("spec", {}).get("operationId", "")),
                conditions=_conditions(
                    False, "Destroying", "cleaning Firecracker runtime"
                ),
            )
            destroy = getattr(
                self.driver, "destroy_confirmed", self.driver.destroy
            )
            destroy(sid, record)
            db.delete(sid)
            projection_deleted = True
            db.write_event(sid, "destroyed", prev)
            self.store.remove_finalizer(sid, resource)
        except Exception as exc:
            if projection_deleted:
                # Runtime and compatibility projection are already gone. A
                # concurrent replica can finish finalizer removal; recreating
                # a failed DynamoDB item here would leave a permanent ghost.
                log_event(
                    "error",
                    "crd_finalizer_remove_failed",
                    sandbox_id=sid,
                    error_type=type(exc).__name__,
                )
            else:
                db.force_update(
                    sid, {"state": "failed", "error": str(exc)[:2048]}
                )
                self._publish(
                    sid,
                    db.get(sid) or record,
                    int(
                        resource.get("metadata", {}).get(
                            "generation", 0
                        ) or 0
                    ),
                    str(resource.get("spec", {}).get("operationId", "")),
                    conditions=_conditions(False, "DestroyFailed", str(exc)),
                )
        finally:
            renewer.stop()
            db.release_lease(sid, lease)

    # ------------------------------------------------------------------
    # Drift, adoption, and maintenance
    # ------------------------------------------------------------------

    def _reconcile_running_drift(
        self,
        sid: str,
        resource: dict,
        record: dict,
        generation: int,
        operation_id: str,
    ) -> None:
        runtime = self.driver.get_runtime_state(sid, record)
        if runtime == "running":
            self._publish(
                sid, record, generation, operation_id,
                conditions=_conditions(True, "Reconciled", "running"),
            )
            return

        # Avoid reacting to a transient node-agent timeout before the node
        # heartbeat TTL has also expired.
        if _record_node_is_active(record):
            self._publish(
                sid, record, generation, operation_id,
                conditions=_conditions(
                    False, "RuntimeProbeFailed",
                    "node is active but runtime probe did not report running",
                ),
            )
            return

        # A snapshot left over from an earlier suspend may be older than the
        # running VM. Automatically loading it would silently roll back user
        # state. Running-node loss therefore becomes orphaned unless an
        # interruption workflow first produced a fresh suspend checkpoint.
        db.force_update(
            sid,
            {
                "state": "orphaned",
                "reconcile_reason": "node_down_no_snapshot",
            },
        )
        current = db.get(sid) or record
        self._publish(
            sid, current, generation, operation_id,
            conditions=_conditions(
                False,
                "Orphaned",
                "node is down and no durable snapshot is available",
            ),
        )

    def adopt_legacy_records(self) -> dict[str, int]:
        stats = {"checked": 0, "created": 0, "errors": 0}
        for record in db.list_by_states(_ACTIVE_STATES):
            if (
                record.get("tenant_id") == "__pool__"
                or record.get("pool_state")
            ):
                continue
            stats["checked"] += 1
            try:
                resource, created = self.store.ensure(record)
                if created:
                    stats["created"] += 1
                    metadata = resource.get("metadata", {})
                    self._publish(
                        record["id"],
                        record,
                        int(metadata.get("generation", 0) or 0),
                        str(resource.get("spec", {}).get("operationId", "")),
                        conditions=_conditions(
                            True, "Adopted", "adopted existing runtime"
                        ),
                    )
            except Exception as exc:
                stats["errors"] += 1
                log_event(
                    "error",
                    "crd_adoption_failed",
                    sandbox_id=record.get("id", ""),
                    error_type=type(exc).__name__,
                )
        return stats

    def enqueue(self, sid: str) -> None:
        with self._queued_lock:
            if sid in self._queued:
                self._dirty.add(sid)
                return
            self._queued.add(sid)
        self._queue.put(sid)

    def enqueue_all(self) -> None:
        for resource in self.store.list():
            if sid := resource.get("metadata", {}).get("name"):
                self.enqueue(sid)

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                sid = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                resource = self.store.get(sid)
                if resource is not None:
                    self.reconcile(resource)
            except Exception as exc:
                log_event(
                    "error",
                    "crd_reconcile_failed",
                    sandbox_id=sid,
                    error_type=type(exc).__name__,
                )
                if not self._stop.is_set():
                    threading.Timer(2, self.enqueue, args=(sid,)).start()
            finally:
                was_dirty = False
                with self._queued_lock:
                    self._queued.discard(sid)
                    if sid in self._dirty:
                        self._dirty.discard(sid)
                        was_dirty = True
                self._queue.task_done()
                # An update received while this id was in-flight was
                # intentionally de-duplicated. Re-read once and immediately
                # enqueue a still-unobserved operation instead of waiting for
                # the periodic resync.
                try:
                    latest = self.store.get(sid)
                    if latest is not None:
                        spec_op = str(
                            latest.get("spec", {}).get("operationId", "")
                        )
                        status_op = str(
                            latest.get("status", {}).get(
                                "observedOperationId", ""
                            )
                        )
                        if was_dirty or spec_op != status_op:
                            self.enqueue(sid)
                except Exception:
                    pass

    def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.enqueue_all()
                if self.is_leader:
                    self._run_leader_maintenance()
            except Exception as exc:
                self.is_leader = False
                log_event(
                    "error",
                    "operator_maintenance_failed",
                    error_type=type(exc).__name__,
                )
            elapsed = time.monotonic() - started
            self._stop.wait(max(1.0, RESYNC_S - elapsed))

    def _spot_recovery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.is_leader:
                    stats = self.spot_recovery.reconcile_once()
                    for sid in stats.get("touched", []):
                        self.enqueue(sid)
            except Exception as exc:
                log_event(
                    "error",
                    "spot_recovery_loop_failed",
                    error_type=type(exc).__name__,
                )
            self._stop.wait(SPOT_RECOVERY_POLL_S)

    def _leadership_loop(self) -> None:
        interval = max(2.0, LEADER_TTL_S / 3)
        while not self._stop.is_set():
            try:
                rvn = db.acquire_leader_lock(
                    LEADER_LOCK_ID, self.owner, LEADER_TTL_S
                )
                self.is_leader = rvn is not None
            except Exception as exc:
                self.is_leader = False
                log_event(
                    "error",
                    "operator_leadership_failed",
                    error_type=type(exc).__name__,
                )
            self._stop.wait(interval)

    def _run_leader_maintenance(self) -> None:
        now = time.monotonic()
        if (
            now - self._last_warm_refill >= WARM_REFILL_S
            and self._warm_refill_lock.acquire(blocking=False)
        ):
            self._last_warm_refill = now
            threading.Thread(
                target=self._replenish_warm_pool,
                name="warm-pool-refill",
                daemon=True,
            ).start()
        if AUTO_SLEEP_ENABLED:
            self._request_idle_suspends()

    def _replenish_warm_pool(self) -> None:
        try:
            if self.is_leader:
                self.warm_pool.replenish()
        finally:
            self._warm_refill_lock.release()

    def _request_idle_suspends(self) -> None:
        for record in db.list_by_states(["running"]):
            try:
                if (
                    record.get("tenant_id") == "__pool__"
                    or record.get("pool_state")
                    or not _autostop_enabled(record)
                ):
                    continue
                decision = self.idle_detector.decide(record)
                if not decision.idle:
                    continue
                resource = self.store.get(record["id"])
                if resource is None:
                    resource, _ = self.store.ensure(record)
                current_spec = resource.get("spec", {})
                if current_spec.get("desiredState") == "Suspended":
                    # Never overwrite an explicit/manual suspend request with
                    # an idle reason.
                    continue
                self.store.request_state(
                    record["id"],
                    "Suspended",
                    uuid.uuid4().hex,
                    suspend_reason="idle",
                    resource_version=str(
                        resource.get("metadata", {}).get(
                            "resourceVersion", ""
                        )
                    ),
                )
                self.enqueue(record["id"])
            except Exception as exc:
                log_event(
                    "error",
                    "autosleep_request_failed",
                    sandbox_id=record.get("id", ""),
                    error_type=type(exc).__name__,
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_record(self, resource: dict) -> dict:
        sid = resource["metadata"]["name"]
        existing = db.get(sid)
        if existing is not None:
            return existing

        spec = resource.get("spec", {})
        status = resource.get("status", {})
        now = db._utcnow()
        record = {
            "id": sid,
            "tenant_id": spec.get("tenantId", "default"),
            "state": status.get("phase", "creating"),
            "driver": DRIVER_NAME,
            "image": spec.get("image", ""),
            "cpu": int(spec.get("cpu", 2)),
            "mem_mib": int(spec.get("memoryMiB", 4096)),
            "env": spec.get("env", {}),
            "services": spec.get("services", []),
            "meta": spec.get("meta", {}),
            "pool": spec.get("pool", ""),
            "created_at": resource.get("metadata", {}).get(
                "creationTimestamp", now
            ),
            "updated_at": now,
            "last_active_at": now,
        }
        annotations = resource.get("metadata", {}).get("annotations") or {}
        if idem := annotations.get(
            "sandbox.memorion.ai/idempotency-key"
        ):
            record["idempotency_key"] = idem
        try:
            db.put(record)
        except ClientError as exc:
            if exc.response.get("Error", {}).get(
                "Code"
            ) != "ConditionalCheckFailedException":
                raise
        return db.get(sid) or record

    def _lease(self, sid: str) -> str | None:
        try:
            return db.acquire_lease(sid, duration_s=OPERATION_LEASE_S)
        except ClientError as exc:
            if exc.response.get("Error", {}).get(
                "Code"
            ) == "ConditionalCheckFailedException":
                return None
            raise

    def _publish(
        self,
        sid: str,
        record: dict,
        generation: int,
        operation_id: str,
        *,
        conditions: list[dict],
    ) -> None:
        try:
            self.store.patch_status(
                sid,
                record,
                observed_generation=generation,
                observed_operation_id=operation_id,
                conditions=conditions,
            )
        except Exception as exc:
            # Runtime/DynamoDB projection has already converged. A later watch
            # resync retries status publication without repeating the action.
            log_event(
                "error",
                "crd_status_publish_failed",
                sandbox_id=sid,
                error_type=type(exc).__name__,
            )

    def _operation_failed(
        self,
        sid: str,
        generation: int,
        operation_id: str,
        reason: str,
        exc: Exception,
    ) -> None:
        db.force_update(
            sid,
            {
                "state": "failed",
                "error": str(exc)[:2048],
                "operation_error": str(exc)[:2048],
                "failed_operation_id": operation_id,
            },
        )
        record = db.get(sid) or {
            "id": sid,
            "state": "failed",
            "error": str(exc),
        }
        db.write_event(
            sid,
            "operation_failed",
            "unknown",
            {"reason": reason, "error_type": type(exc).__name__},
        )
        self._publish(
            sid,
            record,
            generation,
            operation_id,
            conditions=_conditions(False, reason, str(exc)),
        )

    def _fail_without_action(
        self,
        sid: str,
        record: dict,
        generation: int,
        operation_id: str,
        reason: str,
        message: str,
    ) -> None:
        db.force_update(sid, {"state": "failed", "error": message})
        self._publish(
            sid,
            db.get(sid) or record,
            generation,
            operation_id,
            conditions=_conditions(False, reason, message),
        )

    def _snapshot_base_async(self, sid: str) -> None:
        snapshot_base = getattr(self.driver, "snapshot_base", None)
        if not AUTO_BASE or snapshot_base is None:
            return

        def run() -> None:
            lease = None
            renewer = None
            try:
                self._stop.wait(BASE_SNAPSHOT_DELAY_S)
                if self._stop.is_set():
                    return
                with self._base_sem:
                    lease = self._lease(sid)
                    if not lease:
                        return
                    renewer = _LeaseRenewer(sid, lease)
                    renewer.start()
                    record = db.get(sid)
                    if record and record.get("state") == "running":
                        info = snapshot_base(sid, record)
                        db.write_event(
                            sid, "base_snapshot", "running", info
                        )
            except Exception as exc:
                log_event(
                    "error",
                    "base_snapshot_failed",
                    sandbox_id=sid,
                    error_type=type(exc).__name__,
                )
            finally:
                if renewer is not None:
                    renewer.stop()
                if lease:
                    db.release_lease(sid, lease)

        self._base_executor.submit(run)


def _sandbox_spec(resource: dict) -> SandboxSpec:
    spec = resource.get("spec", {})
    services = [
        ServiceSpec(
            port=int(item["port"]),
            protocol=item.get("protocol", "tcp"),
            autostop=bool(item.get("autostop", False)),
            autostart=bool(item.get("autostart", False)),
        )
        for item in spec.get("services", [])
    ]
    return SandboxSpec(
        image=spec.get("image", ""),
        cpu=int(spec.get("cpu", 2)),
        mem_mib=int(spec.get("memoryMiB", 4096)),
        env=spec.get("env", {}),
        services=services,
        meta=spec.get("meta", {}),
    )


def _autostop_enabled(record: dict) -> bool:
    if any(service.get("autostop") for service in record.get("services", [])):
        return True
    return bool((record.get("meta") or {}).get("auto_sleep"))


def _record_node_is_active(record: dict) -> bool:
    node = str(record.get("node", ""))
    if not node:
        return False
    host = node.split(":", 1)[0]
    try:
        active = db.list_active_nodes()
    except Exception:
        return True  # fail conservative: do not reschedule on registry outage
    for item in active:
        if node in {item.get("node_id"), item.get("ip")}:
            return True
        if host in {item.get("node_id"), item.get("ip")}:
            return True
    return False


def _terminal_operation_failed(
    status: dict,
    operation_id: str,
    generation: int,
) -> bool:
    if not operation_id:
        return False
    if str(status.get("observedOperationId", "")) != operation_id:
        return False
    if int(status.get("observedGeneration", 0) or 0) != generation:
        return False
    terminal_reasons = {"CreateFailed", "SuspendFailed", "ResumeFailed"}
    return any(
        condition.get("status") == "False"
        and condition.get("reason") in terminal_reasons
        for condition in status.get("conditions", []) or []
    )


class _LeaseRenewer:
    """Keep a long Firecracker operation fenced past the initial lease TTL."""

    def __init__(self, sid: str, lease_id: str):
        self.sid = sid
        self.lease_id = lease_id
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        interval = max(5.0, OPERATION_LEASE_S / 3)

        def renew() -> None:
            while not self.stop_event.wait(interval):
                try:
                    if not db.renew_lease(
                        self.sid,
                        self.lease_id,
                        duration_s=OPERATION_LEASE_S,
                    ):
                        log_event(
                            "error",
                            "operation_lease_lost",
                            sandbox_id=self.sid,
                        )
                        return
                except Exception as exc:
                    log_event(
                        "error",
                        "operation_lease_renew_failed",
                        sandbox_id=self.sid,
                        error_type=type(exc).__name__,
                    )

        self.thread = threading.Thread(
            target=renew,
            name=f"lease-renew-{self.sid}",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=1)


def _conditions(ready: bool, reason: str, message: str) -> list[dict]:
    return [{
        "type": "Ready",
        "status": "True" if ready else "False",
        "reason": reason,
        "message": str(message)[:1024],
        "lastTransitionTime": db._utcnow(),
    }]


def main() -> None:
    FirecrackerSandboxOperator().run()


if __name__ == "__main__":
    main()
