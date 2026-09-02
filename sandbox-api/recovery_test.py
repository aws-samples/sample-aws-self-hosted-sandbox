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
        ],
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
                    "maxSize": self.maximum,
                }
            }
        }

    def update_nodegroup_config(self, **kwargs) -> dict:
        self.update_calls.append(dict(kwargs))
        self.desired = kwargs["scalingConfig"]["desiredSize"]
        return {"update": {"status": "InProgress"}}


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
