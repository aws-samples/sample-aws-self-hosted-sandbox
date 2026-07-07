#!/usr/bin/env python3
"""
控制面冒烟测试 — 本地无 AWS 账号、无 EKS、无 .metal 节点即可跑。

覆盖:
  1. DynamoDB 层 (db.py) — CRUD / lease / tap_idx / 幂等键 / 事件
  2. FirecrackerDriver — 用内嵌 mock node-agent HTTP server
  3. 统一 API (app.py) — create / get / wait / suspend / resume / destroy / exec
  4. Warm Pool — replenish + claim 路径
  5. Capability 模型 — Kata driver 的 suspend 返回 501

所有 DynamoDB 操作通过 moto 在内存中 mock。
node-agent 用 threading.Thread 起一个最小 stub HTTP server。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

# ---------- moto 必须在 boto3 import 前 patch ----------
import boto3
from moto import mock_aws

# moto 가 localhost HTTP 를 차단하지 않도록 passthrough 설정
os.environ.setdefault("MOTO_ALLOW_NONEXISTENT_REGION", "true")
# responses 라이브러리가 localhost passthrough 허용
os.environ.setdefault("RESPONSES_PASSTHROUGH_PREFIXES", "http://127.0.0.1,http://localhost")

# 路径设置:项目根有 sandbox_api -> sandbox-api/ 软链接
# 把项目根加进 sys.path 即可 import sandbox_api.*
_HERE = os.path.dirname(os.path.abspath(__file__))  # sandbox-api/
_ROOT = os.path.dirname(_HERE)                       # 项目根
sys.path.insert(0, _ROOT)
os.environ.update({
    "AWS_DEFAULT_REGION":        "us-east-1",
    "AWS_ACCESS_KEY_ID":         "test",
    "AWS_SECRET_ACCESS_KEY":     "test",
    "DYNAMODB_TABLE":            "sandboxes",
    "DYNAMODB_EVENTS_TABLE":     "sandbox_events",
    "DYNAMODB_TAPIDX_TABLE":     "sandbox_tap_idx",
    "DYNAMODB_NODES_TABLE":      "sandbox_nodes",
    "DYNAMODB_LOCKS_TABLE":      "sandbox_locks",
    "SANDBOX_DRIVER":            "firecracker",
    "FC_KERNEL_PATH":            "/fake/vmlinux",
    "SNAPSHOT_S3_BUCKET":        "test-bucket",
    "WARM_POOL_SIZE":            "2",
    "WARM_POOL_REFILL_S":        "9999",  # 禁止自动 refill 干扰测试
    "ALLOW_UNAUTHENTICATED":     "1",     # 测试环境跳过认证
})


# ────────────────────────────────────────────────
# Mock node-agent stub
# ────────────────────────────────────────────────

class _AgentStub(BaseHTTPRequestHandler):
    """最小 node-agent stub:记录调用、返回合理假数据。"""
    calls: list[tuple[str, str, dict]] = []

    def log_message(self, *_): pass

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        b = self._body()
        _AgentStub.calls.append(("POST", self.path, b))
        sid = b.get("id", "unknown")
        if self.path == "/vm/create":
            return self._send(200, {"state": "running", "ip": "172.18.1.2"})
        if self.path == "/vm/destroy":
            return self._send(200, {"deleted": True})
        if self.path == "/vm/suspend":
            return self._send(200, {
                "snapshot_create_time_s": 0.012,
                "mem_file_bytes": 1024,
            })
        if self.path == "/vm/resume":
            return self._send(200, {"restore_time_s": 0.007, "ip": "172.18.1.2"})
        if self.path == "/vm/exec":
            return self._send(200, {"rc": 0, "stdout": "hello", "stderr": ""})
        self._send(404, {"error": "not found"})

    def do_GET(self):
        _AgentStub.calls.append(("GET", self.path, {}))
        if self.path == "/health":
            return self._send(200, {"node_id": "mock-node", "free_mem_mib": 90000, "vm_count": 0})
        if self.path.startswith("/vm/"):
            return self._send(200, {"state": "running", "ip": "172.18.1.2", "pid": 12345})
        self._send(404, {"error": "not found"})


def _start_stub_agent() -> tuple[HTTPServer, int]:
    server = HTTPServer(("127.0.0.1", 0), _AgentStub)
    port   = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


# 全局 stub agent:模块加载时启动一次,所有测试共用,避免 port 变化
_STUB_SERVER, _STUB_PORT = _start_stub_agent()
os.environ["FC_NODES"]       = f"127.0.0.1:{_STUB_PORT}"
os.environ["NODE_AGENT_PORT"] = str(_STUB_PORT)


# ────────────────────────────────────────────────
# DynamoDB table setup helper
# ────────────────────────────────────────────────

def _create_tables():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")

    ddb.create_table(
        TableName="sandboxes",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "id",               "AttributeType": "S"},
            {"AttributeName": "tenant_id",        "AttributeType": "S"},
            {"AttributeName": "updated_at",       "AttributeType": "S"},
            {"AttributeName": "idempotency_key",  "AttributeType": "S"},
            {"AttributeName": "pool_state",       "AttributeType": "S"},
            {"AttributeName": "driver",           "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "tenant_id-updated_at-index",
                "KeySchema": [
                    {"AttributeName": "tenant_id",  "KeyType": "HASH"},
                    {"AttributeName": "updated_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "idempotency_key-index",
                "KeySchema": [{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "pool_state-driver-index",
                "KeySchema": [
                    {"AttributeName": "pool_state", "KeyType": "HASH"},
                    {"AttributeName": "driver",     "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
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
        TableName="sandbox_tap_idx",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "node", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "node", "AttributeType": "S"}],
    )
    # 初始化 counter
    ddb.Table("sandbox_tap_idx").put_item(
        Item={"node": "global", "next_idx": 0}
    )

    # P0:节点心跳注册表 + 分布式锁表
    ddb.create_table(
        TableName="sandbox_nodes",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "node_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "node_id", "AttributeType": "S"}],
    )
    ddb.create_table(
        TableName="sandbox_locks",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "lock_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "lock_id", "AttributeType": "S"}],
    )


# ────────────────────────────────────────────────
# Test cases
# ────────────────────────────────────────────────

class TestDB(unittest.TestCase):
    """db.py 单元测试。"""

    @mock_aws
    def setUp(self):
        _create_tables()

    def _run(self, fn):
        """每个 test 方法内部需要 mock_aws context,用此 helper wrap。"""
        pass  # setUp 已在 mock_aws 里,但每个 test 方法需要自己的 decorator

    @mock_aws
    def test_put_and_get(self):
        _create_tables()
        from sandbox_api import db
        db.put({"id": "aaa", "tenant_id": "t1", "state": "running",
                "updated_at": db._utcnow()})
        r = db.get("aaa")
        self.assertEqual(r["state"], "running")

    @mock_aws
    def test_get_missing(self):
        _create_tables()
        from sandbox_api import db
        self.assertIsNone(db.get("nonexistent"))

    @mock_aws
    def test_update_state_ok(self):
        _create_tables()
        from sandbox_api import db
        db.put({"id": "bbb", "tenant_id": "t1", "state": "running",
                "updated_at": db._utcnow()})
        db.update_state("bbb", "suspending", "running")
        self.assertEqual(db.get("bbb")["state"], "suspending")

    @mock_aws
    def test_update_state_wrong_expected(self):
        _create_tables()
        from sandbox_api import db
        from botocore.exceptions import ClientError
        db.put({"id": "ccc", "tenant_id": "t1", "state": "running",
                "updated_at": db._utcnow()})
        with self.assertRaises(ClientError) as ctx:
            db.update_state("ccc", "suspending", "suspended")  # 当前是 running,期望 suspended → 应该失败
        self.assertIn("ConditionalCheckFailed",
                      ctx.exception.response["Error"]["Code"])

    @mock_aws
    def test_lease_acquire_and_release(self):
        _create_tables()
        from sandbox_api import db
        db.put({"id": "ddd", "tenant_id": "t1", "state": "running",
                "updated_at": db._utcnow()})
        lid = db.acquire_lease("ddd")
        self.assertTrue(len(lid) > 0)
        # 再抢同一把锁 → 失败
        from botocore.exceptions import ClientError
        with self.assertRaises(ClientError):
            db.acquire_lease("ddd")
        # 释放后可重新抢
        db.release_lease("ddd", lid)
        lid2 = db.acquire_lease("ddd")
        self.assertNotEqual(lid, lid2)

    @mock_aws
    def test_tap_idx_monotonic(self):
        _create_tables()
        from sandbox_api import db
        idx1 = db.alloc_tap_idx()
        idx2 = db.alloc_tap_idx()
        idx3 = db.alloc_tap_idx()
        self.assertEqual(idx2, idx1 + 1)
        self.assertEqual(idx3, idx1 + 2)

    @mock_aws
    def test_idempotency_key(self):
        _create_tables()
        from sandbox_api import db
        db.put({"id": "eee", "tenant_id": "t1", "state": "running",
                "idempotency_key": "idem-abc", "updated_at": db._utcnow()})
        r = db.get_by_idempotency_key("idem-abc")
        self.assertIsNotNone(r)
        self.assertEqual(r["id"], "eee")

    @mock_aws
    def test_write_event(self):
        _create_tables()
        from sandbox_api import db
        db.write_event("fff", "created", "creating", {"detail": "ok"})
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        resp = ddb.Table("sandbox_events").query(
            KeyConditionExpression="id = :i",
            ExpressionAttributeValues={":i": "fff"},
        )
        self.assertEqual(len(resp["Items"]), 1)
        self.assertEqual(resp["Items"][0]["event"], "created")

    @mock_aws
    def test_warm_pool_claim(self):
        _create_tables()
        from sandbox_api import db
        db.put({"id": "warm-001", "tenant_id": "pool", "state": "warm",
                "driver": "firecracker", "pool_state": "warm",
                "updated_at": db._utcnow()})
        claimed = db.claim_warm_item("firecracker")
        self.assertEqual(claimed, "warm-001")
        # 已被 claim,再取应返回 None
        self.assertIsNone(db.claim_warm_item("firecracker"))


class TestFirecrackerDriver(unittest.TestCase):
    """FirecrackerDriver + node-agent stub 集成测试。"""

    def setUp(self):
        _AgentStub.calls.clear()

    @mock_aws
    def test_create_and_destroy(self):
        _create_tables()
        from sandbox_api.drivers.firecracker import FirecrackerDriver
        from sandbox_api.driver import SandboxSpec
        drv = FirecrackerDriver()
        spec = SandboxSpec(image="test:img", cpu=2, mem_mib=512)

        result = drv.create("sbx-test1", spec)
        self.assertIn("node", result)
        self.assertIn("guest_ip", result)
        self.assertIn("tap_idx", result)

        calls = [c[1] for c in _AgentStub.calls]
        self.assertIn("/vm/create", calls)

        drv.destroy("sbx-test1", {**result, "tap_idx": result["tap_idx"]})
        calls = [c[1] for c in _AgentStub.calls]
        self.assertIn("/vm/destroy", calls)

    @mock_aws
    def test_suspend_and_resume(self):
        _create_tables()
        from sandbox_api.drivers.firecracker import FirecrackerDriver
        from sandbox_api.driver import SandboxSpec
        drv  = FirecrackerDriver()
        spec = SandboxSpec(image="test:img", cpu=2, mem_mib=512)

        cf = drv.create("sbx-test2", spec)
        record = {**cf, "id": "sbx-test2", "state": "running"}

        snap = drv.suspend("sbx-test2", record)
        # 方案C:快照落节点持久状态 EBS,不传 S3;返回 snapshot_type 而非 snapshot_s3。
        self.assertIn("snapshot_type", snap)
        self.assertIn("snapshot_create_time_s", snap)

        record["state"] = "suspended"
        resumed = drv.resume("sbx-test2", record)
        self.assertIn("node", resumed)
        self.assertIn("guest_ip", resumed)

    @mock_aws
    def test_capabilities(self):
        _create_tables()
        from sandbox_api.drivers.firecracker import FirecrackerDriver
        caps = FirecrackerDriver().capabilities()
        self.assertTrue(caps.suspend_resume)
        self.assertTrue(caps.warm_pool)

    @mock_aws
    def test_exec(self):
        _create_tables()
        from sandbox_api.drivers.firecracker import FirecrackerDriver
        from sandbox_api.driver import SandboxSpec
        drv = FirecrackerDriver()
        cf  = drv.create("sbx-exec", SandboxSpec(image="t", cpu=1, mem_mib=256))
        rc, out, err = drv.exec("sbx-exec", cf, "echo hello")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "hello")


class TestKataCapabilities(unittest.TestCase):
    """KataDriver capability 模型 — suspend 应返回 501 语义。"""

    def test_suspend_raises(self):
        from sandbox_api.drivers.kata import KataDriver
        from sandbox_api.driver import UnsupportedOperation
        drv = KataDriver()
        self.assertFalse(drv.capabilities().suspend_resume)
        with self.assertRaises(UnsupportedOperation):
            drv.suspend("any", {})

    def test_resume_raises(self):
        from sandbox_api.drivers.kata import KataDriver
        from sandbox_api.driver import UnsupportedOperation
        with self.assertRaises(UnsupportedOperation):
            KataDriver().resume("any", {})


class TestAPIEndToEnd(unittest.TestCase):
    """app.py HTTP API 端到端测试(内嵌 ThreadingHTTPServer)。"""

    def _call(self, port: int, method: str, path: str,
              body: dict | None = None,
              api_key: str | None = None) -> tuple[int, dict]:
        url  = f"http://127.0.0.1:{port}{path}"
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req  = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _start_api(self, driver) -> tuple[object, int]:
        from sandbox_api import app as app_module
        from sandbox_api.warm_pool import WarmPool
        from http.server import ThreadingHTTPServer
        app_module._driver = driver
        # 测试期间禁用 warm pool 后台 loop(避免并发干扰)
        app_module._warm_pool = WarmPool.__new__(WarmPool)
        app_module._warm_pool._driver_name = "firecracker"
        app_module._warm_pool._driver = driver
        app_module._warm_pool._lock = threading.Lock()
        # 重写 claim 使其永远返回 False(走冷建路径)
        app_module._warm_pool.claim = lambda *a, **kw: False
        app_module._warm_pool.start_replenish_loop = lambda: None

        srv  = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.1)  # nosemgrep: arbitrary-sleep -- 测试:等后台 HTTP server 线程绑定端口
        return srv, port

    @mock_aws
    def test_full_lifecycle(self):
        _create_tables()
        from sandbox_api.drivers.firecracker import FirecrackerDriver
        srv, port = self._start_api(FirecrackerDriver())

        try:
            c = lambda m, p, b=None: self._call(port, m, p, b)

            # GET / → 服务 info
            code, body = c("GET", "/")
            self.assertEqual(code, 200)
            self.assertIn("endpoints", body)

            # GET /capabilities
            code, body = c("GET", "/capabilities")
            self.assertEqual(code, 200)
            self.assertTrue(body["suspend_resume"])

            # POST /sandboxes → create
            code, body = c("POST", "/sandboxes", {
                "image": "test:latest", "cpu": 2, "mem_mib": 512,
                "tenant_id": "t1",
                "services": [{"port": 8080}],
            })
            self.assertEqual(code, 201)
            sid = body["id"]
            self.assertEqual(body["state"], "running")

            # GET /sandboxes/{id}
            code, body = c("GET", f"/sandboxes/{sid}")
            self.assertEqual(code, 200)
            self.assertEqual(body["id"], sid)

            # GET /sandboxes?tenant_id=t1
            code, body = c("GET", "/sandboxes?tenant_id=t1")
            self.assertEqual(code, 200)
            ids = [s["id"] for s in body["sandboxes"]]
            self.assertIn(sid, ids)

            # GET /sandboxes/{id}/wait?state=running
            code, body = c("GET", f"/sandboxes/{sid}/wait?state=running&timeout=5")
            self.assertEqual(code, 200)
            self.assertEqual(body["state"], "running")

            # GET /sandboxes/{id}/locate
            code, body = c("GET", f"/sandboxes/{sid}/locate")
            self.assertEqual(code, 200)
            self.assertIn("runtime_state", body)

            # POST /sandboxes/{id}/exec
            code, body = c("POST", f"/sandboxes/{sid}/exec", {"cmd": "echo hi"})
            self.assertEqual(code, 200)
            self.assertEqual(body["rc"], 0)

            # POST /sandboxes/{id}/suspend
            code, body = c("POST", f"/sandboxes/{sid}/suspend")
            self.assertEqual(code, 200)
            self.assertEqual(body["state"], "suspended")
            # 方案C:快照落节点持久状态 EBS,不传 S3;返回体含 snapshot_type。
            self.assertIn("snapshot_type", body)

            # POST /sandboxes/{id}/resume
            code, body = c("POST", f"/sandboxes/{sid}/resume")
            self.assertEqual(code, 200)
            self.assertEqual(body["state"], "running")

            # DELETE /sandboxes/{id}
            code, body = c("DELETE", f"/sandboxes/{sid}")
            self.assertEqual(code, 200)
            self.assertTrue(body["deleted"])

            # 删除后 GET → 404
            code, _ = c("GET", f"/sandboxes/{sid}")
            self.assertEqual(code, 404)

        finally:
            srv.shutdown()

    @mock_aws
    def test_idempotency(self):
        _create_tables()
        from sandbox_api.drivers.firecracker import FirecrackerDriver
        srv, port = self._start_api(FirecrackerDriver())
        try:
            body1_req = {"image": "t", "cpu": 1, "mem_mib": 256,
                         "tenant_id": "t1", "idempotency_key": "key-xyz"}
            code1, r1 = self._call(port, "POST", "/sandboxes", body1_req)
            code2, r2 = self._call(port, "POST", "/sandboxes", body1_req)
            self.assertIn(code1, (200, 201))
            self.assertEqual(code2, 200)
            self.assertEqual(r1["id"], r2["id"])
        finally:
            srv.shutdown()

    @mock_aws
    def test_kata_suspend_returns_501(self):
        _create_tables()
        from sandbox_api.drivers.kata import KataDriver
        srv, port = self._start_api(KataDriver())
        try:
            from sandbox_api import db as db_module
            db_module.put({"id": "fake-kata", "tenant_id": "t1",
                           "state": "running", "driver": "kata",
                           "updated_at": db_module._utcnow()})
            code, body = self._call(port, "POST", "/sandboxes/fake-kata/suspend")
            self.assertEqual(code, 501)
        finally:
            srv.shutdown()


class TestWarmPool(unittest.TestCase):
    """WarmPool replenish + claim 路径。"""

    @mock_aws
    def test_replenish_and_claim(self):
        _create_tables()
        from sandbox_api.warm_pool import WarmPool
        from sandbox_api.drivers.firecracker import FirecrackerDriver
        from sandbox_api.driver import SandboxSpec
        from sandbox_api import db

        drv  = FirecrackerDriver()
        pool = WarmPool("firecracker", drv)

        # replenish 应预建 WARM_POOL_SIZE(2) 个暖沙盒
        # 先确认 FC_NODES 已设置
        import os as _os
        self.assertIn("FC_NODES", _os.environ, "FC_NODES not set")
        pool.replenish()
        count = db.count_warm("firecracker")
        self.assertEqual(count, 2, f"warm count={count}, FC_NODES={_os.environ.get('FC_NODES')}")

        # claim 一个
        spec = SandboxSpec(image="t", cpu=1, mem_mib=256)
        ok = pool.claim("real-sbx-1", spec)
        self.assertTrue(ok)

        # 池子减一
        self.assertEqual(db.count_warm("firecracker"), 1)

        # claim 第二个
        ok2 = pool.claim("real-sbx-2", spec)
        self.assertTrue(ok2)
        self.assertEqual(db.count_warm("firecracker"), 0)

        # 池子空了,claim 应返回 False
        ok3 = pool.claim("real-sbx-3", spec)
        self.assertFalse(ok3)


class TestAPIAuth(unittest.TestCase):
    """控制面认证测试。"""

    @mock_aws
    def test_no_auth_when_keys_empty(self):
        """ALLOW_UNAUTHENTICATED=1 时跳过认证（开发模式）。"""
        _create_tables()
        import sandbox_api.app as app_module
        app_module._API_KEYS = set()
        app_module._ALLOW_UNAUTH = True  # 明确开发模式

        from sandbox_api.drivers.kata import KataDriver
        from http.server import ThreadingHTTPServer
        app_module._driver = KataDriver()
        app_module._warm_pool.claim = lambda *a, **kw: False

        srv = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.1)  # nosemgrep: arbitrary-sleep -- 测试:等后台 HTTP server 线程绑定端口
        try:
            # 不带 key 也能访问
            code, body = self._call(port, "GET", "/")
            self.assertEqual(code, 200)
        finally:
            srv.shutdown()

    @mock_aws
    def test_auth_required_when_keys_set(self):
        """API_KEYS 配置后:无 key → 401;正确 key → 200。"""
        _create_tables()
        import sandbox_api.app as app_module
        app_module._API_KEYS = {"test-key-abc"}
        app_module._ALLOW_UNAUTH = False   # 关闭开发模式以测试真实鉴权

        from sandbox_api.drivers.kata import KataDriver
        from http.server import ThreadingHTTPServer
        app_module._driver = KataDriver()
        app_module._warm_pool.claim = lambda *a, **kw: False

        srv = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.1)  # nosemgrep: arbitrary-sleep -- 测试:等后台 HTTP server 线程绑定端口
        try:
            # 无 key → 401
            code, _ = self._call(port, "GET", "/sandboxes")
            self.assertEqual(code, 401)

            # 错误 key → 401
            code, _ = self._call(port, "GET", "/sandboxes", api_key="wrong-key")
            self.assertEqual(code, 401)

            # 正确 key → 200
            code, _ = self._call(port, "GET", "/sandboxes", api_key="test-key-abc")
            self.assertEqual(code, 200)

            # 公开路径不需要 key
            code, _ = self._call(port, "GET", "/")
            self.assertEqual(code, 200)
            code, _ = self._call(port, "GET", "/capabilities")
            self.assertEqual(code, 200)
        finally:
            srv.shutdown()
            app_module._API_KEYS = set()
            app_module._ALLOW_UNAUTH = True  # 恢复开发模式

    def _call(self, port, method, path, body=None, api_key=None):
        import urllib.request, urllib.error, json
        url  = f"http://127.0.0.1:{port}{path}"
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


# ────────────────────────────────────────────────
# P0:节点注册表 + leader 锁
# ────────────────────────────────────────────────

class TestNodeRegistry(unittest.TestCase):
    """心跳注册 + last_seen TTL 过滤(P0-3)。"""

    @mock_aws
    def test_heartbeat_and_list_active(self):
        _create_tables()
        from sandbox_api import db
        db.heartbeat_node("nodeA", "10.0.1.5", 8000, 2)
        db.heartbeat_node("nodeB", "10.0.1.6", 4000, 1)
        active = db.list_active_nodes(ttl_s=90)
        self.assertEqual({n["node_id"] for n in active}, {"nodeA", "nodeB"})

    @mock_aws
    def test_stale_node_filtered(self):
        _create_tables()
        from sandbox_api import db
        # 手动写一条 last_seen 远超 TTL 的旧心跳
        db._nodes().put_item(Item={
            "node_id": "dead", "ip": "10.0.9.9",
            "free_mem_mib": 0, "vm_count": 0,
            "last_seen": db._utcnow_minus(3600),  # 1 小时前
        })
        db.heartbeat_node("live", "10.0.1.5", 8000, 0)
        active = db.list_active_nodes(ttl_s=90)
        self.assertEqual({n["node_id"] for n in active}, {"live"})


class TestLeaderLock(unittest.TestCase):
    """leader 锁抢占/续租/过期(P1-4)。"""

    @mock_aws
    def test_only_one_leader(self):
        _create_tables()
        from sandbox_api import db
        rvn_a = db.acquire_leader_lock("reconciler", "ownerA", ttl_s=30)
        rvn_b = db.acquire_leader_lock("reconciler", "ownerB", ttl_s=30)
        self.assertIsNotNone(rvn_a)
        self.assertIsNone(rvn_b)  # A 未过期,B 抢不到

    @mock_aws
    def test_renew_keeps_leadership(self):
        _create_tables()
        from sandbox_api import db
        rvn1 = db.acquire_leader_lock("reconciler", "ownerA", ttl_s=30)
        rvn2 = db.renew_leader_lock("reconciler", "ownerA", ttl_s=30)
        self.assertIsNotNone(rvn2)
        self.assertGreater(rvn2, rvn1)  # rvn 每次自增(fencing token)

    @mock_aws
    def test_expired_lock_can_be_taken(self):
        _create_tables()
        from sandbox_api import db
        # 写一条已过期的锁(expires 在过去)
        db._locks().put_item(Item={
            "lock_id": "reconciler", "owner": "old",
            "expires": db._utcnow_minus(10), "rvn": 5,
        })
        rvn = db.acquire_leader_lock("reconciler", "newOwner", ttl_s=30)
        self.assertIsNotNone(rvn)  # 过期 → 可被新 owner 抢占


# ────────────────────────────────────────────────
# P0:reconcile 对账
# ────────────────────────────────────────────────

class TestReconcile(unittest.TestCase):
    """状态漂移检测与自动修正(P0-1)。"""

    @mock_aws
    def test_drift_running_marked_orphaned(self):
        _create_tables()
        from sandbox_api import db
        from sandbox_api.reconcile import Reconciler

        # 沙盒记录说 running,落在活节点上,但 runtime 探针返回 unknown
        db.put({"id": "sbx1", "tenant_id": "t1", "state": "running",
                "driver": "firecracker", "node": "10.0.1.5", "tap_idx": 7,
                "updated_at": db._utcnow()})
        db.heartbeat_node("nodeA", "10.0.1.5", 8000, 1)

        class _Drv:
            def capabilities(self):
                from sandbox_api.driver import Capabilities
                return Capabilities(suspend_resume=True, warm_pool=True, migrate=True)
            def get_runtime_state(self, sid, rec):
                return "unknown"  # 节点上已无此 VM → 漂移

        stats = Reconciler(_Drv(), "firecracker").reconcile_once()
        self.assertEqual(stats["orphaned"], 1)
        self.assertEqual(db.get("sbx1")["state"], "orphaned")

    @mock_aws
    def test_dead_node_suspended_marked_needs_reschedule(self):
        _create_tables()
        from sandbox_api import db
        from sandbox_api.reconcile import Reconciler

        # suspended 沙盒落在一个没有心跳的死节点上
        db.put({"id": "sbx2", "tenant_id": "t1", "state": "suspended",
                "driver": "firecracker", "node": "10.0.9.9", "tap_idx": 3,
                "snapshot_s3": "s3://b/sbx2/", "updated_at": db._utcnow()})
        # 不写任何心跳 → 节点视为死

        class _Drv:
            def capabilities(self):
                from sandbox_api.driver import Capabilities
                return Capabilities(suspend_resume=True, warm_pool=True, migrate=True)
            def get_runtime_state(self, sid, rec):
                return "unknown"

        stats = Reconciler(_Drv(), "firecracker").reconcile_once()
        self.assertEqual(stats["needs_reschedule"], 1)
        self.assertEqual(db.get("sbx2")["state"], "needs_reschedule")

    @mock_aws
    def test_healthy_running_left_alone(self):
        _create_tables()
        from sandbox_api import db
        from sandbox_api.reconcile import Reconciler

        db.put({"id": "sbx3", "tenant_id": "t1", "state": "running",
                "driver": "firecracker", "node": "10.0.1.5", "tap_idx": 1,
                "updated_at": db._utcnow()})
        db.heartbeat_node("nodeA", "10.0.1.5", 8000, 1)

        class _Drv:
            def capabilities(self):
                from sandbox_api.driver import Capabilities
                return Capabilities(suspend_resume=True, warm_pool=True, migrate=True)
            def get_runtime_state(self, sid, rec):
                return "running"  # 一致

        stats = Reconciler(_Drv(), "firecracker").reconcile_once()
        self.assertEqual(stats["ok"], 1)
        self.assertEqual(db.get("sbx3")["state"], "running")  # 未被动


# ────────────────────────────────────────────────
# Runner
# ────────────────────────────────────────────────

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()

    for cls in [TestDB, TestFirecrackerDriver, TestKataCapabilities,
                TestAPIEndToEnd, TestWarmPool, TestAPIAuth,
                TestNodeRegistry, TestLeaderLock, TestReconcile]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
