"""
node-agent 宿主执行面安全回归测试(V2401449830)。

对应报告 PoC(attack_test.py)的 S0-S6 场景,断言方向反转:未签名/伪造/越界的请求
必须在触达任何宿主副作用(aws s3 sync / cp / makedirs / ssh / Firecracker)之前被拒绝。
宿主副作用全部替换为记录器,不执行任何外部命令,所有文件写入限定在临时目录。

运行: cd node-agent && python3 -m unittest security_test
"""
from __future__ import annotations

import http.client
import http.server
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

KEY = "k" * 48
BUCKET = "snap-bucket"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# 控制面侧签名实现(sandbox-api/node_agent_auth.py),同时验证两端 canonical 一致
signer = _load("sbx_node_agent_auth", HERE.parent / "sandbox-api" / "node_agent_auth.py")


class _Completed:
    def __init__(self, args, returncode=0, stdout="", stderr=""):
        self.args, self.returncode, self.stdout, self.stderr = args, returncode, stdout, stderr


class _FakePopen:
    pid = 4242

    def __init__(self, args, *a, **k):
        self.args = args

    def kill(self):
        pass


class NodeAgentSecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.tmp.name).resolve()
        cls.sbx = root / "sbx"
        cls.opt = root / "opt"
        cls.opt.mkdir()
        (cls.opt / "vmlinux").write_bytes(b"kernel")
        (cls.opt / "rootfs.ext4").write_bytes(b"rootfs")
        env = {
            "SBX_BASE": str(cls.sbx),
            "FC_ROOTFS_DIR": str(cls.opt),
            "FC_ROOTFS": str(cls.opt / "rootfs.ext4"),
            "NODE_AGENT_AUTH_KEY": KEY,
            "ALLOWED_CALLER_CIDR": "127.0.0.0/8",
            "SNAPSHOT_S3_BUCKET": BUCKET,
        }
        cls._env = patch.dict(os.environ, env)
        cls._env.start()
        os.environ.pop("NODE_AGENT_ENABLE_TEST_HOOKS", None)
        os.environ.pop("NODE_AGENT_LISTEN_HOST", None)
        cls.main = _load("node_agent_main_security", HERE / "main.py")
        cls.real_vsock_uds = staticmethod(cls.main._vsock_uds_in_snapshot)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), cls.main.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._env.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.calls: list[tuple[str, list[str]]] = []
        main = self.main

        def fake_run(args, *a, **k):
            argv = [str(x) for x in (args if isinstance(args, (list, tuple)) else [args])]
            self.calls.append(("run", argv))
            return _Completed(argv, 0, "uid=0(root)\n" if argv[:1] == ["ssh"] else "")

        def fake_popen(args, *a, **k):
            self.calls.append(("popen", [str(x) for x in args]))
            return _FakePopen(args)

        def record(name, result=None):
            def _fn(*a, **k):
                self.calls.append((name, [str(x) for x in a]))
                return result
            return _fn

        for target, value in (
            ("_setup_tap", lambda idx: (f"fctap{idx}", f"172.18.{idx}.1", f"172.18.{idx}.2")),
            ("_wait_sock", lambda *a, **k: True),
            ("_fc", record("fc", {})),
            ("_vsock_exec", record("vsock_exec", {"rc": 0, "stdout": "", "stderr": ""})),
            ("_record_snapshot_verification", record("verify", {})),
            ("_merge_diff_into_base", record("merge")),
            ("_vsock_uds_in_snapshot", lambda p: []),
        ):
            p = patch.object(main, target, value)
            p.start()
            self.addCleanup(p.stop)
        for target, value in (("run", fake_run), ("Popen", fake_popen)):
            p = patch.object(main.subprocess, target, value)
            p.start()
            self.addCleanup(p.stop)
        main._VMS.clear()
        # 每个用例用干净的 SBX_BASE,避免正向用例创建的目录影响"零副作用"断言
        import shutil
        shutil.rmtree(self.sbx, ignore_errors=True)
        self.sbx.mkdir()

    # ---------- helpers ----------

    def call(self, method, path, body=None, signed=True, key=KEY.encode(),
             extra_headers=None, raw=None):
        data = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else b"")
        headers = {"Content-Type": "application/json"}
        if signed:
            headers.update(signer.signed_headers(method, path, data, key=key))
        headers.update(extra_headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=data or None, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
        finally:
            conn.close()
        try:
            return resp.status, json.loads(payload or b"null")
        except ValueError:
            return resp.status, payload

    def host_sinks(self):
        return [c for c in self.calls if c[0] in ("run", "popen", "fc", "vsock_exec", "merge")]

    def resume_body(self, **overrides):
        body = {
            "id": "attacker-chosen",
            "snapshot_local_path": f"{self.sbx}/attacker-chosen/snap",
            "rootfs_path": f"{self.sbx}/attacker-chosen/rootfs.ext4",
            "tap_idx": 7,
            "s3_prefix": f"s3://{BUCKET}/sbx/attacker-chosen/",
        }
        body.update(overrides)
        return body

    # ---------- S0:出厂配置 ----------

    def test_s0_listen_host_is_not_wildcard(self):
        src = (HERE / "main.py").read_text(encoding="utf-8")
        self.assertNotIn('"NODE_AGENT_LISTEN_HOST", "0.0.0.0"', src)
        with patch.object(self.main, "_advertise_ip", return_value="10.0.1.23"):
            self.assertEqual(self.main._listen_host(), "10.0.1.23")

    def test_s0_empty_allowlist_fails_closed(self):
        with patch.object(self.main, "ALLOWED_CALLER_CIDR", ""):
            self.assertFalse(self.main._check_caller_allowed("203.0.113.9"))
            self.assertFalse(self.main._check_caller_allowed("127.0.0.1"))
            status, _ = self.call("POST", "/vm/resume", self.resume_body())
        self.assertEqual(status, 403)
        self.assertEqual(self.host_sinks(), [])

    def test_s0_startup_refuses_without_key_or_allowlist(self):
        with patch.object(self.main, "_AUTH", self.main.agent_auth.Verifier(b"")), \
             patch.object(self.main, "_advertise_ip", return_value="10.0.1.23"):
            with self.assertRaises(SystemExit):
                self.main._startup_security_checks()
        with patch.object(self.main, "ALLOWED_CALLER_CIDR", ""), \
             patch.object(self.main, "_advertise_ip", return_value="10.0.1.23"):
            with self.assertRaises(SystemExit):
                self.main._startup_security_checks()
        with patch.object(self.main, "LISTEN_HOST", "0.0.0.0"):
            with self.assertRaises(SystemExit):
                self.main._startup_security_checks()

    # ---------- S1/S4/S5:未签名请求在任何宿主副作用之前被拒绝 ----------

    def test_s1_unsigned_resume_rejected_before_any_host_write(self):
        attack = {
            "id": "attacker-chosen",
            "snapshot_local_path": str(pathlib.Path(self.tmp.name) / "etc"),
            "rootfs_path": str(pathlib.Path(self.tmp.name) / "pwn" / "rootfs.ext4"),
            "tap_idx": 7,
            "s3_prefix": "s3://attacker-bucket/",
        }
        status, body = self.call("POST", "/vm/resume", attack, signed=False)
        self.assertEqual(status, 401)
        self.assertEqual(body, {"error": "unauthorized"})
        self.assertEqual(self.host_sinks(), [])
        self.assertFalse((pathlib.Path(self.tmp.name) / "etc").exists())

    def test_s4_s5_every_protected_route_requires_signature(self):
        self.main._VMS["victim-sbx"] = {"state": "running", "ip": "172.18.7.2",
                                        "dir": f"{self.sbx}/victim-sbx"}
        for method, path, body in (
            ("POST", "/vm/exec", {"id": "victim-sbx", "cmd": "id"}),
            ("POST", "/vm/create", {"id": "x1", "tap_idx": 3}),
            ("POST", "/vm/destroy", {"id": "victim-sbx"}),
            ("POST", "/vm/suspend", {"id": "victim-sbx"}),
            ("POST", "/vm/snapshot_base", {"id": "victim-sbx"}),
            ("POST", "/reclaim/simulate", {}),
            ("POST", "/reclaim/reset", {}),
            ("GET", "/reclaim/status", None),
            ("GET", "/vm/victim-sbx", None),
            ("GET", "/proxy/victim-sbx/8000/", None),
            ("PUT", "/proxy/victim-sbx/8000/x", {"a": 1}),
        ):
            with self.subTest(path=path):
                status, _ = self.call(method, path, body, signed=False)
                self.assertEqual(status, 401)
        self.assertEqual(self.host_sinks(), [])
        self.assertIn("victim-sbx", self.main._VMS)

    def test_forged_tampered_replayed_and_expired_signatures_rejected(self):
        body = {"id": "victim-sbx", "cmd": "id"}
        self.main._VMS["victim-sbx"] = {"state": "running", "ip": "172.18.7.2",
                                        "dir": f"{self.sbx}/victim-sbx"}
        status, _ = self.call("POST", "/vm/exec", body, key=b"x" * 48)
        self.assertEqual(status, 401, "wrong key")

        data = json.dumps(body).encode()
        headers = signer.signed_headers("POST", "/vm/exec", data, key=KEY.encode())
        tampered = json.dumps({"id": "victim-sbx", "cmd": "cat /etc/shadow"}).encode()
        status, _ = self.call("POST", "/vm/exec", signed=False, raw=tampered,
                              extra_headers=headers)
        self.assertEqual(status, 401, "body swapped under a valid signature")

        headers = signer.signed_headers("POST", "/vm/exec", data, key=KEY.encode())
        status, _ = self.call("POST", "/vm/exec", signed=False, raw=data, extra_headers=headers)
        self.assertEqual(status, 200)
        status, _ = self.call("POST", "/vm/exec", signed=False, raw=data, extra_headers=headers)
        self.assertEqual(status, 401, "replayed nonce")

        with patch.object(signer.time, "time", return_value=time.time() - 3600):
            old = signer.signed_headers("POST", "/vm/exec", data, key=KEY.encode())
        status, _ = self.call("POST", "/vm/exec", signed=False, raw=data, extra_headers=old)
        self.assertEqual(status, 401, "expired timestamp")

        headers = signer.signed_headers("POST", "/vm/exec", data, key=KEY.encode())
        status, _ = self.call("POST", "/vm/destroy", signed=False, raw=data,
                              extra_headers=headers)
        self.assertEqual(status, 401, "signature bound to a different route")

    # ---------- 来源网段:guest tap 范围对所有路由(含健康检查)拒绝 ----------

    def test_guest_tap_range_denied_even_for_health(self):
        self.assertTrue(self.main._caller_denied("172.18.7.2"))
        self.assertTrue(self.main._caller_denied("::ffff:172.18.1.2"))
        self.assertFalse(self.main._caller_denied("10.0.1.5"))
        with patch.object(self.main, "DENIED_CALLER_CIDR", "127.0.0.0/8"):
            status, _ = self.call("GET", "/health", signed=False)
            self.assertEqual(status, 403)
            status, _ = self.call("POST", "/vm/resume", self.resume_body())
            self.assertEqual(status, 403)
        self.assertEqual(self.host_sinks(), [])

    def test_health_and_metrics_stay_public(self):
        for path in ("/health", "/livez", "/metrics"):
            status, _ = self.call("GET", path, signed=False)
            self.assertIn(status, (200, 503), path)

    # ---------- S1/S3/S6:即使签名合法,越界路径 / 前缀 / id 也被拒绝 ----------

    def test_s1_s3_s6_out_of_convention_paths_rejected_with_valid_signature(self):
        sbx = str(self.sbx)
        cases = {
            "tmp etc": dict(snapshot_local_path=str(pathlib.Path(self.tmp.name) / "etc")),
            "raw /etc": dict(snapshot_local_path="/etc"),
            "proc root": dict(snapshot_local_path="/proc/1/root/etc"),
            "opt sbx": dict(snapshot_local_path="/opt/sbx"),
            "victim dir": dict(snapshot_local_path=f"{sbx}/victim-sbx"),
            "dotdot": dict(snapshot_local_path=f"{sbx}/attacker-chosen/../../etc/snap"),
            "relative": dict(snapshot_local_path="attacker-chosen/snap"),
            "rootfs elsewhere": dict(rootfs_path="/opt/sbx/rootfs.ext4"),
            "rootfs other owner": dict(rootfs_path=f"{sbx}/victim-sbx/rootfs.ext4"),
            "attacker bucket": dict(s3_prefix="s3://attacker-bucket/"),
            "foreign prefix": dict(s3_prefix=f"s3://{BUCKET}/sbx/victim-sbx/"),
            "prefix traversal": dict(s3_prefix=f"s3://{BUCKET}/sbx/attacker-chosen/../victim/"),
            "bad id": dict(id="../../etc"),
            "tap negative": dict(tap_idx=-1),
            "tap not int": dict(tap_idx="7; reboot"),
            "id trailing newline": dict(id="attacker-chosen\n"),
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                status, body = self.call("POST", "/vm/resume", self.resume_body(**override))
                self.assertEqual(status, 400, body)
        self.assertEqual(self.host_sinks(), [])
        self.assertFalse(any(self.sbx.glob("attacker-chosen*")))

    def test_symlinked_sandbox_dir_rejected(self):
        outside = pathlib.Path(self.tmp.name) / "outside"
        outside.mkdir(exist_ok=True)
        self.sbx.mkdir(exist_ok=True)
        link = self.sbx / "linked-sbx"
        if not link.exists():
            link.symlink_to(outside, target_is_directory=True)
        status, body = self.call("POST", "/vm/resume", self.resume_body(
            id="linked-sbx",
            snapshot_local_path=f"{self.sbx}/linked-sbx/snap",
            rootfs_path=f"{self.sbx}/linked-sbx/rootfs.ext4",
            s3_prefix=f"s3://{BUCKET}/sbx/linked-sbx/",
        ))
        self.assertEqual(status, 400)
        self.assertEqual(body.get("field"), "snapshot_local_path")
        self.assertEqual(self.host_sinks(), [])

    def test_destroy_and_create_validate_id_and_kernel(self):
        for body in ({"id": ".."}, {"id": "a/b"}, {"id": "x" * 65}):
            status, _ = self.call("POST", "/vm/destroy", body)
            self.assertEqual(status, 400, body)
        for kernel in ("/etc/shadow", f"{self.opt}/../vmlinux", f"{self.opt}/rootfs.ext4"):
            status, _ = self.call("POST", "/vm/create",
                                  {"id": "newbox", "tap_idx": 3, "kernel": kernel})
            self.assertEqual(status, 400, kernel)
        status, _ = self.call("POST", "/vm/create",
                              {"id": "newbox", "tap_idx": 3, "rootfs_path": "/etc/passwd"})
        self.assertEqual(status, 400)
        self.assertEqual(self.host_sinks(), [])

    # ---------- 正向对照:控制面按约定发出的合法请求仍然工作 ----------

    def test_valid_signed_resume_follows_server_side_convention(self):
        body = self.resume_body()
        status, resp = self.call("POST", "/vm/resume", body)
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["restore_mode"], "s3")
        sync = [c[1] for c in self.calls if c[0] == "run" and c[1][:3] == ["aws", "s3", "sync"]]
        self.assertEqual(len(sync), 1)
        argv = sync[0]
        self.assertEqual(argv[3:5], [body["s3_prefix"], body["snapshot_local_path"]])
        self.assertEqual(argv[5:7], ["--exclude", "*"])
        self.assertIn("vm.snapshot", argv)
        self.assertIn("integrity.json", argv)
        self.assertIn("attacker-chosen", self.main._VMS)

    def test_warm_pool_claim_resume_still_allowed(self):
        status, resp = self.call("POST", "/vm/resume", self.resume_body(
            id="real0001",
            snapshot_local_path=f"{self.sbx}/warm-abcd1234/snap",
            rootfs_path=f"{self.sbx}/warm-abcd1234/rootfs.ext4",
            s3_prefix=f"s3://{BUCKET}/sbx/warm-abcd1234/",
        ))
        self.assertEqual(status, 200, resp)

    # ---------- S5:测试钩子生产关闭 ----------

    def test_s5_reclaim_test_hooks_disabled_by_default(self):
        with patch.object(self.main, "_evacuate_local") as evacuate:
            status, _ = self.call("POST", "/reclaim/simulate", {"type": "spot-termination"})
            self.assertEqual(status, 404)
            status, _ = self.call("POST", "/reclaim/reset", {})
            self.assertEqual(status, 404)
            evacuate.assert_not_called()
        with patch.object(self.main, "ENABLE_TEST_HOOKS", True), \
             patch.object(self.main, "_evacuate_local", return_value={"plan": []}):
            status, _ = self.call("POST", "/reclaim/simulate", {})
            self.assertEqual(status, 200)
            status, _ = self.call("POST", "/reclaim/simulate", {}, signed=False)
            self.assertEqual(status, 401)

    # ---------- 反代:签名头不泄露给 guest ----------

    def test_proxy_does_not_forward_agent_auth_headers_to_guest(self):
        seen: dict = {}

        class Guest(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.update({k.lower(): v for k, v in self.headers.items()})
                payload = b"guest-ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        guest = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Guest)
        threading.Thread(target=guest.serve_forever, daemon=True).start()
        try:
            self.main._VMS["webbox"] = {"state": "running", "ip": "127.0.0.1"}
            status, payload = self.call("GET", f"/proxy/webbox/{guest.server_address[1]}/x?q=1",
                                        extra_headers={"X-App": "1"})
        finally:
            guest.shutdown()
            guest.server_close()
        self.assertEqual(status, 200)
        self.assertEqual(payload, b"guest-ok")
        self.assertEqual(seen.get("x-app"), "1")
        self.assertFalse(any(k.startswith("x-sbx-agent-") for k in seen), seen)

    # ---------- 宿主防火墙规则 ----------

    def test_guest_firewall_rules(self):
        restore_inputs: list[str] = []
        # 模拟 kube-proxy 已把自己的规则插在链首、我们的跳转在第 3 条的场景
        chains = {
            "INPUT": ["-P INPUT ACCEPT", "-A INPUT -j KUBE-FIREWALL",
                      "-A INPUT -i fctap+ -j SBX-GUEST-IN", "-A INPUT -i fctap+ -j SBX-GUEST-IN"],
            "FORWARD": ["-P FORWARD ACCEPT", "-A FORWARD -j KUBE-FORWARD"],
        }

        def fake_run(args, *a, **k):
            argv = [str(x) for x in args]
            self.calls.append(("run", argv))
            if argv[0] == "iptables-restore":
                restore_inputs.append(k.get("input", ""))
            if argv[:2] == ["iptables", "-S"]:
                return _Completed(argv, 0, "\n".join(chains[argv[2]]) + "\n")
            return _Completed(argv, 1 if "-C" in argv else 0)

        with patch.object(self.main.subprocess, "run", fake_run):
            self.main._install_guest_firewall()
        self.assertEqual(len(restore_inputs), 1)
        ruleset = restore_inputs[0].splitlines()
        port = str(self.main.LISTEN_PORT)
        self.assertEqual(ruleset[:3], ["*filter", ":SBX-GUEST-IN - [0:0]", ":SBX-GUEST-FWD - [0:0]"])
        self.assertEqual(ruleset[-1], "COMMIT")
        self.assertIn("-A SBX-GUEST-IN -j DROP", ruleset)
        self.assertIn("-A SBX-GUEST-FWD -o fctap+ -j DROP", ruleset)
        self.assertIn("-A SBX-GUEST-FWD -d 169.254.169.254/32 -j DROP", ruleset)
        self.assertIn(f"-A SBX-GUEST-FWD -p tcp --dport {port} -j DROP", ruleset)
        self.assertIn('--log-prefix "sbx-guest-agent-probe "', restore_inputs[0])
        self.assertLess(
            ruleset.index("-A SBX-GUEST-IN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"),
            ruleset.index("-A SBX-GUEST-IN -j DROP"))
        cmds = [" ".join(c[1]) for c in self.calls if c[1][0] == "iptables"]
        # 跳转被重新插到第 1 位,再按行号倒序删掉原来的两条副本(插入后它们变成第 3、4 条)
        self.assertIn("iptables -I INPUT 1 -i fctap+ -j SBX-GUEST-IN", cmds)
        self.assertLess(cmds.index("iptables -I INPUT 1 -i fctap+ -j SBX-GUEST-IN"),
                        cmds.index("iptables -D INPUT 4"))
        self.assertLess(cmds.index("iptables -D INPUT 4"), cmds.index("iptables -D INPUT 3"))
        self.assertIn("iptables -I FORWARD 1 -i fctap+ -j SBX-GUEST-FWD", cmds)
        self.assertFalse(any(c.startswith("iptables -D FORWARD") for c in cmds))
        self.assertFalse(any(c.startswith("iptables -F") for c in cmds), "no non-atomic flush")

    def test_jump_already_first_is_left_alone(self):
        def fake_run(args, *a, **k):
            argv = [str(x) for x in args]
            self.calls.append(("run", argv))
            chain = argv[2] if argv[:2] == ["iptables", "-S"] else ""
            target = "SBX-GUEST-IN" if chain == "INPUT" else "SBX-GUEST-FWD"
            return _Completed(argv, 0, f"-P {chain} ACCEPT\n-A {chain} -i fctap+ -j {target}\n"
                                       f"-A {chain} -j KUBE-X\n")

        with patch.object(self.main.subprocess, "run", fake_run):
            self.main.ensure_guest_firewall_jumps()
        mutations = [c[1] for c in self.calls if c[1][:2] in (["iptables", "-I"], ["iptables", "-D"])]
        self.assertEqual(mutations, [])

    def test_malformed_auth_headers_get_401_not_crash(self):
        data = b"{}"
        headers = signer.signed_headers("POST", "/vm/exec", data, key=KEY.encode())
        for field, value in (("X-Sbx-Agent-Timestamp", "\u00b2"),
                             ("X-Sbx-Agent-Timestamp", headers["X-Sbx-Agent-Timestamp"] + "\n"),
                             ("X-Sbx-Agent-Nonce", "short")):
            with self.subTest(field=field, value=value):
                bad = dict(headers, **{field: value})
                # http.client 拒绝发送含换行/非 latin-1 的头,直接调用校验器
                with self.assertRaises(self.main.agent_auth.AuthError) as ctx:
                    self.main._AUTH.verify_headers(bad, "POST", "/vm/exec")
                self.assertEqual(ctx.exception.reason, "malformed")

    def test_large_tap_idx_still_accepted(self):
        # 控制面 alloc_tap_idx 只增不减:不能因 tap_idx > 255 拒绝 create/resume(回归保护)
        status, resp = self.call("POST", "/vm/resume", self.resume_body(tap_idx=300))
        self.assertEqual(status, 200, resp)

    def test_vsock_paths_from_snapshot_are_validated(self):
        snap = pathlib.Path(self.tmp.name) / "evil.snapshot"
        base = str(self.sbx).encode()
        snap.write_bytes(b"\x00" + base + b"/../v.sock\x00" + base + b"/warm-abc12345/v.sock\x00"
                         + base + b"/./v.sock")
        # setUp 把 _vsock_uds_in_snapshot 换成了桩,这里用加载时保存的真实实现
        found = self.real_vsock_uds(str(snap))
        self.assertEqual(found, [f"{self.sbx}/warm-abc12345/v.sock"])

    def test_metric_routes_are_bounded(self):
        normalize = self.main.normalize_route
        self.assertEqual(normalize("/x123"), "/other")
        self.assertEqual(normalize("/vm/abc/extra"), "/other")
        self.assertEqual(normalize("/vm/create"), "/vm/create")
        self.assertEqual(normalize("/vm/abc"), "/vm/{id}")
        self.assertEqual(normalize("/reclaim/status"), "/reclaim/status")
        from observability import normalize_method
        self.assertEqual(normalize_method("FOO"), "OTHER")

    def test_rejections_are_counted_for_alerting(self):
        self.call("POST", "/vm/exec", {"id": "a"}, signed=False)
        self.call("POST", "/vm/resume", self.resume_body(snapshot_local_path="/etc"))
        payload, _ = self.main.metrics_payload()
        metrics = payload.decode()
        self.assertRegex(metrics, r'node_agent_security_events_total\{kind="auth_denied",reason="missing"\} [1-9]')
        self.assertRegex(metrics, r'node_agent_security_events_total\{kind="invalid_request",reason="path_outside_sbx_base"\} [1-9]')


if __name__ == "__main__":
    unittest.main(verbosity=2)
