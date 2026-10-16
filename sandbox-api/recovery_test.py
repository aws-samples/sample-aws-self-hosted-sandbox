#!/usr/bin/env python3
"""Unit tests for same-AZ state-EBS Spot recovery."""
from __future__ import annotations

import copy
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import boto3
from moto import mock_aws


_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.environ.update({
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "DYNAMODB_TABLE": "sandboxes",
    "DYNAMODB_EVENTS_TABLE": "sandbox_events",
    "DYNAMODB_NODES_TABLE": "sandbox_nodes",
})


def _create_tables() -> None:
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="sandboxes",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
            {
                "AttributeName": "recovery_target_instance_id",
                "AttributeType": "S",
            },
            {"AttributeName": "updated_at", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": (
                "recovery_target_instance_id-updated_at-index"
            ),
            "KeySchema": [
                {
                    "AttributeName": "recovery_target_instance_id",
                    "KeyType": "HASH",
                },
                {
                    "AttributeName": "updated_at",
                    "KeyType": "RANGE",
                },
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
    )
    ddb.create_table(
        TableName="sandbox_events",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[
            {"AttributeName": "id", "KeyType": "HASH"},
            {"AttributeName": "ts", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
            {"AttributeName": "ts", "AttributeType": "S"},
        ],
    )
    ddb.create_table(
        TableName="sandbox_nodes",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "node_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "node_id", "AttributeType": "S"},
        ],
    )


class _FakeEC2:
    def __init__(self, state: str = "available"):
        attachments = []
        if state == "in-use":
            attachments = [{
                "InstanceId": "i-source",
                "State": "attached",
                "Device": "/dev/sdf",
            }]
        self.volume = {
            "VolumeId": "vol-source",
            "AvailabilityZone": "us-east-1a",
            "State": state,
            "Attachments": attachments,
            "Tags": [
                {"Key": "eks:cluster-name", "Value": "claude-sbx"},
            ],
        }
        self.instance = {
            "InstanceId": "i-standby",
            "PrivateIpAddress": "10.0.1.20",
            "State": {"Name": "running"},
            "Placement": {"AvailabilityZone": "us-east-1a"},
            "Tags": [
                {
                    "Key": "eks:cluster-name",
                    "Value": "claude-sbx",
                },
                {
                    "Key": "eks:nodegroup-name",
                    "Value": "claude-sbx-recovery-0",
                },
            ],
        }
        self.attach_calls: list[dict] = []
        self.tag_calls: list[dict] = []

    def describe_instances(self, InstanceIds: list[str]) -> dict:
        if InstanceIds != ["i-standby"]:
            return {"Reservations": []}
        return {
            "Reservations": [{
                "Instances": [copy.deepcopy(self.instance)],
            }],
        }

    def describe_volumes(self, VolumeIds: list[str]) -> dict:
        if VolumeIds != ["vol-source"]:
            return {"Volumes": []}
        return {"Volumes": [copy.deepcopy(self.volume)]}

    def attach_volume(self, **kwargs) -> dict:
        self.attach_calls.append(dict(kwargs))
        self.volume["State"] = "in-use"
        self.volume["Attachments"] = [{
            "InstanceId": kwargs["InstanceId"],
            "State": "attached",
            "Device": kwargs["Device"],
        }]
        return copy.deepcopy(self.volume["Attachments"][0])

    def create_tags(self, **kwargs) -> None:
        self.tag_calls.append(dict(kwargs))


class _FakeDriver:
    def __init__(self):
        self.mount_calls: list[tuple[str, str, int]] = []
        self.resume_calls: list[tuple[str, dict]] = []

    def mount_recovery_volume(
        self,
        node: str,
        volume_id: str,
        *,
        timeout_s: int,
    ) -> dict:
        self.mount_calls.append((node, volume_id, timeout_s))
        return {"mounted": True, "volume_id": volume_id}

    def resume(self, sandbox_id: str, record: dict) -> dict:
        self.resume_calls.append((sandbox_id, dict(record)))
        return {
            "node": record["recovery_target_node"],
            "guest_ip": f"172.18.0.{10 + len(self.resume_calls)}",
            "restore_time_s": 0.1,
            "restore_mode": "ebs-local",
        }


class _FakeEKS:
    def __init__(self):
        self.desired = 1
        self.maximum = 5
        self.update_calls: list[dict] = []

    def describe_nodegroup(self, **_kwargs) -> dict:
        return {
            "nodegroup": {
                "scalingConfig": {
                    "desiredSize": self.desired,
                    "minSize": 1,
                    "maxSize": self.maximum,
                }
            }
        }

    def update_nodegroup_config(self, **kwargs) -> dict:
        self.update_calls.append(dict(kwargs))
        self.desired = kwargs["scalingConfig"]["desiredSize"]
        return {"update": {"status": "InProgress"}}


class _RepatriationEC2:
    def __init__(self):
        common_tags = [
            {"Key": "eks:cluster-name", "Value": "claude-sbx"},
        ]
        self.instances = {
            "i-od": {
                "InstanceId": "i-od",
                "PrivateIpAddress": "10.0.1.20",
                "State": {"Name": "running"},
                "Placement": {"AvailabilityZone": "us-east-1a"},
                "Tags": common_tags + [
                    {
                        "Key": "eks:nodegroup-name",
                        "Value": "claude-sbx-recovery-0",
                    },
                    {
                        "Key": "aws:autoscaling:groupName",
                        "Value": "eks-claude-sbx-recovery-0-test",
                    },
                ],
            },
            "i-spot": {
                "InstanceId": "i-spot",
                "PrivateIpAddress": "10.0.1.30",
                "InstanceLifecycle": "spot",
                "State": {"Name": "running"},
                "Placement": {"AvailabilityZone": "us-east-1a"},
                "Tags": common_tags + [
                    {
                        "Key": "eks:nodegroup-name",
                        "Value": "claude-sbx-active-0",
                    },
                ],
            },
        }
        self.volumes = {
            "vol-source": {
                "VolumeId": "vol-source",
                "AvailabilityZone": "us-east-1a",
                "State": "in-use",
                "Attachments": [{
                    "InstanceId": "i-od",
                    "State": "attached",
                    "Device": "/dev/sdf",
                }],
                "Tags": copy.deepcopy(common_tags),
            },
            "vol-fresh": {
                "VolumeId": "vol-fresh",
                "AvailabilityZone": "us-east-1a",
                "State": "in-use",
                "Attachments": [{
                    "InstanceId": "i-spot",
                    "State": "attached",
                    "Device": "/dev/sdf",
                }],
                "Tags": copy.deepcopy(common_tags),
            },
        }
        self.detach_calls: list[dict] = []
        self.attach_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    def describe_instances(self, InstanceIds: list[str]) -> dict:
        instances = [
            copy.deepcopy(self.instances[instance_id])
            for instance_id in InstanceIds
            if instance_id in self.instances
        ]
        return {"Reservations": [{"Instances": instances}]}

    def describe_volumes(self, VolumeIds: list[str]) -> dict:
        return {
            "Volumes": [
                copy.deepcopy(self.volumes[volume_id])
                for volume_id in VolumeIds
                if volume_id in self.volumes
            ]
        }

    def detach_volume(self, **kwargs) -> dict:
        self.detach_calls.append(dict(kwargs))
        volume = self.volumes[kwargs["VolumeId"]]
        volume["State"] = "available"
        volume["Attachments"] = []
        return {}

    def attach_volume(self, **kwargs) -> dict:
        self.attach_calls.append(dict(kwargs))
        volume = self.volumes[kwargs["VolumeId"]]
        volume["State"] = "in-use"
        volume["Attachments"] = [{
            "InstanceId": kwargs["InstanceId"],
            "State": "attached",
            "Device": kwargs["Device"],
        }]
        return {}

    def delete_volume(self, **kwargs) -> dict:
        self.delete_calls.append(dict(kwargs))
        del self.volumes[kwargs["VolumeId"]]
        return {}


class _RepatriationDriver:
    def __init__(self):
        self.unmount_calls: list[tuple[str, str]] = []
        self.mount_calls: list[tuple[str, str]] = []
        self.checkpoint_calls: list[str] = []
        self.resume_calls: list[str] = []

    def get_runtime_state(self, sandbox_id: str, _record: dict) -> str:
        return "running"

    def checkpoint_for_repatriation(
        self,
        sandbox_id: str,
        _record: dict,
    ) -> dict:
        self.checkpoint_calls.append(sandbox_id)
        return {"snapshot_type": "diff"}

    def unmount_recovery_volume(
        self,
        node: str,
        volume_id: str,
        *,
        timeout_s: int,
    ) -> dict:
        self.unmount_calls.append((node, volume_id))
        return {"unmounted": True}

    def mount_recovery_volume(
        self,
        node: str,
        volume_id: str,
        *,
        timeout_s: int,
    ) -> dict:
        self.mount_calls.append((node, volume_id))
        return {"mounted": True}

    def resume(self, sandbox_id: str, record: dict) -> dict:
        self.resume_calls.append(sandbox_id)
        return {
            "node": record["recovery_target_node"],
            "guest_ip": "172.18.1.2",
            "restore_mode": "ebs-local",
        }

    def node_health(self, _node: str) -> dict:
        return {
            "state_volume_id": "vol-source",
            "vm_count": len(self.resume_calls),
        }


class _FakeAutoscaling:
    def __init__(self):
        self.terminate_calls: list[dict] = []

    def terminate_instance_in_auto_scaling_group(self, **kwargs) -> dict:
        self.terminate_calls.append(dict(kwargs))
        return {"Activity": {"StatusCode": "InProgress"}}


def _seed_session(*, states: tuple[str, ...] = ("checkpointed", "checkpointed")):
    from sandbox_api import db

    deadline = db._utcnow_plus(120)
    for idx, state in enumerate(states):
        db.put({
            "id": f"sbx-{idx}",
            "tenant_id": "tenant",
            "driver": "firecracker",
            "state": state,
            "node": "10.0.1.10",
            "tap_idx": idx + 1,
            "recovery_session_id": "session-1",
            "recovery_expected_count": len(states),
            "recovery_source_volume_id": "vol-source",
            "recovery_source_instance_id": "i-source",
            "recovery_az": "us-east-1a",
            "recovery_deadline_at": deadline,
            "updated_at": db._utcnow(),
        })
    boto3.resource(
        "dynamodb", region_name="us-east-1"
    ).Table("sandbox_nodes").put_item(Item={
        "node_id": "standby-a",
        "ip": "10.0.1.20",
        "instance_id": "i-standby",
        "availability_zone": "us-east-1a",
        "recovery_role": "standby",
        "recovery_group": "claude-sbx-recovery-0",
        "state_volume_id": "",
        "free_mem_mib": 250000,
        "vm_count": 0,
        "last_seen": db._utcnow(),
    })


class TestSpotRecovery(unittest.TestCase):
    def setUp(self) -> None:
        from sandbox_api import recovery

        cluster_name = patch.object(
            recovery, "EKS_CLUSTER_NAME", "claude-sbx"
        )
        cluster_name.start()
        self.addCleanup(cluster_name.stop)

    def test_enabled_recovery_requires_cluster_name(self) -> None:
        from sandbox_api import recovery

        with (
            patch.object(recovery, "ENABLED", True),
            patch.object(recovery, "EKS_CLUSTER_NAME", ""),
            patch.object(
                recovery.db, "list_by_states"
            ) as list_by_states,
        ):
            manager = recovery.SpotRecoveryManager(
                _FakeDriver(), ec2=_FakeEC2()
            )
            with self.assertRaisesRegex(
                RuntimeError, "EKS_CLUSTER_NAME is required"
            ):
                manager.reconcile_once()

        list_by_states.assert_not_called()

    def test_destructive_cleanup_requires_cluster_name(self) -> None:
        from sandbox_api import recovery

        ec2 = _RepatriationEC2()
        ec2.volumes["vol-fresh"]["State"] = "available"
        ec2.volumes["vol-fresh"]["Attachments"] = []
        autoscaling = _FakeAutoscaling()
        manager = recovery.SpotRecoveryManager(
            _RepatriationDriver(),
            ec2=ec2,
            autoscaling=autoscaling,
        )
        with patch.object(recovery, "EKS_CLUSTER_NAME", ""):
            with self.assertRaisesRegex(
                RuntimeError, "EKS_CLUSTER_NAME is required"
            ):
                manager._delete_cluster_volume("vol-fresh")
            with self.assertRaisesRegex(
                RuntimeError, "EKS_CLUSTER_NAME is required"
            ):
                manager._terminate_recovery_instance(
                    {"instance_id": "i-od"},
                    "claude-sbx-recovery-0",
                )

        self.assertEqual(ec2.delete_calls, [])
        self.assertEqual(autoscaling.terminate_calls, [])

    @mock_aws
    def test_checkpointed_session_attaches_mounts_and_resumes(self) -> None:
        _create_tables()
        _seed_session()
        from sandbox_api import db, recovery

        ec2 = _FakeEC2()
        eks = _FakeEKS()
        driver = _FakeDriver()
        with (
            patch.object(recovery, "ENABLED", True),
            patch.object(recovery, "EKS_CLUSTER_NAME", "claude-sbx"),
        ):
            result = recovery.SpotRecoveryManager(
                driver, ec2=ec2, eks=eks
            ).reconcile_once()

        self.assertEqual(result["recovered"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(
            ec2.attach_calls,
            [{
                "VolumeId": "vol-source",
                "InstanceId": "i-standby",
                "Device": "/dev/sdf",
            }],
        )
        self.assertEqual(
            driver.mount_calls[0][:2],
            ("10.0.1.20", "vol-source"),
        )
        self.assertEqual(
            eks.update_calls[0]["scalingConfig"],
            {"desiredSize": 2},
        )
        self.assertEqual(
            {sid for sid, _record in driver.resume_calls},
            {"sbx-0", "sbx-1"},
        )
        for sid in ("sbx-0", "sbx-1"):
            record = db.get(sid)
            self.assertEqual(record["state"], "running")
            self.assertEqual(record["node"], "10.0.1.20")
            self.assertEqual(record["recovery_phase"], "recovered")
            events = db.list_events(sid)
            self.assertEqual(events[0]["event"], "spot_recovered")

    @mock_aws
    def test_checkpoint_only_session_does_not_claim_standby(self) -> None:
        _create_tables()
        _seed_session()
        from sandbox_api import db, recovery

        for sid in ("sbx-0", "sbx-1"):
            db.force_update(
                sid,
                {"recovery_checkpoint_only": True},
            )
        ec2 = _FakeEC2()
        driver = _FakeDriver()
        with patch.object(recovery, "ENABLED", True):
            result = recovery.SpotRecoveryManager(
                driver, ec2=ec2
            ).reconcile_once()

        self.assertEqual(result["sessions"], 0)
        self.assertEqual(result["recovered"], 0)
        self.assertEqual(ec2.attach_calls, [])
        self.assertEqual(driver.mount_calls, [])
        self.assertIsNone(db.get_recovery_claim("session-1"))

    @mock_aws
    def test_waits_for_source_instance_to_release_volume(self) -> None:
        _create_tables()
        _seed_session()
        from sandbox_api import db, recovery

        ec2 = _FakeEC2(state="in-use")
        driver = _FakeDriver()
        with patch.object(recovery, "ENABLED", True):
            result = recovery.SpotRecoveryManager(
                driver, ec2=ec2
            ).reconcile_once()

        self.assertEqual(result["waiting"], 1)
        self.assertEqual(ec2.attach_calls, [])
        self.assertEqual(driver.mount_calls, [])
        self.assertEqual(
            db.get("sbx-0")["recovery_phase"],
            "waiting_for_volume_detach",
        )

    @mock_aws
    def test_waits_for_all_checkpoint_results_before_takeover(self) -> None:
        _create_tables()
        _seed_session(states=("checkpointed", "checkpointing"))
        from sandbox_api import recovery

        ec2 = _FakeEC2()
        driver = _FakeDriver()
        with patch.object(recovery, "ENABLED", True):
            result = recovery.SpotRecoveryManager(
                driver, ec2=ec2
            ).reconcile_once()

        self.assertEqual(result["waiting"], 1)
        self.assertEqual(ec2.attach_calls, [])
        self.assertEqual(driver.mount_calls, [])

    @mock_aws
    def test_expired_checkpoint_is_marked_failed(self) -> None:
        _create_tables()
        _seed_session(states=("checkpointed", "checkpointing"))
        from sandbox_api import db, recovery

        db.force_update(
            "sbx-0",
            {"recovery_deadline_at": db._utcnow_minus(1)},
        )
        db.force_update(
            "sbx-1",
            {"recovery_deadline_at": db._utcnow_minus(1)},
        )
        with patch.object(recovery, "ENABLED", True):
            result = recovery.SpotRecoveryManager(
                _FakeDriver(), ec2=_FakeEC2()
            ).reconcile_once()

        self.assertEqual(result["failed"], 1)
        self.assertEqual(db.get("sbx-0")["state"], "running")
        self.assertEqual(db.get("sbx-1")["state"], "recovery_failed")
        self.assertEqual(
            db.get("sbx-1")["recovery_phase"],
            "checkpoint_deadline_exceeded",
        )

    @mock_aws
    def test_rejects_spoofed_standby_instance_identity(self) -> None:
        _create_tables()
        _seed_session(states=("checkpointed",))
        from sandbox_api import db, recovery

        ec2 = _FakeEC2()
        ec2.instance["Tags"][1]["Value"] = "unrelated-node-group"
        with (
            patch.object(recovery, "ENABLED", True),
            patch.object(recovery, "EKS_CLUSTER_NAME", "claude-sbx"),
        ):
            result = recovery.SpotRecoveryManager(
                _FakeDriver(), ec2=ec2
            ).reconcile_once()

        self.assertEqual(result["failed"], 1)
        self.assertEqual(ec2.attach_calls, [])
        self.assertEqual(
            db.get("sbx-0")["recovery_phase"],
            "volume_takeover_failed",
        )

    @mock_aws
    def test_standby_claim_is_same_az_and_exclusive(self) -> None:
        _create_tables()
        from sandbox_api import db

        table = boto3.resource(
            "dynamodb", region_name="us-east-1"
        ).Table("sandbox_nodes")
        now = db._utcnow()
        for node_id, az in (
            ("standby-b", "us-east-1b"),
            ("standby-a", "us-east-1a"),
        ):
            table.put_item(Item={
                "node_id": node_id,
                "ip": f"10.0.0.{1 if az.endswith('a') else 2}",
                "instance_id": f"i-{node_id}",
                "availability_zone": az,
                "recovery_role": "standby",
                "state_volume_id": "",
                "free_mem_mib": 1000,
                "vm_count": 0,
                "last_seen": now,
            })

        claimed = db.claim_recovery_standby(
            "us-east-1a", "session-a"
        )
        self.assertEqual(claimed["node_id"], "standby-a")
        self.assertIsNone(
            db.claim_recovery_standby("us-east-1a", "session-b")
        )
        self.assertEqual(
            db.get_recovery_claim("session-a")["node_id"],
            "standby-a",
        )

    @mock_aws
    def test_nonempty_standby_cannot_be_claimed(self) -> None:
        _create_tables()
        from sandbox_api import db

        boto3.resource(
            "dynamodb", region_name="us-east-1"
        ).Table("sandbox_nodes").put_item(Item={
            "node_id": "standby-busy",
            "ip": "10.0.0.8",
            "instance_id": "i-busy",
            "availability_zone": "us-east-1a",
            "recovery_role": "standby",
            "state_volume_id": "",
            "free_mem_mib": 1000,
            "vm_count": 1,
            "last_seen": db._utcnow(),
        })

        self.assertIsNone(
            db.claim_recovery_standby("us-east-1a", "session-busy")
        )

    @mock_aws
    def test_partial_checkpoint_session_is_reported_failed(self) -> None:
        _create_tables()
        _seed_session(states=("checkpointed", "recovery_failed"))
        from sandbox_api import db, recovery

        ec2 = _FakeEC2()
        driver = _FakeDriver()
        with patch.object(recovery, "ENABLED", True):
            result = recovery.SpotRecoveryManager(
                driver, ec2=ec2
            ).reconcile_once()

        self.assertEqual(result["failed"], 1)
        self.assertEqual(db.get("sbx-0")["state"], "running")
        self.assertEqual(db.get("sbx-1")["state"], "recovery_failed")

    @mock_aws
    def test_pending_node_prevents_duplicate_replenish(self) -> None:
        _create_tables()
        _seed_session()
        from sandbox_api import recovery

        ec2 = _FakeEC2()
        eks = _FakeEKS()
        eks.desired = 2
        driver = _FakeDriver()
        with (
            patch.object(recovery, "ENABLED", True),
            patch.object(recovery, "EKS_CLUSTER_NAME", "claude-sbx"),
        ):
            recovery.SpotRecoveryManager(
                driver, ec2=ec2, eks=eks
            ).reconcile_once()

        self.assertEqual(eks.update_calls, [])

    @mock_aws
    def test_recovered_od_is_repatriated_to_spot_and_scaled_down(self) -> None:
        _create_tables()
        from sandbox_api import db, recovery

        db.put({
            "id": "sbx-repatriate",
            "tenant_id": "tenant",
            "driver": "firecracker",
            "state": "running",
            "node": "10.0.1.20",
            "tap_idx": 7,
            "recovery_session_id": "session-repatriate",
            "recovery_source_volume_id": "vol-source",
            "recovery_target_instance_id": "i-od",
            "recovery_target_node": "10.0.1.20",
            "recovery_az": "us-east-1a",
            "updated_at": db._utcnow(),
        })
        nodes = boto3.resource(
            "dynamodb", region_name="us-east-1"
        ).Table("sandbox_nodes")
        nodes.put_item(Item={
            "node_id": "od-node",
            "ip": "10.0.1.20",
            "instance_id": "i-od",
            "availability_zone": "us-east-1a",
            "recovery_role": "active",
            "recovery_group": "claude-sbx-recovery-0",
            "recovery_claim_id": "session-repatriate",
            "recovery_claim_expires": db._utcnow_plus(1800),
            "state_volume_id": "vol-source",
            "pool": "protected",
            "free_mem_mib": 200000,
            "vm_count": 1,
            "draining": False,
            "last_seen": db._utcnow(),
        })
        nodes.put_item(Item={
            "node_id": "spot-node",
            "ip": "10.0.1.30",
            "instance_id": "i-spot",
            "availability_zone": "us-east-1a",
            "recovery_role": "active",
            "recovery_group": "claude-sbx-recovery-0",
            "state_volume_id": "vol-fresh",
            "pool": "spot",
            "free_mem_mib": 200000,
            "vm_count": 0,
            "draining": False,
            "last_seen": db._utcnow(),
        })

        ec2 = _RepatriationEC2()
        driver = _RepatriationDriver()
        autoscaling = _FakeAutoscaling()
        eks = _FakeEKS()
        eks.desired = 2
        with (
            patch.object(recovery, "ENABLED", True),
            patch.object(recovery, "OD_RECYCLE_ENABLED", True),
            patch.object(recovery, "EKS_CLUSTER_NAME", "claude-sbx"),
        ):
            result = recovery.SpotRecoveryManager(
                driver,
                ec2=ec2,
                eks=eks,
                autoscaling=autoscaling,
            ).reconcile_once()

        self.assertEqual(result["repatriated"], 1)
        self.assertEqual(result["recycle_failed"], 0)
        self.assertEqual(driver.checkpoint_calls, ["sbx-repatriate"])
        self.assertEqual(driver.resume_calls, ["sbx-repatriate"])
        self.assertEqual(
            driver.unmount_calls,
            [
                ("10.0.1.20", "vol-source"),
                ("10.0.1.30", "vol-fresh"),
            ],
        )
        self.assertEqual(
            driver.mount_calls,
            [("10.0.1.30", "vol-source")],
        )
        self.assertEqual(
            ec2.attach_calls[0]["InstanceId"],
            "i-spot",
        )
        self.assertEqual(
            ec2.delete_calls,
            [{"VolumeId": "vol-fresh"}],
        )
        self.assertEqual(
            autoscaling.terminate_calls,
            [{
                "InstanceId": "i-od",
                "ShouldDecrementDesiredCapacity": True,
            }],
        )
        record = db.get("sbx-repatriate")
        self.assertEqual(record["state"], "running")
        self.assertEqual(record["node"], "10.0.1.30")
        self.assertEqual(record["recovery_phase"], "repatriated")
        self.assertIsNone(
            db.get_repatriation_claim("session-repatriate")
        )

    @mock_aws
    def test_post_resume_retry_cleans_claim_and_recycles_od(self) -> None:
        _create_tables()
        from sandbox_api import db, recovery

        nodes = boto3.resource(
            "dynamodb", region_name="us-east-1"
        ).Table("sandbox_nodes")
        nodes.put_item(Item={
            "node_id": "od-node",
            "ip": "10.0.1.20",
            "instance_id": "i-od",
            "availability_zone": "us-east-1a",
            "recovery_role": "standby",
            "recovery_group": "claude-sbx-recovery-0",
            "recovery_claim_id": "session-repatriate",
            "recovery_claim_expires": db._utcnow_plus(1800),
            # Simulate one stale heartbeat after the EBS already moved. The
            # cleanup retry must not delete the volume now attached to Spot.
            "state_volume_id": "vol-source",
            "pool": "protected",
            "free_mem_mib": 200000,
            "vm_count": 0,
            "draining": False,
            "last_seen": db._utcnow(),
        })
        nodes.put_item(Item={
            "node_id": "spot-node",
            "ip": "10.0.1.30",
            "instance_id": "i-spot",
            "availability_zone": "us-east-1a",
            "recovery_role": "active",
            "recovery_group": "claude-sbx-recovery-0",
            "repatriation_claim_id": "session-repatriate",
            "repatriation_claim_expires": db._utcnow_plus(1800),
            "repatriation_replaced_volume_id": "vol-fresh",
            "state_volume_id": "vol-source",
            "pool": "spot",
            "free_mem_mib": 190000,
            "vm_count": 1,
            "draining": False,
            "last_seen": db._utcnow(),
        })

        ec2 = _RepatriationEC2()
        ec2.volumes["vol-source"]["Attachments"] = [{
            "InstanceId": "i-spot",
            "State": "attached",
            "Device": "/dev/sdf",
        }]
        ec2.volumes["vol-fresh"]["State"] = "available"
        ec2.volumes["vol-fresh"]["Attachments"] = []
        autoscaling = _FakeAutoscaling()
        eks = _FakeEKS()
        # Simulate a failed pre-recovery scale-up: terminating the claimed host
        # must keep desired=1 so ASG replaces the standby baseline.
        eks.desired = 1
        with (
            patch.object(recovery, "ENABLED", True),
            patch.object(recovery, "OD_RECYCLE_ENABLED", True),
            patch.object(recovery, "EKS_CLUSTER_NAME", "claude-sbx"),
        ):
            result = recovery.SpotRecoveryManager(
                _RepatriationDriver(),
                ec2=ec2,
                eks=eks,
                autoscaling=autoscaling,
            ).reconcile_once()

        self.assertEqual(result["repatriated"], 1)
        self.assertEqual(
            ec2.delete_calls,
            [{"VolumeId": "vol-fresh"}],
        )
        self.assertIn("vol-source", ec2.volumes)
        self.assertEqual(
            autoscaling.terminate_calls,
            [{
                "InstanceId": "i-od",
                "ShouldDecrementDesiredCapacity": False,
            }],
        )
        self.assertIsNone(
            db.get_repatriation_claim("session-repatriate")
        )

    @mock_aws
    def test_repatriation_waits_for_existing_sandbox_lease(self) -> None:
        _create_tables()
        from sandbox_api import db, recovery

        db.put({
            "id": "sbx-leased",
            "tenant_id": "tenant",
            "driver": "firecracker",
            "state": "running",
            "updated_at": db._utcnow(),
        })
        existing = db.acquire_lease("sbx-leased", duration_s=900)
        leases = recovery._SandboxLeaseSet([{"id": "sbx-leased"}])

        self.assertFalse(leases.acquire())
        self.assertEqual(db.get("sbx-leased")["lease_id"], existing)
        db.release_lease("sbx-leased", existing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
