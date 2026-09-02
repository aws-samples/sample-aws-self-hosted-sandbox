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
import socket
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import boto3
from botocore.exceptions import ClientError

from sandbox_api import db
from sandbox_api.autosleep import AutoSleeper
from sandbox_api.crd import (
    CRDCreateOutcomeUnknown,
    FirecrackerSandboxStore,
    crd_control_enabled,
)
from sandbox_api.driver import SandboxSpec, ServiceSpec, UnsupportedOperation
from sandbox_api.idle_detection import IdleDetector
from sandbox_api.observability import (
    RESUME_INFLIGHT,
    RESUME_QUEUE_WAIT,
    WAKE_RPC_DURATION,
    finish_server_span,
    inject_trace_headers,
    log_event,
    metrics_payload,
    new_request_id,
    normalize_route,
    observed_operation,
    record_http,
    start_server_span,
    stale_loops,
)
from sandbox_api.reconcile import Reconciler
from sandbox_api.warm_pool import WarmPool

# ---------- driver(仅 Firecracker,抽象层已拍平)----------
# _DRIVER_NAME 仅作为写入 DynamoDB 记录的 driver 标签 + 暖池 GSI 分区键,固定 firecracker。
_DRIVER_NAME = "firecracker"
from sandbox_api.drivers.firecracker import FirecrackerDriver
_driver = FirecrackerDriver()

# 路线A:CRD 只接管生命周期 desired state;node-agent/Firecracker API 完全保留。
# 关闭开关时仍走原同步路径,可作为无数据迁移的快速回滚。
_CRD_CONTROL_ENABLED = crd_control_enabled()
_crd_store = FirecrackerSandboxStore() if _CRD_CONTROL_ENABLED else None
_warm_pool = WarmPool(_DRIVER_NAME, _driver)

# 旧模式由 API Pod 自己跑 reconcile/warm-pool/autosleep。CRD 模式下这些后台
# 职责全部移到 firecracker-operator,避免两个 lifecycle authority 同时操作 VM。
if not _CRD_CONTROL_ENABLED:
    _reconciler = Reconciler(_driver)
    _reconciler.start_loop()
    _warm_pool.start_replenish_loop(
        is_leader=lambda: _reconciler.is_leader
    )
else:
    _reconciler = None

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

# 端口暴露鉴权(#5):EXPOSE_TOKEN 非空时,访问 /s/{id}/{port}/ 必须带该 token
# (query ?token=xxx 或 Cookie sbx_token=xxx 或 Header X-Sbx-Token)。首次带 query token
# 访问会种 Cookie,之后浏览器内的子请求(JS/CSS/API)自动带 Cookie 免重复。
# 留空(默认)= 公开可达(demo)。生产多租户应设置它,给每个 demo 链接附 ?token=。
EXPOSE_TOKEN = os.environ.get("EXPOSE_TOKEN", "")

# ---------- 自动休眠 / 唤醒(auto-sleep / auto-wake,对齐 fly.io)----------
# 没流量 → 自动 sleep(打快照释放 RAM,进独立状态 slept);来请求 → 网关层透明 resume。
# 与手动 /suspend 严格区分:手动挂起标 suspended,网关【不会】自动唤醒它;只有自动休眠
# 的 slept 会被 /s/{id}/{port}/ 请求触发唤醒。opt-in:仅对声明了 autostop/autostart 的
# 沙盒生效(见 _autostop_enabled / _autostart_enabled),默认关,符合"显式开"。
AUTO_SLEEP_ENABLED   = os.environ.get("AUTO_SLEEP_ENABLED", "1").lower() in ("1", "true")
AUTO_WAKE_TIMEOUT_S  = int(os.environ.get("AUTO_WAKE_TIMEOUT_S", "30"))  # 网关唤醒等待上限
CRD_API_WAIT_S       = int(os.environ.get("CRD_API_WAIT_S", "700"))
CRD_DELETE_WAIT_S    = int(os.environ.get("CRD_DELETE_WAIT_S", "120"))

# ---------- 认证 ----------
# API_KEYS: 逗号分隔的有效 key 列表
# 生产必须通过 K8s Secret 注入(见 terraform/stage2-control-plane/main.tf api-keys Secret)
# ALLOW_UNAUTHENTICATED=1 仅用于本地开发/测试,生产严禁设置
_API_KEYS: set[str] = {
    k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()
}
_ALLOW_UNAUTH = os.environ.get("ALLOW_UNAUTHENTICATED", "").lower() in ("1", "true")
# 无需认证的路径(健康检查)
_PUBLIC_PATHS = {"/", "/capabilities", "/livez", "/readyz", "/metrics"}

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


# ---------- 自动休眠 / 唤醒 辅助 ----------

def _persist_activity(sid: str) -> None:
    db.force_update(sid, {"last_active_at": db._utcnow()})


_idle_detector = IdleDetector(_persist_activity)


def _touch_activity(sid: str, *, force: bool = False) -> None:
    _idle_detector.touch(sid, force=force)


def _autostop_enabled(record: dict) -> bool:
    """该沙盒是否开启自动休眠(Fly 语义)。任一 service.autostop 为真,或 meta.auto_sleep 为真。
    默认关(opt-in):无声明则不自动休眠。"""
    if any(s.get("autostop") for s in record.get("services", [])):
        return True
    return bool((record.get("meta") or {}).get("auto_sleep"))


def _autostart_enabled(record: dict) -> bool:
    """该沙盒是否允许网关透明唤醒(Fly 语义)。任一 service.autostart 为真,或 meta.auto_wake 为真。
    默认关(opt-in):无声明则网关不自动唤醒(维持 409)。"""
    if any(s.get("autostart") for s in record.get("services", [])):
        return True
    return bool((record.get("meta") or {}).get("auto_wake"))


# ---------- CRD lifecycle bridge ----------

def _wait_record_states(
    sid: str,
    states: set[str],
    timeout_s: int,
) -> dict | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        record = db.get(sid)
        if record is None or record.get("state") in states:
            return record
        time.sleep(0.25)  # nosemgrep: arbitrary-sleep -- 等 Operator 收敛
    return db.get(sid)


def _ensure_crd(record: dict) -> None:
    if _crd_store is None:
        raise RuntimeError("CRD lifecycle control is not initialized")
    _crd_store.ensure(record)


def _request_crd_state(
    record: dict,
    desired_state: str,
    *,
    suspend_reason: str = "",
) -> str:
    _ensure_crd(record)
    operation_id = uuid.uuid4().hex
    _crd_store.request_state(
        record["id"],
        desired_state,
        operation_id,
        suspend_reason=suspend_reason,
    )
    return operation_id


def _crd_transition_result(
    sid: str,
    success_states: set[str],
    timeout_s: int = CRD_API_WAIT_S,
    operation_id: str = "",
) -> tuple[int, dict]:
    deadline = time.monotonic() + timeout_s
    record: dict | None = None
    while time.monotonic() < deadline:
        record = db.get(sid)
        if record is None:
            break
        if record.get("state") in success_states | {"failed"}:
            break
        if (
            operation_id
            and record.get("failed_operation_id") == operation_id
        ):
            break
        time.sleep(0.25)  # nosemgrep: arbitrary-sleep -- 等 Operator 收敛
    if record is None:
        return 404, {"error": "not found"}
    if record.get("state") in success_states:
        return 200, record
    if record.get("state") == "failed" or (
        operation_id
        and record.get("failed_operation_id") == operation_id
    ):
        return 500, record
    return 504, {
        "error": "operator reconciliation timeout",
        "id": sid,
        "current_state": record.get("state"),
    }


# ---------- 业务逻辑 ----------

# ---------- M2:受保护池 / 抢占池放置策略 ----------
# 池分离把承载沙盒的节点分成两类:
#   protected —— on-demand,不会被回收。活跃/交互沙盒放这里,避免 spot 抢占中断用户。
#   spot      —— 抢占实例,便宜但随时可能被回收。空闲/可疏散沙盒放这里省成本。
# 放置为【软约束】:目标池无活节点时 driver 会回退跨池放置(有胜于无)。
# 默认新建沙盒 → protected(刚创建通常紧接着交互/装环境,不该被抢占打断);
# 可用 DEFAULT_CREATE_POOL 改默认,或请求体 pool / meta.pool 显式指定。
# 节点池归属由 node-agent 经 IMDS instance-life-cycle 自证并上报心跳(见 node-agent/_detect_pool)。
DEFAULT_CREATE_POOL = os.environ.get("DEFAULT_CREATE_POOL", "protected").strip().lower()
# 总开关:关掉(0)则 pool 一律 None(不限池,回到原行为),便于单池集群或回滚。
POOL_PLACEMENT_ENABLED = os.environ.get("POOL_PLACEMENT_ENABLED", "1").lower() in ("1", "true")


def _placement_pool(body: dict, spec: "SandboxSpec") -> str | None:
    """决定新建沙盒的目标池。返回 "spot"/"protected",或 None(不限池)。
    优先级:请求体 pool > meta.pool > DEFAULT_CREATE_POOL。非法值回退默认池(默认也非法则 None)。"""
    if not POOL_PLACEMENT_ENABLED:
        return None
    explicit = body.get("pool") or (spec.meta or {}).get("pool")
    cand = str(explicit or DEFAULT_CREATE_POOL or "").strip().lower()
    if cand in ("spot", "protected"):
        return cand
    dflt = str(DEFAULT_CREATE_POOL or "").strip().lower()
    return dflt if dflt in ("spot", "protected") else None


@observed_operation("create")
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
    pool      = _placement_pool(body, spec)   # M2:目标池(protected/spot/None)

    record: dict = {
        "id":               sid,
        "tenant_id":        tenant_id,
        "state":            "creating",
        "driver":           _DRIVER_NAME,
        "image":            spec.image,
        "cpu":              spec.cpu,
        "mem_mib":          spec.mem_mib,
        "env":              spec.env,
        # M2:期望池(记录供观测;实际落点见 driver 返回的 node)。
        "pool":             pool or "",
        "created_at":       db._utcnow(),
        "updated_at":       db._utcnow(),
        # 自动休眠用:最后活跃时刻。初始 = 创建时刻;之后由 proxy/exec 热路径 _touch_activity 刷新。
        "last_active_at":   db._utcnow(),
        "meta":             spec.meta,
        # 声明要暴露的服务端口 —— 供端口暴露反代(/s/{id}/{port})校验"该端口是否允许对外"。
        "services":         [{"port": s.port, "protocol": s.protocol,
                             "autostop": s.autostop, "autostart": s.autostart}
                            for s in spec.services],
    }
    if idem_key:
        record["idempotency_key"] = idem_key

    db.put(record)

    if _CRD_CONTROL_ENABLED:
        try:
            assert _crd_store is not None
            _crd_store.create_confirmed(record)
            code, result = _crd_transition_result(
                sid, {"running"}, CRD_API_WAIT_S
            )
            return (201 if code == 200 else code), result
        except CRDCreateOutcomeUnknown as e:
            # The Kubernetes write may already have committed. Keep the DDB
            # projection so the operator and caller have a stable id to
            # reconcile instead of silently creating an untracked microVM.
            return 503, {
                "error": str(e),
                "id": sid,
                "current_state": "creating",
                "retryable": True,
            }
        except Exception as e:
            # create_confirmed only reaches this branch after a successful GET
            # proved that no CR exists. Delete the projection so an
            # idempotency key can safely retry.
            try:
                db.delete(sid)
            except Exception:
                pass
            return 500, {"error": str(e)}

    try:
        # 先尝试从暖池 resume(FC 模式 ~7ms);失败或不支持则冷建。
        # 暖池预热的是默认(min)rootfs;若请求了非默认 image,暖池的快照不匹配 →
        # 跳过暖池直接冷建,才会走 op_create 的模板选择(CoW 对应 rootfs-{name}.ext4)。
        from sandbox_api.drivers.firecracker import (
            normalize_image,
            requested_placement_group,
        )
        wants_default = normalize_image(spec.image) == "min"
        placement_group = requested_placement_group(spec)
        # 暖池条目带明确 protected/spot 归属，只在请求池匹配时 claim；
        # spot 请求不会误拿 protected 预热 VM。显式恢复组的请求跳过通用
        # 暖池，因为暖池条目没有恢复组归属，不能证明位于目标组。
        use_warm = (
            wants_default
            and placement_group is None
            and _warm_pool.can_claim(pool)
        )
        claimed = _warm_pool.claim(sid, spec, pool=pool) if use_warm else False
        if not claimed:
            driver_fields = _driver.create(sid, spec, pool=pool)
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
        except Exception as exc:
            log_event(
                "error", "base_snapshot_failed",
                sandbox_id=sid, error_type=type(exc).__name__,
            )  # base 失败 → 疏散时降级 Full,不阻断

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
    "creating", "running", "suspending", "suspended", "slept", "resuming",
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


def admin_images() -> tuple[int, dict]:
    """可用镜像/rootfs 模板列表(供 Portal 创建表单下拉)。"""
    from sandbox_api.drivers.firecracker import available_images
    imgs = available_images()
    # 附带简短说明,web 预设自带站点
    desc = {
        "min": "基础镜像(python + sshd + exec agent),无预置服务",
        "web": "自带 demo 站点,开机自起 :80 —— 端口暴露打开即见页面",
    }
    return 200, {"images": [{"name": n, "desc": desc.get(n, "")} for n in imgs]}


def admin_cluster() -> tuple[int, dict]:
    """集群级信息,供 Portal 拼接端口暴露 URL。"""
    return 200, {
        "nlb_hostname": NLB_HOSTNAME,
        # 端口暴露访问前缀:{prefix}/s/{sid}/{port}/;NLB 未配置时前端用相对路径。
        "proxy_base": f"http://{NLB_HOSTNAME}" if NLB_HOSTNAME else "",
        # 任意端口模式:前端据此提供"输入任意端口打开"的入口,而非只列 declared 端口。
        "allow_all_ports": ALLOW_ALL_PORTS,
        # 端口暴露鉴权:非空则访问 URL 需附 ?token=。此接口本身要求 admin key,
        # 返回 token 供 Portal 拼接可点击链接(admin 视角,可接受)。
        "expose_token": EXPOSE_TOKEN,
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


def health_report(require_dependencies: bool) -> tuple[int, dict]:
    stale = stale_loops()
    checks: dict[str, object] = {
        "background_loops": "ok" if not stale else {"stale": stale},
    }
    healthy = not stale
    if require_dependencies:
        try:
            nodes = db.list_active_nodes()
            checks["dynamodb"] = "ok"
            checks["active_nodes"] = len(nodes)
            if not nodes:
                healthy = False
        except Exception as exc:
            checks["dynamodb"] = {"error_type": type(exc).__name__}
            checks["active_nodes"] = 0
            healthy = False
        if _CRD_CONTROL_ENABLED:
            try:
                assert _crd_store is not None
                _crd_store.ready()
                checks["firecracker_crd"] = "ok"
            except Exception as exc:
                checks["firecracker_crd"] = {
                    "error_type": type(exc).__name__
                }
                healthy = False
    return (200 if healthy else 503), {
        "status": "ok" if healthy else "unhealthy",
        "checks": checks,
    }


# ---------- 端口暴露反代(sandbox-proxy)----------
# 路径路由 /s/{sid}/{port}/{rest} —— 用路径(而非 Host 子域名)定位沙盒,因为:
#   1) 先用 NLB 自带域名,挂不了通配符子域名,Host 头无法区分沙盒;
#   2) 需支持"多沙盒暴露同一内部端口"——路由键是 sid,与宿主端口/Host 解耦。
# 解析出 (sid, port) → 查 DynamoDB 拿沙盒所在 node → 转发到该 node-agent 的
# /proxy/{sid}/{port}/{rest}(node-agent 再转进 guest 172.18.x.2:port)。

def _raw_tunnel(a, b, on_activity=None, heartbeat_s: float = 5.0) -> None:
    """两个已连接 socket 间双向透传字节,任一方关闭即结束(WebSocket 隧道用)。"""
    import select
    socks = [a, b]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, heartbeat_s)
            if x:
                break
            if not r:
                if on_activity:
                    on_activity()
                continue
            for s in r:
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                (b if s is a else a).sendall(data)
                if on_activity:
                    on_activity()
    finally:
        for s in socks:
            try:
                s.shutdown(2)  # SHUT_RDWR
            except OSError:
                pass


def _ensure_awake(sid: str, record: dict) -> dict:
    """
    网关透明唤醒(fly.io 式):请求打到网关时,若沙盒是自动休眠(slept)且允许 autostart,
    则触发 resume 并等待其回到 running,返回最新 record;否则原样返回(由调用方走既有 409)。

    - running          → 原样返回。
    - slept + autostart → 触发 resume(并发请求靠 lease 条件写天然互斥,只有一个真正 resume,
      其余落到轮询等待);轮询 db.get 直到 running 或 AUTO_WAKE_TIMEOUT_S 超时。
    - 手动 suspended / 其他态 → 不唤醒,原样返回(维持既有行为,手动挂起不被请求唤醒)。
    """
    if record.get("state") == "running":
        return record
    if record.get("state") != "slept" or not _autostart_enabled(record):
        return record

    wake_started = time.monotonic()
    wake_result = "timeout"
    # 触发唤醒。resume_sandbox 内部用 lease,并发请求只有一个抢到锁真正 resume;
    # 抢不到的直接进下面的轮询,等 leader/first 请求把它拉起来。
    try:
        resume_sandbox(sid)
    except Exception:
        pass  # 唤醒失败(如锁被占)不抛;交给下面轮询看最终态

    deadline = time.monotonic() + AUTO_WAKE_TIMEOUT_S
    while time.monotonic() < deadline:
        cur = db.get(sid)
        if not cur:
            wake_result = "missing"
            WAKE_RPC_DURATION.labels(wake_result).observe(time.monotonic() - wake_started)
            return record
        state = cur.get("state")
        if state == "running":
            wake_result = "success"
            WAKE_RPC_DURATION.labels(wake_result).observe(time.monotonic() - wake_started)
            return cur
        if state == "failed":
            wake_result = "failed"
            WAKE_RPC_DURATION.labels(wake_result).observe(time.monotonic() - wake_started)
            return cur  # resume 失败,不再干等
        # resuming(本请求或并发请求正在拉起)→ 继续轮询;slept/其他态短暂窗口也再看一眼
        time.sleep(0.3)  # nosemgrep: arbitrary-sleep -- 轮询唤醒收敛
    WAKE_RPC_DURATION.labels(wake_result).observe(time.monotonic() - wake_started)
    return db.get(sid) or record


def resolve_proxy_target(sid: str, port: int) -> tuple[int, dict] | tuple[int, str, str]:
    """
    校验并解析反代目标。
    成功 → (200, node_host, upstream_path_prefix);失败 → (code, {error}).
    """
    record = db.get(sid)
    if not record:
        return 404, {"error": "sandbox not found", "id": sid}
    # 自动休眠(slept)+ autostart:网关透明唤醒后继续用新 record 转发(首请求秒级阻塞,后续无感)。
    # 手动 suspended 不会被唤醒 → 仍走下面的 409。
    if record.get("state") != "running":
        record = _ensure_awake(sid, record)
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
    _touch_activity(sid)  # 经网关的 HTTP 流量算活跃 —— 刷新最后活跃时间(内存节流)
    host = node if ":" in node else f"{node}:{NODE_AGENT_PORT}"
    return 200, host, f"/proxy/{sid}/{port}"


@observed_operation("destroy")
def destroy_sandbox(sid: str, caller_tenant: str | None = None) -> tuple[int, dict]:
    record = db.get(sid)
    if not record:
        return 404, {"error": "not found"}
    if (denied := _check_tenant_access(record, caller_tenant)):
        return denied

    if _CRD_CONTROL_ENABLED:
        try:
            _ensure_crd(record)
            assert _crd_store is not None
            _crd_store.delete(sid)
            initial_failed = record.get("state") == "failed"
            initial_error = record.get("error", "")
            initial_updated_at = record.get("updated_at", "")
            deadline = time.monotonic() + CRD_DELETE_WAIT_S
            while time.monotonic() < deadline:
                current = db.get(sid)
                if current is None:
                    return 200, {"id": sid, "deleted": True}
                if current.get("state") == "failed":
                    # A sandbox may already be failed before DELETE. That is
                    # still a valid cleanup request, so wait for the
                    # finalizer instead of reporting the old failure as a
                    # deletion failure. A new failed projection after the
                    # request indicates the destroy action itself failed.
                    delete_failed = (
                        not initial_failed
                        or current.get("error", "") != initial_error
                        or current.get("updated_at", "")
                        != initial_updated_at
                    )
                    if delete_failed:
                        return 500, current
                time.sleep(0.25)  # nosemgrep: arbitrary-sleep -- 等 finalizer
            current = db.get(sid) or {}
            return 504, {
                "error": "operator deletion timeout",
                "id": sid,
                "current_state": current.get("state"),
            }
        except Exception as exc:
            return 500, {"error": str(exc)}

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


@observed_operation("suspend")
def suspend_sandbox(sid: str, caller_tenant: str | None = None) -> tuple[int, dict]:
    record = db.get(sid)
    if not record:
        return 404, {"error": "not found"}
    if (denied := _check_tenant_access(record, caller_tenant)):
        return denied

    if _CRD_CONTROL_ENABLED:
        if record.get("state") != "running":
            return 409, {
                "error": "sandbox is not in running state",
                "state": record.get("state"),
            }
        try:
            operation_id = _request_crd_state(
                record, "Suspended", suspend_reason="manual"
            )
            return _crd_transition_result(
                sid, {"suspended"}, operation_id=operation_id
            )
        except Exception as exc:
            return 500, {"error": str(exc)}

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


@observed_operation("auto_sleep")
def auto_sleep_sandbox(sid: str) -> tuple[int, dict]:
    """
    自动休眠:把空闲的 running 沙盒打快照进 slept 状态(区别于手动 suspend 的 suspended)。
    镜像 suspend_sandbox 的 lease + prev_state 条件写 + 失败回滚,复用同一套并发保护;
    差别仅在目标状态标 slept(供网关识别"可自动唤醒")+ 事件带 idle 原因。

    被 AutoSleeper 扫描 loop 注入调用。拿到 lease 后【二次校验仍空闲】——防扫描判定与加锁
    之间刚来请求的竞态(不空闲则放弃本次,下轮再看)。
    """
    record = db.get(sid)
    if not record or record.get("state") != "running":
        return 409, {"error": "not running"}

    if _CRD_CONTROL_ENABLED:
        decision = _idle_detector.decide(record)
        if not decision.idle:
            return 200, {
                "skipped": "no longer idle",
                "idle_s": decision.idle_seconds,
                "blockers": list(decision.blockers),
            }
        try:
            operation_id = _request_crd_state(
                record, "Suspended", suspend_reason="idle"
            )
            return _crd_transition_result(
                sid, {"slept"}, operation_id=operation_id
            )
        except Exception as exc:
            return 500, {"error": str(exc)}

    lease_id = None
    try:
        lease_id = db.acquire_lease(sid)
        # 二次校验:重读 record,确认仍 running 且确实空闲(last_active_at 超过 idle 阈值)。
        fresh = db.get(sid)
        if not fresh or fresh.get("state") != "running":
            return 409, {"error": "state changed"}
        decision = _idle_detector.decide(fresh)
        if not decision.idle:
            return 200, {
                "skipped": "no longer idle",
                "idle_s": decision.idle_seconds,
                "blockers": list(decision.blockers),
            }
        idle_s = decision.idle_seconds

        db.update_state(sid, "suspending", "running")
        snap_info = _driver.suspend(sid, fresh)
        db.update_state(sid, "slept", "suspending", snap_info)
        db.write_event(sid, "slept", "running",
                       {**snap_info, "reason": "idle", "idle_s": round(idle_s, 1)})
        # 休眠后从活跃节流缓存移除(已不在 running,无需再节流)
        _idle_detector.forget(sid)
        return 200, db.get(sid)
    except UnsupportedOperation as e:
        return 501, {"error": str(e)}
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return 409, {"error": "sandbox is not in running state or is locked"}
        return 500, {"error": str(e)}
    except Exception as e:
        # 快照失败:node-agent 已尽力把 VM 恢复运行,内存未释放 → 回滚 running(同 suspend)。
        db.force_update(sid, {"state": "running", "error": str(e)})
        return 500, {"error": str(e)}
    finally:
        if lease_id:
            db.release_lease(sid, lease_id)


def _idle_seconds(record: dict) -> float | None:
    """Compatibility helper for API/tests; the detector owns timestamp parsing."""
    return _idle_detector.decide(record).idle_seconds


@observed_operation("resume")
def resume_sandbox(sid: str, caller_tenant: str | None = None) -> tuple[int, dict]:
    record = db.get(sid)
    if not record:
        return 404, {"error": "not found"}
    if (denied := _check_tenant_access(record, caller_tenant)):
        return denied

    if _CRD_CONTROL_ENABLED:
        prev = record.get("state")
        if prev not in (
            "suspended", "slept", "needs_reschedule", "orphaned"
        ):
            return 409, {
                "error": "sandbox is not in a resumable state",
                "state": prev,
            }
        try:
            operation_id = _request_crd_state(record, "Running")
            return _crd_transition_result(
                sid, {"running"}, operation_id=operation_id
            )
        except Exception as exc:
            return 500, {"error": str(exc)}

    lease_id = None
    try:
        lease_id = db.acquire_lease(sid)
        # prev 允许 suspended(手动挂起)或 slept(自动休眠)——两者底层同一套快照,resume 共用。
        # 用当前 record.state 作为条件写的期望值(而非写死 suspended),网关自动唤醒 slept 也走这里。
        prev = record["state"]
        if prev not in ("suspended", "slept"):
            return 409, {"error": "sandbox is not in a resumable state", "state": prev}
        db.update_state(sid, "resuming", prev)
        # 限流:同时最多 _RESUME_CONCURRENCY 个 resume 走 driver(合并+load 的 EBS I/O 重)。
        # 排队期间沙盒停在 resuming(可观测),不额外占资源。t0 只计真正 resume 不含排队。
        queue_started = time.monotonic()
        if _RESUME_SEM is not None:
            _RESUME_SEM.acquire()
        queue_wait = time.monotonic() - queue_started
        RESUME_QUEUE_WAIT.observe(queue_wait)
        RESUME_INFLIGHT.inc()
        try:
            t0 = time.monotonic()
            driver_fields = _driver.resume(sid, record)
            restore_time  = round(time.monotonic() - t0, 4)
        finally:
            RESUME_INFLIGHT.dec()
            if _RESUME_SEM is not None:
                _RESUME_SEM.release()
        # resume 成功即回到活跃,给刚唤醒的沙盒一个完整 idle 周期。
        _idle_detector.forget(sid)
        db.update_state(sid, "running", "resuming",
                        {**driver_fields, "restore_time_s": str(restore_time),
                         "resume_queue_wait_s": str(round(queue_wait, 4)),
                         "last_active_at": db._utcnow()})
        db.write_event(sid, "resumed", prev,
                       {"restore_time_s": restore_time,
                        "resume_queue_wait_s": round(queue_wait, 4)})
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


@observed_operation("exec")
def exec_sandbox(sid: str, cmd: str, caller_tenant: str | None = None) -> tuple[int, dict]:
    record = db.get(sid)
    if not record:
        return 404, {"error": "not found"}
    if (denied := _check_tenant_access(record, caller_tenant)):
        return denied
    _touch_activity(sid)  # exec 算活跃 —— 刷新最后活跃时间,避免正在用的沙盒被自动休眠
    rc, stdout, stderr = _driver.exec(sid, record, cmd)
    return (200 if rc == 0 else 500), {
        "id": sid, "cmd": cmd, "rc": rc,
        "stdout": stdout, "stderr": stderr,
    }


# ---------- 文件上传/下载(#2)----------
# 走 exec 通道(base64 传输落 guest 文件系统):node-agent 在 microVM 外,访问 guest 文件
# 只能经 exec。base64 避二进制在 shell/JSON 里的转义问题。适合中小文件(demo/代码/产物);
# 大文件应走端口暴露 + guest 内 http。

import base64 as _b64
import shlex as _shlex

# 单文件大小上限(base64 over exec,过大易超时/占内存)。可用 env 调整。
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", str(10 * 1024 * 1024)))  # 10MB


def upload_file(sid: str, path: str, content_b64: str,
                caller_tenant: str | None = None) -> tuple[int, dict]:
    record = db.get(sid)
    if not record:
        return 404, {"error": "not found"}
    if (denied := _check_tenant_access(record, caller_tenant)):
        return denied
    _touch_activity(sid)
    try:
        raw = _b64.b64decode(content_b64, validate=True)
    except Exception:
        return 400, {"error": "content_b64 not valid base64"}
    if len(raw) > MAX_FILE_BYTES:
        return 413, {"error": f"file too large (> {MAX_FILE_BYTES} bytes)"}
    # 在 guest 内:建父目录 + base64 解码写入。path 用 shlex 防注入。
    qpath = _shlex.quote(path)
    cmd = (f"mkdir -p \"$(dirname {qpath})\" && "
           f"printf %s {_shlex.quote(content_b64)} | base64 -d > {qpath} && "
           f"echo OK $(wc -c < {qpath})")
    rc, stdout, stderr = _driver.exec(sid, record, cmd)
    _touch_activity(sid, force=True)
    if rc != 0:
        return 500, {"error": "write failed", "stderr": stderr}
    return 200, {"id": sid, "path": path, "bytes": len(raw), "result": stdout.strip()}


def download_file(sid: str, path: str,
                  caller_tenant: str | None = None) -> tuple[int, dict]:
    record = db.get(sid)
    if not record:
        return 404, {"error": "not found"}
    if (denied := _check_tenant_access(record, caller_tenant)):
        return denied
    _touch_activity(sid)
    qpath = _shlex.quote(path)
    # 先校验存在 + 大小,再 base64 输出(避免把超大文件读进内存)
    cmd = (f"test -f {qpath} || {{ echo __NOFILE__; exit 3; }}; "
           f"sz=$(wc -c < {qpath}); "
           f"if [ \"$sz\" -gt {MAX_FILE_BYTES} ]; then echo __TOOBIG__; exit 4; fi; "
           f"base64 {qpath}")
    rc, stdout, stderr = _driver.exec(sid, record, cmd)
    _touch_activity(sid, force=True)
    if "__NOFILE__" in stdout:
        return 404, {"error": "file not found in sandbox", "path": path}
    if "__TOOBIG__" in stdout:
        return 413, {"error": f"file too large (> {MAX_FILE_BYTES} bytes)"}
    if rc != 0:
        return 500, {"error": "read failed", "stderr": stderr}
    # stdout 是 base64(可能含换行),原样回;前端解码。
    return 200, {"id": sid, "path": path, "content_b64": stdout.strip()}


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


# ---------- 自动休眠扫描 loop 接线 ----------
# 放在这里(而非 import 顶部)是因为需要引用后面定义的 auto_sleep_sandbox / _idle_seconds /
# _autostop_enabled。与 reconcile / 暖池共用同一 leader 门控:多副本下只有 leader 扫描。
# AUTO_SLEEP_ENABLED=0 可整体关闭(仍不影响手动 suspend/resume 与网关唤醒逻辑)。
if AUTO_SLEEP_ENABLED and not _CRD_CONTROL_ENABLED:
    _autosleeper = AutoSleeper(
        sleep_fn         = auto_sleep_sandbox,
        idle_decision_fn = _idle_detector.decide,
        autostop_fn      = _autostop_enabled,
    )
    _autosleeper.start_loop(is_leader=lambda: _reconciler.is_leader)


# ---------- HTTP handler ----------

class Handler(BaseHTTPRequestHandler):

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        if parsed:
            self.request_id = new_request_id(self.headers.get("X-Request-ID"))
            self._otel_span, self._otel_token = start_server_span(
                self.headers, self.command, self.path
            )
        return parsed

    def handle_one_request(self) -> None:
        started = time.monotonic()
        self._response_status = 500
        self.command = None
        self.path = None
        self._otel_span = None
        self._otel_token = None
        try:
            super().handle_one_request()
        finally:
            if getattr(self, "command", None) and getattr(self, "path", None):
                duration = time.monotonic() - started
                route = record_http(
                    self.command, self.path, self._response_status, duration
                )
                if route not in {"/livez", "/readyz", "/metrics"}:
                    log_event(
                        "info", "http_request",
                        method=self.command,
                        route=route,
                        status=self._response_status,
                        duration_ms=round(duration * 1000, 3),
                    )
            finish_server_span(
                self._otel_span, self._otel_token, self._response_status
            )

    def send_response(self, code: int, message=None) -> None:
        self._response_status = code
        super().send_response(code, message)

    def end_headers(self) -> None:
        if request_id := getattr(self, "request_id", ""):
            self.send_header("X-Request-ID", request_id)
        super().end_headers()

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
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

    def _send_internal_error(self, exc: Exception) -> None:
        log_event(
            "error", "request_failed",
            method=getattr(self, "command", ""),
            route=normalize_route(getattr(self, "path", "")),
            error_type=type(exc).__name__,
        )
        self._send(500, {"error": str(exc)})

    def _parts(self) -> list[str]:
        return urlparse(self.path).path.strip("/").split("/")

    def _qs(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    # 端口暴露鉴权(#5):EXPOSE_TOKEN 非空时校验 token。
    # 来源优先级:query ?token= > Cookie sbx_token= > Header X-Sbx-Token。
    # 返回 (ok, set_cookie_token)。set_cookie_token 非空表示应把它种进 Cookie。
    def _check_expose_token(self, parsed) -> tuple[bool, str]:
        if not EXPOSE_TOKEN:
            return True, ""  # 未启用 → 公开
        q = parse_qs(parsed.query)
        qtok = (q.get("token") or [""])[0]
        if qtok:
            return (qtok == EXPOSE_TOKEN), (qtok if qtok == EXPOSE_TOKEN else "")
        # Cookie
        cookie = self.headers.get("Cookie", "")
        for kv in cookie.split(";"):
            if "=" in kv:
                k, v = kv.strip().split("=", 1)
                if k == "sbx_token" and v == EXPOSE_TOKEN:
                    return True, ""
        # Header
        if self.headers.get("X-Sbx-Token", "") == EXPOSE_TOKEN:
            return True, ""
        return False, ""

    # ---------- 端口暴露反代 /s/{sid}/{port}/{rest} ----------
    # 命中则透传到沙盒服务并返回 True。**不走 Bearer 鉴权**(浏览器打开 web 预览不带 API key);
    # 改由 EXPOSE_TOKEN 可选把关(见 _check_expose_token):留空=公开(demo),设置=需 token。
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

        ok, set_cookie = self._check_expose_token(parsed)
        if not ok:
            self._send(401, {"error": "unauthorized",
                             "hint": "append ?token=<EXPOSE_TOKEN> to the URL"})
            return True

        target = resolve_proxy_target(sid, port)
        if target[0] != 200:
            self._send(target[0], target[1]); return True
        _, node_host, up_prefix = target

        qs = f"?{parsed.query}" if parsed.query else ""
        upstream_path = f"{up_prefix}/{rest}{qs}"

        # WebSocket / Upgrade:开原始 socket 到 node-agent,重放请求后双向透传。
        if "upgrade" in self.headers.get("Connection", "").lower() and \
           self.headers.get("Upgrade", "").lower() == "websocket":
            return self._tunnel_ws(sid, node_host, upstream_path)

        try:
            n = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            n = 0
        req_body = self.rfile.read(n) if n else None

        hop = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade", "host"}
        fwd = {k: v for k, v in self.headers.items() if k.lower() not in hop}
        fwd["X-Request-ID"] = getattr(self, "request_id", "")
        inject_trace_headers(fwd)

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
        # 首次用 ?token= 通过校验 → 种 Cookie,后续子请求(JS/CSS/XHR)自动带,免重复带 token。
        if set_cookie:
            self.send_header("Set-Cookie", f"sbx_token={set_cookie}; Path=/s/{sid}/; HttpOnly")
        self.end_headers()
        self.wfile.write(data)
        conn.close()
        return True

    def _tunnel_ws(self, sid: str, node_host: str, upstream_path: str) -> bool:
        """WebSocket 反代:向 node-agent 建原始 TCP,重放请求(含 Upgrade 头),再双向透传。"""
        host, _, port_s = node_host.partition(":")
        try:
            up = socket.create_connection((host, int(port_s or NODE_AGENT_PORT)), timeout=10)
        except OSError as e:
            self._send(502, {"error": "ws node-agent unreachable", "hint": str(e)})
            return True
        lines = [f"{self.command} {upstream_path} HTTP/1.1"]
        for k, v in self.headers.items():
            if k.lower() in {"host", "x-request-id"}:
                continue
            lines.append(f"{k}: {v}")
        lines.append(f"Host: {node_host}")
        lines.append(f"X-Request-ID: {getattr(self, 'request_id', '')}")
        trace_headers: dict[str, str] = {}
        inject_trace_headers(trace_headers)
        if traceparent := trace_headers.get("traceparent"):
            lines.append(f"traceparent: {traceparent}")
        up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        with _idle_detector.connection(sid):
            _raw_tunnel(
                self.connection,
                up,
                on_activity=lambda: _touch_activity(sid),
                heartbeat_s=max(1.0, min(5.0, _idle_detector.idle_s / 3)),
            )
        return True

    def do_GET(self):
        if self._maybe_proxy():
            return
        if not _check_auth(self):
            return
        try:
            self._handle_get()
        except Exception as e:
            self._send_internal_error(e)

    def do_POST(self):
        if self._maybe_proxy():
            return
        if not _check_auth(self):
            return
        try:
            self._handle_post()
        except Exception as e:
            self._send_internal_error(e)

    def do_DELETE(self):
        if self._maybe_proxy():
            return
        if not _check_auth(self):
            return
        try:
            self._handle_delete()
        except Exception as e:
            self._send_internal_error(e)

    def do_PUT(self):
        # /s/ 反代优先;否则走鉴权 + PUT 业务(文件上传)
        if self._maybe_proxy():
            return
        if not _check_auth(self):
            return
        try:
            self._handle_put()
        except Exception as e:
            self._send_internal_error(e)

    # web 应用常用的其余 method —— 仅服务 /s/ 反代
    def _proxy_only(self):
        if self._maybe_proxy():
            return
        self._send(404, {"error": "not found"})

    do_PATCH   = _proxy_only
    do_OPTIONS = _proxy_only
    do_HEAD    = _proxy_only

    def _handle_get(self):
        p = self._parts()

        if p == ["metrics"]:
            body, content_type = metrics_payload()
            return self._send_bytes(200, body, content_type)

        if p == ["livez"]:
            code, result = health_report(require_dependencies=False)
            return self._send(code, result)

        if p == ["readyz"]:
            code, result = health_report(require_dependencies=True)
            return self._send(code, result)

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
            if p == ["admin", "images"]:
                code, result = admin_images()
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

            # GET /sandboxes/{id}/files?path=/abs/path  下载(返回 content_b64)
            if len(p) == 3 and p[2] == "files":
                path = (self._qs().get("path") or [""])[0]
                if not path:
                    return self._send(400, {"error": "missing ?path="})
                code, result = download_file(sid, path, _get_caller_tenant(self))
                return self._send(code, result)

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
                "GET    /admin/images",
                "ANY    /s/{id}/{port}/{path}  (sandbox port proxy)",
                "PUT    /sandboxes/{id}/files?path=  (upload, body: content_b64)",
                "GET    /sandboxes/{id}/files?path=  (download → content_b64)",
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

    def _handle_put(self):
        p = self._parts()
        # PUT /sandboxes/{id}/files?path=/abs/path  body: {content_b64}  上传
        if len(p) == 3 and p[0] == "sandboxes" and p[2] == "files":
            path = (self._qs().get("path") or [""])[0]
            if not path:
                return self._send(400, {"error": "missing ?path="})
            body = self._body()
            content_b64 = body.get("content_b64", "")
            if not content_b64:
                return self._send(400, {"error": "missing content_b64"})
            code, result = upload_file(p[1], path, content_b64, _get_caller_tenant(self))
            return self._send(code, result)
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"控制面 API [{_DRIVER_NAME} driver] 在 http://{LISTEN_HOST}:{LISTEN_PORT}")
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
