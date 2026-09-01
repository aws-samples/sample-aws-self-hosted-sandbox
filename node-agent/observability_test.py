from __future__ import annotations

import importlib.util
import os
import pathlib
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
