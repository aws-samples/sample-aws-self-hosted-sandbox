"""Same-AZ state-EBS takeover for Spot-interrupted Firecracker nodes."""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from sandbox_api import db
from sandbox_api.observability import log_event


ENABLED = os.environ.get(
    "SPOT_RECOVERY_ENABLED", "0"
).strip().lower() in {"1", "true", "yes"}
ATTACH_TIMEOUT_S = max(
    15, int(os.environ.get("SPOT_RECOVERY_ATTACH_TIMEOUT_S", "120"))
)
RESUME_CONCURRENCY = max(
    1, int(os.environ.get("SPOT_RECOVERY_RESUME_CONCURRENCY", "12"))
)
REPLENISH_ENABLED = os.environ.get(
    "SPOT_RECOVERY_REPLENISH_ENABLED", "1"
).strip().lower() in {"1", "true", "yes"}
MIN_STANDBY_PER_AZ = max(
    1, int(os.environ.get("SPOT_RECOVERY_MIN_STANDBY_PER_AZ", "1"))
)
EKS_CLUSTER_NAME = os.environ.get("EKS_CLUSTER_NAME", "").strip()
OD_RECYCLE_ENABLED = os.environ.get(
    "SPOT_RECOVERY_OD_RECYCLE_ENABLED", "1"
).strip().lower() in {"1", "true", "yes"}
REPATRIATION_LEASE_S = max(
    120, int(os.environ.get("SPOT_REPATRIATION_LEASE_S", "900"))
)
RECOVERY_STATES = [
    "checkpointing",
    "checkpointed",
    "attaching",
    "recovering",
    "recovery_failed",
]


def _required_cluster_name() -> str:
    cluster_name = EKS_CLUSTER_NAME.strip()
    if not cluster_name:
        raise RuntimeError(
            "EKS_CLUSTER_NAME is required when Spot recovery is enabled"
        )
    return cluster_name


class _SandboxLeaseSet:
    """Fence every sandbox while its shared state EBS moves between hosts."""

    def __init__(self, records: list[dict]):
        self.sandbox_ids = sorted({
            str(record.get("id", "")).strip()
            for record in records
            if str(record.get("id", "")).strip()
        })
        self.leases: dict[str, str] = {}
        self.stop_event = threading.Event()
        self.lost_event = threading.Event()
        self.thread: threading.Thread | None = None

    def acquire(self) -> bool:
        for sandbox_id in self.sandbox_ids:
            try:
                self.leases[sandbox_id] = db.acquire_lease(
                    sandbox_id,
                    duration_s=REPATRIATION_LEASE_S,
                )
            except ClientError as exc:
                self.release()
                if exc.response.get("Error", {}).get(
                    "Code"
                ) == "ConditionalCheckFailedException":
                    return False
                raise
            except Exception:
                self.release()
                raise
        if self.leases:
            self._start_renewer()
        return True

    def _start_renewer(self) -> None:
        interval = max(10.0, REPATRIATION_LEASE_S / 3)

        def renew() -> None:
            while not self.stop_event.wait(interval):
                for sandbox_id, lease_id in self.leases.items():
                    try:
                        held = db.renew_lease(
                            sandbox_id,
                            lease_id,
                            duration_s=REPATRIATION_LEASE_S,
                        )
                    except Exception as exc:
                        held = False
                        log_event(
                            "error",
                            "spot_repatriation_lease_renew_failed",
                            sandbox_id=sandbox_id,
                            error_type=type(exc).__name__,
                        )
                    if not held:
                        self.lost_event.set()
                        return

        self.thread = threading.Thread(
            target=renew,
            name="spot-repatriation-lease-renewer",
            daemon=True,
        )
        self.thread.start()

    def assert_held(self) -> None:
        if self.lost_event.is_set():
            raise RuntimeError("sandbox repatriation lease was lost")

    def release(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
            self.thread = None
        for sandbox_id, lease_id in self.leases.items():
            db.release_lease(sandbox_id, lease_id)
        self.leases.clear()


class SpotRecoveryManager:
    """Level-triggered recovery of one source-node EBS onto one standby."""

    def __init__(
        self,
        driver: Any,
        ec2: Any | None = None,
        eks: Any | None = None,
        autoscaling: Any | None = None,
    ):
        self.driver = driver
        self.ec2 = ec2 or boto3.client(
            "ec2",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        self.eks = eks
        self.autoscaling = autoscaling

    def reconcile_once(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "enabled": ENABLED,
            "sessions": 0,
            "waiting": 0,
            "recovered": 0,
            "failed": 0,
            "repatriation_waiting": 0,
            "repatriated": 0,
            "recycle_failed": 0,
            "touched": [],
        }
        if not ENABLED:
            return stats
        _required_cluster_name()

        grouped: dict[str, list[dict]] = defaultdict(list)
        for record in db.list_by_states(RECOVERY_STATES):
            # Throughput benchmarks use the exact checkpoint path while
            # deliberately keeping the source instance alive. They must not
            # claim a standby or mutate EBS attachment state.
            if bool(record.get("recovery_checkpoint_only")):
                continue
            session_id = record.get("recovery_session_id", "")
            if session_id:
                grouped[session_id].append(record)

        for session_id, records in grouped.items():
            stats["sessions"] += 1
            result = self._recover_session(session_id, records)
            stats[result["outcome"]] += 1
            stats["touched"].extend(result.get("touched", []))
        recycle = self._repatriate_recovery_hosts()
        for key in (
            "repatriation_waiting",
            "repatriated",
            "recycle_failed",
        ):
            stats[key] += int(recycle.get(key, 0))
        stats["touched"].extend(recycle.get("touched", []))
        stats["touched"] = sorted(set(stats["touched"]))
        return stats

    def _recover_session(
        self,
        session_id: str,
        records: list[dict],
    ) -> dict[str, Any]:
        recoverable = [
            record for record in records
            if record.get("state") in {
                "checkpointed", "attaching", "recovering"
            }
        ]
        failed = [
            record for record in records
            if record.get("state") == "recovery_failed"
        ]
        pending = [
            record for record in records
            if record.get("state") == "checkpointing"
        ]
        expected = max(
            (
                int(record.get("recovery_expected_count", 0) or 0)
                for record in records
            ),
            default=len(records),
        )
        completed = len(recoverable) + len(failed)
        deadline = _parse_time(
            next(
                (
                    record.get("recovery_deadline_at", "")
                    for record in records
                    if record.get("recovery_deadline_at")
                ),
                "",
            )
        )
        if completed < expected and (deadline is None or _utcnow() < deadline):
            return {"outcome": "waiting", "touched": []}
        if pending:
            for record in pending:
                db.force_update(record["id"], {
                    "state": "recovery_failed",
                    "recovery_phase": "checkpoint_deadline_exceeded",
                    "recovery_error": (
                        "checkpoint did not finish before the Spot deadline"
                    ),
                })
            failed.extend(pending)
        missing_count = max(0, expected - len(records))
        if not recoverable:
            return {
                "outcome": "failed",
                "touched": [record["id"] for record in failed],
            }

        first = recoverable[0]
        volume_id = first.get("recovery_source_volume_id", "")
        availability_zone = first.get("recovery_az", "")
        if not volume_id or not availability_zone:
            self._set_phase(
                recoverable,
                "recovery_metadata_missing",
                error="source volume id or availability zone is missing",
            )
            return {
                "outcome": "failed",
                "touched": [record["id"] for record in recoverable],
            }

        target = db.get_recovery_claim(session_id)
        if target is None:
            target = db.claim_recovery_standby(
                availability_zone, session_id
            )
        if target is None:
            self._set_phase(recoverable, "waiting_for_standby")
            return {
                "outcome": "waiting",
                "touched": [record["id"] for record in recoverable],
            }

        target_ip = target.get("ip") or target.get("node_id", "")
        target_instance = target.get("instance_id", "")
        if not target_ip or not target_instance:
            self._set_phase(
                recoverable,
                "standby_metadata_missing",
                error="standby instance id or IP is missing",
            )
            return {
                "outcome": "failed",
                "touched": [record["id"] for record in recoverable],
            }

        target_fields = {
            "recovery_target_node": target_ip,
            "recovery_target_instance_id": target_instance,
            "recovery_phase": "waiting_for_volume",
        }
        for record in recoverable:
            db.force_update(record["id"], {
                **target_fields,
                "state": "attaching",
            })

        try:
            self._validate_target_instance(
                target_instance,
                target_ip,
                availability_zone,
                str(target.get("recovery_group", "")),
            )
            volume = self._volume(volume_id)
            attachments = volume.get("Attachments") or []
            if volume.get("AvailabilityZone") != availability_zone:
                raise RuntimeError(
                    f"volume {volume_id} is in "
                    f"{volume.get('AvailabilityZone')}, expected "
                    f"{availability_zone}"
                )
            if EKS_CLUSTER_NAME and (
                _resource_tags(volume).get("eks:cluster-name")
                != EKS_CLUSTER_NAME
            ):
                raise RuntimeError(
                    f"volume {volume_id} does not belong to EKS cluster "
                    f"{EKS_CLUSTER_NAME}"
                )
            attached_to_target = any(
                attachment.get("InstanceId") == target_instance
                and attachment.get("State") in {"attaching", "attached"}
                for attachment in attachments
            )
            if volume.get("State") == "in-use" and not attached_to_target:
                self._set_phase(recoverable, "waiting_for_volume_detach")
                return {
                    "outcome": "waiting",
                    "touched": [record["id"] for record in recoverable],
                }
            if volume.get("State") == "available":
                self._set_phase(recoverable, "attaching_volume")
                self.ec2.attach_volume(
                    VolumeId=volume_id,
                    InstanceId=target_instance,
                    Device="/dev/sdf",
                )
            self._wait_attached(volume_id, target_instance)
            self.ec2.create_tags(
                Resources=[volume_id],
                Tags=[
                    {"Key": "RecoverySession", "Value": session_id},
                    {"Key": "RecoveryTarget", "Value": target_instance},
                ],
            )

            self._set_phase(recoverable, "mounting_volume")
            self.driver.mount_recovery_volume(
                target_ip,
                volume_id,
                timeout_s=ATTACH_TIMEOUT_S,
            )
        except Exception as exc:
            self._set_phase(
                recoverable,
                "volume_takeover_failed",
                error=str(exc)[:2048],
            )
            log_event(
                "error",
                "spot_volume_takeover_failed",
                recovery_session_id=session_id,
                error_type=type(exc).__name__,
            )
            return {
                "outcome": "failed",
                "touched": [record["id"] for record in recoverable],
            }

        results: list[tuple[str, bool, str]] = []
        with ThreadPoolExecutor(
            max_workers=min(RESUME_CONCURRENCY, len(recoverable)),
            thread_name_prefix="spot-resume",
        ) as executor:
            futures = {
                executor.submit(
                    self._resume_one,
                    record,
                    target_ip,
                    target_instance,
                ): record["id"]
                for record in recoverable
            }
            for future in as_completed(futures):
                sid = futures[future]
                try:
                    future.result()
                    results.append((sid, True, ""))
                except Exception as exc:
                    results.append((sid, False, str(exc)[:2048]))

        failures = [item for item in results if not item[1]]
        total_failed = len(failed) + len(failures) + missing_count
        try:
            self._ensure_standby_capacity(target)
        except Exception as exc:
            # Replenishment is important for the next interruption, but it is
            # deliberately outside the current sandboxes' recovery outcome.
            log_event(
                "error",
                "spot_standby_replenish_failed",
                recovery_session_id=session_id,
                recovery_group=target.get("recovery_group", ""),
                error_type=type(exc).__name__,
            )
        log_event(
            "info" if not total_failed else "error",
            "spot_recovery_completed",
            recovery_session_id=session_id,
            recovered=len(results) - len(failures),
            failed=total_failed,
            source_volume_id=volume_id,
            target_instance_id=target_instance,
        )
        return {
            "outcome": "recovered" if not total_failed else "failed",
            "touched": (
                [item[0] for item in results]
                + [record["id"] for record in failed]
            ),
        }

    def _repatriate_recovery_hosts(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "repatriation_waiting": 0,
            "repatriated": 0,
            "recycle_failed": 0,
            "touched": [],
        }
        if not OD_RECYCLE_ENABLED:
            return stats
        for source in db.list_active_nodes():
            session_id = str(source.get("recovery_claim_id", "")).strip()
            source_instance = str(source.get("instance_id", "")).strip()
            if (
                not session_id
                or not source_instance
                or source.get("pool") == "spot"
            ):
                continue
            records = db.list_by_recovery_target_instance(source_instance)
            expected_vm_count = int(source.get("vm_count", 0) or 0)
            if expected_vm_count > len(records):
                # The recovery-target GSI is eventually consistent and the
                # heartbeat is the host's current runtime count. Never move or
                # reclaim the shared EBS from a partial ownership view.
                stats["repatriation_waiting"] += 1
                continue
            if any(
                record.get("state") in {
                    "checkpointing",
                    "checkpointed",
                    "attaching",
                    "recovering",
                }
                for record in records
            ):
                stats["repatriation_waiting"] += 1
                continue
            try:
                outcome = self._repatriate_host(
                    session_id,
                    source,
                    records,
                )
                stats[outcome] += 1
                stats["touched"].extend(
                    record["id"] for record in records
                )
            except Exception as exc:
                stats["recycle_failed"] += 1
                stats["touched"].extend(
                    record["id"] for record in records
                )
                for record in records:
                    db.force_update(record["id"], {
                        "recovery_phase": "repatriation_failed",
                        "recovery_error": str(exc)[:2048],
                    })
                log_event(
                    "error",
                    "spot_recovery_od_recycle_failed",
                    recovery_session_id=session_id,
                    source_instance_id=source_instance,
                    error_type=type(exc).__name__,
                )
        return stats

    def _repatriate_host(
        self,
        session_id: str,
        source: dict,
        records: list[dict],
    ) -> str:
        source_instance = str(source.get("instance_id", ""))
        # GSI polling keeps the steady-state loop cheap, but the ownership set
        # that fences an EBS handoff must come from a strongly consistent base
        # table read. This catches suspended sandboxes after an agent restart,
        # when heartbeat vm_count alone is not an authoritative record count.
        records = db.list_by_recovery_target_instance(
            source_instance,
            consistent=True,
        )
        leases = _SandboxLeaseSet(records)
        if not leases.acquire():
            return "repatriation_waiting"
        try:
            fresh_records: list[dict] = []
            for record in records:
                fresh = db.get(record["id"])
                if (
                    not fresh
                    or str(
                        fresh.get("recovery_target_instance_id", "")
                    ) != source_instance
                ):
                    return "repatriation_waiting"
                fresh_records.append(fresh)
            return self._repatriate_host_locked(
                session_id,
                source,
                fresh_records,
                leases,
            )
        finally:
            leases.release()

    def _repatriate_host_locked(
        self,
        session_id: str,
        source: dict,
        records: list[dict],
        leases: _SandboxLeaseSet,
    ) -> str:
        source_instance = str(source.get("instance_id", ""))
        source_ip = str(source.get("ip") or source.get("node_id") or "")
        source_volume = str(source.get("state_volume_id", ""))
        availability_zone = str(source.get("availability_zone", ""))
        recovery_group = str(source.get("recovery_group", ""))
        if not source_ip or not availability_zone or not recovery_group:
            raise RuntimeError("recovery host metadata is incomplete")

        # If every sandbox has already left this host, only the exact OD
        # instance and any now-unused state volume remain to be reclaimed.
        if not records:
            target = db.get_repatriation_claim(session_id)
            if target is not None:
                replaced_volume = str(
                    target.get("repatriation_replaced_volume_id", "")
                )
                if replaced_volume:
                    self._delete_cluster_volume(replaced_volume)
                db.release_repatriation_claim(
                    str(target.get("node_id", "")),
                    session_id,
                )
            if source_volume:
                source_attachment = self._volume_attachment(
                    source_volume,
                    source_instance,
                    missing_ok=True,
                )
                if source_attachment:
                    self.driver.unmount_recovery_volume(
                        source_ip,
                        source_volume,
                        timeout_s=ATTACH_TIMEOUT_S,
                    )
                    self._detach_volume(source_volume, source_instance)
                    self._delete_cluster_volume(source_volume)
                else:
                    try:
                        volume = self._volume(source_volume)
                    except RuntimeError:
                        volume = {}
                    if (
                        volume.get("State") == "available"
                        and not (volume.get("Attachments") or [])
                    ):
                        self._delete_cluster_volume(source_volume)
            self._terminate_recovery_instance(source, recovery_group)
            log_event(
                "info",
                "spot_recovery_empty_od_recycled",
                recovery_session_id=session_id,
                source_instance_id=source_instance,
            )
            return "repatriated"

        target = db.get_repatriation_claim(session_id)
        if target is None:
            target = db.claim_repatriation_target(
                availability_zone,
                recovery_group,
                session_id,
            )
        if target is None:
            # Recovery is already complete and the sandbox remains usable on
            # OD. Waiting for a replacement Spot node is a capacity condition,
            # not a sandbox lifecycle regression, so keep the public
            # recovery_phase="recovered" until a target is actually claimed.
            return "repatriation_waiting"

        target_node_id = str(target.get("node_id", ""))
        target_ip = str(target.get("ip") or target_node_id or "")
        target_instance = str(target.get("instance_id", ""))
        replacement_volume = str(
            target.get("repatriation_replaced_volume_id")
            or target.get("state_volume_id", "")
        )
        if (
            not target_node_id
            or not target_ip
            or not target_instance
            or not replacement_volume
        ):
            raise RuntimeError("Spot repatriation target metadata is incomplete")
        self._validate_spot_target(
            target_instance,
            target_ip,
            availability_zone,
            recovery_group,
        )

        for record in records:
            original_state = str(
                record.get("repatriation_original_state")
                or record.get("state")
                or "running"
            )
            db.force_update(record["id"], {
                "state": "repatriating",
                "recovery_phase": "repatriation_checkpointing",
                "repatriation_original_state": original_state,
                "repatriation_source_instance_id": source_instance,
                "repatriation_target_node": target_ip,
                "repatriation_target_instance_id": target_instance,
                "repatriation_replaced_volume_id": replacement_volume,
                "recovery_error": "",
            })

        # Running guests receive one fresh local checkpoint. Already-suspended
        # guests need no VMM work; their complete state is already on this EBS.
        running_records = [
            record for record in records
            if (
                record.get("repatriation_original_state")
                or record.get("state")
            ) == "running"
            and not bool(record.get("repatriation_checkpointed"))
        ]
        checkpoint_failures: list[str] = []
        if running_records:
            with ThreadPoolExecutor(
                max_workers=min(
                    RESUME_CONCURRENCY,
                    len(running_records),
                ),
                thread_name_prefix="spot-repatriate-checkpoint",
            ) as executor:
                futures = {
                    executor.submit(
                        self._checkpoint_for_repatriation,
                        record,
                        source_ip,
                    ): record["id"]
                    for record in running_records
                }
                for future in as_completed(futures):
                    sid = futures[future]
                    try:
                        future.result()
                    except Exception:
                        checkpoint_failures.append(sid)
        if checkpoint_failures:
            raise RuntimeError(
                "repatriation checkpoint failed for "
                + ", ".join(sorted(checkpoint_failures))
            )

        for record in records:
            db.force_update(record["id"], {
                "recovery_phase": "repatriation_volume_handoff",
            })

        # The operations below are intentionally level-triggered. If the
        # operator restarts between detach/attach/mount, the next pass observes
        # EC2 attachment state and continues without creating another volume.
        source_volume = (
            str(records[0].get("recovery_source_volume_id", ""))
            or source_volume
        )
        if not source_volume:
            raise RuntimeError("recovery source volume is missing")
        if replacement_volume == source_volume:
            raise RuntimeError(
                "refusing repatriation because source and replacement "
                "volume identities are equal"
            )
        leases.assert_held()
        if not db.renew_repatriation_claim(
            target_node_id,
            session_id,
        ):
            raise RuntimeError("Spot repatriation target claim was lost")
        source_attachment = self._volume_attachment(
            source_volume, source_instance
        )
        if source_attachment:
            self.driver.unmount_recovery_volume(
                source_ip,
                source_volume,
                timeout_s=ATTACH_TIMEOUT_S,
            )
            self._detach_volume(source_volume, source_instance)

        replacement_attachment = self._volume_attachment(
            replacement_volume,
            target_instance,
            missing_ok=True,
        )
        if replacement_attachment:
            self.driver.unmount_recovery_volume(
                target_ip,
                replacement_volume,
                timeout_s=ATTACH_TIMEOUT_S,
            )
            self._detach_volume(replacement_volume, target_instance)

        source_attachment = self._volume_attachment(
            source_volume,
            target_instance,
        )
        if not source_attachment:
            volume = self._volume(source_volume)
            if volume.get("State") != "available":
                raise RuntimeError(
                    f"source volume {source_volume} is not available for Spot"
                )
            self.ec2.attach_volume(
                VolumeId=source_volume,
                InstanceId=target_instance,
                Device="/dev/sdf",
            )
        self._wait_attached(source_volume, target_instance)
        self.driver.mount_recovery_volume(
            target_ip,
            source_volume,
            timeout_s=ATTACH_TIMEOUT_S,
        )

        resume_failures: list[str] = []
        running_records = []
        for record in records:
            fresh = db.get(record["id"]) or record
            original_state = str(
                fresh.get("repatriation_original_state") or "running"
            )
            db.force_update(record["id"], {
                "recovery_phase": "repatriation_resuming",
            })
            if original_state == "running":
                running_records.append(fresh)
            else:
                db.force_update(record["id"], {
                    "state": original_state,
                    "node": target_ip,
                    "recovery_target_node": target_ip,
                    "recovery_target_instance_id": target_instance,
                    "recovery_phase": "repatriated",
                    "repatriated_at": db._utcnow(),
                    "recovery_error": "",
                })

        if running_records:
            if not db.renew_repatriation_claim(
                target_node_id,
                session_id,
            ):
                raise RuntimeError("Spot repatriation target claim was lost")
            with ThreadPoolExecutor(
                max_workers=min(
                    RESUME_CONCURRENCY,
                    len(running_records),
                ),
                thread_name_prefix="spot-repatriate-resume",
            ) as executor:
                futures = {
                    executor.submit(
                        self._resume_repatriated,
                        record,
                        target_ip,
                        target_instance,
                    ): record["id"]
                    for record in running_records
                }
                for future in as_completed(futures):
                    sid = futures[future]
                    try:
                        future.result()
                    except Exception:
                        resume_failures.append(sid)
        if resume_failures:
            raise RuntimeError(
                "repatriation resume failed for "
                + ", ".join(sorted(resume_failures))
            )

        expected_running = sum(
            1
            for record in records
            if (
                record.get("repatriation_original_state")
                or record.get("state")
            ) == "running"
        )
        target_health = self.driver.node_health(target_ip)
        if target_health.get("state_volume_id") != source_volume:
            raise RuntimeError(
                "Spot target has not published the repatriated state volume"
            )
        if int(target_health.get("vm_count", 0)) < expected_running:
            raise RuntimeError(
                "Spot target has not published all resumed sandboxes"
            )

        leases.assert_held()
        self._delete_cluster_volume(replacement_volume)
        db.release_repatriation_claim(
            target_node_id,
            session_id,
        )
        self._terminate_recovery_instance(source, recovery_group)
        log_event(
            "info",
            "spot_recovery_od_repatriated",
            recovery_session_id=session_id,
            source_instance_id=source_instance,
            target_instance_id=target_instance,
            state_volume_id=source_volume,
            sandboxes=len(records),
        )
        return "repatriated"

    def _checkpoint_for_repatriation(
        self,
        record: dict,
        source_ip: str,
    ) -> None:
        sid = record["id"]
        fresh = db.get(sid) or record
        runtime = self.driver.get_runtime_state(sid, {
            **fresh,
            "node": source_ip,
        })
        fields: dict[str, Any] = {}
        if runtime == "running":
            fields.update(
                self.driver.checkpoint_for_repatriation(
                    sid,
                    {**fresh, "node": source_ip},
                )
            )
        elif runtime != "suspended":
            raise RuntimeError(
                f"sandbox {sid} runtime is {runtime}, cannot repatriate"
            )
        db.force_update(sid, {
            **fields,
            "state": "repatriating",
            "repatriation_checkpointed": True,
            "recovery_phase": "repatriation_checkpointed",
        })

    def _resume_repatriated(
        self,
        record: dict,
        target_ip: str,
        target_instance: str,
    ) -> None:
        sid = record["id"]
        fresh = db.get(sid) or record
        fields = self.driver.resume(sid, {
            **fresh,
            "node": target_ip,
            "recovery_target_node": target_ip,
        })
        db.force_update(sid, {
            **fields,
            "state": "running",
            "node": target_ip,
            "recovery_target_node": target_ip,
            "recovery_target_instance_id": target_instance,
            "recovery_phase": "repatriated",
            "repatriated_at": db._utcnow(),
            "recovery_error": "",
            "last_active_at": db._utcnow(),
        })
        db.write_event(
            sid,
            "spot_repatriated",
            "repatriating",
            {
                "target_node": target_ip,
                "target_instance_id": target_instance,
            },
        )

    def _validate_spot_target(
        self,
        instance_id: str,
        target_ip: str,
        availability_zone: str,
        recovery_group: str,
    ) -> None:
        instance = self._instance(instance_id)
        tags = _resource_tags(instance)
        if instance.get("State", {}).get("Name") != "running":
            raise RuntimeError(
                f"Spot target {instance_id} is not running"
            )
        if instance.get("InstanceLifecycle") != "spot":
            raise RuntimeError(
                f"repatriation target {instance_id} is not Spot"
            )
        if instance.get("PrivateIpAddress") != target_ip:
            raise RuntimeError("Spot target IP does not match heartbeat")
        if instance.get("Placement", {}).get(
            "AvailabilityZone"
        ) != availability_zone:
            raise RuntimeError("Spot target is in a different AZ")
        if tags.get("eks:nodegroup-name") == recovery_group:
            raise RuntimeError(
                "Spot target unexpectedly belongs to the OD recovery group"
            )
        if EKS_CLUSTER_NAME and (
            tags.get("eks:cluster-name") != EKS_CLUSTER_NAME
        ):
            raise RuntimeError(
                f"Spot target does not belong to {EKS_CLUSTER_NAME}"
            )

    def _instance(self, instance_id: str) -> dict:
        response = self.ec2.describe_instances(
            InstanceIds=[instance_id]
        )
        instances = [
            instance
            for reservation in response.get("Reservations") or []
            for instance in reservation.get("Instances") or []
        ]
        if len(instances) != 1:
            raise RuntimeError(f"instance {instance_id} was not found")
        return instances[0]

    def _volume_attachment(
        self,
        volume_id: str,
        instance_id: str,
        *,
        missing_ok: bool = False,
    ) -> dict | None:
        try:
            volume = self._volume(volume_id)
        except RuntimeError:
            if missing_ok:
                return None
            raise
        return next(
            (
                attachment
                for attachment in volume.get("Attachments") or []
                if attachment.get("InstanceId") == instance_id
                and attachment.get("State") in {
                    "attaching", "attached", "detaching"
                }
            ),
            None,
        )

    def _detach_volume(
        self,
        volume_id: str,
        instance_id: str,
    ) -> None:
        attachment = self._volume_attachment(
            volume_id,
            instance_id,
            missing_ok=True,
        )
        if attachment and attachment.get("State") != "detaching":
            self.ec2.detach_volume(
                VolumeId=volume_id,
                InstanceId=instance_id,
            )
        deadline = time.monotonic() + ATTACH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                volume = self._volume(volume_id)
            except RuntimeError:
                return
            if (
                volume.get("State") == "available"
                and not (volume.get("Attachments") or [])
            ):
                return
            time.sleep(2)  # nosemgrep: arbitrary-sleep -- EBS detach poll
        raise TimeoutError(
            f"volume {volume_id} did not detach from {instance_id}"
        )

    def _delete_cluster_volume(self, volume_id: str) -> None:
        cluster_name = _required_cluster_name()
        try:
            volume = self._volume(volume_id)
        except RuntimeError:
            return
        if volume.get("State") != "available":
            raise RuntimeError(
                f"volume {volume_id} is not available for deletion"
            )
        if (
            _resource_tags(volume).get("eks:cluster-name")
            != cluster_name
        ):
            raise RuntimeError(
                f"refusing to delete volume outside {cluster_name}"
            )
        self.ec2.delete_volume(VolumeId=volume_id)

    def _terminate_recovery_instance(
        self,
        source: dict,
        recovery_group: str,
    ) -> None:
        cluster_name = _required_cluster_name()
        instance_id = str(source.get("instance_id", ""))
        instance = self._instance(instance_id)
        tags = _resource_tags(instance)
        if instance.get("InstanceLifecycle") == "spot":
            raise RuntimeError("refusing to recycle a Spot source as OD")
        if tags.get("eks:nodegroup-name") != recovery_group:
            raise RuntimeError(
                "recovery instance no longer belongs to the claimed node group"
            )
        if tags.get("eks:cluster-name") != cluster_name:
            raise RuntimeError(
                f"recovery instance does not belong to {cluster_name}"
            )
        autoscaling_group = tags.get("aws:autoscaling:groupName", "")
        if not autoscaling_group:
            raise RuntimeError(
                f"recovery instance {instance_id} has no ASG identity"
            )
        autoscaling = self.autoscaling or boto3.client(
            "autoscaling",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        self.autoscaling = autoscaling
        should_decrement = False
        if REPLENISH_ENABLED:
            eks = self.eks or boto3.client(
                "eks",
                region_name=os.environ.get("AWS_REGION", "us-east-1"),
            )
            self.eks = eks
            response = eks.describe_nodegroup(
                clusterName=cluster_name,
                nodegroupName=recovery_group,
            )
            scaling = response["nodegroup"]["scalingConfig"]
            desired = int(scaling["desiredSize"])
            minimum = int(scaling["minSize"])
            # Replenishment normally raised desired above min while the claimed
            # OD host was busy. If that scale-up failed, keep desired unchanged
            # so ASG replaces the terminated host instead of shrinking to zero.
            should_decrement = desired > minimum
        autoscaling.terminate_instance_in_auto_scaling_group(
            InstanceId=instance_id,
            ShouldDecrementDesiredCapacity=should_decrement,
        )

    def _ensure_standby_capacity(self, claimed: dict) -> None:
        """Keep at least MIN_STANDBY_PER_AZ unclaimed nodes in the group.

        The claimed node remains in the managed node group while it hosts the
        recovered sandboxes. Desired capacity therefore grows by one per
        concurrent takeover. Comparing desired capacity with live heartbeats
        treats already-requested but not-yet-joined nodes as pending spare
        capacity, making repeated reconciles idempotent.
        """
        group = str(claimed.get("recovery_group", "")).strip()
        if not REPLENISH_ENABLED or not EKS_CLUSTER_NAME or not group:
            return
        az = claimed.get("availability_zone", "")
        live = [
            node for node in db.list_active_nodes()
            if node.get("recovery_group") == group
            and (not az or node.get("availability_zone") == az)
        ]
        available = [
            node for node in live
            if node.get("recovery_role") == "standby"
            and not node.get("recovery_claim_id")
            and not node.get("state_volume_id")
            and int(node.get("vm_count", 0)) == 0
            and not bool(node.get("draining"))
        ]
        eks = self.eks or boto3.client(
            "eks",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        self.eks = eks
        response = eks.describe_nodegroup(
            clusterName=EKS_CLUSTER_NAME,
            nodegroupName=group,
        )
        scaling = response["nodegroup"]["scalingConfig"]
        desired = int(scaling["desiredSize"])
        maximum = int(scaling["maxSize"])
        pending = max(0, desired - len(live))
        deficit = max(
            0,
            MIN_STANDBY_PER_AZ - len(available) - pending,
        )
        if deficit == 0:
            return
        new_desired = min(maximum, desired + deficit)
        if new_desired <= desired:
            raise RuntimeError(
                f"standby node group {group} reached maxSize={maximum}"
            )
        eks.update_nodegroup_config(
            clusterName=EKS_CLUSTER_NAME,
            nodegroupName=group,
            scalingConfig={"desiredSize": new_desired},
        )
        log_event(
            "info",
            "spot_standby_replenish_requested",
            recovery_group=group,
            availability_zone=az,
            desired_before=desired,
            desired_after=new_desired,
            available=len(available),
            pending=pending,
        )

    def _resume_one(
        self,
        record: dict,
        target_ip: str,
        target_instance: str,
    ) -> None:
        sid = record["id"]
        db.force_update(sid, {
            "state": "recovering",
            "recovery_phase": "resuming",
            "recovery_target_node": target_ip,
            "recovery_target_instance_id": target_instance,
            "recovery_error": "",
        })
        fresh = db.get(sid) or {
            **record,
            "recovery_target_node": target_ip,
        }
        try:
            fields = self.driver.resume(sid, fresh)
            db.force_update(sid, {
                **fields,
                "state": "running",
                "recovery_phase": "recovered",
                "recovered_at": db._utcnow(),
                "recovery_error": "",
                "last_active_at": db._utcnow(),
            })
            db.write_event(
                sid,
                "spot_recovered",
                "checkpointed",
                {
                    "target_node": target_ip,
                    "target_instance_id": target_instance,
                    "source_volume_id": record.get(
                        "recovery_source_volume_id", ""
                    ),
                },
            )
        except Exception as exc:
            db.force_update(sid, {
                "state": "recovery_failed",
                "recovery_phase": "resume_failed",
                "recovery_error": str(exc)[:2048],
            })
            raise

    def _volume(self, volume_id: str) -> dict:
        response = self.ec2.describe_volumes(VolumeIds=[volume_id])
        volumes = response.get("Volumes") or []
        if not volumes:
            raise RuntimeError(f"volume {volume_id} not found")
        return volumes[0]

    def _validate_target_instance(
        self,
        instance_id: str,
        target_ip: str,
        availability_zone: str,
        recovery_group: str,
    ) -> None:
        if not recovery_group:
            raise RuntimeError("standby recovery group is missing")
        response = self.ec2.describe_instances(
            InstanceIds=[instance_id]
        )
        instances = [
            instance
            for reservation in response.get("Reservations") or []
            for instance in reservation.get("Instances") or []
        ]
        if len(instances) != 1:
            raise RuntimeError(
                f"standby instance {instance_id} was not found"
            )
        instance = instances[0]
        tags = _resource_tags(instance)
        if instance.get("State", {}).get("Name") != "running":
            raise RuntimeError(
                f"standby instance {instance_id} is not running"
            )
        if instance.get("InstanceLifecycle") == "spot":
            raise RuntimeError(
                f"standby instance {instance_id} is Spot, expected On-Demand"
            )
        if instance.get("Placement", {}).get(
            "AvailabilityZone"
        ) != availability_zone:
            raise RuntimeError(
                f"standby instance {instance_id} is not in "
                f"{availability_zone}"
            )
        if instance.get("PrivateIpAddress") != target_ip:
            raise RuntimeError(
                f"standby instance {instance_id} IP does not match heartbeat"
            )
        if tags.get("eks:nodegroup-name") != recovery_group:
            raise RuntimeError(
                f"standby instance {instance_id} is not in node group "
                f"{recovery_group}"
            )
        if EKS_CLUSTER_NAME and (
            tags.get("eks:cluster-name") != EKS_CLUSTER_NAME
        ):
            raise RuntimeError(
                f"standby instance {instance_id} does not belong to EKS "
                f"cluster {EKS_CLUSTER_NAME}"
            )

    def _wait_attached(
        self,
        volume_id: str,
        instance_id: str,
    ) -> None:
        deadline = time.monotonic() + ATTACH_TIMEOUT_S
        while time.monotonic() < deadline:
            volume = self._volume(volume_id)
            if any(
                attachment.get("InstanceId") == instance_id
                and attachment.get("State") == "attached"
                for attachment in volume.get("Attachments") or []
            ):
                return
            time.sleep(2)  # nosemgrep: arbitrary-sleep -- EBS attach poll
        raise TimeoutError(
            f"volume {volume_id} did not attach to {instance_id}"
        )

    @staticmethod
    def _set_phase(
        records: list[dict],
        phase: str,
        *,
        error: str = "",
    ) -> None:
        for record in records:
            fields = {"recovery_phase": phase}
            if error:
                fields["recovery_error"] = error
            db.force_update(record["id"], fields)


def _parse_time(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resource_tags(resource: dict) -> dict[str, str]:
    return {
        str(tag.get("Key", "")): str(tag.get("Value", ""))
        for tag in resource.get("Tags") or []
        if tag.get("Key")
    }
