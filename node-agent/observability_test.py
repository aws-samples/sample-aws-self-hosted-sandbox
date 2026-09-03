from __future__ import annotations

import importlib.util
import hashlib
import hmac
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


class TestNodeAgentObservability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["SBX_BASE"] = cls.tmp.name
        os.environ["FC_BIN"] = os.path.join(cls.tmp.name, "firecracker")
        pathlib.Path(os.environ["FC_BIN"]).touch(mode=0o755)

        spec = importlib.util.spec_from_file_location(
            "node_agent_main", HERE / "main.py"
        )
        cls.main = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.main)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_livez_and_readyz(self):
        code, body = self.main.health_report(require_dependencies=False)
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")

        self.main._HEARTBEAT_LAST_SUCCESS = time.monotonic()
        with patch.object(self.main.os.path, "exists", return_value=True), \
             patch.object(self.main.os.path, "isfile", return_value=True), \
             patch.object(self.main.os, "access", return_value=True):
            code, body = self.main.health_report(require_dependencies=True)
        self.assertEqual(code, 200)
        self.assertTrue(body["checks"]["heartbeat"])

        self.main._HEARTBEAT_LAST_ITERATION = (
            time.monotonic() - self.main.HEARTBEAT_EVERY_S * 4
        )
        code, body = self.main.health_report(require_dependencies=False)
        self.assertEqual(code, 503)
        self.assertFalse(body["checks"]["heartbeat_loop"])
        self.main._HEARTBEAT_LAST_ITERATION = time.monotonic()

    def test_node_agent_hmac_auth_rejects_replay_and_tampering(self):
        secret = "a" * 48
        now = 1_700_000_000
        body = b'{"id":"sbx-1"}'
        nonce = "nonce-0123456789abcdef"
        timestamp = str(now)
        content_hash = hashlib.sha256(body).hexdigest()
        signature = hmac.new(
            secret.encode(),
            self.main._auth_canonical_request(
                "POST",
                "/vm/destroy",
                timestamp,
                nonce,
                content_hash,
            ),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-SBX-Auth-Version": "v1",
            "X-SBX-Timestamp": timestamp,
            "X-SBX-Nonce": nonce,
            "X-SBX-Content-SHA256": content_hash,
            "X-SBX-Signature": signature,
        }
        self.main._AUTH_NONCES.clear()
        with (
            patch.object(
                self.main, "NODE_AGENT_AUTH_SECRET", secret
            ),
            patch.object(
                self.main, "NODE_AGENT_AUTH_REQUIRED", True
            ),
        ):
            allowed, reason = self.main._verify_node_agent_auth(
                "POST",
                "/vm/destroy",
                body,
                headers,
                now=now,
            )
            self.assertTrue(allowed, reason)
            replayed, replay_reason = self.main._verify_node_agent_auth(
                "POST",
                "/vm/destroy",
                body,
                headers,
                now=now,
            )
            self.assertFalse(replayed)
            self.assertIn("already used", replay_reason)

            self.main._AUTH_NONCES.clear()
            tampered, tamper_reason = self.main._verify_node_agent_auth(
                "POST",
                "/vm/destroy",
                b'{"id":"other"}',
                headers,
                now=now,
            )
            self.assertFalse(tampered)
            self.assertIn("hash mismatch", tamper_reason)

    def test_host_systemd_runtime_is_independent_from_agent_process(self):
        sandbox_id = "sbx-host-runtime"
        sandbox_dir = pathlib.Path(self.tmp.name, sandbox_id)
        sandbox_dir.mkdir()
        calls = []

        def fake_host_control(executable, args, *, timeout):
            calls.append((executable, list(args), timeout))
            return {
                "unit": f"sbx-vmm-{sandbox_id}.service",
                "pid": 4321,
                "socket": f"/srv/jailer/firecracker/{sandbox_id}/root/run/api.sock",
            }

        with (
            patch.object(
                self.main, "VMM_LAUNCH_MODE", "host-systemd"
            ),
            patch.object(self.main, "VMM_USE_JAILER", True),
            patch.object(
                self.main, "_host_control", side_effect=fake_host_control
            ),
        ):
            pid, socket_path, unit = self.main._launch_vmm(
                sandbox_id,
                "api.sock",
                2048,
            )

        self.assertEqual(pid, 4321)
        self.assertEqual(
            unit, f"sbx-vmm-{sandbox_id}.service"
        )
        self.assertIn("/srv/jailer/", socket_path)
        self.assertIn("--memory-mib", calls[0][1])
        self.assertIn("3072", calls[0][1])
        self.assertIn("--jailer", calls[0][1])
        self.assertIn("1", calls[0][1])

    def test_host_runtime_rollout_still_stops_legacy_child_pid(self):
        with (
            patch.object(
                self.main, "VMM_LAUNCH_MODE", "host-systemd"
            ),
            patch.object(self.main.os, "kill") as kill,
            patch.object(self.main, "_host_control") as host_control,
        ):
            self.main._stop_vmm(
                "legacy-child",
                {"pid": 1234, "runtime_unit": ""},
            )
        kill.assert_called_once_with(1234, self.main.signal.SIGTERM)
        host_control.assert_not_called()

    def test_host_runtime_stop_failure_preserves_jail_directory(self):
        fake_bin = pathlib.Path(self.tmp.name, "fake-bin")
        fake_bin.mkdir(exist_ok=True)
        fake_systemctl = fake_bin / "systemctl"
        fake_systemctl.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == \"is-active\" ]]; then exit 0; fi\n"
            "if [[ \"$1\" == \"stop\" ]]; then exit 1; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_systemctl.chmod(0o755)
        jailer_base = pathlib.Path(self.tmp.name, "jailer")
        jail_dir = jailer_base / "firecracker" / "stop-failure"
        jail_dir.mkdir(parents=True)
        helper = HERE.parent / "scripts" / "sbx-vmm-runtime"
        env = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "JAILER_BASE": str(jailer_base),
            "FC_BIN": "/usr/local/bin/firecracker",
        }

        completed = subprocess.run(
            [str(helper), "stop", "--id", "stop-failure"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("failed to stop systemd unit", completed.stderr)
        self.assertTrue(jail_dir.is_dir())

    def test_host_runtime_rejects_unpatched_jailer_version(self):
        fake_bin = pathlib.Path(self.tmp.name, "old-runtime-bin")
        fake_bin.mkdir(exist_ok=True)
        for command in ("systemd-run", "systemctl", "mount"):
            executable = fake_bin / command
            executable.write_text(
                "#!/usr/bin/env bash\nexit 0\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
        firecracker = fake_bin / "firecracker"
        firecracker.write_text(
            "#!/usr/bin/env bash\necho 'Firecracker v1.13.1'\n",
            encoding="utf-8",
        )
        firecracker.chmod(0o755)
        jailer = fake_bin / "jailer"
        jailer.write_text(
            "#!/usr/bin/env bash\necho 'Jailer v1.13.1'\n",
            encoding="utf-8",
        )
        jailer.chmod(0o755)
        helper = HERE.parent / "scripts" / "sbx-vmm-runtime"

        completed = subprocess.run(
            [str(helper), "check"],
            env={
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "FC_BIN": str(firecracker),
                "JAILER_BIN": str(jailer),
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("below required v1.14.1", completed.stderr)

    def test_restart_recovers_host_runtime_socket_without_old_symlink(self):
        sandbox_id = "sbx-host-recover"
        sandbox_dir = pathlib.Path(self.tmp.name, sandbox_id)
        sandbox_dir.mkdir()
        runtime_socket = pathlib.Path(
            self.tmp.name,
            "jailer",
            sandbox_id,
            "root",
            "run",
            "api.sock",
        )
        runtime_socket.parent.mkdir(parents=True)
        runtime_socket.touch()
        unit = f"sbx-vmm-{sandbox_id}.service"
        self.main._persist_runtime_metadata(sandbox_id, {
            "state": "running",
            "tap": "fctap23",
            "tap_idx": 23,
            "ip": "172.18.23.2",
            "runtime_unit": unit,
            "owned_dirs": [str(sandbox_dir)],
        })

        try:
            with (
                patch.object(
                    self.main,
                    "_runtime_status",
                    return_value={
                        "active": True,
                        "pid": 4321,
                        "unit": unit,
                        "socket": str(runtime_socket),
                    },
                ),
                patch.object(self.main, "_wait_sock", return_value=True),
                patch.object(
                    self.main,
                    "_fc",
                    return_value={"state": "Running"},
                ),
                patch.object(
                    self.main.os,
                    "listdir",
                    return_value=[sandbox_id],
                ),
            ):
                recovered = self.main._recover_vms()

            self.assertEqual(recovered, 1)
            self.assertEqual(
                self.main._VMS[sandbox_id]["runtime_unit"],
                unit,
            )
            self.assertEqual(
                self.main._VMS[sandbox_id]["sock"],
                str(runtime_socket),
            )
            self.assertEqual(
                self.main._VMS[sandbox_id]["tap"],
                "fctap23",
            )
            self.assertEqual(
                self.main._VMS[sandbox_id]["tap_idx"],
                23,
            )
            self.assertEqual(
                self.main._VMS[sandbox_id]["ip"],
                "172.18.23.2",
            )
            self.assertTrue((sandbox_dir / "api.sock").is_symlink())
        finally:
            self.main._VMS.pop(sandbox_id, None)

    def test_metrics_and_routes_do_not_expose_sandbox_ids(self):
        self.main._VMS["private-id"] = {"state": "running"}
        self.main._refresh_metrics()
        payload, _ = self.main.metrics_payload()
        metrics = payload.decode()
        self.assertIn("fcnode_scratch_free_bytes", metrics)
        self.assertIn('fcnode_scratch_bytes{kind="total"', metrics)
        self.assertNotIn("private-id", metrics)

        from observability import normalize_route
        self.assertEqual(normalize_route("/vm/private-id"), "/vm/{id}")
        self.assertEqual(
            normalize_route("/proxy/private-id/8080/private/path?token=secret"),
            "/proxy/{id}/{port}/{path}",
        )

    def test_snapshot_integrity_manifest_detects_corruption(self):
        snap_dir = pathlib.Path(self.tmp.name) / "snapshot-test"
        snap_dir.mkdir()
        (snap_dir / "vm.snapshot").write_bytes(b"snapshot-metadata")
        (snap_dir / "vm.mem").write_bytes(b"memory-state")

        manifest = self.main._write_snapshot_manifest(str(snap_dir))
        self.assertEqual(manifest["algorithm"], "sha256")
        self.main._record_snapshot_verification(str(snap_dir))

        (snap_dir / "vm.mem").write_bytes(b"memory-broken")
        with self.assertRaises(self.main.SnapshotIntegrityError):
            self.main._record_snapshot_verification(str(snap_dir))

        payload, _ = self.main.metrics_payload()
        metrics = payload.decode()
        self.assertIn('fc_snapshot_verify_total{result="success"}', metrics)
        self.assertIn('fc_snapshot_verify_total{result="error"}', metrics)
        self.assertIn('fc_snapshot_errors_total{phase="verify"}', metrics)

    def test_snapshot_alert_series_have_zero_baselines(self):
        payload, _ = self.main.metrics_payload()
        metrics = payload.decode()

        self.assertIn('fc_snapshot_verify_total{result="error"} 0.0', metrics)
        self.assertIn('fc_snapshot_errors_total{phase="upload"} 0.0', metrics)
        self.assertIn('fc_snapshot_errors_total{phase="download"} 0.0', metrics)
        self.assertIn('fc_snapshot_errors_total{phase="verify"} 0.0', metrics)
        self.assertIn(
            'fc_snapshot_legacy_migrations_total{result="error"} 0.0', metrics
        )

    def test_legacy_snapshot_gets_manifest_before_restore(self):
        snap_dir = pathlib.Path(self.tmp.name) / "legacy-snapshot"
        snap_dir.mkdir()
        (snap_dir / "vm.snapshot").write_bytes(b"snapshot-metadata")
        (snap_dir / "vm.mem").write_bytes(b"memory-state")

        manifest = self.main._record_snapshot_verification(str(snap_dir))

        self.assertTrue((snap_dir / "integrity.json").is_file())
        self.assertIn("vm.snapshot", manifest["files"])
        metrics, _ = self.main.metrics_payload()
        self.assertIn(
            'fc_snapshot_legacy_migrations_total{result="success"}',
            metrics.decode(),
        )

    def test_base_snapshot_hash_is_cached_and_invalidated(self):
        snap_dir = pathlib.Path(self.tmp.name) / "cached-snapshot"
        snap_dir.mkdir()
        (snap_dir / "vm.snapshot").write_bytes(b"snapshot-metadata")
        (snap_dir / "vm.mem").write_bytes(b"memory-state")
        base = snap_dir / "vm.mem.base"
        base.write_bytes(b"base-memory")

        with patch.object(
            self.main, "_sha256_file", wraps=self.main._sha256_file
        ) as sha256:
            self.main._write_snapshot_manifest(str(snap_dir))
            self.main._write_snapshot_manifest(str(snap_dir))
            base_calls = [
                call for call in sha256.call_args_list
                if call.args[0] == str(base)
            ]
            self.assertEqual(len(base_calls), 1)

            base.write_bytes(b"changed-base-memory")
            self.main._write_snapshot_manifest(str(snap_dir))
            base_calls = [
                call for call in sha256.call_args_list
                if call.args[0] == str(base)
            ]
            self.assertEqual(len(base_calls), 2)

    def test_destroy_clears_base_snapshot_hash_cache(self):
        sandbox_dir = pathlib.Path(self.tmp.name) / "destroy-cache"
        base = sandbox_dir / "snap" / "vm.mem.base"
        self.main._BASE_HASH_CACHE[str(base)] = (
            (1, 2, 3, 4, 5), "cached-digest"
        )

        self.main._clear_base_hash_cache(str(sandbox_dir))

        self.assertNotIn(str(base), self.main._BASE_HASH_CACHE)

    def test_create_is_idempotent_for_existing_running_vm(self):
        self.main._VMS["already-running"] = {
            "state": "running",
            "ip": "172.18.9.2",
        }
        try:
            with patch.object(self.main.subprocess, "run") as run:
                result = self.main.op_create({
                    "id": "already-running",
                    "tap_idx": 9,
                    "cpu": 2,
                    "mem_mib": 512,
                })
            self.assertTrue(result["already_exists"])
            self.assertEqual(result["ip"], "172.18.9.2")
            run.assert_not_called()
        finally:
            self.main._VMS.pop("already-running", None)

    def test_resume_is_idempotent_for_existing_running_vm(self):
        self.main._VMS["already-resumed"] = {
            "state": "running",
            "ip": "172.18.10.2",
        }
        try:
            with patch.object(
                self.main, "_record_snapshot_verification"
            ) as verify:
                result = self.main.op_resume({
                    "id": "already-resumed",
                    "snapshot_local_path": "/must/not/be/read",
                    "rootfs_path": "/must/not/be/read/rootfs.ext4",
                    "tap_idx": 10,
                })
            verify.assert_not_called()
            self.assertTrue(result["already_exists"])
            self.assertEqual(result["ip"], "172.18.10.2")
            self.assertEqual(result["restore_mode"], "existing")
        finally:
            self.main._VMS.pop("already-resumed", None)

    def test_resume_accepts_existing_suspended_vm(self):
        self.main._VMS["normally-suspended"] = {
            "state": "suspended",
            "pid": None,
            "ip": "172.18.11.2",
        }
        try:
            self.assertIsNone(
                self.main._existing_resume_result("normally-suspended")
            )
        finally:
            self.main._VMS.pop("normally-suspended", None)

    def test_resume_rejects_other_existing_states(self):
        self.main._VMS["still-paused"] = {
            "state": "paused",
            "pid": 123,
        }
        try:
            with self.assertRaisesRegex(
                RuntimeError, "already exists in state paused"
            ):
                self.main._existing_resume_result("still-paused")
        finally:
            self.main._VMS.pop("still-paused", None)

    def test_warm_resume_transfers_source_ownership_and_destroy_cleans_it(self):
        source_dir = pathlib.Path(self.tmp.name) / "warm-source"
        real_dir = pathlib.Path(self.tmp.name) / "real-sandbox"
        (source_dir / "snap").mkdir(parents=True)
        real_dir.mkdir()
        self.main._VMS["warm-source"] = {
            "state": "suspended",
            "pid": None,
            "dir": str(source_dir),
        }

        self.main._register_resumed_vm(
            "real-sandbox",
            {
                "state": "running",
                "pid": None,
                "tap": "",
                "dir": str(real_dir),
            },
            str(source_dir / "snap"),
        )

        self.assertNotIn("warm-source", self.main._VMS)
        self.assertEqual(
            set(self.main._VMS["real-sandbox"]["owned_dirs"]),
            {str(source_dir), str(real_dir)},
        )

        # A subsequent normal suspend/resume uses real-sandbox/snap. It must
        # preserve ownership of the original warm source for final cleanup.
        (real_dir / "snap").mkdir()
        self.main._VMS["real-sandbox"]["state"] = "suspended"
        self.main._register_resumed_vm(
            "real-sandbox",
            {
                "state": "running",
                "pid": None,
                "tap": "",
                "dir": str(real_dir),
            },
            str(real_dir / "snap"),
        )
        self.assertEqual(
            set(self.main._VMS["real-sandbox"]["owned_dirs"]),
            {str(source_dir), str(real_dir)},
        )

        with patch.object(self.main, "_teardown_tap"):
            self.main.op_destroy({"id": "real-sandbox"})
        self.assertFalse(source_dir.exists())
        self.assertFalse(real_dir.exists())

    def test_warm_resume_rejects_active_snapshot_source(self):
        source_dir = pathlib.Path(self.tmp.name) / "warm-active"
        (source_dir / "snap").mkdir(parents=True)
        self.main._VMS["warm-active"] = {
            "state": "running",
            "pid": 123,
        }
        try:
            with self.assertRaisesRegex(
                RuntimeError, "snapshot source warm-active is still running"
            ):
                self.main._ensure_resume_source_available(
                    "real-active", str(source_dir / "snap")
                )
        finally:
            self.main._VMS.pop("warm-active", None)

    def test_destroy_removes_untracked_inactive_directory(self):
        sandbox_dir = pathlib.Path(self.tmp.name) / "untracked-stale"
        sandbox_dir.mkdir()
        (sandbox_dir / "stale-file").write_text("old")

        with patch.object(
            self.main, "_live_runtime_socket", return_value=None
        ):
            result = self.main.op_destroy({"id": "untracked-stale"})

        self.assertTrue(result["deleted"])
        self.assertFalse(sandbox_dir.exists())

    def test_concurrent_direct_destroy_is_serialized(self):
        sandbox_id = "concurrent-destroy"
        first_stop_started = threading.Event()
        release_first_stop = threading.Event()
        results = []
        self.main._VMS[sandbox_id] = {
            "state": "running",
            "pid": 123,
            "tap": "fctap42",
        }

        def slow_stop(_sid, _vm):
            first_stop_started.set()
            self.assertTrue(release_first_stop.wait(timeout=2))

        def destroy():
            results.append(self.main.op_destroy({"id": sandbox_id}))

        first = threading.Thread(target=destroy)
        second = threading.Thread(target=destroy)
        try:
            with (
                patch.object(
                    self.main, "_stop_vmm", side_effect=slow_stop
                ) as stop_vmm,
                patch.object(
                    self.main, "_teardown_tap"
                ) as teardown_tap,
                patch.object(
                    self.main, "_live_runtime_socket", return_value=None
                ),
            ):
                first.start()
                self.assertTrue(first_stop_started.wait(timeout=2))
                second.start()
                time.sleep(0.05)
                self.assertTrue(second.is_alive())
                release_first_stop.set()
                first.join(timeout=2)
                second.join(timeout=2)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(
                results,
                [{"deleted": True}, {"deleted": True}],
            )
            stop_vmm.assert_called_once()
            teardown_tap.assert_called_once_with("fctap42")
            self.assertNotIn(sandbox_id, self.main._VM_OP_LOCKS)
            self.assertNotIn(sandbox_id, self.main._VM_OP_LOCK_USERS)
        finally:
            release_first_stop.set()
            first.join(timeout=2)
            second.join(timeout=2)
            self.main._VMS.pop(sandbox_id, None)
            self.main._VM_OP_LOCKS.pop(sandbox_id, None)
            self.main._VM_OP_LOCK_USERS.pop(sandbox_id, None)

    def test_destroy_stops_untracked_host_runtime_before_cleanup(self):
        sandbox_id = "untracked-host"
        sandbox_dir = pathlib.Path(self.tmp.name) / sandbox_id
        sandbox_dir.mkdir()
        self.main._persist_runtime_metadata(sandbox_id, {
            "state": "running",
            "tap": "fctap41",
            "tap_idx": 41,
            "ip": "172.18.41.2",
            "runtime_unit": f"sbx-vmm-{sandbox_id}.service",
            "owned_dirs": [str(sandbox_dir)],
        })

        with (
            patch.object(
                self.main, "VMM_LAUNCH_MODE", "host-systemd"
            ),
            patch.object(
                self.main,
                "_runtime_status",
                return_value={
                    "active": True,
                    "pid": 4321,
                    "unit": f"sbx-vmm-{sandbox_id}.service",
                    "socket": (
                        "/srv/jailer/firecracker/untracked-host/"
                        "root/run/api.sock"
                    ),
                },
            ) as runtime_status,
            patch.object(self.main, "_host_control") as host_control,
            patch.object(self.main, "_teardown_tap") as teardown_tap,
        ):
            result = self.main.op_destroy({"id": sandbox_id})

        self.assertEqual(result, {"deleted": True})
        runtime_status.assert_called_once_with(sandbox_id, strict=True)
        host_control.assert_called_once_with(
            self.main.HOST_VMM_CTL,
            ["stop", "--id", sandbox_id],
            timeout=30,
        )
        teardown_tap.assert_called_once_with("fctap41")
        self.assertFalse(sandbox_dir.exists())

    def test_http_observability_endpoints(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.main.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/livez",
                headers={"X-Request-ID": "node-test-request"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    response.headers["X-Request-ID"], "node-test-request"
                )

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/metrics", timeout=5
            ) as response:
                metrics = response.read().decode()
            self.assertIn("fc_vms", metrics)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
