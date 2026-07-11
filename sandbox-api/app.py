#!/usr/bin/env python3
"""
统一沙盒控制面 API — v2

后端为 FirecrackerDriver(裸 FC microVM + node-agent,支持 suspend/resume 快照)。
(历史上曾有可插拔的 Kata 后端,因无法快照/恢复、与 spot 疏散核心诉求不符,已移除。)

接口(对齐 Fly Machines API):
  POST   /sandboxes                    创建沙盒
  GET    /sandboxes                    列出(按 tenant_id 过滤)
  GET    /sandboxes/{id}               查单个
  GET    /sandboxes/{id}/wait          等待状态(长轮询)
  DELETE /sandboxes/{id}               销毁
  POST   /sandboxes/{id}/suspend       挂起 + 快照
  POST   /sandboxes/{id}/resume        从快照恢复
  POST   /sandboxes/{id}/exec          在沙盒内执行命令
  GET    /sandboxes/{id}/locate        定位 VMM(调试用)
  GET    /capabilities                 当前 driver 能力

运行:
  FC_NODES=10.0.1.5 python3 app.py
"""
from __future__ import annotations

import http.client
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import boto3
from botocore.exceptions import ClientError

from sandbox_api import db
from sandbox_api.driver import SandboxSpec, ServiceSpec, UnsupportedOperation
from sandbox_api.reconcile import Reconciler
from sandbox_api.warm_pool import WarmPool

# ---------- driver(仅 Firecracker,抽象层已拍平)----------
# _DRIVER_NAME 仅作为写入 DynamoDB 记录的 driver 标签 + 暖池 GSI 分区键,固定 firecracker。
_DRIVER_NAME = "firecracker"
from sandbox_api.drivers.firecracker import FirecrackerDriver
_driver = FirecrackerDriver()

# reconcile loop + leader 选举(P0-1 / P1-4)。
# 暖池补充与 reconcile 共用同一 leader 门控:多副本控制面下只有 leader 跑后台
# loop,请求路径仍全副本无状态服务。
_reconciler = Reconciler(_driver)
_reconciler.start_loop()

_warm_pool = WarmPool(_DRIVER_NAME, _driver)
_warm_pool.start_replenish_loop(is_leader=lambda: _reconciler.is_leader)

LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8000"))

# 端口暴露:node-agent 端口 + 对外访问前缀(NLB 自带域名,由部署注入)。
# NLB_HOSTNAME 供 Portal 拼接可点击 URL(如 http://<nlb>/s/<sid>/<port>/);未配置则前端回退相对路径。
NODE_AGENT_PORT = int(os.environ.get("NODE_AGENT_PORT", "8002"))
NLB_HOSTNAME    = os.environ.get("NLB_HOSTNAME", "")
# 任意端口暴露:默认 True —— 用户在沙盒内起在任何端口的服务都可经 /s/{id}/{port}/ 访问,
# 无需 create 时预先声明(对齐 E2B/Fly"想暴露什么端口都行"的体验)。
# 设为 0/false 则退回"仅 services 声明端口可暴露"的白名单模式(更安全,适合多租户生产)。
ALLOW_ALL_PORTS = os.environ.get("ALLOW_ALL_PORTS", "1").lower() in ("1", "true")

# ---------- 认证 ----------
# API_KEYS: 逗号分隔的有效 key 列表
# 生产必须通过 K8s Secret 注入(见 terraform/stage2-control-plane/main.tf api-keys Secret)
# ALLOW_UNAUTHENTICATED=1 仅用于本地开发/测试,生产严禁设置
_API_KEYS: set[str] = {
    k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()
}
_ALLOW_UNAUTH = os.environ.get("ALLOW_UNAUTHENTICATED", "").lower() in ("1", "true")
# 无需认证的路径(健康检查)
_PUBLIC_PATHS = {"/", "/capabilities"}

# API_KEY → tenant_id 映射（格式: "key:tenant_id,key2:tenant_id2" 或仅 "key"）
# 若 key 未绑定 tenant，则该 key 的调用方视为 tenant "default"
_KEY_TENANT: dict[str, str] = {}
for _entry in os.environ.get("API_KEYS", "").split(","):
    _entry = _entry.strip()
    if ":" in _entry:
        _k, _t = _entry.split(":", 1)
        _KEY_TENANT[_k.strip()] = _t.strip()
        _API_KEYS.add(_k.strip())
    elif _entry:
        _KEY_TENANT[_entry] = "default"


def _get_caller_tenant(handler: "Handler") -> str | None:
    """从 Authorization header 解析调用方 tenant_id。未认证或无绑定时返回 None。"""
    auth = handler.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return _KEY_TENANT.get(token)  # None 表示无效 token


# 启动时警告（不阻断，让 _check_auth 在请求时失败）
if not _API_KEYS and not _ALLOW_UNAUTH:
    import sys
    print("[WARNING] API_KEYS not set and ALLOW_UNAUTHENTICATED!=1 — "
          "all protected endpoints will return 503 until API_KEYS is configured", file=sys.stderr)


def _check_auth(handler: "Handler") -> bool:
    """返回 True 表示通过;False 表示已发送 401 响应。"""
    path = urlparse(handler.path).path
    if path in _PUBLIC_PATHS:
        return True
    if _ALLOW_UNAUTH:
        # 仅限本地开发/测试 —— 生产严禁
        return True
    if not _API_KEYS:
        # API_KEYS 未配置且未显式允许无鉴权 → 拒绝，强制安全失败
        handler._send(503, {
            "error": "control plane not configured",
            "hint": "Set API_KEYS env var (K8s Secret) before exposing this service",
        })
        return False
    auth = handler.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if token in _API_KEYS:
        return True
    handler._send(401, {"error": "unauthorized", "hint": "Authorization: Bearer <api_key>"})
    return False


# ---------- 业务逻辑 ----------

def create_sandbox(body: dict) -> tuple[int, dict]:
    idem_key = body.get("idempotency_key")
    if idem_key:
        existing = db.get_by_idempotency_key(idem_key)
        if existing:
            return 200, existing

    spec = SandboxSpec(
        image    = body.get("image", os.environ.get("SANDBOX_IMAGE", "")),
        cpu      = int(body.get("cpu", 2)),
        mem_mib  = int(body.get("mem_mib", 4096)),
        env      = body.get("env", {}),
        services = [ServiceSpec(**s) for s in body.get("services", [])],
        meta     = body.get("meta", {}),
    )
    tenant_id = body.get("tenant_id", "default")
    sid       = uuid.uuid4().hex[:8]

    record: dict = {
        "id":               sid,
        "tenant_id":        tenant_id,
        "state":            "creating",
        "driver":           _DRIVER_NAME,
        "image":            spec.image,
        "cpu":              spec.cpu,
        "mem_mib":          spec.mem_mib,
        "created_at":       db._utcnow(),
        "updated_at":       db._utcnow(),
        "meta":             spec.meta,
        # 声明要暴露的服务端口 —— 供端口暴露反代(/s/{id}/{port})校验"该端口是否允许对外"。
        "services":         [{"port": s.port, "protocol": s.protocol,
                             "autostop": s.autostop, "autostart": s.autostart}
                            for s in spec.services],
    }
    if idem_key:
        record["idempotency_key"] = idem_key

    db.put(record)

    try:
        # 先尝试从暖池 resume(FC 模式 ~7ms);失败或不支持则冷建
        claimed = _warm_pool.claim(sid, spec)
        if not claimed:
            driver_fields = _driver.create(sid, spec)
            db.force_update(sid, {**driver_fields, "state": "running"})
        db.write_event(sid, "created", "creating")
        # 方案C:create 成功后异步打一次 Full base 快照(供后续 Diff 疏散),不阻塞 create 返回。
        _maybe_snapshot_base_async(sid)
        return 201, db.get(sid)
    except Exception as e:
        try:
            db.force_update(sid, {"state": "failed", "error": str(e)})
        except Exception:
            pass
        return 500, {"error": str(e)}


# 方案C:是否在 create 后自动打 base(Diff 前提)。默认开启;可用 AUTO_SNAPSHOT_BASE=0 关闭。
_AUTO_BASE = os.environ.get("AUTO_SNAPSHOT_BASE", "1").lower() in ("1", "true")
# base 是 Full 快照(写 2GB),多个并发会打满状态 EBS 带宽 → 每个都变慢、guest 冻结更久。
# 用信号量限制【同时进行的 base 数】,避免 50 个 base 同时打把 EBS 撑爆(实测会拖到 ~200s/个)。
import threading as _threading
_BASE_CONCURRENCY = int(os.environ.get("BASE_SNAPSHOT_CONCURRENCY", "2"))
_BASE_SEM = _threading.Semaphore(_BASE_CONCURRENCY)

# 方案C:resume 侧限流。跨机疏散后在新节点批量 resume,每个都要把 base+diff 合并成
# 完整内存镜像(~base 大小的读 + 写,单个 ~1.5GB×2 I/O)。50 个同时 resume 会打满
# 单块状态 EBS 带宽 → 每个 merge 从 ~6s 恶化到 ~25s,墙钟不降反噪、易触发超时误判。
# 实测(单块 1000MB/s gp3,50 个 1.5G):并发 6→墙钟 63s;并发 15→33s(单个~9s);
# 并发 47→33s 但单个恶化到 25s。带宽在 ~15 并发饱和,超过纯属徒增单个延迟。
# 故限流到 ~12:墙钟接近最优且留 headroom。0 → 不限流。
_RESUME_CONCURRENCY = int(os.environ.get("RESUME_CONCURRENCY", "12"))
_RESUME_SEM = _threading.Semaphore(_RESUME_CONCURRENCY) if _RESUME_CONCURRENCY > 0 else None


def _maybe_snapshot_base_async(sid: str) -> None:
    """后台线程给 sandbox 打 Full base 快照(方案C Diff 前提)。off 关键路径,失败不影响 create。
    用信号量限制并发,避免多个 base Full 同时打满状态 EBS 带宽。"""
    snap_base = getattr(_driver, "snapshot_base", None)
    if not _AUTO_BASE or snap_base is None:
        return

    def _do():
        try:
            import time as _t
            _t.sleep(float(os.environ.get("BASE_SNAPSHOT_DELAY_S", "20")))  # 等 guest boot 稳定
            with _BASE_SEM:  # 限流:同时最多 _BASE_CONCURRENCY 个 base 在打
                rec = db.get(sid)
                if rec and rec.get("state") == "running":
                    info = snap_base(sid, rec)
                    db.write_event(sid, "base_snapshot", "running", info)
        except Exception:
            pass  # base 失败 → 疏散时降级 Full,不阻断

    _threading.Thread(target=_do, daemon=True).start()


def _check_tenant_access(record: dict, caller_tenant: str | None) -> tuple[int, dict] | None:
    """
    校验调用方是否有权操作该沙盒。
    返回 None 表示允许；返回 (code, body) 表示拒绝。
    caller_tenant=None 表示无法从 token 解析租户（鉴权未启用时退化为 None → 允许）。
    """
    if caller_tenant is None:
        return None  # 无鉴权模式（ALLOW_UNAUTHENTICATED=1）
    sandbox_tenant = record.get("tenant_id", "default")
    if caller_tenant == "default":
        return None  # default key 有管理员权限
    if sandbox_tenant != caller_tenant:
        return 403, {"error": "forbidden", "hint": "sandbox belongs to a different tenant"}
    return None


# ---------- 只读聚合视图(portal Dashboard 用)----------
# 现有对外 API 只能按单租户列表,SaaS 全局总览拿不到跨租户聚合视图。
# 这些数据都在 DynamoDB 里(db.py 已有 list_by_states / list_active_nodes / count_warm),
# 这里仅把它们暴露成只读 GET。均要求 admin(default)key,不改任何写路径。
# 覆盖沙盒的全部生命周期状态(含暖池/对账态),供总览表格与计数卡片。
_ALL_STATES = [
    "creating", "running", "suspending", "suspended", "resuming",
    "destroying", "failed", "warm", "orphaned", "needs_reschedule",
]


def _require_admin(handler: "Handler") -> bool:
    """聚合视图仅限 admin(default key)。返回 True 放行;False 表示已发送 403。
    无鉴权开发模式(caller_tenant=None)下放行,便于本地 portal 联调。"""
    caller = _get_caller_tenant(handler)
    if caller is None or caller == "default":
        return True
    handler._send(403, {"error": "forbidden", "hint": "admin (default) API key required"})
    return False


def admin_sandboxes() -> tuple[int, dict]:
    """全租户沙盒列表(供总览表格)。"""
    return 200, {"sandboxes": db.list_by_states(_ALL_STATES)}


def admin_nodes() -> tuple[int, dict]:
    """当前活节点(free_mem_mib / vm_count / last_seen / labels)。"""
    return 200, {"nodes": db.list_active_nodes()}


def admin_events(sandbox_id: str | None, limit: int) -> tuple[int, dict]:
    """事件时间线;sandbox_id 为空则返回全局时间线。"""
    return 200, {"events": db.list_events(sandbox_id, limit)}


def admin_cluster() -> tuple[int, dict]:
    """集群级信息,供 Portal 拼接端口暴露 URL。"""
    return 200, {
        "nlb_hostname": NLB_HOSTNAME,
        # 端口暴露访问前缀:{prefix}/s/{sid}/{port}/;NLB 未配置时前端用相对路径。
        "proxy_base": f"http://{NLB_HOSTNAME}" if NLB_HOSTNAME else "",
        # 任意端口模式:前端据此提供"输入任意端口打开"的入口,而非只列 declared 端口。
        "allow_all_ports": ALLOW_ALL_PORTS,
    }


def admin_stats() -> tuple[int, dict]:
    """汇总卡片:各 state 计数、节点数、集群总/空闲内存、暖池水位。"""
    sandboxes = db.list_by_states(_ALL_STATES)
    nodes     = db.list_active_nodes()
    by_state: dict[str, int] = {}
    for s in sandboxes:
        st = s.get("state", "unknown")
        by_state[st] = by_state.get(st, 0) + 1
    free_mem_mib = sum(int(n.get("free_mem_mib", 0)) for n in nodes)
    vm_count     = sum(int(n.get("vm_count", 0)) for n in nodes)
    return 200, {
        "total_sandboxes": len(sandboxes),
        "by_state":        by_state,
        "node_count":      len(nodes),
        "cluster_free_mem_mib": free_mem_mib,
        "running_vm_count": vm_count,
        "warm_pool":       db.count_warm(_DRIVER_NAME),
        "driver":          _DRIVER_NAME,
    }


# ---------- 端口暴露反代(sandbox-proxy)----------
# 路径路由 /s/{sid}/{port}/{rest} —— 用路径(而非 Host 子域名)定位沙盒,因为:
#   1) 先用 NLB 自带域名,挂不了通配符子域名,Host 头无法区分沙盒;
#   2) 需支持"多沙盒暴露同一内部端口"——路由键是 sid,与宿主端口/Host 解耦。
# 解析出 (sid, port) → 查 DynamoDB 拿沙盒所在 node → 转发到该 node-agent 的
# /proxy/{sid}/{port}/{rest}(node-agent 再转进 guest 172.18.x.2:port)。

def resolve_proxy_target(sid: str, port: int) -> tuple[int, dict] | tuple[int, str, str]:
    """
    校验并解析反代目标。
    成功 → (200, node_host, upstream_path_prefix);失败 → (code, {error}).
    """
    record = db.get(sid)
    if not record:
        return 404, {"error": "sandbox not found", "id": sid}
    if record.get("state") != "running":
        return 409, {"error": "sandbox not running", "state": record.get("state")}

    # 端口白名单校验:
    #   ALLOW_ALL_PORTS(默认开)→ 任意端口都放行,用户在 guest 内起在哪个端口都能访问,
    #     无需 create 时预声明(E2B/Fly 式"想暴露什么端口都行")。
    #   关闭 → 仅放行 services 声明过的端口(白名单,更适合多租户生产)。
    if not ALLOW_ALL_PORTS:
        declared = {int(s.get("port")) for s in record.get("services", []) if s.get("port") is not None}
        if declared and port not in declared:
            return 403, {"error": "port not exposed",
                         "hint": f"declare it in services: {sorted(declared)} (or enable ALLOW_ALL_PORTS)"}

    # 基本端口范围校验(防明显非法值)
    if port < 1 or port > 65535:
        return 400, {"error": "port out of range (1-65535)"}

    node = record.get("node")
    if not node:
        return 503, {"error": "sandbox has no node yet"}
    host = node if ":" in node else f"{node}:{NODE_AGENT_PORT}"
    return 200, host, f"/proxy/{sid}/{port}"


def destroy_sandbox(sid: str, caller_tenant: str | None = None) -> tuple[int, dict]:
    record = db.get(sid)
    if not record:
        return 404, {"error": "not found"}
    if (denied := _check_tenant_access(record, caller_tenant)):
        return denied

    lease_id = None
    try:
        lease_id = db.acquire_lease(sid)
        prev = record["state"]
        db.update_state(sid, "destroying", prev)
        _driver.destroy(sid, record)
        db.delete(sid)
        db.write_event(sid, "destroyed", prev)
        return 200, {"id": sid, "deleted": True}
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return 409, {"error": "sandbox is locked by another operation"}
        return 500, {"error": str(e)}
    finally:
        if lease_id:
            db.release_lease(sid, lease_id)


def suspend_sandbox(sid: str, caller_tenant: str | None = None) -> tuple[int, dict]:
    record = db.get(sid)
    if not record:
        return 404, {"error": "not found"}
    if (denied := _check_tenant_access(record, caller_tenant)):
        return denied

    lease_id = None
    try:
        lease_id = db.acquire_lease(sid)
        db.update_state(sid, "suspending", "running")
        snap_info = _driver.suspend(sid, record)
        db.update_state(sid, "suspended", "suspending", snap_info)
        db.write_event(sid, "suspended", "running", snap_info)
        return 200, db.get(sid)
    except UnsupportedOperation as e:
        return 501, {"error": str(e)}
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return 409, {"error": "sandbox is not in running state or is locked"}
        return 500, {"error": str(e)}
    except Exception as e:
        # suspend 失败(如快照上传失败):node-agent 已尝试恢复 VM 到运行态,
        # 内存未释放 → 回滚 running(而非 failed),绝不标 suspended。
        # 若 VM 实际已死,后台 reconcile 会探到 runtime 不存在并标 orphaned。
        db.force_update(sid, {"state": "running", "error": str(e)})
        return 500, {"error": str(e)}
    finally:
        if lease_id:
            db.release_lease(sid, lease_id)


def resume_sandbox(sid: str, caller_tenant: str | None = None) -> tuple[int, dict]:
    record = db.get(sid)
    if not record:
        return 404, {"error": "not found"}
    if (denied := _check_tenant_access(record, caller_tenant)):
        return denied

    lease_id = None
    try:
        lease_id = db.acquire_lease(sid)
        db.update_state(sid, "resuming", "suspended")
        # 限流:同时最多 _RESUME_CONCURRENCY 个 resume 走 driver(合并+load 的 EBS I/O 重)。
        # 排队期间沙盒停在 resuming(可观测),不额外占资源。t0 只计真正 resume 不含排队。
        if _RESUME_SEM is not None:
            _RESUME_SEM.acquire()
        try:
            t0 = time.monotonic()
            driver_fields = _driver.resume(sid, record)
            restore_time  = round(time.monotonic() - t0, 4)
        finally:
            if _RESUME_SEM is not None:
                _RESUME_SEM.release()
        db.update_state(sid, "running", "resuming",
                        {**driver_fields, "restore_time_s": str(restore_time)})
        db.write_event(sid, "resumed", "suspended",
                       {"restore_time_s": restore_time})
        return 200, db.get(sid)
    except UnsupportedOperation as e:
        return 501, {"error": str(e)}
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return 409, {"error": "sandbox is not in suspended state or is locked"}
        return 500, {"error": str(e)}
    except Exception as e:
        db.force_update(sid, {"state": "failed", "error": str(e)})
        return 500, {"error": str(e)}
    finally:
        if lease_id:
            db.release_lease(sid, lease_id)


def exec_sandbox(sid: str, cmd: str, caller_tenant: str | None = None) -> tuple[int, dict]:
    record = db.get(sid)
    if not record:
        return 404, {"error": "not found"}
    if (denied := _check_tenant_access(record, caller_tenant)):
        return denied
    rc, stdout, stderr = _driver.exec(sid, record, cmd)
    return (200 if rc == 0 else 500), {
        "id": sid, "cmd": cmd, "rc": rc,
        "stdout": stdout, "stderr": stderr,
    }


def wait_sandbox(sid: str, target_state: str, timeout: int = 30) -> tuple[int, dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = db.get(sid)
        if not record:
            return 404, {"error": "not found"}
        if record["state"] == target_state or record["state"] == "failed":
            return 200, record
        time.sleep(1)  # nosemgrep: arbitrary-sleep -- 轮询 DynamoDB 状态变更的间隔
    record = db.get(sid) or {}
    return 408, {"error": "timeout", "current_state": record.get("state")}


# ---------- HTTP handler ----------

class Handler(BaseHTTPRequestHandler):

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            n = 0
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _parts(self) -> list[str]:
        return urlparse(self.path).path.strip("/").split("/")

    def _qs(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    # ---------- 端口暴露反代 /s/{sid}/{port}/{rest} ----------
    # 命中则透传到沙盒服务并返回 True。**不走 Bearer 鉴权** —— 浏览器打开 web 预览
    # 不会带 API key(demo:公开可达;生产应在此加 token/租户校验,见设计文档 §6)。
    def _maybe_proxy(self) -> bool:
        parsed = urlparse(self.path)
        parts  = parsed.path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "s":
            return False
        sid, port_s = parts[1], parts[2]
        rest = "/".join(parts[3:])
        try:
            port = int(port_s)
        except ValueError:
            self._send(400, {"error": "bad port"}); return True

        target = resolve_proxy_target(sid, port)
        if target[0] != 200:
            self._send(target[0], target[1]); return True
        _, node_host, up_prefix = target

        qs = f"?{parsed.query}" if parsed.query else ""
        upstream_path = f"{up_prefix}/{rest}{qs}"

        try:
            n = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            n = 0
        req_body = self.rfile.read(n) if n else None

        hop = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade", "host"}
        fwd = {k: v for k, v in self.headers.items() if k.lower() not in hop}

        try:
            conn = http.client.HTTPConnection(node_host, timeout=30)
            conn.request(self.command, upstream_path, body=req_body, headers=fwd)
            resp = conn.getresponse()
            data = resp.read()
        except Exception as e:
            self._send(502, {"error": "node-agent unreachable", "hint": str(e)})
            return True

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in hop or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        conn.close()
        return True

    def do_GET(self):
        if self._maybe_proxy():
            return
        if not _check_auth(self):
            return
        try:
            self._handle_get()
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        if self._maybe_proxy():
            return
        if not _check_auth(self):
            return
        try:
            self._handle_post()
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_DELETE(self):
        if self._maybe_proxy():
            return
        if not _check_auth(self):
            return
        try:
            self._handle_delete()
        except Exception as e:
            self._send(500, {"error": str(e)})

    # web 应用常用的其余 method —— 仅服务 /s/ 反代
    def _proxy_only(self):
        if self._maybe_proxy():
            return
        self._send(404, {"error": "not found"})

    do_PUT     = _proxy_only
    do_PATCH   = _proxy_only
    do_OPTIONS = _proxy_only
    do_HEAD    = _proxy_only

    def _handle_get(self):
        p = self._parts()

        # GET /capabilities
        if p == ["capabilities"]:
            caps = _driver.capabilities()
            return self._send(200, {
                "driver": _DRIVER_NAME,
                "suspend_resume": caps.suspend_resume,
                "warm_pool": caps.warm_pool,
                "migrate": caps.migrate,
            })

        # GET /admin/* — 只读聚合视图(portal Dashboard),仅限 admin key
        if p and p[0] == "admin":
            if not _require_admin(self):
                return
            if p == ["admin", "sandboxes"]:
                code, result = admin_sandboxes()
                return self._send(code, result)
            if p == ["admin", "nodes"]:
                code, result = admin_nodes()
                return self._send(code, result)
            if p == ["admin", "stats"]:
                code, result = admin_stats()
                return self._send(code, result)
            if p == ["admin", "events"]:
                qs    = self._qs()
                sid   = (qs.get("id") or [None])[0]
                limit = int((qs.get("limit") or ["100"])[0])
                code, result = admin_events(sid, limit)
                return self._send(code, result)
            if p == ["admin", "cluster"]:
                code, result = admin_cluster()
                return self._send(code, result)
            return self._send(404, {"error": "not found"})

        # GET /sandboxes
        if p == ["sandboxes"]:
            qs = self._qs()
            tenant = (qs.get("tenant_id") or ["default"])[0]
            return self._send(200, {"sandboxes": db.list_by_tenant(tenant)})

        if len(p) >= 2 and p[0] == "sandboxes":
            sid = p[1]

            # GET /sandboxes/{id}/wait?state=running&timeout=30
            if len(p) == 3 and p[2] == "wait":
                qs      = self._qs()
                target  = (qs.get("state") or ["running"])[0]
                timeout = int((qs.get("timeout") or ["30"])[0])
                code, result = wait_sandbox(sid, target, timeout)
                return self._send(code, result)

            # GET /sandboxes/{id}/locate
            if len(p) == 3 and p[2] == "locate":
                record = db.get(sid)
                if not record:
                    return self._send(404, {"error": "not found"})
                state = _driver.get_runtime_state(sid, record)
                return self._send(200, {**record, "runtime_state": state})

            # GET /sandboxes/{id}
            record = db.get(sid)
            if record:
                return self._send(200, record)
            return self._send(404, {"error": "not found"})

        # GET /
        self._send(200, {
            "service": "sandbox-control-plane",
            "driver":  _DRIVER_NAME,
            "endpoints": [
                "POST   /sandboxes",
                "GET    /sandboxes",
                "GET    /sandboxes/{id}",
                "GET    /sandboxes/{id}/wait?state=running&timeout=30",
                "DELETE /sandboxes/{id}",
                "POST   /sandboxes/{id}/suspend",
                "POST   /sandboxes/{id}/resume",
                "POST   /sandboxes/{id}/exec",
                "GET    /sandboxes/{id}/locate",
                "GET    /capabilities",
                "GET    /admin/sandboxes",
                "GET    /admin/nodes",
                "GET    /admin/stats",
                "GET    /admin/events?id=&limit=",
                "GET    /admin/cluster",
                "ANY    /s/{id}/{port}/{path}  (sandbox port proxy)",
            ],
        })

    def _handle_post(self):
        p = self._parts()

        # POST /sandboxes
        if p == ["sandboxes"]:
            code, result = create_sandbox(self._body())
            return self._send(code, result)

        if len(p) == 3 and p[0] == "sandboxes":
            sid    = p[1]
            action = p[2]

            ct = _get_caller_tenant(self)

            if action == "suspend":
                code, result = suspend_sandbox(sid, ct)
                return self._send(code, result)

            if action == "resume":
                code, result = resume_sandbox(sid, ct)
                return self._send(code, result)

            if action == "exec":
                cmd = self._body().get("cmd", "echo no-cmd")
                code, result = exec_sandbox(sid, cmd, ct)
                return self._send(code, result)

        self._send(404, {"error": "not found"})

    def _handle_delete(self):
        p = self._parts()
        if len(p) == 2 and p[0] == "sandboxes":
            ct = _get_caller_tenant(self)
            code, result = destroy_sandbox(p[1], ct)
            return self._send(code, result)
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"控制面 API [{_DRIVER_NAME} driver] 在 http://{LISTEN_HOST}:{LISTEN_PORT}")
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
