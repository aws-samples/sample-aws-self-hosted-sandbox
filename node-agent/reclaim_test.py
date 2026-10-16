#!/usr/bin/env python3
"""Unit tests for the hard-deadline Spot checkpoint path."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch


_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
_TMP = tempfile.TemporaryDirectory()
os.environ.update({
    "SBX_BASE": _TMP.name,
    "RECLAIM_AUTO_EVACUATE": "1",
    "RECLAIM_SNAPSHOT_CONCURRENCY": "2",
    "RECLAIM_BUDGET_S": "10",
    "RECLAIM_COMMIT_RESERVE_S": "2",
})

_SPEC = importlib.util.spec_from_file_location(
    "node_agent_main", _HERE / "main.py"
)
assert _SPEC and _SPEC.loader
agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(agent)


class TestSpotReclaim(unittest.TestCase):
    def setUp(self) -> None:
        agent._VMS.clear()
        agent._VM_OP_LOCKS.clear()
        agent._VM_OP_LOCK_USERS.clear()
        agent._STATE_VOLUME_CACHE = ""
        agent._NODE_RECOVERY_IDENTITY_CACHE.update({
            "role": "",
            "group": "",
            "resolved": False,
            "fetched_at": 0.0,
        })
        agent._RECLAIM_STATE.update({
            "detected": False,
            "signal": None,
            "at": None,
            "plan": None,
            "evacuated": False,
            "injected": None,
        })
        for idx in range(4):
            sid = f"sbx-{idx}"
            agent._VMS[sid] = {
                "state": "running",
                "pid": 1000 + idx,
                "sock": f"/tmp/{sid}.sock",
                "tap": f"fctap{idx}",
                "tap_idx": idx,
            }

    def test_parallel_checkpoint_and_durable_progress(self) -> None:
        updates: list[tuple[str, dict]] = []

        def fake_suspend(body: dict) -> dict:
            time.sleep(0.2)
            agent._VMS[body["id"]]["state"] = "suspended"
            return {
                "snapshot_type": "diff",
                "snapshot_create_time_s": 0.2,
                "mem_file_bytes": 2 * 1024**3,
                "mem_actual_bytes": 256 * 1024**2,
            }

        def fake_update(sid: str, fields: dict) -> tuple[bool, str]:
            updates.append((sid, dict(fields)))
            return True, ""

        started = time.monotonic()
        with (
            patch.object(agent, "op_suspend", side_effect=fake_suspend),
            patch.object(
                agent, "_update_sandbox_recovery", side_effect=fake_update
            ),
            patch.object(agent, "_instance_id", return_value="i-test"),
            patch.object(agent, "_availability_zone", return_value="us-east-1a"),
            patch.object(agent, "_state_volume_id", return_value="vol-test"),
            patch.object(
                agent,
                "_preserve_state_volume",
                return_value={
                    "preserved": True,
                    "instance_id": "i-test",
                    "volume_id": "vol-test",
                    "device": "/dev/sdf",
                },
            ),
        ):
            plan = agent._evacuate_local({
                "type": "spot-termination",
                "time": datetime.now(timezone.utc).isoformat(),
                "injected": True,
            })
        elapsed = time.monotonic() - started

        self.assertEqual(plan["evacuated_ok"], 4)
        self.assertEqual(plan["failed"], 0)
        self.assertTrue(plan["volume_preservation"]["preserved"])
        self.assertEqual(plan["snapshot_concurrency"], 2)
        self.assertTrue(plan["phase"] == "checkpointed")
        # Four 0.2s checkpoints at concurrency=2 should finish in roughly
        # two waves, not the 0.8s serial path.
        self.assertLess(elapsed, 0.7)
        self.assertEqual(plan["total_actual_bytes"], 4 * 256 * 1024**2)

        phases_by_sid: dict[str, list[str]] = {}
        for sid, fields in updates:
            phases_by_sid.setdefault(sid, []).append(fields["recovery_phase"])
            self.assertEqual(fields["recovery_source_volume_id"], "vol-test")
        self.assertEqual(set(phases_by_sid), set(agent._VMS))
        for phases in phases_by_sid.values():
            self.assertEqual(phases, ["checkpointing", "checkpointed"])

    def test_failure_is_journaled_without_hiding_successes(self) -> None:
        updates: list[tuple[str, dict]] = []

        def fake_suspend(body: dict) -> dict:
            if body["id"] == "sbx-2":
                raise RuntimeError("synthetic EBS timeout")
            return {
                "snapshot_type": "diff",
                "mem_file_bytes": 1024,
                "mem_actual_bytes": 512,
            }

        with (
            patch.object(agent, "op_suspend", side_effect=fake_suspend),
            patch.object(
                agent,
                "_update_sandbox_recovery",
                side_effect=lambda sid, fields: (
                    updates.append((sid, dict(fields))) or (True, "")
                ),
            ),
            patch.object(agent, "_instance_id", return_value="i-test"),
            patch.object(agent, "_availability_zone", return_value="us-east-1a"),
            patch.object(agent, "_state_volume_id", return_value="vol-test"),
            patch.object(
                agent,
                "_preserve_state_volume",
                return_value={
                    "preserved": True,
                    "instance_id": "i-test",
                    "volume_id": "vol-test",
                    "device": "/dev/sdf",
                },
            ),
        ):
            plan = agent._evacuate_local({
                "type": "spot-termination",
                "injected": True,
            })

        self.assertEqual(plan["evacuated_ok"], 3)
        self.assertEqual(plan["failed"], 1)
        self.assertEqual(plan["phase"], "partial")
        failed_updates = [
            fields for sid, fields in updates
            if sid == "sbx-2" and fields["recovery_phase"] == "checkpoint_failed"
        ]
        self.assertEqual(len(failed_updates), 1)
        self.assertIn("synthetic EBS timeout", failed_updates[0]["recovery_error"])

    def test_empty_node_does_not_retain_state_volume(self) -> None:
        agent._VMS.clear()
        with (
            patch.object(
                agent, "_has_local_sandbox_state", return_value=False
            ),
            patch.object(agent, "_preserve_state_volume") as preserve,
        ):
            plan = agent._evacuate_local({
                "type": "spot-termination",
                "injected": True,
            })

        preserve.assert_not_called()
        self.assertTrue(plan["volume_preservation"]["skipped"])
        self.assertEqual(
            plan["volume_preservation"]["reason"],
            "no local sandbox state",
        )
        self.assertEqual(plan["phase"], "checkpointed")
        self.assertTrue(agent._RECLAIM_STATE["evacuated"])

    def test_volume_preservation_failure_stops_checkpointing(self) -> None:
        updates: list[tuple[str, dict]] = []
        with (
            patch.object(
                agent,
                "_preserve_state_volume",
                side_effect=RuntimeError("synthetic preserve failure"),
            ),
            patch.object(agent, "op_suspend") as suspend,
            patch.object(
                agent,
                "_update_sandbox_recovery",
                side_effect=lambda sid, fields: (
                    updates.append((sid, dict(fields))) or (True, "")
                ),
            ),
            patch.object(agent, "_instance_id", return_value="i-test"),
            patch.object(
                agent, "_availability_zone", return_value="us-east-1a"
            ),
            patch.object(agent, "_state_volume_id", return_value="vol-test"),
        ):
            plan = agent._evacuate_local({
                "type": "spot-termination",
                "injected": True,
            })

        suspend.assert_not_called()
        self.assertEqual(plan["phase"], "volume_preservation_failed")
        self.assertEqual(plan["failed"], 4)
        self.assertFalse(agent._RECLAIM_STATE["evacuated"])
        self.assertEqual(len(updates), 4)
        self.assertTrue(all(
            fields["state"] == "recovery_failed"
            and fields["recovery_phase"] == "volume_preservation_failed"
            for _sid, fields in updates
        ))

    def test_state_volume_is_preserved_and_verified_before_checkpoint(
        self,
    ) -> None:
        responses = [
            CompletedProcess(
                [],
                0,
                stdout=json.dumps({
                    "Volumes": [{
                        "VolumeId": "vol-test",
                        "Attachments": [{
                            "InstanceId": "i-test",
                            "Device": "/dev/sdf",
                        }],
                    }],
                }),
                stderr="",
            ),
            CompletedProcess([], 0, stdout="", stderr=""),
            CompletedProcess(
                [],
                0,
                stdout=json.dumps({
                    "Reservations": [{
                        "Instances": [{
                            "InstanceId": "i-test",
                            "BlockDeviceMappings": [{
                                "DeviceName": "/dev/sdf",
                                "Ebs": {
                                    "VolumeId": "vol-test",
                                    "DeleteOnTermination": False,
                                },
                            }],
                        }],
                    }],
                }),
                stderr="",
            ),
        ]
        with (
            patch.object(agent, "_instance_id", return_value="i-test"),
            patch.object(agent, "_state_volume_id", return_value="vol-test"),
            patch.object(
                agent.subprocess,
                "run",
                side_effect=responses,
            ) as run,
        ):
            result = agent._preserve_state_volume()

        self.assertTrue(result["preserved"])
        self.assertEqual(result["device"], "/dev/sdf")
        self.assertEqual(run.call_count, 3)
        modify_command = run.call_args_list[1].args[0]
        self.assertIn("modify-instance-attribute", modify_command)
        mapping = json.loads(
            modify_command[modify_command.index("--block-device-mappings") + 1]
        )
        self.assertFalse(mapping[0]["Ebs"]["DeleteOnTermination"])

    def test_checkpoint_only_skips_volume_preservation_and_is_journaled(
        self,
    ) -> None:
        updates: list[dict] = []

        def fake_suspend(body: dict) -> dict:
            agent._VMS[body["id"]]["state"] = "suspended"
            return {
                "snapshot_type": "diff",
                "mem_file_bytes": 1024,
                "mem_actual_bytes": 512,
            }

        with (
            patch.object(agent, "op_suspend", side_effect=fake_suspend),
            patch.object(
                agent,
                "_update_sandbox_recovery",
                side_effect=lambda _sid, fields: (
                    updates.append(dict(fields)) or (True, "")
                ),
            ),
            patch.object(agent, "_instance_id", return_value="i-test"),
            patch.object(
                agent, "_availability_zone", return_value="us-east-1a"
            ),
            patch.object(agent, "_state_volume_id", return_value="vol-test"),
            patch.object(agent, "_preserve_state_volume") as preserve,
        ):
            plan = agent._evacuate_local({
                "type": "spot-termination",
                "checkpoint_only": True,
                "injected": True,
            })

        preserve.assert_not_called()
        self.assertTrue(plan["volume_preservation"]["skipped"])
        self.assertEqual(plan["evacuated_ok"], 4)
        self.assertTrue(updates)
        self.assertTrue(all(
            item["recovery_checkpoint_only"] for item in updates
        ))

    def test_future_notice_time_becomes_hard_deadline(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(seconds=90)
        termination, checkpoint = agent._signal_deadline({
            "time": future.isoformat(),
        })
        self.assertAlmostEqual(
            termination.timestamp(), future.timestamp(), delta=1
        )
        self.assertAlmostEqual(
            (termination - checkpoint).total_seconds(),
            agent.RECLAIM_COMMIT_RESERVE_S,
            delta=0.1,
        )

    def test_standby_does_not_misidentify_root_disk_as_state_volume(
        self,
    ) -> None:
        with (
            patch.object(
                agent, "_configured_recovery_role", return_value="standby"
            ),
            patch.object(agent, "STATE_VOLUME_ID_OVERRIDE", ""),
            patch.object(
                agent,
                "_findmnt_field",
                side_effect=lambda target, field: {
                    (agent.SBX_BASE, "SOURCE"):
                        "/dev/nvme0n1p1[/var/lib/sbx]",
                    (agent.SBX_BASE, "MAJ:MIN"): "259:1",
                    (agent.ROOTFS_DIR, "MAJ:MIN"): "259:1",
                }.get((target, field), ""),
            ),
            patch.object(agent.subprocess, "run") as run,
        ):
            self.assertEqual(agent._state_volume_id(), "")
            self.assertEqual(agent._recovery_role(), "standby")
            run.assert_not_called()

            agent._STATE_VOLUME_CACHE = "vol-recovered"
            self.assertEqual(agent._state_volume_id(), "vol-recovered")
            self.assertEqual(agent._recovery_role(), "active")

    def test_recovered_standby_rediscovers_mounted_ebs_after_restart(
        self,
    ) -> None:
        def fake_run(command: list[str], **_kwargs) -> CompletedProcess:
            self.assertEqual(
                command,
                ["lsblk", "-ndo", "SERIAL", "/dev/nvme1n1"],
            )
            return CompletedProcess(
                command,
                0,
                stdout="vol08a9739e3a46723dd\n",
                stderr="",
            )

        with (
            patch.object(
                agent, "_configured_recovery_role", return_value="standby"
            ),
            patch.object(agent, "STATE_VOLUME_ID_OVERRIDE", ""),
            patch.object(
                agent,
                "_findmnt_field",
                side_effect=lambda target, field: {
                    (agent.SBX_BASE, "SOURCE"): "/dev/nvme1n1",
                    (agent.SBX_BASE, "MAJ:MIN"): "259:4",
                    (agent.ROOTFS_DIR, "MAJ:MIN"): "259:1",
                }.get((target, field), ""),
            ),
            patch.object(agent.os.path, "realpath", side_effect=lambda path: path),
            patch.object(agent.subprocess, "run", side_effect=fake_run),
        ):
            self.assertEqual(
                agent._state_volume_id(),
                "vol-08a9739e3a46723dd",
            )
            self.assertEqual(agent._recovery_role(), "active")

    def test_recovery_identity_comes_from_kubernetes_node_labels(self) -> None:
        with patch.object(
            agent,
            "_fetch_node_labels",
            return_value={
                "sandbox.memorion.ai/recovery-role": "standby",
                "sandbox.memorion.ai/recovery-group": "standby-us-east-1a",
            },
        ):
            role, group, resolved = agent._node_recovery_identity(force=True)
        self.assertTrue(resolved)
        self.assertEqual(role, "standby")
        self.assertEqual(group, "standby-us-east-1a")

    def test_recovery_identity_keeps_last_good_value_on_api_failure(
        self,
    ) -> None:
        agent._NODE_RECOVERY_IDENTITY_CACHE.update({
            "role": "standby",
            "group": "standby-us-east-1a",
            "resolved": True,
            "fetched_at": time.monotonic() - 120,
        })
        with patch.object(
            agent,
            "_fetch_node_labels",
            side_effect=RuntimeError("apiserver unavailable"),
        ):
            role, group, resolved = agent._node_recovery_identity(force=True)
        self.assertTrue(resolved)
        self.assertEqual(role, "standby")
        self.assertEqual(group, "standby-us-east-1a")

    def test_cloud_init_modules_final_gates_node_registration(self) -> None:
        status_path = Path(_TMP.name) / "cloud-init-status.json"
        status_path.write_text(json.dumps({
            "v1": {
                "init": {"finished": 1, "errors": []},
                "modules-config": {"finished": 2, "errors": []},
                "modules-final": {"finished": 3, "errors": []},
            }
        }))
        with (
            patch.object(agent, "NODE_BOOTSTRAP_MARKER", ""),
            patch.object(
                agent, "CLOUD_INIT_STATUS_PATH", str(status_path)
            ),
            patch.object(agent, "NODE_STABILITY_MIN_AGE_S", 0),
        ):
            self.assertTrue(agent._node_bootstrap_ready())
            status_path.write_text(json.dumps({
                "v1": {
                    "modules-final": {
                        "finished": None,
                        "errors": [],
                    }
                }
            }))
            self.assertFalse(agent._node_bootstrap_ready())

    def test_fresh_node_waits_for_stability_window(self) -> None:
        status_path = Path(_TMP.name) / "cloud-init-stable-status.json"
        status_path.write_text(json.dumps({
            "v1": {
                "modules-final": {"finished": 1, "errors": []},
            }
        }))
        fresh = datetime.now(timezone.utc) - timedelta(seconds=60)
        stable = datetime.now(timezone.utc) - timedelta(seconds=181)
        with (
            patch.object(agent, "NODE_BOOTSTRAP_MARKER", ""),
            patch.object(
                agent, "CLOUD_INIT_STATUS_PATH", str(status_path)
            ),
            patch.object(agent, "NODE_STABILITY_MIN_AGE_S", 180),
            patch.object(
                agent,
                "_fetch_node_object",
                return_value={
                    "metadata": {
                        "creationTimestamp": fresh.isoformat(),
                    }
                },
            ),
        ):
            self.assertFalse(agent._node_bootstrap_ready())

        with (
            patch.object(agent, "NODE_BOOTSTRAP_MARKER", ""),
            patch.object(
                agent, "CLOUD_INIT_STATUS_PATH", str(status_path)
            ),
            patch.object(agent, "NODE_STABILITY_MIN_AGE_S", 180),
            patch.object(
                agent,
                "_fetch_node_object",
                return_value={
                    "metadata": {
                        "creationTimestamp": stable.isoformat(),
                    }
                },
            ),
        ):
            self.assertTrue(agent._node_bootstrap_ready())


if __name__ == "__main__":
    unittest.main(verbosity=2)
