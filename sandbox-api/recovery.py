"""Same-AZ state-EBS takeover for Spot-interrupted Firecracker nodes."""
from __future__ import annotations

import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import boto3

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
RECOVERY_STATES = [
    "checkpointing",
    "checkpointed",
    "attaching",
    "recovering",
    "recovery_failed",
]


class SpotRecoveryManager:
    """Level-triggered recovery of one source-node EBS onto one standby."""

    def __init__(
        self,
        driver: Any,
        ec2: Any | None = None,
        eks: Any | None = None,
    ):
        self.driver = driver
        self.ec2 = ec2 or boto3.client(
            "ec2",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        self.eks = eks

    def reconcile_once(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "enabled": ENABLED,
            "sessions": 0,
            "waiting": 0,
            "recovered": 0,
            "failed": 0,
            "touched": [],
        }
        if not ENABLED:
            return stats

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
