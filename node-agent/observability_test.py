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
