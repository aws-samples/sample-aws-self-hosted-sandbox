#!/usr/bin/env python3
"""
node-agent — 每台 .metal 节点上的 on-host 执行手。

职责(只做本地操作,状态读写全走控制面 / DynamoDB):
  - 启动 / 销毁 Firecracker microVM(用 jailer 包裹)
  - 管理 tap 网络(tap_idx 由控制面分配,不再自分配)
  - 触发快照创建(本地)+ 异步上传 S3
  - 从 S3 拉快照三件套恢复 microVM

接口:
  POST /vm/create   {id, rootfs_path, tap_idx, cpu, mem_mib, kernel, env}
  POST /vm/destroy  {id}
  POST /vm/suspend  {id, snapshot_local_path, s3_prefix}
  POST /vm/resume   {id, snapshot_local_path, rootfs_path, tap_idx, s3_prefix}
  POST /vm/exec     {id, cmd}
  GET  /vm/{id}     → {pid, state, ip}
  GET  /health      → {node_id, free_mem_mib, vm_count}

安全(V2401449830):node-agent 是 root 宿主执行面,只允许控制面调用。
  - 除 /health /livez /readyz /metrics 外,所有路由要求 HMAC 签名(agent_auth.py),
    且来源必须在 ALLOWED_CALLER_CIDR 内(空 = 拒绝);guest tap 网段 172.18.0.0/16 一律拒绝。
  - 只监听节点管理网卡 IP(不再 0.0.0.0);启动时安装 SBX-GUEST-IN/FWD iptables 链,
    guest 无法访问宿主端口 / IMDS / 其他 guest / 其他节点 node-agent。
  - 请求里的 id / 宿主路径 / s3_prefix 必须符合服务端路径约定(见"请求参数校验")。
  - /reclaim/simulate、/reclaim/reset 仅 NODE_AGENT_ENABLE_TEST_HOOKS=1 时可用。
  任一前置条件缺失(密钥、白名单、防火墙)即拒绝启动。

运行(需 root,在 .metal 宿主;生产走 terraform stage2 的 DaemonSet):
  sudo env NODE_AGENT_AUTH_KEY_FILE=/etc/sbx/node-agent.key \
           ALLOWED_CALLER_CIDR=10.0.0.0/16 SNAPSHOT_S3_BUCKET=<bucket> \
           python3 main.py   # 默认 <节点内网 IP>:8002
  密钥文件(>=32 字节,root 0600)须与控制面 NODE_AGENT_AUTH_KEY 一致。宿主进程模式下 aws CLI
  使用实例角色:该角色只应授予快照桶 sbx/ 前缀的读写(同 stage2 aws_iam_role_policy.node_agent),
  不要挂 AmazonS3ReadOnlyAccess 之类的宽权限。
"""
from __future__ import annotations

import http.client
import hashlib
import hmac
import json
import os
import re
import signal
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse

from observability import (
    FC_RESUME_INFLIGHT,
    NODE_HEARTBEAT_ERRORS,
    finish_server_span,
    log_event,
    metrics_payload,
    new_request_id,
    normalize_route,
    record_fc_operation,
    record_http,
    record_restore_mode,
    record_resume_stage,
    record_security_event,
    record_snapshot_error,
    record_snapshot_legacy_migration,
    record_snapshot_transfer,
    record_snapshot_verify,
    refresh_node_metrics,
    start_server_span,
)

import agent_auth

# ---------- 配置 ----------
LISTEN_PORT  = int(os.environ.get("NODE_AGENT_PORT", "8002"))
# 监听地址:默认绑本节点主网卡内网 IP(管理面,见 _listen_host),不再绑 0.0.0.0。
# DaemonSet 用 status.hostIP 显式注入。注意 Linux 弱主机模型下 guest 仍能经 tap 网关
# 访问宿主任意本地地址,真正挡住 guest 的是 _install_guest_firewall 的 INPUT 规则 + 签名鉴权。
LISTEN_HOST  = os.environ.get("NODE_AGENT_LISTEN_HOST", "")
# 允许调用受保护路由的来源 CIDR(逗号分隔)。空 = 全部拒绝(fail closed);
# 生产设为控制面 Pod 所在网段(terraform stage2 默认集群 VPC CIDR)。
ALLOWED_CALLER_CIDR = os.environ.get("ALLOWED_CALLER_CIDR", "")
# 永远拒绝的来源 CIDR:guest tap 网段(172.18.x / tap_idx>255 时 172.19.x,见 _tap_addresses),
# 对所有路由(含健康检查)生效。
DENIED_CALLER_CIDR  = os.environ.get("DENIED_CALLER_CIDR", "172.18.0.0/16,172.19.0.0/16")
# 控制面 ↔ node-agent 共享的 HMAC 密钥(见 agent_auth.py)。缺失则拒绝启动。
_AUTH = agent_auth.Verifier(
    agent_auth.load_key(os.environ.get("NODE_AGENT_AUTH_KEY", ""),
                        os.environ.get("NODE_AGENT_AUTH_KEY_FILE", "")),
    max_skew_s=int(os.environ.get("NODE_AGENT_AUTH_MAX_SKEW_S", "300")),
)
# /reclaim/simulate、/reclaim/reset 仅测试用,生产默认关闭(返回 404)。
ENABLE_TEST_HOOKS = os.environ.get("NODE_AGENT_ENABLE_TEST_HOOKS", "0").lower() in ("1", "true")
# 启动时安装宿主 iptables 规则隔离 guest(禁止 guest 访问宿主/IMDS/其他 guest/其他节点 agent)。
GUEST_FIREWALL = os.environ.get("NODE_AGENT_GUEST_FIREWALL", "1").lower() in ("1", "true")
# 快照 S3 桶:s3_prefix 只允许 s3://{SNAPSHOT_S3_BUCKET}/sbx/{id}/,未配置则不允许任何 S3 前缀。
SNAPSHOT_S3_BUCKET = os.environ.get("SNAPSHOT_S3_BUCKET", "").strip()
# 控制类请求(/vm/*、/reclaim/*)body 上限;/proxy/ 透传 body 上限另设。
MAX_CONTROL_BODY_BYTES = int(os.environ.get("NODE_AGENT_MAX_BODY_BYTES", str(1024 * 1024)))
MAX_PROXY_BODY_BYTES   = int(os.environ.get("NODE_AGENT_MAX_PROXY_BODY_BYTES", str(512 * 1024 * 1024)))
SBX_BASE     = os.environ.get("SBX_BASE", "/var/lib/sbx")       # 统一路径约定
ROOTFS       = os.environ.get("FC_ROOTFS",  "/opt/sbx/rootfs.ext4")  # 基础(默认)rootfs 模板
ROOTFS_DIR   = os.environ.get("FC_ROOTFS_DIR", "/opt/sbx")      # 命名 rootfs 模板目录


_ROOTFS_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _available_rootfs_templates() -> dict[str, str]:
    """扫 ROOTFS_DIR,枚举已存在的 rootfs-{name}.ext4 → {name: 绝对路径}。
    路径全部来自 os.listdir 的可信结果,不含任何用户输入拼接。"""
    out: dict[str, str] = {}
    try:
        for fn in os.listdir(ROOTFS_DIR):
            if fn.startswith("rootfs-") and fn.endswith(".ext4"):
                nm = fn[len("rootfs-"):-len(".ext4")]
                if nm:
                    out[nm] = os.path.join(ROOTFS_DIR, fn)
    except OSError:
        pass
    return out


def _rootfs_template_path(name: str) -> str:
    """按镜像模板名返回 rootfs.ext4 路径。
    name 为空/"min"/"default" → 默认 ROOTFS;否则在【已枚举的模板字典】里查该 name。
    查不到(未构建 / 非法名)一律回退默认,保证任何 image 都能起。

    安全:用户输入 name 仅作 dict 键查找,返回的路径来自 os.listdir 结果,
    绝不拼接用户输入到路径 → 无路径注入可能。额外用正则先挡非法名。"""
    name = (name or "").strip()
    if not name or name in ("min", "default") or not _ROOTFS_NAME_RE.match(name):
        return ROOTFS
    return _available_rootfs_templates().get(name, ROOTFS)


# rootfs 模板在根盘 /opt/sbx,沙盒目录在状态盘 SBX_BASE:跨文件系统时
# cp --reflink=auto 退化成整份实拷(每沙盒 2 GiB)。在状态盘上缓存一份
# 稀疏化模板,沙盒 rootfs 从缓存 reflink(XFS reflink=1 / ZFS block clone),
# 只有 guest 改写的块才占新空间。ROOTFS_TEMPLATE_CACHE=0 关闭。
ROOTFS_TEMPLATE_CACHE = os.environ.get(
    "ROOTFS_TEMPLATE_CACHE", "1"
).lower() in ("1", "true")
_TEMPLATE_CACHE_DIR = os.path.join(SBX_BASE, ".templates")
_TEMPLATE_CACHE_LOCK = threading.Lock()


def _cached_rootfs_template(src: str) -> str:
    """返回与 SBX_BASE 同文件系统的模板副本路径;失败时回退 src。"""
    if not ROOTFS_TEMPLATE_CACHE:
        return src
    try:
        st = os.stat(src)
        if os.stat(SBX_BASE).st_dev == st.st_dev:
            return src
        name = os.path.basename(src)
        # 源模板变化(重建/更新)时 size/mtime 变化 → 新缓存文件
        cached = os.path.join(
            _TEMPLATE_CACHE_DIR, f"{name}.{st.st_size}.{st.st_mtime_ns}"
        )
        if os.path.exists(cached):
            return cached
        with _TEMPLATE_CACHE_LOCK:
            if os.path.exists(cached):
                return cached
            os.makedirs(_TEMPLATE_CACHE_DIR, exist_ok=True)
            tmp = f"{cached}.tmp"
            subprocess.run(["cp", "--sparse=always", src, tmp], check=True)
            os.chmod(tmp, 0o444)
            os.replace(tmp, cached)
        return cached
    except Exception as exc:  # 缓存只是优化,任何失败都回退原路径
        print(f"[rootfs] template cache disabled for {src}: {exc}",
              file=sys.stderr, flush=True)
        return src


# 默认给每个新建 VM 挂 virtio-balloon 并开启 free page reporting:guest 释放的
# 内存会还给宿主(真机实测 3.2GiB→240MiB);不开时宿主 RSS 只涨不降,内存超分
# 随时间失效。create 请求显式传 balloon 时以请求为准。
BALLOON_FREE_PAGE_REPORTING = os.environ.get(
    "BALLOON_FREE_PAGE_REPORTING", "1"
).lower() in ("1", "true")
_DEFAULT_BALLOON = {
    "amount_mib": 0,
    "deflate_on_oom": True,
    "stats_polling_interval_s": 0,
    "free_page_reporting": True,
}


JAILER_BIN   = os.environ.get("JAILER_BIN", "/usr/local/bin/firecracker-jailer")
FC_BIN       = os.environ.get("FC_BIN",     "/usr/local/bin/firecracker")
VMM_LAUNCH_MODE = os.environ.get(
    "VMM_LAUNCH_MODE", "subprocess"
).strip().lower()
# Keep the historical USE_BARE_FC switch as a compatibility fallback, while
# making the positive production setting explicit.
VMM_USE_JAILER = os.environ.get(
    "VMM_USE_JAILER",
    "0" if os.environ.get("USE_BARE_FC", "1") == "1" else "1",
).strip().lower() in {"1", "true", "yes"}
HOST_NSENTER = os.environ.get("HOST_NSENTER", "/usr/bin/nsenter")
HOST_VMM_CTL = os.environ.get(
    "HOST_VMM_CTL", "/usr/local/sbin/sbx-vmm-runtime"
)
HOST_STATE_CTL = os.environ.get(
    "HOST_STATE_CTL", "/usr/local/sbin/sbx-state-volume"
)
HOST_IFACE   = os.environ.get("HOST_IFACE", "")                 # 空则自动探测
AWS_REGION   = os.environ.get("AWS_REGION", "us-east-1")
NODE_ID      = os.environ.get("NODE_ID", socket.gethostname())

# ---------- 心跳注册(P0-3:控制面按 last_seen 判活,替换 FC_NODES 硬编码)----------
NODES_TABLE       = os.environ.get("DYNAMODB_NODES_TABLE", "sandbox_nodes")
SANDBOXES_TABLE   = os.environ.get("DYNAMODB_TABLE", "sandboxes")
EVENTS_TABLE      = os.environ.get("DYNAMODB_EVENTS_TABLE", "sandbox_events")
HEARTBEAT_EVERY_S = int(os.environ.get("HEARTBEAT_EVERY_S", "30"))
# 上报给控制面的节点标识:必须是控制面能 HTTP 连到 node-agent 的地址。
# 默认自动探测主网卡内网 IP;可用 NODE_ADVERTISE_IP 覆盖。
NODE_ADVERTISE_IP = os.environ.get("NODE_ADVERTISE_IP", "")

# ---------- Spot 回收信号监听(Block 1:IMDS → 自动疏散)----------
# node-agent 在本节点(有 IMDS 访问)轮询 spot 回收信号,收到后疏散本节点沙盒。
# 默认 DRY-RUN:只记录/上报"会疏散哪些",不真打快照 —— 先验证检测+决策链路。
IMDS_BASE             = os.environ.get("IMDS_BASE", "http://169.254.169.254")
RECLAIM_WATCH         = os.environ.get("RECLAIM_WATCH_ENABLED", "1").lower() in ("1", "true")
RECLAIM_POLL_S        = int(os.environ.get("RECLAIM_POLL_S", "5"))
# =0(默认)DRY-RUN 只记录计划;=1 才真正触发疏散(打 Diff 快照到持久 EBS)。
RECLAIM_AUTO_EVACUATE = os.environ.get("RECLAIM_AUTO_EVACUATE", "0").lower() in ("1", "true")
RECLAIM_SNAPSHOT_CONCURRENCY = max(
    1, int(os.environ.get("RECLAIM_SNAPSHOT_CONCURRENCY", "12"))
)
RECLAIM_BUDGET_S = max(10, int(os.environ.get("RECLAIM_BUDGET_S", "120")))
# 在实例终止前留出写 journal、fsync、控制面观测的尾部预算。
RECLAIM_COMMIT_RESERVE_S = max(
    2, int(os.environ.get("RECLAIM_COMMIT_RESERVE_S", "8"))
)
NODE_RECOVERY_ROLE = os.environ.get("NODE_RECOVERY_ROLE", "").strip().lower()
NODE_RECOVERY_GROUP = os.environ.get("NODE_RECOVERY_GROUP", "").strip()
STATE_VOLUME_ID_OVERRIDE = os.environ.get("STATE_VOLUME_ID", "").strip()
NODE_LABEL_REFRESH_S = max(
    5, int(os.environ.get("NODE_LABEL_REFRESH_S", "30"))
)
K8S_SERVICE_ACCOUNT_DIR = os.environ.get(
    "K8S_SERVICE_ACCOUNT_DIR",
    "/var/run/secrets/kubernetes.io/serviceaccount",
)
CLOUD_INIT_STATUS_PATH = os.environ.get(
    "CLOUD_INIT_STATUS_PATH",
    "/host/var/lib/cloud/data/status.json",
)
NODE_BOOTSTRAP_MARKER = os.environ.get(
    "NODE_BOOTSTRAP_MARKER",
    "/opt/sbx/.bootstrap-complete",
)
NODE_BOOTSTRAP_WAIT_S = max(
    0, int(os.environ.get("NODE_BOOTSTRAP_WAIT_S", "900"))
)
NODE_STABILITY_MIN_AGE_S = max(
    0, int(os.environ.get("NODE_STABILITY_MIN_AGE_S", "180"))
)
# 最近一次回收检测/疏散计划(供 GET /reclaim/status 观测;injected 供测试注入)
_RECLAIM_STATE: dict = {"detected": False, "signal": None, "at": None,
                        "plan": None, "evacuated": False, "injected": None}
_INSTANCE_ID_CACHE = ""
_AZ_CACHE = ""
_STATE_VOLUME_CACHE = ""
_NODE_RECOVERY_IDENTITY_CACHE: dict[str, object] = {
    "role": "",
    "group": "",
    "resolved": False,
    "fetched_at": 0.0,
}
_NODE_RECOVERY_IDENTITY_LOCK = threading.Lock()

# ---------- JuiceFS 配置（方案 B：workspace 在 S3，快照不含磁盘）----------
JUICEFS_ENABLED    = os.environ.get("JUICEFS_ENABLED", "false").lower() == "true"
JUICEFS_BUCKET     = os.environ.get("JUICEFS_BUCKET", "")
JUICEFS_REDIS_ADDR = os.environ.get("JUICEFS_REDIS_ADDR", "")
JUICEFS_MOUNT_POINT = "/workspace"                # guest 内挂载点（固定）
JUICEFS_FS_NAME    = "sbxfs"                      # JuiceFS 文件系统名（全局唯一）

# 进程内运行时表:id → {pid, sock, tap, ip, state}
# 重启后靠控制面重新 reconcile;这里只是操作句柄缓存。
_VMS: dict[str, dict] = {}
_LOCK = threading.Lock()
_VM_OP_LOCKS: dict[str, threading.RLock] = {}
_VM_OP_LOCK_USERS: dict[str, int] = {}
_HEARTBEAT_LAST_SUCCESS = 0.0
_HEARTBEAT_LAST_ITERATION = time.monotonic()

os.makedirs(SBX_BASE, exist_ok=True)


def _node_bootstrap_ready() -> bool:
    """Only advertise after bootstrap and a fresh-node stability window.

    The explicit marker is written at the very end of our custom bootstrap and
    is authoritative. Older launch templates do not write it, so they must
    satisfy both cloud-init completion and a minimum Kubernetes Node age. This
    keeps the agent from accepting work before a late kubelet/containerd
    restart rebuilds its pod sandbox.
    """
    if NODE_BOOTSTRAP_MARKER and os.path.exists(NODE_BOOTSTRAP_MARKER):
        return True
    try:
        with open(CLOUD_INIT_STATUS_PATH, encoding="utf-8") as status_file:
            status = json.load(status_file)
        stages = status.get("v1", {})
        final = stages.get("modules-final", {})
        if not final.get("finished") or final.get("errors"):
            return False
        if any(
            stage.get("errors")
            for stage in stages.values()
            if isinstance(stage, dict)
        ):
            return False
        if NODE_STABILITY_MIN_AGE_S == 0:
            return True
        metadata = _fetch_node_object().get("metadata", {})
        created_at = str(metadata.get("creationTimestamp", "")).strip()
        if not created_at:
            return False
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_s = (datetime.now(timezone.utc) - created).total_seconds()
        return age_s >= NODE_STABILITY_MIN_AGE_S
    except (OSError, ValueError, TypeError, RuntimeError, urllib.error.URLError):
        return False


# ---------- 请求参数校验 ----------
# node-agent 以 root 操作宿主文件:请求体里的 id / 路径 / S3 前缀全部按服务端路径约定
# 严格校验,不接受任何"由调用方指定的宿主路径"。约定(与控制面 drivers/firecracker.py 一致):
#   沙盒目录   {SBX_BASE}/{id}
#   快照目录   {SBX_BASE}/{id}/snap
#   rootfs    {SBX_BASE}/{id}/rootfs.ext4
#   S3 前缀    s3://{SNAPSHOT_S3_BUCKET}/sbx/{id}/
# id 是单个路径段(控制面生成 8 位 hex / warm-xxxxxxxx),拒绝 . / .. / 斜杠 / 符号链接。

_SANDBOX_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")      # 用 fullmatch
_KERNEL_NAME_RE = re.compile(r"vmlinux[A-Za-z0-9._-]*")              # 用 fullmatch
# tap_idx 只做整数/范围合法性校验(tap 名 fctap{idx} ≤ 15 字符)。控制面 alloc_tap_idx 目前只增不减,
# 不按 255 上限拒绝;>255 的地址映射见 _tap_addresses。
MAX_TAP_IDX = 9_999_999_999
MAX_VCPU    = int(os.environ.get("NODE_AGENT_MAX_VCPU", "64"))
MAX_MEM_MIB = int(os.environ.get("NODE_AGENT_MAX_MEM_MIB", str(512 * 1024)))


class RequestValidationError(ValueError):
    """请求参数不符合服务端约定 → 400。reason 为固定枚举,用于日志/指标。"""

    def __init__(self, reason: str, field: str):
        super().__init__(f"invalid {field}")
        self.reason = reason
        self.field = field


def _sandbox_id(value, field: str = "id") -> str:
    if not isinstance(value, str) or not _SANDBOX_ID_RE.fullmatch(value):
        raise RequestValidationError("bad_id", field)
    return value


def _no_symlink(path: str, field: str) -> None:
    """SBX_BASE 之下的每一级(已存在的)都不能是符号链接。SBX_BASE 自身属宿主配置,可信。"""
    base = os.path.abspath(SBX_BASE)
    rel = os.path.relpath(path, base)
    cur = base
    for part in rel.split(os.sep):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise RequestValidationError("symlink", field)


def _sandbox_path(value, leaf: str, field: str) -> tuple[str, str]:
    """校验 value == {SBX_BASE}/{id}/{leaf},返回 (path, id)。"""
    if not isinstance(value, str) or not value:
        raise RequestValidationError("missing_path", field)
    if "\x00" in value or not os.path.isabs(value) or os.path.normpath(value) != value:
        raise RequestValidationError("bad_path", field)
    base = os.path.abspath(SBX_BASE)
    parent, name = os.path.split(value)
    if name != leaf or os.path.dirname(parent) != base:
        raise RequestValidationError("path_outside_sbx_base", field)
    owner = _sandbox_id(os.path.basename(parent), field)
    _no_symlink(value, field)
    return value, owner


def _owned_snapshot_dir(body: dict, sid: str) -> str:
    """snapshot/suspend:快照目录必须是该沙盒自己的 {SBX_BASE}/{sid}/snap。"""
    snap_dir, owner = _sandbox_path(body.get("snapshot_local_path"), "snap",
                                    "snapshot_local_path")
    if owner != sid:
        raise RequestValidationError("path_owner_mismatch", "snapshot_local_path")
    return snap_dir


def _s3_prefix_for(owner: str, value, field: str = "s3_prefix") -> str:
    """s3_prefix 只能是空串或本节点配置的快照桶下该沙盒的前缀。"""
    if value in (None, ""):
        return ""
    expected = f"s3://{SNAPSHOT_S3_BUCKET}/sbx/{owner}/" if SNAPSHOT_S3_BUCKET else None
    if not isinstance(value, str) or expected is None or value != expected:
        raise RequestValidationError("bad_s3_prefix", field)
    return value


def _int_in_range(value, low: int, high: int, field: str) -> int:
    if isinstance(value, bool):
        raise RequestValidationError("bad_int", field)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise RequestValidationError("bad_int", field) from None
    if not low <= number <= high:
        raise RequestValidationError("out_of_range", field)
    return number


def _kernel_path(value) -> str:
    """kernel 只能是 ROOTFS_DIR 下的 vmlinux* 文件(默认 /opt/sbx/vmlinux)。"""
    if value in (None, ""):
        return os.path.join(ROOTFS_DIR, "vmlinux")
    if not isinstance(value, str) or os.path.normpath(value) != value \
            or os.path.dirname(value) != os.path.abspath(ROOTFS_DIR) \
            or not _KERNEL_NAME_RE.fullmatch(os.path.basename(value)) \
            or os.path.islink(value):
        raise RequestValidationError("bad_kernel", "kernel")
    return value



def _wait_for_node_bootstrap() -> None:
    if NODE_BOOTSTRAP_WAIT_S == 0:
        return
    deadline = time.monotonic() + NODE_BOOTSTRAP_WAIT_S
    last_log = 0.0
    while not _node_bootstrap_ready():
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError(
                "node bootstrap did not complete within "
                f"{NODE_BOOTSTRAP_WAIT_S}s"
            )
        if now - last_log >= 30:
            print(
                "[bootstrap] waiting for cloud-init/custom bootstrap "
                "completion",
                flush=True,
            )
            last_log = now
        time.sleep(2)  # nosemgrep: arbitrary-sleep -- node startup gate
    print("[bootstrap] node is stable and ready for Firecracker", flush=True)


@contextmanager
def _vm_operation_lock(sandbox_id: str):
    """Serialize one VM's operations without dropping locks under waiters."""
    with _LOCK:
        lock = _VM_OP_LOCKS.setdefault(sandbox_id, threading.RLock())
        _VM_OP_LOCK_USERS[sandbox_id] = (
            _VM_OP_LOCK_USERS.get(sandbox_id, 0) + 1
        )
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _LOCK:
            remaining = _VM_OP_LOCK_USERS.get(sandbox_id, 1) - 1
            if remaining > 0:
                _VM_OP_LOCK_USERS[sandbox_id] = remaining
            else:
                _VM_OP_LOCK_USERS.pop(sandbox_id, None)
                if _VM_OP_LOCKS.get(sandbox_id) is lock:
                    _VM_OP_LOCKS.pop(sandbox_id, None)


# ---------- tap 网络 ----------

# tap_idx 是控制面全局只增计数器(db.alloc_tap_idx),累计创建超过 255 次后旧的
# 172.18.{tap_idx}.x 映射会生成非法地址(ip addr add 静默失败 → guest 无 IPv4)。
# 1..255 保持旧映射(已有快照里 guest 的 IP 固化在内存态,恢复时必须一致);
# 更大的 idx 映射进 172.19.0.0/16 的 /30 块(16384 个,取模复用)。
_TAP_LEGACY_MAX_IDX = 255
_TAP_EXT_BLOCKS = 16384


def _tap_addresses(tap_idx: int) -> tuple[str, str]:
    """tap_idx → (host_ip, guest_ip),同一 /30 内 .1/.2。"""
    if tap_idx < 0:
        raise ValueError(f"invalid tap_idx {tap_idx}")
    if tap_idx <= _TAP_LEGACY_MAX_IDX:
        return f"172.18.{tap_idx}.1", f"172.18.{tap_idx}.2"
    offset = ((tap_idx - _TAP_LEGACY_MAX_IDX - 1) % _TAP_EXT_BLOCKS) * 4
    third, fourth = divmod(offset, 256)
    return f"172.19.{third}.{fourth + 1}", f"172.19.{third}.{fourth + 2}"


def _setup_tap(tap_idx: int) -> tuple[str, str, str]:
    """建 tap + /30 子网。返回 (tap_name, host_ip, guest_ip)。"""
    tap      = f"fctap{tap_idx}"
    host_ip, guest_ip = _tap_addresses(tap_idx)
    host_if  = _host_iface()
    # 幂等清理同名残留 tap:suspend/destroy 后 tap 设备不会自动消失,
    # 若 tap_idx 被复用(暖池 resume、tap_idx 回收重分配),残留的旧 tap 会让
    # FC snapshot/load 打开设备时报 "Resource busy (os error 16)" → resume 失败。
    # 先删再建,保证设备干净。
    subprocess.run(["ip", "link", "del", tap], stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "tuntap", "add", tap, "mode", "tap"],
                   stderr=subprocess.DEVNULL)
    # guest 不用 IPv6:关掉 tap 上的 IPv6,避免 guest 经 link-local 访问宿主监听在 :: 的服务。
    _disable_ipv6(tap)
    # 地址配置失败不能静默吞掉:否则 guest 起来但没有网络,只有 vsock 能用。
    addr = subprocess.run(["ip", "addr", "add", f"{host_ip}/30", "dev", tap],
                          capture_output=True, text=True)
    if addr.returncode != 0:
        subprocess.run(["ip", "link", "del", tap], stderr=subprocess.DEVNULL)
        raise RuntimeError(
            f"ip addr add {host_ip}/30 dev {tap} failed: {addr.stderr.strip()}"
        )
    subprocess.run(["ip", "link", "set", tap, "up"])
    # NAT(幂等)
    # nosec B602 / nosemgrep: subprocess-shell-true -- shell=True 仅为 "-C 检查 || -A 添加" 幂等惯用法;
    # tap_idx 为控制面分配的 int、host_if 来自本机路由表自动探测,均非用户输入,无注入面。
    subprocess.run(
        f"iptables -t nat -C POSTROUTING -o {host_if} -j MASQUERADE 2>/dev/null || "
        f"iptables -t nat -A POSTROUTING -o {host_if} -j MASQUERADE",
        shell=True,  # nosec B602
    )
    subprocess.run(
        f"iptables -C FORWARD -i {tap} -o {host_if} -j ACCEPT 2>/dev/null || "
        f"iptables -A FORWARD -i {tap} -o {host_if} -j ACCEPT",
        shell=True,  # nosec B602
    )
    return tap, host_ip, guest_ip


_GUEST_IN_CHAIN  = "SBX-GUEST-IN"
_GUEST_FWD_CHAIN = "SBX-GUEST-FWD"
# guest 绝不应直接访问的地址:IMDS(节点实例角色凭据)、EKS Pod Identity agent。
# 169.254.169.253(VPC DNS)guest 需要用,不在此列。
_GUEST_BLOCKED_DESTS = ("169.254.169.254/32", "169.254.170.23/32")


def _guest_firewall_rules() -> list[list[str]]:
    """宿主 iptables 规则(幂等:专用链每次 flush 重建,主链只插一次跳转)。

    INPUT  -i fctap+ → SBX-GUEST-IN :只放行宿主主动发起连接的回包(SSH/反代进 guest),
                                    其余全部丢弃 —— guest 无法访问宿主任何端口(含 node-agent)。
    FORWARD -i fctap+ → SBX-GUEST-FWD:禁止 guest→其他 guest、guest→IMDS、guest→任意节点的
                                    node-agent 端口;其余(公网/VPC 服务)照常 NAT 出去。
    命中 node-agent 端口的探测先限速 LOG(前缀 sbx-guest-agent-probe),供告警检测。
    """
    port = str(LISTEN_PORT)
    probe_log = ["-m", "limit", "--limit", "6/min", "--limit-burst", "10",
                 "-j", "LOG", "--log-prefix", "sbx-guest-agent-probe "]
    rules = [
        ["-A", _GUEST_IN_CHAIN, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
         "-j", "ACCEPT"],
        ["-A", _GUEST_IN_CHAIN, "-p", "tcp", "--dport", port, *probe_log],
        ["-A", _GUEST_IN_CHAIN, "-j", "DROP"],
        ["-A", _GUEST_FWD_CHAIN, "-o", "fctap+", "-j", "DROP"],
    ]
    for dest in _GUEST_BLOCKED_DESTS:
        rules.append(["-A", _GUEST_FWD_CHAIN, "-d", dest, "-j", "DROP"])
    rules += [
        ["-A", _GUEST_FWD_CHAIN, "-p", "tcp", "--dport", port, *probe_log],
        ["-A", _GUEST_FWD_CHAIN, "-p", "tcp", "--dport", port, "-j", "DROP"],
        ["-A", _GUEST_FWD_CHAIN, "-j", "RETURN"],
    ]
    return rules


def _restore_arg(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def _guest_firewall_restore_input() -> str:
    """iptables-restore 输入:声明即 flush 并重建两条专用链,整表一次原子提交,无未防护窗口。"""
    lines = ["*filter", f":{_GUEST_IN_CHAIN} - [0:0]", f":{_GUEST_FWD_CHAIN} - [0:0]"]
    lines += [" ".join(_restore_arg(a) for a in rule) for rule in _guest_firewall_rules()]
    lines += ["COMMIT", ""]
    return "\n".join(lines)


def _ensure_jump_first(parent: str, chain: str) -> None:
    """保证 `-i fctap+ -j <chain>` 是 parent 链第 1 条且只有一条。
    kube-proxy 等组件之后插到链首的规则(如 KUBE-FORWARD 的 ACCEPT)不得先于 guest 隔离生效。
    先插到第 1 位再按行号删多余副本,任何时刻都至少有一条跳转生效。"""
    want = f"-A {parent} -i fctap+ -j {chain}"
    listed = subprocess.run(["iptables", "-S", parent], capture_output=True,
                            text=True, check=True).stdout.splitlines()
    rules = [line.strip() for line in listed if line.startswith("-A ")]
    if not rules or rules[0] != want:
        subprocess.run(["iptables", "-I", parent, "1", "-i", "fctap+", "-j", chain],
                       check=True)
        rules.insert(0, want)
    for number in sorted((i + 1 for i, r in enumerate(rules) if r == want and i > 0),
                         reverse=True):
        subprocess.run(["iptables", "-D", parent, str(number)], check=True)


def ensure_guest_firewall_jumps() -> None:
    for parent, chain in (("INPUT", _GUEST_IN_CHAIN), ("FORWARD", _GUEST_FWD_CHAIN)):
        _ensure_jump_first(parent, chain)


def _install_guest_firewall() -> None:
    """安装 guest 隔离规则;失败抛异常(调用方据此拒绝启动,fail closed)。"""
    subprocess.run(["iptables-restore", "--noflush"], input=_guest_firewall_restore_input(),
                   text=True, check=True)
    ensure_guest_firewall_jumps()
    # guest 不配 IPv6(tap 上也已 disable_ipv6):IPv6 方向一律丢弃(尽力而为,
    # 宿主未启用 ip6tables 时忽略)。
    for parent in ("INPUT", "FORWARD"):
        drop = [parent, "-i", "fctap+", "-j", "DROP"]
        try:
            if subprocess.run(["ip6tables", "-C", *drop], stderr=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL).returncode != 0:
                subprocess.run(["ip6tables", "-I", parent, "1", "-i", "fctap+", "-j", "DROP"],
                               stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        except OSError:
            break


def _disable_ipv6(tap: str) -> None:
    try:
        with open(f"/proc/sys/net/ipv6/conf/{tap}/disable_ipv6", "w") as f:
            f.write("1")
    except OSError:
        pass  # 宿主未启用 IPv6 时该文件不存在,本就无 IPv6 面


def _teardown_tap(tap: str) -> None:
    subprocess.run(["ip", "link", "del", tap], stderr=subprocess.DEVNULL)
    # 同时删掉 _setup_tap 追加的 FORWARD 规则,否则每次创建都残留一条,
    # 高密度/长时间运行后 FORWARD 链线性膨胀。
    subprocess.run(
        ["iptables", "-D", "FORWARD", "-i", tap, "-o", _host_iface(),
         "-j", "ACCEPT"],
        stderr=subprocess.DEVNULL,
    )


def _host_iface() -> str:
    if HOST_IFACE:
        return HOST_IFACE
    r = subprocess.run("ip route | awk '/default/{print $5;exit}'",
                       shell=True, capture_output=True, text=True)
    return r.stdout.strip() or "eth0"


# ---------- Firecracker 宿主运行层(systemd + jailer) ----------

_VMM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")


def _validated_vmm_id(sandbox_id: str) -> str:
    if not _VMM_ID_RE.fullmatch(sandbox_id):
        raise ValueError("sandbox id is not safe for the host runtime")
    return sandbox_id


def _host_control(
    executable: str,
    args: list[str],
    *,
    timeout: float = 30,
) -> dict:
    """Run a narrowly-scoped host helper through PID 1's namespaces."""
    command = [
        HOST_NSENTER,
        "--target", "1",
        "--mount",
        "--uts",
        "--ipc",
        "--net",
        "--pid",
        "--",
        executable,
        *args,
    ]
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = completed.stdout.strip().splitlines()
    if not output:
        return {}
    try:
        return json.loads(output[-1])
    except ValueError as exc:
        raise RuntimeError(
            f"host helper returned invalid JSON: {output[-1][:512]}"
        ) from exc


def _runtime_unit_name(sandbox_id: str) -> str:
    return f"sbx-vmm-{_validated_vmm_id(sandbox_id)}.service"


def _runtime_uid(sandbox_id: str) -> int:
    # Numeric IDs do not need host passwd entries. Use a 31-bit range so
    # thousand-sandbox fleets have negligible collision probability.
    digest = hashlib.sha256(sandbox_id.encode()).digest()
    return 100000 + int.from_bytes(digest[:4], "big") % 2_000_000_000


def _vmm_writable_dir(sandbox_id: str, path: str) -> None:
    """建 VMM 需要写入的目录(如快照目录)。

    jailer 只在启动时 chown -R 沙盒目录;启动之后由 node-agent(root)新建的
    子目录仍归 root,降权运行的 firecracker 写快照会 EACCES(真机实测
    snapshot/create → Permission denied)。这里把目录交给该 VM 的运行 uid。
    """
    os.makedirs(path, exist_ok=True)
    if VMM_LAUNCH_MODE == "host-systemd" and VMM_USE_JAILER:
        uid = _runtime_uid(sandbox_id)
        os.chown(path, uid, uid)


def _runtime_bind_dirs(sandbox_id: str, extra_dirs=None) -> list[str]:
    candidates = [f"{SBX_BASE}/{sandbox_id}", *(extra_dirs or [])]
    result: list[str] = []
    for path in candidates:
        managed = _managed_sandbox_dir(str(path))
        if not managed:
            raise ValueError(f"unsafe jail bind path: {path}")
        if managed not in result:
            result.append(managed)
    return result


def _link_runtime_socket(sandbox_id: str, socket_name: str, target: str) -> None:
    """Keep the historical socket path stable for restart recovery."""
    compatibility_path = f"{SBX_BASE}/{sandbox_id}/{socket_name}"
    if os.path.abspath(compatibility_path) == os.path.abspath(target):
        return
    try:
        if os.path.lexists(compatibility_path):
            os.remove(compatibility_path)
        os.symlink(target, compatibility_path)
    except OSError:
        # The actual target remains authoritative; the compatibility link only
        # improves restart discovery and legacy diagnostics.
        pass


def _launch_vmm(
    sandbox_id: str,
    socket_name: str,
    mem_mib: int,
    *,
    bind_dirs=None,
    log_name: str = "vmm.log",
) -> tuple[int, str, str]:
    """Launch one VMM and return (pid, host_api_socket, runtime_unit)."""
    sid = _validated_vmm_id(sandbox_id)
    d = f"{SBX_BASE}/{sid}"
    os.makedirs(d, exist_ok=True)
    if socket_name not in {"api.sock", "api-resume.sock"}:
        raise ValueError("unsupported Firecracker API socket name")

    if VMM_LAUNCH_MODE == "host-systemd":
        uid = _runtime_uid(sid)
        args = [
            "start",
            "--id", sid,
            "--socket-name", socket_name,
            # Guest memory plus VMM/device overhead. The service cgroup is
            # independent from the node-agent Pod cgroup.
            "--memory-mib", str(
                int(mem_mib) + max(1024, int(mem_mib) // 4)
            ),
            "--uid", str(uid),
            "--gid", str(uid),
            "--jailer", "1" if VMM_USE_JAILER else "0",
        ]
        for path in _runtime_bind_dirs(sid, bind_dirs):
            args.extend(["--bind-dir", path])
        result = _host_control(HOST_VMM_CTL, args, timeout=45)
        pid = int(result.get("pid", 0) or 0)
        sock = str(result.get("socket", ""))
        unit = str(result.get("unit", "")) or _runtime_unit_name(sid)
        if pid <= 0 or not sock:
            raise RuntimeError("host runtime did not return pid/socket")
        _link_runtime_socket(sid, socket_name, sock)
        return pid, sock, unit

    if VMM_LAUNCH_MODE != "subprocess":
        raise RuntimeError(
            f"unsupported VMM_LAUNCH_MODE={VMM_LAUNCH_MODE!r}"
        )
    if VMM_USE_JAILER:
        raise RuntimeError(
            "VMM_USE_JAILER requires VMM_LAUNCH_MODE=host-systemd"
        )

    sock = f"{d}/{socket_name}"
    try:
        os.remove(sock)
    except FileNotFoundError:
        pass
    with open(f"{d}/{log_name}", "w") as log_file:
        process = subprocess.Popen(
            [FC_BIN, "--api-sock", sock],
            stdout=log_file,
            stderr=log_file,
        )
    return process.pid, sock, ""


def _runtime_status(sandbox_id: str, *, strict: bool = False) -> dict:
    if VMM_LAUNCH_MODE != "host-systemd":
        return {}
    try:
        return _host_control(
            HOST_VMM_CTL,
            ["status", "--id", _validated_vmm_id(sandbox_id)],
            timeout=5,
        )
    except Exception:
        if strict:
            raise
        return {}


def _legacy_vmm_pid(api_socket: str) -> int | None:
    """Find a pre-systemd Firecracker child during an in-place rollout."""
    expected = {
        api_socket,
        os.path.realpath(api_socket),
    }
    try:
        proc_entries = os.scandir("/proc")
    except OSError:
        return None
    with proc_entries:
        for entry in proc_entries:
            if not entry.name.isdigit():
                continue
            try:
                raw = _read_proc_cmdline(
                    f"/proc/{entry.name}/cmdline"
                )
                argv = [
                    value.decode(errors="replace")
                    for value in raw.split(b"\0")
                    if value
                ]
                for index, value in enumerate(argv[:-1]):
                    if value == "--api-sock" and argv[index + 1] in expected:
                        return int(entry.name)
            except (OSError, ValueError):
                continue
    return None


def _read_proc_cmdline(path: str) -> bytes:
    """Small indirection kept patchable in unit tests."""
    with open(path, "rb") as stream:
        return stream.read()


def _stop_vmm(sandbox_id: str, vm: dict) -> None:
    """Stop a host-owned service or the legacy child process."""
    if vm.get("runtime_unit"):
        _host_control(
            HOST_VMM_CTL,
            ["stop", "--id", _validated_vmm_id(sandbox_id)],
            timeout=30,
        )
        return
    if vm.get("pid"):
        try:
            os.kill(int(vm["pid"]), signal.SIGTERM)
        except (ProcessLookupError, ValueError, TypeError):
            pass
        return
    runtime = _runtime_status(sandbox_id)
    if runtime.get("active"):
        _host_control(
            HOST_VMM_CTL,
            ["stop", "--id", _validated_vmm_id(sandbox_id)],
            timeout=30,
        )


# ---------- Firecracker 启动 ----------

def _start_fc(sandbox_id: str, rootfs: str, tap: str, cpu: int,
               mem_mib: int, kernel: str, env: dict,
               guest_ip: str = "", host_ip: str = "",
               balloon: dict | None = None) -> tuple[int, str, str]:
    """
    启动 Firecracker，返回 (pid, api_sock, runtime_unit)。
    生产模式由宿主 systemd + jailer 提供独立 cgroup/chroot。
    """
    d    = f"{SBX_BASE}/{sandbox_id}"
    os.makedirs(d, exist_ok=True)
    pid, sock, runtime_unit = _launch_vmm(
        sandbox_id,
        "api.sock",
        mem_mib,
        bind_dirs=[d],
        log_name="vm.log",
    )

    if not _wait_sock(sock, timeout=30.0):
        _stop_vmm(
            sandbox_id,
            {"pid": pid, "runtime_unit": runtime_unit},
        )
        raise RuntimeError("firecracker API socket 未就绪")

    try:
        # 配置 VM（JuiceFS 模式：通过 boot_args 把 Redis/S3 地址注入 guest init）
        # 里程碑 B: 注入 guest 网络(SBX_IP/SBX_GW),让 init 配成 node-agent 期望的
        # 172.18.{tap_idx}.2,从而宿主能 SSH 到 guest 做 exec。
        net_args = (
            f"SBX_IP={guest_ip} SBX_GW={host_ip} "
            if guest_ip and host_ip else ""
        )
        if JUICEFS_ENABLED and JUICEFS_REDIS_ADDR and JUICEFS_BUCKET:
            jfs_env = (
                f"JFS_REDIS={JUICEFS_REDIS_ADDR} "
                f"JFS_BUCKET={JUICEFS_BUCKET} "
                f"JFS_NAME={JUICEFS_FS_NAME} "
                f"AWS_REGION={AWS_REGION} "
            )
            boot_args = (
                "console=ttyS0 reboot=k panic=1 pci=off "
                f"init=/sbin/sbxinit {net_args}{jfs_env}"
            )
        else:
            boot_args = (
                "console=ttyS0 reboot=k panic=1 pci=off "
                f"init=/sbin/sbxinit {net_args}"
            )

        _fc(sock, "PUT", "/boot-source", {
            "kernel_image_path": kernel,
            "boot_args": boot_args,
        })
        _fc(sock, "PUT", "/drives/rootfs", {
            "drive_id": "rootfs", "path_on_host": rootfs,
            "is_root_device": True, "is_read_only": False,
        })
        # track_dirty_pages=True:开启脏页跟踪,是 Diff 增量快照的前提。
        _fc(sock, "PUT", "/machine-config", {
            "vcpu_count": cpu,
            "mem_size_mib": mem_mib,
            "track_dirty_pages": True,
        })
        _fc(sock, "PUT", "/network-interfaces/eth0", {
            "iface_id": "eth0",
            "host_dev_name": tap,
        })
        # 可选 virtio-balloon(只能 boot 前配置):free_page_reporting 让 guest
        # 把释放的页还给宿主(宿主 RSS 随之下降),是内存超分的前提之一。
        if balloon:
            _fc(sock, "PUT", "/balloon", {
                "amount_mib": int(balloon.get("amount_mib", 0)),
                "deflate_on_oom": bool(balloon.get("deflate_on_oom", True)),
                "stats_polling_interval_s": int(
                    balloon.get("stats_polling_interval_s", 0)
                ),
                **({"free_page_reporting": True}
                   if balloon.get("free_page_reporting") else {}),
            })
        # vsock 配置失败不阻断 VM 启动(exec 回退 SSH)。
        vsock_path = f"{d}/v.sock"
        try:
            _fc(sock, "PUT", "/vsock", {
                "vsock_id": "vsock0",
                "guest_cid": 3,
                "uds_path": vsock_path,
            })
        except Exception:
            pass
        _fc(sock, "PUT", "/actions", {"action_type": "InstanceStart"})
    except Exception:
        _stop_vmm(
            sandbox_id,
            {"pid": pid, "runtime_unit": runtime_unit},
        )
        raise

    return pid, sock, runtime_unit


def _tap_idx_from_d(d: str) -> int:
    try:
        with _LOCK:
            for v in _VMS.values():
                if v.get("dir") == d:
                    return v.get("tap_idx", 1)
    except Exception:
        pass
    return 1


# ---------- Firecracker UDS HTTP ----------

def _fc(
    sock: str,
    method: str,
    path: str,
    body=None,
    timeout: float = 15,
) -> dict:
    conn = http.client.HTTPConnection("localhost", timeout=timeout)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    conn.sock = s
    try:
        s.connect(sock)
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data,
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        raw = r.read()
        if r.status >= 400:
            raise RuntimeError(f"firecracker {method} {path} → {r.status}: {raw.decode(errors='replace')}")
        return json.loads(raw) if raw else {}
    finally:
        conn.close()


def _wait_sock(path: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.1)
                    s.connect(path)
                return True
            except OSError:
                pass
        time.sleep(0.05)  # nosemgrep: arbitrary-sleep -- 轮询 socket 就绪的退避间隔
    return False


# ---------- S3 helpers(调 aws cli,零额外依赖) ----------

def _s3_upload_sync(local_dir: str, s3_prefix: str, retries: int = 3) -> None:
    """
    同步把 local_dir 里的快照上传到 s3_prefix,失败指数退避重试。
    全部失败则抛异常 —— 调用方(op_suspend)据此决定不释放 VMM 内存,
    保证不变式:标 suspended ⟺ S3 确有快照(P0-2,杜绝静默丢数据)。
    """
    if not s3_prefix:
        return
    started = time.monotonic()
    last_err: Exception | None = None
    for attempt in range(retries):
        r = subprocess.run(
            ["aws", "s3", "sync", local_dir, s3_prefix,
             "--region", AWS_REGION, "--only-show-errors"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            record_snapshot_transfer(
                "upload", "success", time.monotonic() - started
            )
            return
        last_err = RuntimeError(
            f"aws s3 sync rc={r.returncode}: {r.stderr.strip()[:500]}")
        if attempt < retries - 1:
            time.sleep(2 ** attempt)  # nosemgrep: arbitrary-sleep -- 上传重试退避
    record_snapshot_transfer("upload", "error", time.monotonic() - started)
    record_snapshot_error("upload")
    raise last_err or RuntimeError("s3 upload failed")


def _s3_download(s3_prefix: str, local_dir: str) -> None:
    """同步从 S3 拉三件套到 local_dir(resume 前调用)。"""
    if not s3_prefix:
        return
    os.makedirs(local_dir, exist_ok=True)
    started = time.monotonic()
    # 只拉快照约定的顶层文件,前缀下其它 key(子目录、任意文件名)一律不落盘。
    include_args: list[str] = ["--exclude", "*"]
    for name in (*_SNAPSHOT_FILES, _SNAPSHOT_MANIFEST, "rootfs.ext4"):
        include_args += ["--include", name]
    try:
        subprocess.run(
            ["aws", "s3", "sync", s3_prefix, local_dir, *include_args,
             "--region", AWS_REGION, "--quiet"],
            check=True,
        )
        record_snapshot_transfer(
            "download", "success", time.monotonic() - started
        )
    except Exception:
        record_snapshot_transfer(
            "download", "error", time.monotonic() - started
        )
        record_snapshot_error("download")
        raise


_SNAPSHOT_MANIFEST = "integrity.json"
_SNAPSHOT_FILES = (
    "vm.snapshot",
    "vm.mem",
    "vm.snapshot.base",
    "vm.mem.base",
)
_BASE_SNAPSHOT_FILES = frozenset(("vm.snapshot.base", "vm.mem.base"))
_BASE_HASH_CACHE: dict[str, tuple[tuple[int, int, int, int, int], str]] = {}
_BASE_HASH_CACHE_LOCK = threading.Lock()

# Diff 快照的落盘方式:
#   sparse  旧方式。Diff 写成新的稀疏文件,resume 时按 SEEK_DATA/SEEK_HOLE 叠加到
#           base 上。要求文件系统的空洞粒度精确到 4 KiB 页(XFS/ext4 满足);
#           ZFS(空洞粒度=recordsize)和 Lustre 不满足 → 未写的页被当成"有数据"
#           以零覆盖 base,resume 后 guest 静默损坏(真机实测 triple fault)。
#   inplace 先把 base block-clone 成同尺寸 vm.mem,再让 Firecracker 把 Diff 原地
#           写进去(Firecracker 对已存在且尺寸匹配的 mem 文件做原地合并),结果即
#           完整内存镜像,resume 直接 load,不依赖 SEEK_DATA。
#   auto    ZFS 上用 inplace,其余用 sparse(默认)。
SNAPSHOT_DIFF_MODE = os.environ.get("SNAPSHOT_DIFF_MODE", "auto").strip().lower()
_INPLACE_MARKER = "vm.mem.inplace"
# 这些文件系统自带端到端块校验,inplace 的整份 vm.mem 不在关键路径上再做
# sha256(否则每个沙盒要冷读一遍完整内存镜像,吃掉 Spot 窗口的 EBS 带宽)。
_CHECKSUMMING_FS = frozenset(("zfs",))


def _fs_type(path: str) -> str:
    try:
        return subprocess.run(
            ["stat", "-f", "-c", "%T", path],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except Exception:
        return ""


def _diff_mode(snap_dir: str) -> str:
    if SNAPSHOT_DIFF_MODE in ("sparse", "inplace"):
        return SNAPSHOT_DIFF_MODE
    return "inplace" if _fs_type(snap_dir) == "zfs" else "sparse"


class SnapshotIntegrityError(RuntimeError):
    pass


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: str) -> tuple[int, int, int, int, int]:
    stat = os.stat(path)
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def _snapshot_file_hash(path: str, name: str) -> str:
    if name not in _BASE_SNAPSHOT_FILES:
        return _sha256_file(path)
    identity = _file_identity(path)
    with _BASE_HASH_CACHE_LOCK:
        cached = _BASE_HASH_CACHE.get(path)
    if cached and cached[0] == identity:
        return cached[1]
    digest = _sha256_file(path)
    with _BASE_HASH_CACHE_LOCK:
        _BASE_HASH_CACHE[path] = (identity, digest)
    return digest


def _clear_base_hash_cache(sandbox_dir: str) -> None:
    prefix = os.path.abspath(sandbox_dir) + os.sep
    with _BASE_HASH_CACHE_LOCK:
        stale = [
            path for path in _BASE_HASH_CACHE
            if os.path.abspath(path).startswith(prefix)
        ]
        for path in stale:
            del _BASE_HASH_CACHE[path]


_FS_INTEGRITY_FILES = frozenset(("vm.mem", "vm.mem.base"))


def _fs_checksummed_mem(snap_dir: str) -> bool:
    """inplace 模式且文件系统自带块校验:整份内存镜像交给文件系统担保。"""
    return (
        _diff_mode(snap_dir) == "inplace"
        and _fs_type(snap_dir) in _CHECKSUMMING_FS
    )


def _write_snapshot_manifest(snap_dir: str) -> dict:
    files = {}
    fs_checksummed = _fs_checksummed_mem(snap_dir)
    for name in _SNAPSHOT_FILES:
        path = os.path.join(snap_dir, name)
        if os.path.isfile(path):
            if fs_checksummed and name in _FS_INTEGRITY_FILES:
                files[name] = {
                    "size": os.path.getsize(path),
                    "integrity": f"filesystem:{_fs_type(snap_dir)}",
                }
                continue
            files[name] = {
                "size": os.path.getsize(path),
                "sha256": _snapshot_file_hash(path, name),
            }
    if "vm.snapshot" not in files or "vm.mem" not in files:
        raise SnapshotIntegrityError("snapshot is missing required files")
    manifest = {"version": 1, "algorithm": "sha256", "files": files}
    target = os.path.join(snap_dir, _SNAPSHOT_MANIFEST)
    temporary = f"{target}.tmp"
    with open(temporary, "w", encoding="ascii") as stream:
        json.dump(manifest, stream, sort_keys=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    return manifest


def _verify_snapshot_manifest(snap_dir: str) -> dict:
    target = os.path.join(snap_dir, _SNAPSHOT_MANIFEST)
    try:
        with open(target, encoding="ascii") as stream:
            manifest = json.load(stream)
    except (OSError, ValueError) as exc:
        raise SnapshotIntegrityError("snapshot integrity manifest is unavailable") from exc
    if manifest.get("version") != 1 or manifest.get("algorithm") != "sha256":
        raise SnapshotIntegrityError("snapshot integrity manifest is unsupported")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise SnapshotIntegrityError("snapshot integrity manifest is empty")
    for name, expected in files.items():
        if name not in _SNAPSHOT_FILES or not isinstance(expected, dict):
            raise SnapshotIntegrityError("snapshot integrity manifest is invalid")
        path = os.path.join(snap_dir, name)
        if not os.path.isfile(path):
            raise SnapshotIntegrityError(f"snapshot file is missing: {name}")
        if os.path.getsize(path) != expected.get("size"):
            raise SnapshotIntegrityError(f"snapshot size mismatch: {name}")
        integrity = expected.get("integrity")
        if integrity:
            # 只接受由自带块校验的文件系统担保的 inplace 内存镜像
            if (
                name not in _FS_INTEGRITY_FILES
                or integrity != f"filesystem:{_fs_type(snap_dir)}"
                or _fs_type(snap_dir) not in _CHECKSUMMING_FS
            ):
                raise SnapshotIntegrityError(
                    f"snapshot integrity mode unsupported here: {name} {integrity}"
                )
            continue
        if _snapshot_file_hash(path, name) != expected.get("sha256"):
            raise SnapshotIntegrityError(f"snapshot checksum mismatch: {name}")
    if "vm.snapshot" not in files or "vm.mem" not in files:
        raise SnapshotIntegrityError("snapshot manifest omits required files")
    return manifest


def _record_snapshot_verification(snap_dir: str) -> dict:
    started = time.monotonic()
    try:
        manifest_path = os.path.join(snap_dir, _SNAPSHOT_MANIFEST)
        if not os.path.exists(manifest_path):
            try:
                _write_snapshot_manifest(snap_dir)
                record_snapshot_legacy_migration("success")
                log_event(
                    "warning",
                    "legacy_snapshot_manifest_created",
                    snapshot_dir=snap_dir,
                )
            except Exception:
                record_snapshot_legacy_migration("error")
                raise
        manifest = _verify_snapshot_manifest(snap_dir)
        record_snapshot_verify("success", time.monotonic() - started)
        return manifest
    except Exception:
        record_snapshot_verify("error", time.monotonic() - started)
        record_snapshot_error("verify")
        raise


# ---------- 操作实现 ----------

def op_create(body: dict) -> dict:
    sid      = _sandbox_id(body.get("id"))
    tap_idx  = _int_in_range(body.get("tap_idx"), 0, MAX_TAP_IDX, "tap_idx")
    cpu      = _int_in_range(body.get("cpu", 2), 1, MAX_VCPU, "cpu")
    mem_mib  = _int_in_range(body.get("mem_mib", 4096), 128, MAX_MEM_MIB, "mem_mib")
    kernel   = _kernel_path(body.get("kernel"))
    env      = body.get("env", {})
    # rootfs 路径不由调用方决定:固定 {SBX_BASE}/{id}/rootfs.ext4;若传了必须与约定一致。
    if body.get("rootfs_path") not in (None, ""):
        _, owner = _sandbox_path(body["rootfs_path"], "rootfs.ext4", "rootfs_path")
        if owner != sid:
            raise RequestValidationError("path_owner_mismatch", "rootfs_path")

    # Operator reconciliation is level-triggered: after a watch reconnect or
    # process crash it may repeat a create whose side effect already
    # succeeded. Make the node boundary idempotent so the retry cannot start a
    # second Firecracker process or overwrite the running VM's rootfs.
    with _LOCK:
        existing = _VMS.get(sid)
        if existing and existing.get("state") == "running":
            return {
                "state": "running",
                "ip": existing.get("ip", ""),
                "already_exists": True,
            }
        if existing:
            raise RuntimeError(
                f"sandbox {sid} already exists in state "
                f"{existing.get('state', 'unknown')}"
            )

    d = f"{SBX_BASE}/{sid}"
    _no_symlink(d, "id")
    os.makedirs(d, exist_ok=True)

    # CoW 复制基础 rootfs 到沙盒目录(src 是全局基础镜像,dst 是沙盒私有副本)。
    # 按控制面传的 rootfs_template 选模板 /opt/sbx/rootfs-{name}.ext4;不存在则回退默认 ROOTFS。
    dest_rootfs = f"{d}/rootfs.ext4"
    src_rootfs  = _cached_rootfs_template(
        _rootfs_template_path(body.get("rootfs_template", ""))
    )
    subprocess.run(["cp", "--reflink=auto", src_rootfs, dest_rootfs], check=True)
    os.chmod(dest_rootfs, 0o644)

    tap, host_ip, guest_ip = _setup_tap(tap_idx)

    try:
        pid, sock, runtime_unit = _start_fc(
            sid,
            dest_rootfs,
            tap,
            cpu,
            mem_mib,
            kernel,
            env,
            guest_ip=guest_ip,
            host_ip=host_ip,
            balloon=body.get("balloon") or (
                _DEFAULT_BALLOON if BALLOON_FREE_PAGE_REPORTING else None
            ),
        )
    except Exception:
        _teardown_tap(tap)
        raise

    vm = {
        "state":   "running",
        "pid":     pid,
        "sock":    sock,
        "tap":     tap,
        "tap_idx": tap_idx,
        "ip":      guest_ip,
        "dir":     d,
        "runtime_unit": runtime_unit,
    }
    with _LOCK:
        _VMS[sid] = vm
    try:
        _persist_runtime_metadata(sid, vm)
    except Exception:
        with _LOCK:
            if _VMS.get(sid) is vm:
                _VMS.pop(sid, None)
        _stop_vmm(sid, vm)
        _teardown_tap(tap)
        raise
    return {"state": "running", "ip": guest_ip}


def _managed_sandbox_dir(path: str) -> str | None:
    """Return a normalized direct child of SBX_BASE, or None if unsafe."""
    base = os.path.abspath(SBX_BASE)
    candidate = os.path.abspath(path)
    if os.path.dirname(candidate) != base:
        return None
    return candidate


def _runtime_metadata_path(sid: str) -> str:
    return f"{SBX_BASE}/{_validated_vmm_id(sid)}/runtime.json"


def _persist_runtime_metadata(sid: str, vm: dict) -> None:
    """Persist host-network identity needed after node-agent Pod restart."""
    sandbox_dir = _managed_sandbox_dir(f"{SBX_BASE}/{sid}")
    if not sandbox_dir:
        raise ValueError("unsafe sandbox runtime metadata path")
    os.makedirs(sandbox_dir, exist_ok=True)
    owned_dirs = [
        managed
        for path in vm.get("owned_dirs") or []
        if (managed := _managed_sandbox_dir(str(path)))
    ]
    payload = {
        "version": 1,
        "state": str(vm.get("state", "")),
        "tap": str(vm.get("tap", "")),
        "tap_idx": int(vm.get("tap_idx", 0) or 0),
        "ip": str(vm.get("ip", "")),
        "runtime_unit": str(vm.get("runtime_unit", "")),
        "owned_dirs": sorted(set(owned_dirs)),
    }
    target = _runtime_metadata_path(sid)
    temporary = (
        f"{target}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def _load_runtime_metadata(sid: str) -> dict:
    try:
        with open(
            _runtime_metadata_path(sid),
            encoding="utf-8",
        ) as stream:
            payload = json.load(stream)
    except (OSError, ValueError, TypeError):
        return {}
    if payload.get("version") != 1:
        return {}
    return payload


def _try_persist_runtime_metadata(sid: str, vm: dict) -> bool:
    try:
        _persist_runtime_metadata(sid, vm)
        return True
    except Exception as exc:
        log_event(
            "error",
            "runtime_metadata_persist_failed",
            sandbox_id=sid,
            error_type=type(exc).__name__,
        )
        return False


def _snapshot_source(sid: str, snap_dir: str) -> tuple[str | None, str | None]:
    """Identify a distinct managed sandbox directory backing a snapshot."""
    source_dir = _managed_sandbox_dir(os.path.dirname(snap_dir))
    sandbox_dir = _managed_sandbox_dir(f"{SBX_BASE}/{sid}")
    if not source_dir or source_dir == sandbox_dir:
        return None, None
    return os.path.basename(source_dir), source_dir


def _ensure_resume_source_available(sid: str, snap_dir: str) -> None:
    """A warm snapshot may only be claimed from an inactive source VM."""
    source_sid, _ = _snapshot_source(sid, snap_dir)
    if not source_sid:
        return
    with _LOCK:
        source = _VMS.get(source_sid)
        if source and source.get("state") != "suspended":
            raise RuntimeError(
                f"snapshot source {source_sid} is still "
                f"{source.get('state', 'unknown')}"
            )


def _register_resumed_vm(sid: str, vm: dict, snap_dir: str) -> None:
    """Atomically transfer a warm snapshot's local ownership to real sid."""
    sandbox_dir = _managed_sandbox_dir(f"{SBX_BASE}/{sid}")
    source_sid, source_dir = _snapshot_source(sid, snap_dir)
    owned_dirs = {
        path for path in (sandbox_dir, source_dir)
        if path is not None
    }
    with _LOCK:
        # A warm-claimed sandbox can later suspend into real_id/snap and
        # resume again. That second resume no longer exposes warm_id through
        # snap_dir, so retain the ownership transferred by the first claim.
        previous = _VMS.get(sid) or {}
        for path in previous.get("owned_dirs") or []:
            managed = _managed_sandbox_dir(str(path))
            if managed:
                owned_dirs.add(managed)
        vm["owned_dirs"] = sorted(owned_dirs)
        _VMS[sid] = vm
        if source_sid:
            source = _VMS.get(source_sid)
            if source and source.get("state") == "suspended":
                _VMS.pop(source_sid, None)
    _persist_runtime_metadata(sid, vm)


def _owned_runtime_dirs(sid: str, vm: dict) -> list[str]:
    """Return only safe per-sandbox directories owned by a runtime."""
    candidates = list(vm.get("owned_dirs") or [])
    candidates.extend((vm.get("dir", ""), f"{SBX_BASE}/{sid}"))
    result: list[str] = []
    for candidate in candidates:
        managed = _managed_sandbox_dir(str(candidate)) if candidate else None
        if managed and managed not in result:
            result.append(managed)
    return result


def _live_runtime_socket(sandbox_dir: str) -> str | None:
    for name in ("api.sock", "api-resume.sock"):
        sock = os.path.join(sandbox_dir, name)
        if os.path.exists(sock) and _wait_sock(sock, timeout=0.2):
            return sock
    return None


def op_destroy(body: dict) -> dict:
    sid = _sandbox_id(body.get("id"))
    with _vm_operation_lock(sid):
        return _op_destroy_locked(sid)


def _op_destroy_locked(sid: str) -> dict:
    with _LOCK:
        vm = _VMS.get(sid)
    sandbox_dir = _managed_sandbox_dir(f"{SBX_BASE}/{sid}")
    if not vm and VMM_LAUNCH_MODE == "host-systemd":
        runtime = _runtime_status(sid, strict=True)
        if runtime.get("active"):
            metadata = _load_runtime_metadata(sid)
            vm = {
                "state": "running",
                "pid": int(runtime.get("pid", 0) or 0),
                "sock": str(runtime.get("socket", "")),
                "tap": str(metadata.get("tap", "")),
                "tap_idx": int(metadata.get("tap_idx", 0) or 0),
                "ip": str(metadata.get("ip", "")),
                "dir": sandbox_dir or "",
                "runtime_unit": (
                    str(runtime.get("unit", ""))
                    or _runtime_unit_name(sid)
                ),
                "owned_dirs": metadata.get("owned_dirs", []),
            }
    if not vm and sandbox_dir and _live_runtime_socket(sandbox_dir):
        raise RuntimeError(
            f"sandbox {sid} has a live Firecracker socket but is not tracked"
        )
    if vm:
        _stop_vmm(sid, vm)
        _teardown_tap(vm.get("tap", ""))
        with _LOCK:
            if _VMS.get(sid) is vm:
                _VMS.pop(sid, None)
        # A warm claim restores from the warm source directory because the
        # Firecracker snapshot embeds paths to its rootfs/vsock/memory files.
        # The real sandbox therefore owns both its runtime directory and that
        # source directory until destroy. Remove both after the VMM is dead.
    cleanup_vm = vm or {
        "dir": sandbox_dir or "",
        "owned_dirs": (
            _recovered_owned_dirs(sandbox_dir)
            if sandbox_dir else []
        ),
    }
    for owned_dir in _owned_runtime_dirs(sid, cleanup_vm):
        shutil.rmtree(owned_dir, ignore_errors=True)
        _clear_base_hash_cache(owned_dir)
    return {"deleted": True}


def op_snapshot_base(body: dict) -> dict:
    """
    方案C 预热:在 sandbox 运行期打一次 Full base 快照(不 kill VMM,打完继续跑)。
    目的:spot 疏散时才能走 Diff(只写脏页),而 Diff 的前提是已有 base。
    创建后由控制面异步调用一次;off 关键路径(~16s 无所谓)。
    """
    sid      = _sandbox_id(body.get("id"))
    snap_dir = _owned_snapshot_dir(body, sid)
    with _LOCK:
        vm = _VMS.get(sid)
    if not vm:
        raise KeyError(sid)
    _vmm_writable_dir(sid, snap_dir)
    sock = vm["sock"]
    base_snap = f"{snap_dir}/vm.snapshot.base"
    base_mem  = f"{snap_dir}/vm.mem.base"

    if os.path.exists(base_mem):
        return {"base_exists": True, "skipped": True}

    t0 = time.monotonic()
    _fc(sock, "PATCH", "/vm", {"state": "Paused"})
    try:
        # Full 快照直接写 base 文件(不覆盖后续 Diff 用的 vm.snapshot/vm.mem)
        # timeout=600: base Full 写 2GB;多个 base 并发时共享 EBS 带宽,单个可能耗时数分钟,
        # 必须给足超时,否则 _fc 超时抛异常会跳过下面 finally 的 Resumed → VM 卡在 Paused。
        _fc(sock, "PUT", "/snapshot/create", {
            "snapshot_type": "Full",
            "snapshot_path": base_snap,
            "mem_file_path": base_mem,
        }, timeout=600)
    finally:
        # 打完 base 立即恢复运行(base 是运行期快照,不释放 RAM)
        _fc(sock, "PATCH", "/vm", {"state": "Resumed"})
    dt = time.monotonic() - t0
    # 趁 base 刚写完(大概率仍在页缓存)把哈希算进缓存。否则 Spot 回收时第一次
    # suspend 要把 dense base 从 EBS 冷读一遍,与快照写入争抢实例 EBS 带宽
    # (真机实测读流量 200~630 MiB/s,checkpoint 有效写入只剩 ~450 MiB/s)。
    # 只预热缓存,校验逻辑不变。
    try:
        for name in ("vm.snapshot.base", "vm.mem.base"):
            if name in _FS_INTEGRITY_FILES and _fs_checksummed_mem(snap_dir):
                continue  # 由文件系统块校验担保,不做 sha256
            _snapshot_file_hash(f"{snap_dir}/{name}", name)
    except OSError:
        pass
    return {"base_created": True, "base_snapshot_time_s": round(dt, 3),
            "base_mem_bytes": os.path.getsize(base_mem)}


def op_suspend(body: dict) -> dict:
    sid       = _sandbox_id(body.get("id"))
    snap_dir  = _owned_snapshot_dir(body, sid)
    s3_prefix = _s3_prefix_for(sid, body.get("s3_prefix", ""))

    with _LOCK:
        vm = _VMS.get(sid)
    if not vm:
        raise KeyError(sid)

    _vmm_writable_dir(sid, snap_dir)
    sock = vm["sock"]

    # 方案 B：JuiceFS 模式下，暂停前先 flush 脏页到 S3
    # writeback 缓存里的脏页只有 flush 后才安全。
    # 通过 SSH/exec 在 guest 内执行 sync；失败不阻断（尽力而为）
    if JUICEFS_ENABLED and vm.get("ip"):
        try:
            subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no",
                 "-o", "ConnectTimeout=3", "-o", "BatchMode=yes",
                 f"root@{vm['ip']}", "sync; juicefs sync --help >/dev/null 2>&1 && sync || sync"],
                timeout=10, capture_output=True,
            )
        except Exception:
            pass  # flush 失败不阻断 suspend，但可能丢最近几秒写入

    # 暂停
    _fc(sock, "PATCH", "/vm", {"state": "Paused"})

    # 快照策略(方案C + Diff):
    #   已有 base(op_snapshot_base 预热打过) → Diff 快照(只写脏页,~百MB,秒级)
    #   无 base(未预热/预热失败) → Full 快照(写全量内存)并留一份作 base,后续可 Diff
    # 快照落 snap_dir(位于持久状态 EBS),spot 终止后卷幸存,无需传 S3。
    # 注:同事曾因"resume 后脏页位图重置 → 再 suspend 时 diff 退化成 full"放弃 Diff 改纯 Full;
    #    本方案 P0(op_resume 里 load 设 track_dirty_pages + merged 转正 base)已解决该问题,
    #    多代接力已实测 PASS,故保留 Diff。
    base_snap  = f"{snap_dir}/vm.snapshot.base"
    base_mem   = f"{snap_dir}/vm.mem.base"
    diff_snap  = f"{snap_dir}/vm.snapshot"
    diff_mem   = f"{snap_dir}/vm.mem"
    has_base   = os.path.exists(base_mem)

    # 正常 suspend 留足 600s；Spot 疏散会传入剩余 deadline，确保阻塞中的
    # Firecracker API 调用不会越过 120s 终止窗口。
    snapshot_budget_s = max(
        0.1, min(600.0, float(body.get("snapshot_timeout_s", 600)))
    )
    snapshot_started = time.monotonic()

    def remaining_snapshot_timeout() -> float:
        remaining = snapshot_budget_s - (
            time.monotonic() - snapshot_started
        )
        if remaining <= 0:
            raise TimeoutError("snapshot deadline exhausted")
        return max(0.1, remaining)

    t0 = time.monotonic()
    snap_type = "diff"
    try:
        if not has_base:
            # 无 base:Full 快照,同时保留一份作 base(供本 sandbox 后续 Diff)。
            # 状态盘使用 reflink-enabled XFS，避免物理复制整份内存文件再次消耗
            # EBS 吞吐和 120s 预算。
            snap_type = "full"
            _fc(sock, "PUT", "/snapshot/create", {
                "snapshot_type": "Full",
                "snapshot_path": diff_snap,
                "mem_file_path": diff_mem,
            }, timeout=remaining_snapshot_timeout())
            subprocess.run(
                ["cp", "--reflink=auto", diff_snap, base_snap],
                check=True,
            )
            subprocess.run(
                ["cp", "--reflink=auto", diff_mem, base_mem],
                check=True,
            )
        else:
            # 有 base:Diff 快照(只写自 base 以来的脏页)
            marker = f"{snap_dir}/{_INPLACE_MARKER}"
            inplace = False
            if _diff_mode(snap_dir) == "inplace":
                staged = f"{diff_mem}.clone"
                clone = subprocess.run(
                    ["cp", "--reflink=always", base_mem, staged],
                    capture_output=True, text=True,
                )
                if clone.returncode == 0:
                    if VMM_LAUNCH_MODE == "host-systemd" and VMM_USE_JAILER:
                        uid = _runtime_uid(sid)
                        os.chown(staged, uid, uid)
                    os.replace(staged, diff_mem)
                    with open(marker, "w") as stream:
                        stream.write("full\n")
                    inplace = True
                    snap_type = "diff-inplace"
                else:
                    try:
                        os.remove(staged)
                    except FileNotFoundError:
                        pass
            if not inplace:
                try:
                    os.remove(marker)
                except FileNotFoundError:
                    pass
            try:
                _fc(sock, "PUT", "/snapshot/create", {
                    "snapshot_type": "Diff",
                    "snapshot_path": diff_snap,
                    "mem_file_path": diff_mem,
                }, timeout=remaining_snapshot_timeout())
            except Exception:
                # Diff 失败(如未开 track_dirty_pages)→ 降级 Full，但必须
                # 继续复用原始硬截止时间，不能重新获得一整份 timeout。
                snap_type = "full-fallback"
                _fc(sock, "PUT", "/snapshot/create", {
                    "snapshot_type": "Full",
                    "snapshot_path": diff_snap,
                    "mem_file_path": diff_mem,
                }, timeout=remaining_snapshot_timeout())

        # suspend 只有在完整性清单落盘并立即回读通过后才算成功。
        _write_snapshot_manifest(snap_dir)
        _record_snapshot_verification(snap_dir)
    except Exception:
        try:
            _fc(sock, "PATCH", "/vm", {"state": "Resumed"})
        except Exception:
            pass
        with _LOCK:
            vm["state"] = "running"
        _try_persist_runtime_metadata(sid, vm)
        raise
    dt = time.monotonic() - t0

    # 方案C:快照写在持久状态 EBS 上(snap_dir),spot 终止后卷幸存,
    # 故【不传 S3】——删掉最慢的 S3 传输,是 120s 窗口内跑满 50 个的关键。
    # snapshot/create 同步完成即已落 EBS,数据已持久 → 可安全 kill VMM。
    # Stop the host-owned service (or legacy child process) after the durable
    # snapshot is complete, releasing guest RAM without tying lifecycle to the
    # node-agent Pod.
    _stop_vmm(sid, vm)
    time.sleep(0.2)  # nosemgrep: arbitrary-sleep -- 等 VMM 退出释放 vm.mem 文件句柄后再读大小

    # diff.mem 是稀疏文件:apparent 是全量大小,真实占盘用 st_blocks*512
    st = os.stat(f"{snap_dir}/vm.mem")
    mem_apparent = st.st_size
    mem_actual   = st.st_blocks * 512

    # kill VMM 后释放 tap 设备,防止泄漏堆积(否则 tap 会一直残留在节点上,
    # tap_idx 复用时与残留设备冲突)。resume 侧 _setup_tap 也会幂等重建,双保险。
    tap_name = vm.get("tap") or f"fctap{vm.get('tap_idx', '')}"
    if tap_name and tap_name != "fctap":
        _teardown_tap(tap_name)

    with _LOCK:
        vm["state"] = "suspended"
        vm["pid"]   = None
    _try_persist_runtime_metadata(sid, vm)

    # 快照默认上传 S3 作权威副本(控制面 SNAPSHOT_TO_S3 开关决定是否传 upload_s3)。
    # 上传整份 snap_dir(base + diff + manifest),跨机 resume 时 node-agent 从此前缀
    # 拉回并合并再 load。关开关时 s3_prefix 为空 → 只保留在持久状态 EBS(卷幸存靠 attach)。
    snapshot_s3  = ""
    s3_upload_dt = 0.0
    if s3_prefix and body.get("upload_s3", False):
        _t = time.monotonic()
        _s3_upload_sync(snap_dir, s3_prefix)
        s3_upload_dt = round(time.monotonic() - _t, 3)
        snapshot_s3  = s3_prefix

    return {
        "snapshot_type": snap_type,
        "snapshot_create_time_s": round(dt, 3),
        "mem_file_bytes": mem_apparent,
        "mem_actual_bytes": mem_actual,
        # 回填给控制面:已上传则为 S3 前缀(持久到 record.snapshot_s3),否则空串。
        "snapshot_s3": snapshot_s3,
        "s3_upload_time_s": s3_upload_dt,
    }


def _merge_diff_into_base(base_mem: str, diff_mem: str, merged: str) -> None:
    """
    把 Diff 快照的脏页叠加到 base.mem,产出完整内存镜像供 snapshot/load。
    diff_mem 是稀疏文件:只有"写过的区段"是真实数据,其余是空洞(hole)。
    用 SEEK_DATA/SEEK_HOLE 精确找出 diff 的数据区段,逐段覆盖到 base 副本上。
    (不能用"非零块"判断——脏页可能合法为全零,漏写会导致内存不一致。)
    """
    # 1) 先把 base 复制成 merged(reflink 秒级,不占额外空间)
    subprocess.run(["cp", "--reflink=auto", base_mem, merged], check=True)
    # 2) 用 SEEK_DATA/SEEK_HOLE 遍历 diff 的数据区段,覆盖到 merged
    size = os.path.getsize(diff_mem)
    with open(diff_mem, "rb") as fd, open(merged, "r+b") as fm:
        off = 0
        while off < size:
            try:
                data_start = os.lseek(fd.fileno(), off, os.SEEK_DATA)
            except OSError:
                break  # 后面全是空洞,没有更多数据
            try:
                data_end = os.lseek(fd.fileno(), data_start, os.SEEK_HOLE)
            except OSError:
                data_end = size
            fd.seek(data_start)
            fm.seek(data_start)
            remaining = data_end - data_start
            CHUNK = 8 * 1024 * 1024
            while remaining > 0:
                buf = fd.read(min(CHUNK, remaining))
                if not buf:
                    break
                fm.write(buf)
                remaining -= len(buf)
            off = data_end
        fm.flush()
        os.fsync(fm.fileno())


def _vsock_uds_in_snapshot(snapshot_path: str) -> list[str]:
    """
    从 Firecracker 的 vm.snapshot 里解析出【固化的 vsock host UDS 路径】。

    为什么需要:load 时 FC 会绑定快照里写死的那个 UDS 路径,不是我们约定的路径。
    暖池领取(claim)的沙盒,快照固化的是【暖池源目录】的 v.sock
    (如 {SBX_BASE}/warm-xxxx/v.sock),≠ 本沙盒 sid 目录。若那个路径残留 stale
    socket(上次失败的 FC 尝试留下的),bind 报 "Address in use (os error 98)"
    → resume 必失败。仅按 sid/dirname 猜路径清不掉它(实测 bug)。
    故直接从快照里把真实路径抠出来清理,冷建/暖池两种来源统一覆盖。

    快照是 bincode 二进制,但 UDS 路径以可读字符串内嵌,按 SBX_BASE 前缀正则扫描即可。
    """
    try:
        with open(snapshot_path, "rb") as f:
            blob = f.read()
    except OSError:
        return []
    pat = re.escape(SBX_BASE.encode()) + rb"/([A-Za-z0-9._\-]+)/v\.sock"
    # 快照文件可能来自 S3(不完全可信):目录段必须是合法沙盒 id,拒绝 "." / ".." 之类,
    # 否则后续 os.remove / os.symlink 会作用到 SBX_BASE 之外。
    owners = {m.decode("ascii", "ignore") for m in re.findall(pat, blob)}
    return sorted(f"{SBX_BASE}/{o}/v.sock" for o in owners if _SANDBOX_ID_RE.fullmatch(o))


def _existing_resume_result(sid: str, snap_dir: str = "") -> dict | None:
    # Reconciliation may retry after a successful response was lost. Do not
    # start a second Firecracker process or rebuild tap/rootfs. A normal
    # suspend intentionally retains a lightweight _VMS entry with pid=None;
    # that exact suspended state is the valid input to snapshot restore.
    persist_vm: dict | None = None
    result: dict | None = None
    with _LOCK:
        existing = _VMS.get(sid)
        if existing and existing.get("state") == "running":
            source_sid, source_dir = _snapshot_source(sid, snap_dir)
            if source_dir:
                owned_dirs = set(existing.get("owned_dirs") or [])
                owned_dirs.update((
                    _managed_sandbox_dir(f"{SBX_BASE}/{sid}"),
                    source_dir,
                ))
                existing["owned_dirs"] = sorted(
                    path for path in owned_dirs if path
                )
                source = _VMS.get(source_sid or "")
                if source and source.get("state") == "suspended":
                    _VMS.pop(source_sid or "", None)
            persist_vm = existing
            result = {
                "state": "running",
                "ip": existing.get("ip", ""),
                "already_exists": True,
                "restore_time_s": 0,
                "merge_time_s": 0,
                "restore_mode": "existing",
                "net_fix_ok": True,
                "juicefs_mode": JUICEFS_ENABLED,
            }
        elif existing and existing.get("state") != "suspended":
            raise RuntimeError(
                f"sandbox {sid} already exists in state "
                f"{existing.get('state', 'unknown')}"
            )
    if persist_vm is not None:
        _persist_runtime_metadata(sid, persist_vm)
    return result


def op_resume(body: dict) -> dict:
    sid        = _sandbox_id(body.get("id"))
    # 快照来源 id(snap_owner)可与 sid 不同(暖池 claim:warm_id → real_id),
    # 但 snapshot_local_path / rootfs_path / s3_prefix 三者必须指向同一个来源沙盒,
    # 且都严格落在服务端路径约定内(见"请求参数校验")。
    snap_dir, snap_owner = _sandbox_path(body.get("snapshot_local_path"), "snap",
                                         "snapshot_local_path")
    rootfs, rootfs_owner = _sandbox_path(body.get("rootfs_path"), "rootfs.ext4",
                                         "rootfs_path")
    if rootfs_owner != snap_owner:
        raise RequestValidationError("path_owner_mismatch", "rootfs_path")
    tap_idx    = _int_in_range(body.get("tap_idx"), 0, MAX_TAP_IDX, "tap_idx")
    s3_prefix  = _s3_prefix_for(snap_owner, body.get("s3_prefix", ""))
    _no_symlink(f"{SBX_BASE}/{sid}", "id")

    if existing_result := _existing_resume_result(sid, snap_dir):
        return existing_result
    _ensure_resume_source_available(sid, snap_dir)

    # 兜底:若本地无快照文件且传了 s3_prefix,从 S3 拉回。
    # 注:方案C 从不往 S3 上传快照(见 op_suspend 的 upload_s3 分支),控制面传下来的
    # s3_prefix 恒为空 → 这段兜底当前【不会触发】,为未来可选的 S3 归档预留。
    # 现实的跨机恢复靠持久状态 EBS 卷幸存 + attach 到新节点(卷已 attach 则本地就有快照)。
    restore_mode = "local"
    if not os.path.exists(f"{snap_dir}/vm.snapshot") and s3_prefix:
        _s3_download(s3_prefix, snap_dir)
        restore_mode = "s3"

    # 在合并内存和启动新 Firecracker 进程前校验，损坏快照不会进入 guest。
    _record_snapshot_verification(snap_dir)

    d = f"{SBX_BASE}/{sid}"
    os.makedirs(d, exist_ok=True)

    # 快照来源目录:暖池 claim 时 snap_dir=SBX_BASE/{warm_id}/snap,其父目录
    # (warm_id 目录)是快照固化的 vsock UDS 所在(v.sock 路径写死在快照里,
    # load 时 FC 会重新绑定它)。当 sid(real_id)≠ 快照来源 id 时,vsock 仍绑
    # 在来源目录,需清理来源目录的 v.sock,并在 sid 目录建 symlink 供 exec 用。
    src_dir = os.path.dirname(snap_dir)
    guessed_vsock = f"{src_dir}/v.sock"
    # 权威来源:直接从快照里抠出固化的 vsock UDS 路径。warm claim 后再次
    # suspend 时，snap_dir 已是 real_id/snap，但设备仍绑定 warm_id/v.sock；
    # 只按 snap_dir 推测会漏建 real_id -> warm_id symlink，导致 resume 后 exec 失联。
    snapshot_vsocks = _vsock_uds_in_snapshot(f"{snap_dir}/vm.snapshot")
    vsock_bound = snapshot_vsocks[0] if snapshot_vsocks else guessed_vsock
    # 合并成待清理集合，避免 load 时因残留 socket 报 Address in use。
    stale_vsocks = {f"{d}/v.sock", vsock_bound}
    stale_vsocks.update(snapshot_vsocks)

    # rootfs 准备:方案C 下 rootfs 就在状态 EBS 的 {sid}/rootfs.ext4(随卷迁移,含装的软件)。
    # 回退:本地已有→直接用;快照目录里有→复制;都没有→基础镜像 CoW。
    if not os.path.exists(rootfs):
        snap_rootfs = f"{snap_dir}/rootfs.ext4"
        if os.path.exists(snap_rootfs):
            shutil.copy2(snap_rootfs, rootfs)
        else:
            # 最后兜底:按模板 CoW(与 create 一致,未知模板回退默认)
            src = _rootfs_template_path(body.get("rootfs_template", ""))
            subprocess.run(["cp", "--reflink=auto", src, rootfs], check=True)

    # 内存镜像准备:若本次是 Diff 快照(存在 base.mem),需先把 diff 脏页合并到 base。
    #   Diff 快照的 vm.mem 只含脏页,不能独立 load(实测:仅 diff.mem load 失败/内存不全)。
    base_mem = f"{snap_dir}/vm.mem.base"
    diff_mem = f"{snap_dir}/vm.mem"
    mem_backend_path = diff_mem
    merge_time = 0.0
    inplace_image = os.path.exists(f"{snap_dir}/{_INPLACE_MARKER}")
    if inplace_image:
        # inplace:vm.mem 已是完整内存镜像(base 克隆 + 原地 Diff),直接 load。
        pass
    elif os.path.exists(base_mem) and os.path.exists(diff_mem):
        # ⚠️ 正确性:只要存在 base,就【必须】把 vm.mem 合并到 base 上再 load,不能直接 load vm.mem。
        #   原因:Diff 的 mem 是稀疏文件,自 base 以来【未改动的干净页是空洞(读为0)】。
        #   直接 load diff → 干净页变成 0 → 内存损坏。即使 diff 看起来"很满"(满载),
        #   仍可能有少量干净页是空洞,直接 load 会静默损坏这些页。
        #   合并语义:base 副本 + diff 的非空洞页覆盖 = 完整内存。对 Full 的 vm.mem 合并也安全
        #   (Full 无空洞,覆盖=全量拷贝)。故【无条件合并】,不再用稀疏比例启发式判断。
        merged = f"{snap_dir}/vm.mem.merged"
        tm = time.monotonic()
        _merge_diff_into_base(base_mem, diff_mem, merged)
        merge_time = time.monotonic() - tm
        mem_backend_path = merged

    # resume 前清理旧 vsock socket(快照含 vsock 设备,残留的 v.sock 会导致
    # "Address in use" → snapshot load 失败)。这是 Firecracker 快照恢复已知坑。
    # 清理集合含:sid 目录、目录约定路径、以及从快照抠出的固化真实路径(暖池来源)。
    for stale in stale_vsocks:
        try:
            os.remove(stale)
        except FileNotFoundError:
            pass

    runtime_bind_dirs = [d, src_dir]
    for path in stale_vsocks:
        managed = _managed_sandbox_dir(os.path.dirname(path))
        if managed:
            runtime_bind_dirs.append(managed)
    snapshot_mem_mib = max(
        1,
        (os.path.getsize(mem_backend_path) + 1024 * 1024 - 1)
        // (1024 * 1024),
    )
    pid, sock, runtime_unit = _launch_vmm(
        sid,
        "api-resume.sock",
        snapshot_mem_mib,
        bind_dirs=runtime_bind_dirs,
        log_name="vm-resume.log",
    )

    if not _wait_sock(sock):
        _stop_vmm(
            sid,
            {"pid": pid, "runtime_unit": runtime_unit},
        )
        raise RuntimeError("firecracker resume socket 未就绪")

    # 快照本身已含 vsock 设备配置（含 host UDS 路径 v.sock）。load 时 Firecracker
    # 会自动重建 vsock 并绑定该 UDS；若旧 v.sock 文件残留会导致
    # "Address in use (os error 98)" → 必须先删旧 socket 文件。
    for stale in stale_vsocks:
        try:
            os.remove(stale)
        except FileNotFoundError:
            pass

    # 先重建 tap,再 load 快照。
    # 顺序关键:snapshot/load(resume_vm=True) 会立即恢复快照里保存的网络设备并
    # 打开宿主 tap(fctap{idx});若此时 tap 尚未就绪(或残留旧设备占用),FC 报
    # "Open tap device failed: Resource busy" → load 失败。因此必须先 setup_tap。
    tap = ""
    try:
        tap, _, guest_ip = _setup_tap(tap_idx)

        # load 用 mem_backend_path:有 base(Diff 快照)时 = 上面合并出的 merged;
        # 无 base(Full 快照,如暖池首份)时 = vm.mem 本身。
        t0 = time.monotonic()
        _fc(sock, "PUT", "/snapshot/load", {
            "snapshot_path": f"{snap_dir}/vm.snapshot",
            "mem_backend": {
                "backend_path": mem_backend_path,
                "backend_type": "File",
            },
            "track_dirty_pages": True,
            "resume_vm": True,
        })
        dt = time.monotonic() - t0
    except Exception:
        _stop_vmm(
            sid,
            {"pid": pid, "runtime_unit": runtime_unit},
        )
        if tap:
            _teardown_tap(tap)
        raise

    # ---- P0: merged 转正为新 base(多代接力 + 存储减半)----
    # FC 语义(官方文档):load 时重置脏页位图 → resume 后的 Diff 基准 = 本次 load 的完整内存镜像。
    # 因此 merged 必须成为下一轮 Diff 的 base。同时旧 base/diff 作废,删除以省空间。
    # ⚠️ merged 正被 FC mmap 当运行内存,不能删/不能动它的 inode → 用 os.replace 原子改名(inode 不变)。
    if inplace_image:
        try:
            # 与 merged 同理:完整镜像转正为下一代 base(inode 不变,mmap 不受影响)
            os.replace(diff_mem, base_mem)
            mem_backend_path = base_mem
            os.remove(f"{snap_dir}/{_INPLACE_MARKER}")
        except OSError:
            pass
    elif mem_backend_path.endswith("vm.mem.merged"):
        try:
            # 删旧 diff(已并入 merged)
            if os.path.exists(diff_mem):
                os.remove(diff_mem)
            # merged 原子改名为 base:os.replace 保留 inode + 已建立的 mmap 不受影响,
            # 只是把老 base_mem 的目录项替换掉。老 base 的数据块在 rename 覆盖后被释放。
            os.replace(mem_backend_path, base_mem)  # merged → base(新基准)
            mem_backend_path = base_mem
        except OSError:
            pass  # 转正失败不影响本次已 resume 成功的 VM;下轮 suspend 会降级 Full 兜底

    # 注:tap 已在 snapshot/load 之前重建(见上,顺序关键,避免 Resource busy)。
    # 暖池 claim(sid=real_id ≠ 快照来源):FC 已把 vsock 绑到来源目录的
    # v.sock(快照固化路径),但 exec 按 sid 目录找 {d}/v.sock。建 symlink 让
    # exec 的 vsock 主通道能连上,否则 exec 掉到 SSH 兜底甚至失败。
    if os.path.abspath(vsock_bound) != os.path.abspath(f"{d}/v.sock"):
        try:
            if os.path.lexists(f"{d}/v.sock"):
                os.remove(f"{d}/v.sock")
            os.symlink(vsock_bound, f"{d}/v.sock")
        except OSError:
            pass

    resumed_vm = {
        "state":   "running",
        "pid":     pid,
        "sock":    sock,
        "tap":     tap,
        "tap_idx": tap_idx,
        "ip":      guest_ip,
        "dir":     d,
        "runtime_unit": runtime_unit,
    }
    try:
        _register_resumed_vm(sid, resumed_vm, snap_dir)
    except Exception:
        with _LOCK:
            if _VMS.get(sid) is resumed_vm:
                _VMS.pop(sid, None)
        _stop_vmm(sid, resumed_vm)
        _teardown_tap(tap)
        raise

    # ---- P1: resume 后经 vsock 加速 guest 网络收敛 ----
    # 跨机 resume 后,guest 内存快照固化了旧宿主 tap 的网关 MAC(stale ARP)。
    # 新宿主 tap 是不同 MAC → guest 发包到旧 MAC → 网络不通,要等内核 ARP STALE 探测
    # 重新学习(实测同机 ~6s、50并发跨机 ~30s)。
    # 修复:经 vsock(不依赖 guest IP 网络,正好此刻网络不通) 下发 ip neigh flush + gratuitous ARP,
    #      清掉 stale 项并主动通告,实测 0.1s 即恢复(见 P1 验证)。
    # 走 vsock 通道(v.sock 存在时),失败不阻断 resume(内核最终也会自愈)。
    net_fix_ok = False
    vsock_uds = f"{d}/v.sock"
    if os.path.exists(vsock_uds):
        # ip neigh flush 清 stale;arping -U 发 gratuitous ARP 让网关/邻居更新对 guest 的映射。
        fix_cmd = ("ip neigh flush all 2>/dev/null; "
                   "ip neigh flush dev eth0 2>/dev/null; "
                   f"(command -v arping >/dev/null 2>&1 && arping -U -c1 -w1 -I eth0 {guest_ip} >/dev/null 2>&1); "
                   "echo NETFIX_DONE")
        for _ in range(8):  # 等 guest vsock agent 就绪(resume 后 agent 随内存恢复,通常立即可用)
            try:
                r = _vsock_exec(vsock_uds, fix_cmd, timeout=8)
                if "NETFIX_DONE" in (r.get("stdout") or ""):
                    net_fix_ok = True
                    break
            except Exception:
                time.sleep(0.5)  # nosemgrep: arbitrary-sleep -- 等 vsock agent 就绪的退避

    return {"restore_time_s": round(dt, 4), "ip": guest_ip,
            "merge_time_s": round(merge_time, 4),
            "restore_mode": restore_mode,
            "net_fix_ok": net_fix_ok,
            "juicefs_mode": JUICEFS_ENABLED}


def op_exec(body: dict) -> dict:
    """
    在 running 沙盒内执行命令。优先级:
      1. vsock UDS(不依赖 guest 网络，优先)
      2. TAP 网络 SSH(兜底，需 rootfs 内 sshd)
    """
    sid = _sandbox_id(body.get("id"))
    cmd = body.get("cmd", "echo no-cmd")
    if not isinstance(cmd, str):
        raise RequestValidationError("bad_cmd", "cmd")
    timeout = _int_in_range(body.get("timeout", 60), 1, 3600, "timeout")

    with _LOCK:
        vm = _VMS.get(sid)
    if not vm or vm["state"] != "running":
        raise RuntimeError(f"sandbox {sid} not running")

    # --- 方式 1: vsock（优先）---
    # 不依赖 guest 网络配置（tap IP / sshd），只要 guest 内 vsock-exec-agent 在监听。
    # Firecracker vsock: host 端 UDS = {d}/v.sock，需先发 "CONNECT <port>\n" 握手，
    # FC 回 "OK <assigned_port>\n" 后进入透传，再收发与 guest agent 约定的 JSON 协议。
    # create 乐观返回 running，VM 可能仍在 boot / agent 未起 → 短重试等就绪。
    d     = vm.get("dir", f"{SBX_BASE}/{sid}")
    vsock = f"{d}/v.sock"
    if os.path.exists(vsock):
        last_err = None
        for attempt in range(10):  # 最多重试 ~10s，覆盖 guest boot + agent 启动
            try:
                return _vsock_exec(vsock, cmd, timeout)
            except Exception as e:
                last_err = e
                time.sleep(1)  # nosemgrep: arbitrary-sleep -- 等 guest vsock agent 就绪的退避
        # vsock 始终不通则回退 SSH（记录最后错误供排查）
        _ = last_err

    # --- 方式 2: TAP SSH（兜底，需 rootfs 内 sshd + 正确 guest IP）---
    guest_ip = vm.get("ip", "")
    if guest_ip:
        r = subprocess.run(
            ["ssh",
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", f"ConnectTimeout=5",
             "-o", "BatchMode=yes",
             f"root@{guest_ip}", "--", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 255:  # 255=SSH 连接失败,其他为命令退出码
            return {"rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr}

    raise RuntimeError(
        f"sandbox {sid}: exec failed (vsock agent unreachable, SSH unreachable). "
        "Ensure guest vsock-exec-agent is running or SSH is configured in rootfs."
    )


def _vsock_exec(vsock_uds: str, cmd: str, timeout: int, port: int = 2222) -> dict:
    """通过 Firecracker vsock UDS 把命令发给 guest 的 vsock-exec-agent。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(vsock_uds)
        # Firecracker vsock 握手：CONNECT <guest_port>\n → "OK <host_port>\n"
        s.sendall(f"CONNECT {port}\n".encode())
        ack = b""
        while b"\n" not in ack:
            chunk = s.recv(64)
            if not chunk:
                raise RuntimeError("vsock handshake: no ACK from firecracker")
            ack += chunk
        if not ack.startswith(b"OK"):
            raise RuntimeError(f"vsock handshake failed: {ack!r}")
        # 发请求（一行 JSON）
        s.sendall((json.dumps({"cmd": cmd}) + "\n").encode())
        # 收响应（一行 JSON）
        resp = b""
        while b"\n" not in resp:
            chunk = s.recv(65536)
            if not chunk:
                break
            resp += chunk
        line = resp.split(b"\n", 1)[0]
        data = json.loads(line.decode(errors="replace"))
        return {"rc": data.get("rc", -1),
                "stdout": data.get("stdout", ""),
                "stderr": data.get("stderr", "")}
    finally:
        s.close()


def op_get(sid: str) -> dict:
    sid = _sandbox_id(sid)
    with _LOCK:
        vm = _VMS.get(sid)
    if not vm:
        raise KeyError(sid)
    return {"state": vm["state"], "ip": vm.get("ip", ""), "pid": vm.get("pid")}


def op_health() -> dict:
    mem = _free_mem_mib()
    with _LOCK:
        count = len(_VMS)
        draining = bool(_RECLAIM_STATE.get("detected"))
    recovery_role, recovery_group, _ = _node_recovery_identity()
    state_volume_id = _state_volume_id()
    return {
        "node_id": NODE_ID,
        "free_mem_mib": mem,
        "vm_count": count,
        "pool": _detect_pool(),
        "draining": draining,
        "recovery_role": _effective_recovery_role(recovery_role),
        "recovery_group": recovery_group,
        "instance_id": _instance_id(),
        "availability_zone": _availability_zone(),
        "state_volume_id": state_volume_id,
    }


def _scratch_bytes() -> tuple[int, int]:
    try:
        stat = os.statvfs(SBX_BASE)
        return stat.f_bavail * stat.f_frsize, stat.f_blocks * stat.f_frsize
    except OSError:
        return 0, 0


def _refresh_metrics() -> None:
    with _LOCK:
        states: dict[str, int] = {}
        for vm in _VMS.values():
            state = vm.get("state", "unknown")
            states[state] = states.get(state, 0) + 1
    scratch_free, scratch_total = _scratch_bytes()
    refresh_node_metrics(
        NODE_ID,
        states,
        _free_mem_mib() * 1024 * 1024,
        scratch_free,
        scratch_total,
    )


def health_report(require_dependencies: bool) -> tuple[int, dict]:
    checks: dict[str, object] = {"http": "ok"}
    heartbeat_loop_age = time.monotonic() - _HEARTBEAT_LAST_ITERATION
    heartbeat_loop_ok = heartbeat_loop_age <= max(30, HEARTBEAT_EVERY_S * 3)
    checks["heartbeat_loop"] = heartbeat_loop_ok
    healthy = heartbeat_loop_ok
    if require_dependencies:
        checks["kvm"] = os.path.exists("/dev/kvm")
        checks["firecracker"] = os.path.isfile(FC_BIN) and os.access(FC_BIN, os.X_OK)
        if VMM_LAUNCH_MODE == "host-systemd":
            checks["host_nsenter"] = (
                os.path.isfile(HOST_NSENTER)
                and os.access(HOST_NSENTER, os.X_OK)
            )
            try:
                host_runtime = _host_control(
                    HOST_VMM_CTL, ["check"], timeout=3
                )
                checks["host_runtime"] = bool(host_runtime.get("ok"))
            except Exception:
                checks["host_runtime"] = False
        else:
            checks["host_nsenter"] = True
            checks["host_runtime"] = not VMM_USE_JAILER
        checks["node_agent_auth"] = _AUTH.configured
        checks["state_path"] = os.path.isdir(SBX_BASE) and os.access(SBX_BASE, os.W_OK)
        heartbeat_age = (
            time.monotonic() - _HEARTBEAT_LAST_SUCCESS
            if _HEARTBEAT_LAST_SUCCESS else None
        )
        checks["heartbeat_age_seconds"] = (
            round(heartbeat_age, 3) if heartbeat_age is not None else None
        )
        heartbeat_ok = (
            heartbeat_age is not None
            and heartbeat_age <= max(30, HEARTBEAT_EVERY_S * 3)
        )
        checks["heartbeat"] = heartbeat_ok
        healthy = healthy and all((
            checks["kvm"],
            checks["firecracker"],
            checks["host_nsenter"],
            checks["host_runtime"],
            checks["node_agent_auth"],
            checks["state_path"],
            heartbeat_ok,
        ))
    return (200 if healthy else 503), {
        "status": "ok" if healthy else "unhealthy",
        "checks": checks,
    }


def _free_mem_mib() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


# ---------- 心跳注册(P0-3) ----------

def _advertise_ip() -> str:
    """探测控制面可达的本机内网 IP。NODE_ADVERTISE_IP 优先。"""
    if NODE_ADVERTISE_IP:
        return NODE_ADVERTISE_IP
    try:
        # 无需真正发包:connect 到公网地址让内核选出主网卡源 IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def _heartbeat_once() -> None:
    """
    向 DynamoDB nodes 表 upsert 一条心跳。
    用 aws CLI 子进程(与 S3 快照上传同款依赖),不引入 boto3 —— node-agent 镜像
    只保证装了 awscli,容器里 python3 版本可能与 boto3 安装目标不一致(实测踩坑)。
    """
    global _HEARTBEAT_LAST_SUCCESS
    with _LOCK:
        vm_count = len(_VMS)
        draining = bool(_RECLAIM_STATE.get("detected"))
        reclaim_phase = str(
            (_RECLAIM_STATE.get("plan") or {}).get("phase", "")
        )
        reclaim_session_id = str(
            (_RECLAIM_STATE.get("plan") or {}).get("session_id", "")
        )
    configured_role, recovery_group, identity_resolved = (
        _node_recovery_identity()
    )
    if not identity_resolved:
        raise RuntimeError(
            f"cannot resolve recovery identity for Kubernetes node {NODE_ID}"
        )
    state_volume_id = _state_volume_id()
    # 用 UpdateItem 而不是 PutItem：恢复控制器会在同一节点记录上原子写入
    # recovery_claim_id。心跳必须保留该字段，不能每 30 秒整条覆盖掉。
    names = {
        "#ip": "ip",
        "#free": "free_mem_mib",
        "#vms": "vm_count",
        "#seen": "last_seen",
        "#pool": "pool",
        "#iid": "instance_id",
        "#az": "availability_zone",
        "#vol": "state_volume_id",
        "#role": "recovery_role",
        "#recovery_group": "recovery_group",
        "#draining": "draining",
        "#phase": "reclaim_phase",
        "#session": "reclaim_session_id",
    }
    values = {
        ":ip": {"S": _advertise_ip()},
        ":free": {"N": str(_free_mem_mib())},
        ":vms": {"N": str(vm_count)},
        ":seen": {"S": datetime.now(timezone.utc).isoformat()},
        # M2:节点池归属(spot / protected),控制面据此做放置决策。
        ":pool": {"S": _detect_pool()},
        ":iid": {"S": _instance_id()},
        ":az": {"S": _availability_zone()},
        ":vol": {"S": state_volume_id},
        ":role": {"S": _effective_recovery_role(configured_role)},
        ":recovery_group": {"S": recovery_group},
        ":draining": {"BOOL": draining},
        ":phase": {"S": reclaim_phase},
        ":session": {"S": reclaim_session_id},
    }
    subprocess.run(
        ["aws", "dynamodb", "update-item",
         "--table-name", NODES_TABLE,
         "--region", AWS_REGION,
         "--key", json.dumps({"node_id": {"S": NODE_ID}}),
         "--update-expression",
         (
             "SET #ip=:ip, #free=:free, #vms=:vms, #seen=:seen, "
             "#pool=:pool, #iid=:iid, #az=:az, #vol=:vol, #role=:role, "
             "#recovery_group=:recovery_group, #draining=:draining, "
             "#phase=:phase, #session=:session"
         ),
         "--expression-attribute-names", json.dumps(names),
         "--expression-attribute-values", json.dumps(values)],
        check=True, capture_output=True, text=True,
    )
    _HEARTBEAT_LAST_SUCCESS = time.monotonic()


def start_heartbeat_loop() -> None:
    def _loop():
        global _HEARTBEAT_LAST_ITERATION
        while True:
            _HEARTBEAT_LAST_ITERATION = time.monotonic()
            if GUEST_FIREWALL:
                # 周期校正:kube-proxy 等重启后可能把自己的规则插回链首
                try:
                    ensure_guest_firewall_jumps()
                except Exception as e:
                    log_event("error", "guest_firewall_reassert_failed",
                              error_type=type(e).__name__)
            try:
                _heartbeat_once()
            except Exception as e:
                NODE_HEARTBEAT_ERRORS.labels(NODE_ID).inc()
                log_event(
                    "error", "heartbeat_failed",
                    error_type=type(e).__name__,
                )
            time.sleep(HEARTBEAT_EVERY_S)  # nosemgrep: arbitrary-sleep -- 心跳周期
    threading.Thread(target=_loop, daemon=True).start()


# ---------- 启动自恢复(P0-1:重建 _VMS,防重启后状态漂移) ----------

def _recovered_owned_dirs(sandbox_dir: str) -> list[str]:
    """Infer warm-source ownership from the recovered vsock symlink."""
    owned = {_managed_sandbox_dir(sandbox_dir)}
    vsock = os.path.join(sandbox_dir, "v.sock")
    if os.path.islink(vsock):
        source_dir = _managed_sandbox_dir(
            os.path.dirname(os.path.realpath(vsock))
        )
        if source_dir:
            owned.add(source_dir)
    return sorted(path for path in owned if path)


def _recover_vms() -> int:
    """
    启动时扫 SBX_BASE 下各沙盒目录,对仍有存活 FC api socket 的重建 _VMS。
    探测不到的目录跳过(交给控制面 reconcile 标 orphaned)。返回恢复数量。
    """
    recovered = 0
    if not os.path.isdir(SBX_BASE):
        return 0
    for sid in os.listdir(SBX_BASE):
        if not _VMM_ID_RE.fullmatch(sid):
            continue
        d = f"{SBX_BASE}/{sid}"
        if not os.path.isdir(d):
            continue
        metadata = _load_runtime_metadata(sid)
        runtime = _runtime_status(sid)
        socket_candidates = [
            f"{d}/api.sock",
            f"{d}/api-resume.sock",
        ]
        runtime_socket = str(runtime.get("socket", ""))
        if runtime_socket and runtime_socket not in socket_candidates:
            socket_candidates.insert(0, runtime_socket)
        # create 用 api.sock,resume 用 api-resume.sock —— 依次探测
        for sock in socket_candidates:
            if not os.path.exists(sock):
                continue
            if not _wait_sock(sock, timeout=1.0):
                continue
            # socket 通 → FC 存活,查其真实运行状态(FC: GET / 返回 InstanceInfo)
            try:
                info  = _fc(sock, "GET", "/")
                state = "running" if info.get("state") == "Running" else "paused"
            except Exception:
                state = "running"  # 探不到状态但 socket 通,保守当 running
            recovered_pid = (
                int(runtime.get("pid", 0) or 0)
                or _legacy_vmm_pid(sock)
            )
            socket_name = os.path.basename(sock)
            if socket_name in {"api.sock", "api-resume.sock"}:
                _link_runtime_socket(sid, socket_name, sock)
            with _LOCK:
                _VMS[sid] = {
                    "state": state,
                    "pid":   recovered_pid,
                    "sock":  sock,
                    # Never guess another VM's tap after restart. New
                    # host-systemd runtimes always persist these fields; a
                    # legacy/corrupt record leaves them empty so destroy may
                    # leak one tap but cannot delete a neighbour's interface.
                    "tap": str(metadata.get("tap", "")),
                    "tap_idx": int(metadata.get("tap_idx", 0) or 0),
                    "ip": str(metadata.get("ip", "")),
                    "dir":   d,
                    "runtime_unit": (
                        str(runtime.get("unit", ""))
                        if runtime.get("active") else ""
                    ),
                    # Warm-claimed VMs keep a symlink to the snapshot source
                    # vsock. Recover that source directory so a later destroy
                    # still removes all locally-owned state after restart.
                    "owned_dirs": sorted(set(
                        _recovered_owned_dirs(d)
                        + [
                            managed
                            for path in metadata.get(
                                "owned_dirs", []
                            )
                            if (
                                managed := _managed_sandbox_dir(
                                    str(path)
                                )
                            )
                        ]
                    )),
                }
            recovered += 1
            break
    return recovered

# ---------- Spot 回收信号监听 → 疏散(Block 1) ----------

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _imds_token() -> str | None:
    """取 IMDSv2 token(PUT /latest/api/token)。IMDSv1 环境失败返回 None 仍可继续。"""
    try:
        req = urllib.request.Request(
            f"{IMDS_BASE}/latest/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        with urllib.request.urlopen(req, timeout=1) as r:
            return r.read().decode()
    except Exception:
        return None


def _imds_get(path: str, token: str | None) -> tuple[int, str]:
    headers = {"X-aws-ec2-metadata-token": token} if token else {}
    try:
        req = urllib.request.Request(f"{IMDS_BASE}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=1) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""      # 正常无信号时 spot/instance-action 返回 404
    except Exception:
        return 0, ""           # IMDS 不可达(本地/非 EC2)


def _metadata_value(path: str) -> str:
    token = _imds_token()
    status, body = _imds_get(path, token)
    return (body or "").strip() if status == 200 else ""


def _instance_id() -> str:
    global _INSTANCE_ID_CACHE
    if not _INSTANCE_ID_CACHE:
        _INSTANCE_ID_CACHE = _metadata_value(
            "/latest/meta-data/instance-id"
        )
    return _INSTANCE_ID_CACHE


def _availability_zone() -> str:
    global _AZ_CACHE
    if not _AZ_CACHE:
        _AZ_CACHE = _metadata_value(
            "/latest/meta-data/placement/availability-zone"
        )
    return _AZ_CACHE


def _normalize_volume_id(raw: str) -> str:
    value = (raw or "").strip().lower().replace(" ", "")
    if not value:
        return ""
    if value.startswith("vol-"):
        return value
    if value.startswith("vol") and len(value) > 3:
        return f"vol-{value[3:]}"
    return ""


def _fetch_node_object() -> dict:
    """Read this pod's Kubernetes Node object through the in-cluster API."""
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    if not host or not NODE_ID:
        raise RuntimeError("Kubernetes service address or NODE_ID is missing")
    token_path = os.path.join(K8S_SERVICE_ACCOUNT_DIR, "token")
    ca_path = os.path.join(K8S_SERVICE_ACCOUNT_DIR, "ca.crt")
    with open(token_path, encoding="utf-8") as token_file:
        token = token_file.read().strip()
    request = urllib.request.Request(
        f"https://{host}:{port}/api/v1/nodes/{quote(NODE_ID, safe='')}",
        headers={"Authorization": f"Bearer {token}"},
    )
    context = ssl.create_default_context(cafile=ca_path)
    with urllib.request.urlopen(
        request, timeout=2, context=context
    ) as response:
        payload = json.loads(response.read().decode())
    if not isinstance(payload, dict):
        raise RuntimeError("Kubernetes Node response is not an object")
    return payload


def _fetch_node_labels() -> dict[str, str]:
    """Read this pod's Kubernetes Node labels through the in-cluster API."""
    payload = _fetch_node_object()
    labels = payload.get("metadata", {}).get("labels", {})
    return {
        str(key): str(value)
        for key, value in labels.items()
    }


def _node_recovery_identity(
    force: bool = False,
) -> tuple[str, str, bool]:
    """Resolve role/group from Node labels, retaining the last good result.

    Downward API metadata.labels refers to Pod labels, not Node labels. A
    failed first lookup is therefore reported as unresolved instead of
    guessing that a root-only standby is active.
    """
    now = time.monotonic()
    with _NODE_RECOVERY_IDENTITY_LOCK:
        cached = dict(_NODE_RECOVERY_IDENTITY_CACHE)
    if (
        not force
        and bool(cached.get("resolved"))
        and now - float(cached.get("fetched_at", 0.0))
        < NODE_LABEL_REFRESH_S
    ):
        return (
            str(cached.get("role", "")),
            str(cached.get("group", "")),
            True,
        )
    try:
        labels = _fetch_node_labels()
        role = (
            labels.get("sandbox.memorion.ai/recovery-role")
            or NODE_RECOVERY_ROLE
            or "active"
        ).strip().lower()
        if role not in {"active", "standby"}:
            raise RuntimeError(f"invalid recovery role label: {role!r}")
        group = (
            labels.get("sandbox.memorion.ai/recovery-group")
            or NODE_RECOVERY_GROUP
        ).strip()
        resolved = {
            "role": role,
            "group": group,
            "resolved": True,
            "fetched_at": now,
        }
        with _NODE_RECOVERY_IDENTITY_LOCK:
            _NODE_RECOVERY_IDENTITY_CACHE.update(resolved)
        return role, group, True
    except Exception:
        if bool(cached.get("resolved")):
            return (
                str(cached.get("role", "")),
                str(cached.get("group", "")),
                True,
            )
        if NODE_RECOVERY_ROLE in {"active", "standby"}:
            return NODE_RECOVERY_ROLE, NODE_RECOVERY_GROUP, True
        return "unknown", "", False


def _configured_recovery_role() -> str:
    role, _, _ = _node_recovery_identity()
    return role


def _findmnt_field(target: str, field: str) -> str:
    try:
        return subprocess.run(
            ["findmnt", "-n", "-o", field, "--target", target],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception:
        return ""


def _state_volume_device(volume_id: str = "") -> str:
    """Return the local NVMe device backing the state volume.

    Nitro exposes EBS volumes as NVMe devices and the requested /dev/sdf name
    is not stable. Match by EBS serial first; otherwise inspect the mounted
    filesystem. ``/opt/sbx`` is a hostPath on the host root filesystem, so its
    device identity is a stable baseline: an empty standby has the same
    MAJ:MIN for ``/var/lib/sbx`` and ``/opt/sbx``; a recovered standby has a
    distinct state-EBS MAJ:MIN even after the node-agent Pod restarts.
    """
    target = _normalize_volume_id(volume_id)
    if target:
        compact = target.replace("-", "")
        for path in (
            f"/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_{compact}",
            f"/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_{target}",
        ):
            if os.path.exists(path):
                return os.path.realpath(path)
        try:
            out = subprocess.run(
                ["lsblk", "-ndo", "PATH,SERIAL"],
                check=True, capture_output=True, text=True,
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and _normalize_volume_id(parts[-1]) == target:
                    return parts[0]
        except Exception:
            return ""

    source = _findmnt_field(SBX_BASE, "SOURCE")
    state_device_id = _findmnt_field(SBX_BASE, "MAJ:MIN")
    root_device_id = _findmnt_field(ROOTFS_DIR, "MAJ:MIN")
    if (
        not source.startswith("/dev/")
        or not state_device_id
        or not root_device_id
        or state_device_id == root_device_id
    ):
        return ""
    # Bind-mounted root paths can be rendered as
    # /dev/nvme0n1p1[/var/lib/sbx]. Strip the optional bind suffix before
    # resolving the device path.
    return os.path.realpath(source.split("[", 1)[0])


def _state_volume_id() -> str:
    global _STATE_VOLUME_CACHE
    if STATE_VOLUME_ID_OVERRIDE:
        return _normalize_volume_id(STATE_VOLUME_ID_OVERRIDE)
    if _STATE_VOLUME_CACHE:
        return _STATE_VOLUME_CACHE
    device = _state_volume_device()
    if not device:
        return ""
    try:
        serial = subprocess.run(
            ["lsblk", "-ndo", "SERIAL", device],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        _STATE_VOLUME_CACHE = _normalize_volume_id(serial)
    except Exception:
        _STATE_VOLUME_CACHE = ""
    return _STATE_VOLUME_CACHE


def _effective_recovery_role(configured: str) -> str:
    # A standby becomes an active recovery node as soon as a state EBS is
    # mounted. The Kubernetes label can stay immutable; heartbeat reflects the
    # real role used for placement and atomic standby claiming.
    attached_state_volume = (
        _normalize_volume_id(STATE_VOLUME_ID_OVERRIDE)
        or _STATE_VOLUME_CACHE
    )
    if configured == "standby" and attached_state_volume:
        return "active"
    return configured


def _recovery_role() -> str:
    _state_volume_id()
    return _effective_recovery_role(_configured_recovery_role())


def op_recovery_mount(body: dict) -> dict:
    """Mount an attached, pre-existing state EBS on a warm standby.

    EC2 attachment is performed by the control-plane IRSA role. This privileged
    hostNetwork/hostPID DaemonSet only discovers the Nitro NVMe device by EBS
    serial and mounts it at the canonical path required by Firecracker
    snapshots. mountPropagation=Bidirectional makes the host see the mount.
    """
    global _STATE_VOLUME_CACHE
    volume_id = _normalize_volume_id(str(body.get("volume_id", "")))
    if not volume_id:
        raise ValueError("volume_id is required")
    with _LOCK:
        resident = sorted(_VMS)
    if resident:
        raise RuntimeError(
            "standby has resident sandboxes and cannot take over volume: "
            f"{resident}"
        )

    timeout_s = max(1, min(180, int(body.get("timeout_s", 90))))
    deadline = time.monotonic() + timeout_s
    device = ""
    while time.monotonic() < deadline:
        device = _state_volume_device(volume_id)
        if device:
            break
        time.sleep(1)  # nosemgrep: arbitrary-sleep -- wait Nitro device attach
    if not device:
        raise TimeoutError(f"attached device for {volume_id} not found")

    try:
        mounted_source = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", "--target", SBX_BASE],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except Exception:
        mounted_source = ""
    if mounted_source and os.path.realpath(mounted_source) == os.path.realpath(device):
        _STATE_VOLUME_CACHE = volume_id
        return {
            "mounted": True,
            "already_mounted": True,
            "volume_id": volume_id,
            "device": device,
            "sandbox_dirs": sorted(os.listdir(SBX_BASE)),
        }

    os.makedirs(SBX_BASE, exist_ok=True)
    subprocess.run(["sync"], check=False)
    # Production asks host systemd to own the mount, so it survives Pod and
    # mount-namespace replacement and is correct after a host reboot.
    if VMM_LAUNCH_MODE == "host-systemd":
        _host_control(
            HOST_STATE_CTL,
            ["mount", "--device", device],
            timeout=30,
        )
    else:
        # Local/test compatibility path.
        subprocess.run(
            ["mount", "-t", "xfs", "-o", "noatime,nouuid", device, SBX_BASE],
            check=True, capture_output=True, text=True,
        )
    subprocess.run(["sync"], check=False)
    _STATE_VOLUME_CACHE = volume_id
    return {
        "mounted": True,
        "already_mounted": False,
        "volume_id": volume_id,
        "device": device,
        "sandbox_dirs": sorted(os.listdir(SBX_BASE)),
    }


def op_recovery_unmount(body: dict) -> dict:
    """Quiesce and unmount the current state EBS for a controlled handoff."""
    global _STATE_VOLUME_CACHE
    requested_volume = _normalize_volume_id(
        str(body.get("volume_id", ""))
    )
    current_volume = _state_volume_id()
    if requested_volume and current_volume and requested_volume != current_volume:
        raise RuntimeError(
            f"mounted state volume is {current_volume}, not {requested_volume}"
        )

    with _LOCK:
        running = sorted(
            sid for sid, vm in _VMS.items()
            if vm.get("state") not in {"suspended", "stopped"}
        )
    if running:
        raise RuntimeError(
            "state volume still has running sandboxes: "
            f"{running}"
        )
    if not current_volume:
        return {
            "unmounted": True,
            "already_unmounted": True,
            "volume_id": requested_volume,
        }

    device = _state_volume_device(current_volume)
    if not device:
        raise RuntimeError(
            f"device for mounted state volume {current_volume} not found"
        )
    subprocess.run(["sync"], check=False)
    if VMM_LAUNCH_MODE == "host-systemd":
        result = _host_control(
            HOST_STATE_CTL,
            ["unmount", "--device", device],
            timeout=30,
        )
    else:
        subprocess.run(
            ["umount", SBX_BASE],
            check=True,
            capture_output=True,
            text=True,
        )
        result = {"unmounted": True, "already_unmounted": False}
    with _LOCK:
        # Suspended entries have no VMM process and their durable state is now
        # moving with the EBS. They will be reconstructed on the target.
        _VMS.clear()
    _STATE_VOLUME_CACHE = ""
    return {
        **result,
        "volume_id": current_volume,
        "device": device,
    }


# ---------- 节点池归属(M2:受保护池 / 抢占池分离)----------
# 每个节点属于一个"池":
#   spot      —— 抢占实例,随时可能被回收。空闲/可疏散的沙盒优先放这里(便宜)。
#   protected —— on-demand 实例,不会被回收。活跃/交互中的沙盒放这里,避免抢占中断。
# 归属【单次探测缓存】:先看显式覆盖 NODE_POOL(便于本地/测试),否则查 IMDS
# instance-life-cycle("spot" → spot 池;"on-demand"/其它 → protected 池)。
# IMDS 不可达(非 EC2 本地)默认 protected —— 宁可当"不会被回收"避免误疏散。
_POOL_CACHE: str | None = None


def _detect_pool() -> str:
    """探测本节点池归属并缓存。返回 "spot" 或 "protected"。"""
    global _POOL_CACHE
    if _POOL_CACHE is not None:
        return _POOL_CACHE
    override = os.environ.get("NODE_POOL", "").strip().lower()
    if override in ("spot", "protected"):
        _POOL_CACHE = override
        return _POOL_CACHE
    try:
        token = _imds_token()
        st, body = _imds_get("/latest/meta-data/instance-life-cycle", token)
        life = (body or "").strip().lower()
        _POOL_CACHE = "spot" if (st == 200 and life == "spot") else "protected"
    except Exception:
        _POOL_CACHE = "protected"   # 探不到:保守当不可回收
    return _POOL_CACHE


def _check_reclaim_signal() -> dict | None:
    """
    检查 spot 回收信号,返回信号 dict 或 None。
    - 测试注入(_RECLAIM_STATE['injected'])优先 —— EKS 托管节点非 spot,真 ITN 不会触发,
      故用 POST /reclaim/simulate 注入来验证检测→决策链路。
    - spot/instance-action:硬通知(~120s 明确终止)。
    - events/recommendations/rebalance:软通知(更早的再平衡预警)。
    """
    inj = _RECLAIM_STATE.get("injected")
    if inj:
        return inj
    token = _imds_token()
    st, body = _imds_get("/latest/meta-data/spot/instance-action", token)
    if st == 200 and body:
        try:
            info = json.loads(body)
        except Exception:
            info = {"raw": body}
        return {"type": "spot-termination", **info}
    st, body = _imds_get("/latest/meta-data/events/recommendations/rebalance", token)
    if st == 200 and body:
        try:
            info = json.loads(body)
        except Exception:
            info = {"raw": body}
        return {"type": "rebalance-recommendation", **info}
    return None


def _local_running_vms() -> list[str]:
    with _LOCK:
        return [sid for sid, vm in _VMS.items() if vm.get("state") == "running"]


def _has_local_sandbox_state() -> bool:
    """Return whether the state EBS contains any managed sandbox directory."""
    try:
        return any(
            entry.is_dir(follow_symlinks=False)
            and _VMM_ID_RE.fullmatch(entry.name)
            and _managed_sandbox_dir(entry.path)
            for entry in os.scandir(SBX_BASE)
        )
    except OSError:
        return False


def _dynamodb_attr(value) -> dict:
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    return {"S": str(value)}


def _update_sandbox_recovery(sid: str, fields: dict) -> tuple[bool, str]:
    """Best-effort durable progress journal for the 120s reclaim path.

    Snapshot persistence must continue even if DynamoDB is briefly
    unavailable, so callers record the error but never fail the local
    checkpoint solely because the journal write failed.
    """
    if not SANDBOXES_TABLE:
        return False, "DYNAMODB_TABLE is empty"
    names = {"#id": "id"}
    values: dict[str, dict] = {}
    sets: list[str] = []
    for idx, (key, value) in enumerate(fields.items()):
        name = f"#f{idx}"
        token = f":v{idx}"
        names[name] = key
        values[token] = _dynamodb_attr(value)
        sets.append(f"{name}={token}")
    try:
        subprocess.run(
            [
                "aws", "dynamodb", "update-item",
                "--table-name", SANDBOXES_TABLE,
                "--region", AWS_REGION,
                "--key", json.dumps({"id": {"S": sid}}),
                "--update-expression", "SET " + ", ".join(sets),
                "--condition-expression", "attribute_exists(#id)",
                "--expression-attribute-names", json.dumps(names),
                "--expression-attribute-values", json.dumps(values),
            ],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return True, ""
    except Exception as exc:
        return False, str(exc)[:512]


def _signal_deadline(signal: dict) -> tuple[datetime, datetime]:
    """Return (termination_deadline, checkpoint_deadline).

    EC2 interruption notices normally include the future termination time.
    Rebalance recommendations and local simulations may not, so those receive
    the configured 120-second budget from detection.
    """
    now = datetime.now(timezone.utc)
    termination = now + timedelta(seconds=RECLAIM_BUDGET_S)
    raw = str(signal.get("time", "")).strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            # Ignore stale injection timestamps and implausibly distant times.
            if now + timedelta(seconds=5) < parsed < now + timedelta(hours=1):
                termination = parsed
        except ValueError:
            pass
    checkpoint = termination - timedelta(seconds=RECLAIM_COMMIT_RESERVE_S)
    return termination, max(checkpoint, now + timedelta(seconds=1))


def _preserve_state_volume() -> dict:
    """Make the state EBS survive this instance's imminent termination.

    State disks are normally DeleteOnTermination=true so failed bootstrap and
    ordinary unhealthy-node replacement cannot leak expensive orphan volumes.
    The Spot critical path flips only the current state attachment to false
    before checkpoint I/O starts, then verifies the EC2 attachment setting.
    """
    instance_id = _instance_id()
    volume_id = _state_volume_id()
    if not instance_id or not volume_id:
        raise RuntimeError(
            "cannot preserve state volume without instance_id and volume_id"
        )

    described = subprocess.run(
        [
            "aws", "ec2", "describe-volumes",
            "--region", AWS_REGION,
            "--volume-ids", volume_id,
            "--output", "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(described.stdout or "{}")
    attachments = (
        (payload.get("Volumes") or [{}])[0].get("Attachments") or []
    )
    attachment = next(
        (
            item for item in attachments
            if item.get("InstanceId") == instance_id
        ),
        None,
    )
    if not attachment or not attachment.get("Device"):
        raise RuntimeError(
            f"{volume_id} is not attached to {instance_id}"
        )
    device = str(attachment["Device"])
    subprocess.run(
        [
            "aws", "ec2", "modify-instance-attribute",
            "--region", AWS_REGION,
            "--instance-id", instance_id,
            "--block-device-mappings",
            json.dumps([{
                "DeviceName": device,
                "Ebs": {"DeleteOnTermination": False},
            }]),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    verified = subprocess.run(
        [
            "aws", "ec2", "describe-instances",
            "--region", AWS_REGION,
            "--instance-ids", instance_id,
            "--output", "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    instances = [
        item
        for reservation in (
            json.loads(verified.stdout or "{}").get("Reservations") or []
        )
        for item in reservation.get("Instances") or []
    ]
    mappings = instances[0].get("BlockDeviceMappings") if instances else []
    mapping = next(
        (
            item for item in mappings or []
            if item.get("Ebs", {}).get("VolumeId") == volume_id
        ),
        None,
    )
    if (
        not mapping
        or mapping.get("Ebs", {}).get("DeleteOnTermination") is not False
    ):
        raise RuntimeError(
            f"failed to verify DeleteOnTermination=false for {volume_id}"
        )
    return {
        "preserved": True,
        "instance_id": instance_id,
        "volume_id": volume_id,
        "device": device,
    }


def _checkpoint_one(
    sid: str,
    *,
    session_id: str,
    signal: dict,
    termination_deadline: datetime,
    checkpoint_deadline: datetime,
    expected_count: int,
    monotonic_deadline: float,
) -> dict:
    started = time.monotonic()
    common = {
        "recovery_session_id": session_id,
        "recovery_source_node": NODE_ID,
        "recovery_source_instance_id": _instance_id(),
        "recovery_source_volume_id": _state_volume_id(),
        "recovery_az": _availability_zone(),
        "recovery_expected_count": expected_count,
        "interruption_type": signal.get("type", "spot-termination"),
        "interruption_detected_at": _now_iso(),
        "recovery_deadline_at": termination_deadline.isoformat(),
        "recovery_checkpoint_only": bool(
            signal.get("checkpoint_only", False)
        ),
    }
    journal_errors: list[str] = []
    ok, error = _update_sandbox_recovery(
        sid,
        {
            **common,
            "state": "checkpointing",
            "recovery_phase": "checkpointing",
            "recovery_error": "",
            "updated_at": _now_iso(),
        },
    )
    if not ok:
        journal_errors.append(error)

    try:
        remaining = monotonic_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("checkpoint deadline exhausted before start")
        with _vm_operation_lock(sid):
            info = op_suspend({
                "id": sid,
                "snapshot_local_path": f"{SBX_BASE}/{sid}/snap",
                "snapshot_timeout_s": max(1, int(remaining)),
                # Spot critical path is EBS-only. Cross-AZ S3 replication is
                # asynchronous and must not consume the 120-second window.
                "upload_s3": False,
                "s3_prefix": "",
            })
        elapsed = time.monotonic() - started
        completed_at = _now_iso()
        fields = {
            **common,
            **info,
            "state": "checkpointed",
            "recovery_phase": "checkpointed",
            "checkpoint_completed_at": completed_at,
            "checkpoint_elapsed_s": round(elapsed, 3),
            "updated_at": completed_at,
        }
        ok, error = _update_sandbox_recovery(sid, fields)
        if not ok:
            journal_errors.append(error)
        return {
            "id": sid,
            "ok": True,
            "elapsed_s": round(elapsed, 3),
            "actual_bytes": int(info.get("mem_actual_bytes", 0) or 0),
            "snapshot_type": info.get("snapshot_type", ""),
            "journal_errors": journal_errors,
        }
    except Exception as exc:
        elapsed = time.monotonic() - started
        message = str(exc)[:1024]
        ok, error = _update_sandbox_recovery(
            sid,
            {
                **common,
                "state": "recovery_failed",
                "recovery_phase": "checkpoint_failed",
                "recovery_error": message,
                "checkpoint_elapsed_s": round(elapsed, 3),
                "updated_at": _now_iso(),
            },
        )
        if not ok:
            journal_errors.append(error)
        return {
            "id": sid,
            "ok": False,
            "elapsed_s": round(elapsed, 3),
            "error": message,
            "journal_errors": journal_errors,
        }


def _evacuate_local(signal: dict) -> dict:
    """
    收到回收信号 → 疏散本节点所有 running 沙盒。
    - DRY-RUN(默认):只算并记录疏散计划,不真打快照。
    - REAL(RECLAIM_AUTO_EVACUATE=1):对每个本地 running VM 打 Diff 快照到持久 EBS
      (方案C,不传 S3)。状态回写 DynamoDB 由控制面 reconcile 感知(节点消失→
      needs_reschedule),编排层跨机拉起见 Block 2(尚未实现)。
    """
    import sys
    sids = _local_running_vms()
    session_id = uuid.uuid4().hex
    termination_deadline, checkpoint_deadline = _signal_deadline(signal)
    budget_s = max(
        1.0,
        (checkpoint_deadline - datetime.now(timezone.utc)).total_seconds(),
    )
    monotonic_deadline = time.monotonic() + budget_s
    # 疏散耗时粗估:历史满载约 1.3GB/个 Diff；实际验收以结果中的
    # total_actual_bytes / wall_clock 为准，不再把该估算当成功判据。
    est_s = round(len(sids) * 1.3 + 20, 1)
    mode  = "REAL" if RECLAIM_AUTO_EVACUATE else "DRY-RUN"
    plan  = {
        "session_id": session_id,
        "node": NODE_ID,
        "instance_id": _instance_id(),
        "availability_zone": _availability_zone(),
        "state_volume_id": _state_volume_id(),
        "signal": signal,
        "count": len(sids),
        "sandboxes": sids,
        "est_evac_s": est_s,
        "mode": mode,
        "phase": "detected",
        "termination_deadline": termination_deadline.isoformat(),
        "checkpoint_deadline": checkpoint_deadline.isoformat(),
        "snapshot_concurrency": min(
            RECLAIM_SNAPSHOT_CONCURRENCY, max(1, len(sids))
        ),
    }
    _RECLAIM_STATE.update({"detected": True, "signal": signal,
                           "at": _now_iso(), "plan": plan})
    print(f"[reclaim] SIGNAL={signal.get('type')} → evacuate {len(sids)} sandboxes "
          f"on {NODE_ID} (mode={mode}, est~{est_s}s): {sids}",
          file=sys.stderr, flush=True)
    if not RECLAIM_AUTO_EVACUATE:
        print("[reclaim] DRY-RUN: 不实际疏散。设 RECLAIM_AUTO_EVACUATE=1 开启真疏散。",
              file=sys.stderr, flush=True)
        return plan

    checkpoint_only = bool(signal.get("checkpoint_only", False))
    if not sids and not checkpoint_only and not _has_local_sandbox_state():
        # An empty replacement/test node has no state worth retaining. Keep
        # DeleteOnTermination=true so a no-work interruption cannot leak EBS.
        plan.update({
            "phase": "checkpointed",
            "volume_preservation": {
                "preserved": False,
                "skipped": True,
                "reason": "no local sandbox state",
                "elapsed_s": 0.0,
            },
            "completed": 0,
            "evacuated_ok": 0,
            "failed": 0,
            "wall_clock_s": 0.0,
            "total_actual_bytes": 0,
            "effective_write_mib_s": 0,
            "results": [],
        })
        _RECLAIM_STATE["evacuated"] = True
        return plan

    preserve_started = time.monotonic()
    if checkpoint_only:
        plan["volume_preservation"] = {
            "preserved": False,
            "skipped": True,
            "reason": "checkpoint-only benchmark",
            "elapsed_s": 0.0,
        }
    else:
        try:
            plan["volume_preservation"] = {
                **_preserve_state_volume(),
                "elapsed_s": round(
                    time.monotonic() - preserve_started,
                    3,
                ),
            }
        except Exception as exc:
            message = str(exc)[:1024]
            plan["volume_preservation"] = {
                "preserved": False,
                "error": message,
                "elapsed_s": round(
                    time.monotonic() - preserve_started,
                    3,
                ),
            }
            print(
                "[reclaim] failed to preserve state EBS before termination: "
                f"{exc}",
                file=sys.stderr,
                flush=True,
            )
            results = []
            for sid in sids:
                journal_errors: list[str] = []
                ok, error = _update_sandbox_recovery(
                    sid,
                    {
                        "recovery_session_id": session_id,
                        "recovery_source_node": NODE_ID,
                        "recovery_source_instance_id": _instance_id(),
                        "recovery_source_volume_id": _state_volume_id(),
                        "recovery_az": _availability_zone(),
                        "recovery_expected_count": len(sids),
                        "interruption_type": signal.get(
                            "type", "spot-termination"
                        ),
                        "interruption_detected_at": _now_iso(),
                        "recovery_deadline_at": (
                            termination_deadline.isoformat()
                        ),
                        "recovery_checkpoint_only": False,
                        "state": "recovery_failed",
                        "recovery_phase": "volume_preservation_failed",
                        "recovery_error": message,
                        "updated_at": _now_iso(),
                    },
                )
                if not ok:
                    journal_errors.append(error)
                results.append({
                    "id": sid,
                    "ok": False,
                    "error": message,
                    "journal_errors": journal_errors,
                })
            plan.update({
                "phase": "volume_preservation_failed",
                "completed": len(results),
                "evacuated_ok": 0,
                "failed": len(results),
                "wall_clock_s": 0.0,
                "total_actual_bytes": 0,
                "effective_write_mib_s": 0,
                "results": results,
            })
            _RECLAIM_STATE["evacuated"] = False
            return plan

    # REAL 疏散:并发打 Diff 快照以吃满单卷/实例 EBS 带宽。并发度只改变
    # 饱和速度，不能突破实例 EBS 上限；因此必须同时记录真实写入量和墙钟。
    wall_started = time.monotonic()
    plan["phase"] = "checkpointing"
    results: list[dict] = []
    workers = min(RECLAIM_SNAPSHOT_CONCURRENCY, max(1, len(sids)))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="reclaim-snapshot"
    ) as executor:
        futures = [
            executor.submit(
                _checkpoint_one,
                sid,
                session_id=session_id,
                signal=signal,
                termination_deadline=termination_deadline,
                checkpoint_deadline=checkpoint_deadline,
                expected_count=len(sids),
                monotonic_deadline=monotonic_deadline,
            )
            for sid in sids
        ]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            plan["completed"] = len(results)
            plan["evacuated_ok"] = sum(1 for item in results if item["ok"])
            plan["failed"] = len(results) - plan["evacuated_ok"]

    wall_s = time.monotonic() - wall_started
    ok = sum(1 for item in results if item["ok"])
    total_actual = sum(int(item.get("actual_bytes", 0)) for item in results)
    plan.update({
        "phase": "checkpointed" if ok == len(sids) else "partial",
        "evacuated_ok": ok,
        "failed": len(sids) - ok,
        "wall_clock_s": round(wall_s, 3),
        "total_actual_bytes": total_actual,
        "effective_write_mib_s": round(
            total_actual / 1024 / 1024 / wall_s, 3
        ) if wall_s > 0 else 0,
        "results": sorted(results, key=lambda item: item["id"]),
    })
    _RECLAIM_STATE["evacuated"] = ok == len(sids)
    print(
        f"[reclaim] REAL evacuation done: {ok}/{len(sids)} "
        f"wall={wall_s:.3f}s actual={total_actual}B",
        file=sys.stderr,
        flush=True,
    )
    return plan


def start_reclaim_watch_loop() -> None:
    """后台轮询 IMDS 回收信号;检出一次即疏散(去重:同一检测只触发一次)。"""
    if not RECLAIM_WATCH:
        return
    import sys

    def _loop():
        while True:
            try:
                sig = _check_reclaim_signal()
                if sig and not _RECLAIM_STATE.get("detected"):
                    _evacuate_local(sig)
            except Exception as e:
                print(f"[reclaim] watch error: {e}", file=sys.stderr, flush=True)
            time.sleep(RECLAIM_POLL_S)  # nosemgrep: arbitrary-sleep -- 回收信号轮询周期

    threading.Thread(target=_loop, daemon=True).start()


# ---------- 入站反代(端口暴露)----------
# guest IP(172.18.{tap_idx}.2)只在本 metal 节点本地可达(tap /30 子网),集群其它 pod
# 路由不到。故由控制面 sandbox-proxy 把请求先转到本节点 node-agent(hostNetwork,能访问
# 本机 tap 网段),再由这里应用层反代进 guest 的目标端口。
# 用应用层反代而非 iptables DNAT:无需管理宿主端口分配 → 天然支持"多沙盒同一内部端口"
# (两个 guest 各自 172.18.A.2:80 / 172.18.B.2:80,靠 sid 区分,不抢宿主端口)。

def _guest_ip_for(sid: str) -> str | None:
    """查本节点 running 沙盒的 guest IP;不存在/非 running 返回 None。"""
    with _LOCK:
        vm = _VMS.get(sid)
        if vm and vm.get("state") == "running":
            return vm.get("ip")
    return None


def _raw_tunnel(a: socket.socket, b: socket.socket) -> None:
    """在两个已连接 socket 间双向透传字节,任一方关闭即结束。
    用于 WebSocket 等 Upgrade 连接:101 切换后是二进制帧流,代理无需理解帧,只转发字节。"""
    import select
    socks = [a, b]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, 300)
            if x or not r:
                break
            for s in r:
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                (b if s is a else a).sendall(data)
    finally:
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


# ---------- HTTP handler ----------

# 无需鉴权的只读路由(kubelet 探针 / Prometheus 抓取 / 控制面存活探测),不含租户数据。
# 仍对 guest tap 网段拒绝。其余所有路由都要过:tap 拒绝 → CIDR 白名单 → HMAC 签名。
_PUBLIC_GET_ROUTES = frozenset({"/health", "/livez", "/readyz", "/metrics"})


def _parse_cidrs(value: str) -> list:
    """解析逗号分隔 CIDR;任一非法抛 ValueError(启动时校验,请求时视为拒绝)。"""
    import ipaddress
    return [ipaddress.ip_network(c.strip(), strict=False)
            for c in (value or "").split(",") if c.strip()]


def _ip_in(client_ip: str, cidrs: str) -> bool:
    import ipaddress
    try:
        addr = ipaddress.ip_address(client_ip)
        if getattr(addr, "ipv4_mapped", None):
            addr = addr.ipv4_mapped
        return any(addr in net for net in _parse_cidrs(cidrs))
    except ValueError:
        return False


def _check_caller_allowed(client_ip: str) -> bool:
    """来源 IP 是否在 ALLOWED_CALLER_CIDR 白名单内。白名单为空 → 拒绝(fail closed)。"""
    if not ALLOWED_CALLER_CIDR.strip():
        return False
    return _ip_in(client_ip, ALLOWED_CALLER_CIDR)


def _caller_denied(client_ip: str) -> bool:
    """来源是否属于 guest tap 网段(DENIED_CALLER_CIDR)。配置非法时按"拒绝"处理。"""
    try:
        _parse_cidrs(DENIED_CALLER_CIDR)
    except ValueError:
        return True
    return _ip_in(client_ip, DENIED_CALLER_CIDR)


class Handler(BaseHTTPRequestHandler):
    # 单个 socket 读写超时:防止慢速连接(slowloris)占满线程。长耗时操作(suspend 等)
    # 是服务端计算,不受影响;WebSocket 隧道走 select,空闲 300s 自行结束。
    timeout = int(os.environ.get("NODE_AGENT_SOCKET_TIMEOUT_S", "120"))

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
        self._cached_request_body = None
        try:
            super().handle_one_request()
        finally:
            if getattr(self, "command", None) and getattr(self, "path", None):
                duration = time.monotonic() - started
                route = record_http(
                    self.command, self.path, self._response_status, duration
                )
                if route not in {"/livez", "/readyz", "/health", "/metrics"}:
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
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_): pass

    def _run_vm_operation(self, operation: str, fn):
        started = time.monotonic()
        result = "error"
        snapshot_type = ""
        if operation == "resume":
            FC_RESUME_INFLIGHT.inc()
        try:
            response = fn()
            result = "success"
            snapshot_type = response.get("snapshot_type", "")
            if operation == "resume":
                record_restore_mode(response.get("restore_mode", "unknown"))
                for field, stage in (
                    ("merge_time_s", "memory_merge"),
                    ("restore_time_s", "snapshot_load"),
                ):
                    if response.get(field) is not None:
                        record_resume_stage(
                            stage, result, float(response[field])
                        )
            return response
        finally:
            if operation == "resume":
                FC_RESUME_INFLIGHT.dec()
            record_fc_operation(
                operation,
                result,
                time.monotonic() - started,
                snapshot_type,
            )

    def _deny(self, code: int, reason: str, kind: str = "auth_denied",
              **extra) -> None:
        """拒绝请求并留痕(结构化日志 + node_agent_security_events_total 指标,供告警)。"""
        record_security_event(kind, reason)
        log_event(
            "warning", "node_agent_request_rejected",
            kind=kind, reason=reason,
            client_ip=self.client_address[0],
            method=self.command,
            route=normalize_route(urlparse(self.path).path),
        )
        self.close_connection = True
        error = {401: "unauthorized", 403: "forbidden"}.get(code, reason)
        self._send(code, {"error": error, **extra})

    def _authorize(self) -> bool:
        """受保护路由的准入:tap 网段 → CIDR 白名单(空=拒绝)→ HMAC 签名(读 body 之前)。"""
        client_ip = self.client_address[0]
        if _caller_denied(client_ip):
            self._deny(403, "guest_source")
            return False
        if not _check_caller_allowed(client_ip):
            self._deny(403, "caller_not_allowed")
            return False
        try:
            self._content_sha = _AUTH.verify_headers(self.headers, self.command, self.path)
        except agent_auth.AuthError as e:
            self._deny(401, e.reason)
            return False
        return True

    def _read_body(self, limit: int) -> bytes | None:
        """读请求体并比对签名里声明的 sha256。失败时已回包,返回 None。"""
        if self.headers.get("Transfer-Encoding"):
            self._deny(411, "length_required")
            return None
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = -1
        if n < 0:
            self._deny(400, "bad_content_length")
            return None
        if n > limit:
            self._deny(413, "body_too_large")
            return None
        raw = self.rfile.read(n) if n else b""
        if not agent_auth.body_matches(raw, getattr(self, "_content_sha", "")):
            self._deny(401, "body_mismatch")
            return None
        return raw

    def _protected_body(self) -> bytes | None:
        """鉴权 + 读 body(/proxy/ 与控制类路由上限不同)。失败返回 None。"""
        if not self._authorize():
            return None
        is_proxy = urlparse(self.path).path.startswith("/proxy/")
        return self._read_body(MAX_PROXY_BODY_BYTES if is_proxy else MAX_CONTROL_BODY_BYTES)

    # ---------- 入站反代:/proxy/{sid}/{port}/{rest...} → guest {ip}:{port}/{rest} ----------
    def _maybe_proxy(self, req_body: bytes) -> bool:
        """若 path 命中 /proxy/{sid}/{port}/...,反代到 guest 并返回 True;否则返回 False。"""
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "proxy":
            return False
        sid, port_s = parts[1], parts[2]
        rest = "/".join(parts[3:])
        qs = f"?{parsed.query}" if parsed.query else ""
        upstream_path = f"/{rest}{qs}"

        try:
            port = int(port_s)
        except ValueError:
            self._send(400, {"error": "bad port"}); return True

        guest_ip = _guest_ip_for(sid)
        if not guest_ip:
            self._send(404, {"error": "sandbox not running on this node", "id": sid}); return True

        # WebSocket / Upgrade:开原始 socket 到 guest,原样重放请求行+头,之后双向透传字节。
        if "upgrade" in self.headers.get("Connection", "").lower() and \
           self.headers.get("Upgrade", "").lower() == "websocket":
            return self._tunnel_ws(guest_ip, port, upstream_path)

        req_body = req_body or None

        # 透传除 hop-by-hop 与 node-agent 鉴权头外的请求头(签名不泄露给 guest)
        hop = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade", "host"}
        fwd_headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in hop and k.lower() not in agent_auth.AUTH_HEADERS}
        fwd_headers["Host"] = f"{guest_ip}:{port}"

        try:
            conn = http.client.HTTPConnection(guest_ip, port, timeout=30)
            conn.request(self.command, upstream_path, body=req_body, headers=fwd_headers)
            resp = conn.getresponse()
            data = resp.read()
        except Exception as e:
            self._send(502, {"error": "upstream unreachable",
                             "hint": f"guest {guest_ip}:{port} — {e}"})
            return True

        # 回写上游响应(状态码 + 头 + body),跳过会导致长度冲突的 hop-by-hop 头
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

    def _tunnel_ws(self, guest_ip: str, port: int, upstream_path: str) -> bool:
        """WebSocket 反代:向 guest 建原始 TCP,重放本请求(含 Upgrade 头),
        然后在 client<->guest 间双向透传字节。"""
        try:
            up = socket.create_connection((guest_ip, port), timeout=10)
        except OSError as e:
            self._send(502, {"error": "ws upstream unreachable", "hint": f"{guest_ip}:{port} — {e}"})
            return True
        # 重放请求行 + 原始头(WS 握手头如 Sec-WebSocket-Key 必须原样带上;Host 改成 guest)
        lines = [f"{self.command} {upstream_path} HTTP/1.1"]
        for k, v in self.headers.items():
            if k.lower() == "host" or k.lower() in agent_auth.AUTH_HEADERS:
                continue
            lines.append(f"{k}: {v}")
        lines.append(f"Host: {guest_ip}:{port}")
        up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        # 交出底层 socket,双向透传(guest 的 101 响应也会原样回给 client)
        _raw_tunnel(self.connection, up)
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        # 健康检查和指标:不需签名(kubelet / Prometheus 调用),不含租户数据;guest 网段仍拒绝。
        if path in _PUBLIC_GET_ROUTES:
            if _caller_denied(self.client_address[0]):
                return self._deny(403, "guest_source")
            if path == "/health":
                return self._send(200, op_health())
            if path == "/livez":
                code, result = health_report(require_dependencies=False)
                return self._send(code, result)
            if path == "/readyz":
                code, result = health_report(require_dependencies=True)
                return self._send(code, result)
            _refresh_metrics()
            body, content_type = metrics_payload()
            return self._send_bytes(200, body, content_type)
        raw = self._protected_body()
        if raw is None:
            return
        if path.startswith("/proxy/") and self._maybe_proxy(raw):
            return
        try:
            if path == "/reclaim/status":
                return self._send(200, _RECLAIM_STATE)
            if path == "/recovery/status":
                configured_role, recovery_group, _ = (
                    _node_recovery_identity()
                )
                state_volume_id = _state_volume_id()
                return self._send(200, {
                    "node": NODE_ID,
                    "instance_id": _instance_id(),
                    "availability_zone": _availability_zone(),
                    "recovery_role": _effective_recovery_role(
                        configured_role
                    ),
                    "recovery_group": recovery_group,
                    "state_volume_id": state_volume_id,
                    "draining": bool(_RECLAIM_STATE.get("detected")),
                })
            parts = path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "vm":
                return self._send(200, op_get(parts[1]))
            self._send(404, {"error": "not found"})
        except RequestValidationError as e:
            self._deny(400, e.reason, kind="invalid_request", field=e.field)
        except KeyError:
            self._send(404, {"error": "not found"})
        except Exception as e:
            log_event(
                "error", "request_failed",
                method="GET", route=normalize_route(path),
                error_type=type(e).__name__,
            )
            self._send(500, {"error": str(e)})

    def do_POST(self):
        raw = self._protected_body()
        if raw is None:
            return
        path = urlparse(self.path).path
        if path.startswith("/proxy/") and self._maybe_proxy(raw):
            return
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._deny(400, "bad_json", kind="invalid_request")
        if not isinstance(body, dict):
            return self._deny(400, "bad_json", kind="invalid_request")
        try:
            if path.startswith("/vm/") and body.get("id"):
                sid = _sandbox_id(body.get("id"))
                with _vm_operation_lock(sid):
                        if path == "/vm/create":
                            return self._send(200, self._run_vm_operation(
                                "create", lambda: op_create(body)))
                        if path == "/vm/destroy":
                            return self._send(200, self._run_vm_operation(
                                "destroy", lambda: op_destroy(body)))
                        if path == "/vm/snapshot_base":
                            return self._send(200, self._run_vm_operation(
                                "snapshot_base", lambda: op_snapshot_base(body)))
                        if path == "/vm/suspend":
                            return self._send(200, self._run_vm_operation(
                                "suspend", lambda: op_suspend(body)))
                        if path == "/vm/resume":
                            return self._send(200, self._run_vm_operation(
                                "resume", lambda: op_resume(body)))
                        if path == "/vm/exec":
                            return self._send(200, self._run_vm_operation(
                                "exec", lambda: op_exec(body)))
            # Spot 恢复:standby 节点接管/交还状态 EBS(控制面签名调用)。
            if path == "/recovery/mount":
                return self._send(200, op_recovery_mount(body))
            if path == "/recovery/unmount":
                return self._send(200, op_recovery_unmount(body))
            # Block 1 测试钩子:仅 NODE_AGENT_ENABLE_TEST_HOOKS=1 时启用,生产默认 404。
            if ENABLE_TEST_HOOKS and path == "/reclaim/simulate":
                # 注入一个回收信号,立即算疏散计划(EKS 节点非 spot,用它验证链路)。
                sig = {"type": str(body.get("type", "spot-termination")),
                       "action": str(body.get("action", "terminate")),
                       "time": str(body.get("time", _now_iso())),
                       "injected": True}
                _RECLAIM_STATE["detected"] = False  # 允许重复测试
                return self._send(200, _evacuate_local(sig))
            # 清除检测态(测试用:恢复后重置,让 watch loop 可再次触发)
            if ENABLE_TEST_HOOKS and path == "/reclaim/reset":
                _RECLAIM_STATE.update({"detected": False, "signal": None, "at": None,
                                       "plan": None, "evacuated": False, "injected": None})
                return self._send(200, {"reset": True})
            self._send(404, {"error": "not found"})
        except RequestValidationError as e:
            self._deny(400, e.reason, kind="invalid_request", field=e.field)
        except KeyError:
            self._send(404, {"error": "not found"})
        except Exception as e:
            log_event(
                "error", "request_failed",
                method="POST", route=normalize_route(path),
                error_type=type(e).__name__,
            )
            self._send(500, {"error": str(e)})

    # 反代需要覆盖 web 常用的其余 method(PUT/DELETE/PATCH/HEAD/OPTIONS)。
    # 这些仅用于 /proxy/,非 proxy 路径返回 404。
    def _proxy_only(self):
        raw = self._protected_body()
        if raw is None:
            return
        if urlparse(self.path).path.startswith("/proxy/") and self._maybe_proxy(raw):
            return
        self._send(404, {"error": "not found"})

    do_PUT     = _proxy_only
    do_DELETE  = _proxy_only
    do_PATCH   = _proxy_only
    do_OPTIONS = _proxy_only
    do_HEAD    = _proxy_only


def _listen_host() -> str:
    """监听地址:显式 NODE_AGENT_LISTEN_HOST 优先,否则本节点主网卡内网 IP(管理面)。"""
    return LISTEN_HOST or _advertise_ip()


def _startup_security_checks() -> None:
    """启动前安全自检,任一不满足即拒绝启动(fail closed)。"""
    import sys
    problems = []
    if not _AUTH.configured:
        problems.append(
            "NODE_AGENT_AUTH_KEY / NODE_AGENT_AUTH_KEY_FILE 未配置或短于 "
            f"{agent_auth.MIN_KEY_BYTES} 字节(控制面与 node-agent 共享的 HMAC 密钥)")
    for name, value in (("ALLOWED_CALLER_CIDR", ALLOWED_CALLER_CIDR),
                        ("DENIED_CALLER_CIDR", DENIED_CALLER_CIDR)):
        try:
            _parse_cidrs(value)
        except ValueError:
            problems.append(f"{name} 不是合法的 CIDR 列表: {value!r}")
    if not ALLOWED_CALLER_CIDR.strip():
        problems.append("ALLOWED_CALLER_CIDR 为空 —— 设为控制面 Pod 所在网段(空值将拒绝全部调用)")
    if _listen_host() in ("0.0.0.0", "::", ""):
        problems.append("NODE_AGENT_LISTEN_HOST 不能是通配地址,设为节点管理网卡 IP")
    if problems:
        for p in problems:
            print(f"[node-agent] FATAL: {p}", file=sys.stderr, flush=True)
        sys.exit(2)
    if GUEST_FIREWALL:
        try:
            _install_guest_firewall()
        except Exception as e:
            print(f"[node-agent] FATAL: 安装 guest 隔离 iptables 规则失败: {e}",
                  file=sys.stderr, flush=True)
            sys.exit(2)
    else:
        print("[node-agent] WARNING: NODE_AGENT_GUEST_FIREWALL=0,guest→宿主隔离规则需由外部保证",
              file=sys.stderr, flush=True)


if __name__ == "__main__":
    _startup_security_checks()
    # EKS AL2023 can schedule DaemonSet pods while cloud-init is still
    # downloading rootfs assets. When cloud-init finishes, kubelet/containerd
    # may recreate every pod sandbox. Do not register this node or accept
    # Firecracker workloads until the host bootstrap is fully complete.
    _wait_for_node_bootstrap()

    # 启动自恢复:重建残留 VM 的操作句柄,避免重启后状态漂移(P0-1)
    try:
        n = _recover_vms()
        if n:
            print(f"node-agent 自恢复 {n} 个残留 VM")
    except Exception as e:
        print(f"node-agent 自恢复失败(忽略): {e}")

    # 心跳注册:控制面据此发现活节点(P0-3)
    start_heartbeat_loop()

    # Block 1:spot 回收信号监听 → 自动疏散(默认 DRY-RUN)
    start_reclaim_watch_loop()
    print(f"[reclaim] watch={'on' if RECLAIM_WATCH else 'off'} "
          f"poll={RECLAIM_POLL_S}s mode={'REAL' if RECLAIM_AUTO_EVACUATE else 'DRY-RUN'}")

    listen_host = _listen_host()
    print(f"node-agent [{NODE_ID}] 在 {listen_host}:{LISTEN_PORT} "
          f"(advertise: {_advertise_ip()}, allowed callers: {ALLOWED_CALLER_CIDR}, "
          f"denied: {DENIED_CALLER_CIDR}, auth: hmac-v1, "
          f"test hooks: {'on' if ENABLE_TEST_HOOKS else 'off'}, "
          f"guest firewall: {'on' if GUEST_FIREWALL else 'off'})")
    ThreadingHTTPServer((listen_host, LISTEN_PORT), Handler).serve_forever()
