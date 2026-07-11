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

运行(需 root,在 .metal 宿主):
  sudo python3 main.py   # 默认 :8002
"""
from __future__ import annotations

import http.client
import json
import os
import re
import signal
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ---------- 配置 ----------
LISTEN_PORT  = int(os.environ.get("NODE_AGENT_PORT", "8002"))
# 监听地址：默认只绑 127.0.0.1（本机回环），防止集群内其他 Pod 直接访问宿主级执行面
# hostNetwork=true 模式下 127.0.0.1 对控制面 Pod 不可达；
# 生产：通过 ALLOWED_CALLER_CIDR 限制可访问 IP，或走 NetworkPolicy 白名单控制面 Pod CIDR
LISTEN_HOST  = os.environ.get("NODE_AGENT_LISTEN_HOST", "0.0.0.0")  # 生产改为节点内网 IP
# 允许调用的来源 CIDR（逗号分隔，空=不限制）——生产应设为控制面 Pod CIDR
ALLOWED_CALLER_CIDR = os.environ.get("ALLOWED_CALLER_CIDR", "")
SBX_BASE     = os.environ.get("SBX_BASE", "/var/lib/sbx")       # 统一路径约定
ROOTFS       = os.environ.get("FC_ROOTFS",  "/opt/sbx/rootfs.ext4")  # 基础 rootfs 模板
JAILER_BIN   = os.environ.get("JAILER_BIN", "/usr/local/bin/firecracker-jailer")
FC_BIN       = os.environ.get("FC_BIN",     "/usr/local/bin/firecracker")
HOST_IFACE   = os.environ.get("HOST_IFACE", "")                 # 空则自动探测
AWS_REGION   = os.environ.get("AWS_REGION", "us-east-1")
NODE_ID      = os.environ.get("NODE_ID", socket.gethostname())

# ---------- 心跳注册(P0-3:控制面按 last_seen 判活,替换 FC_NODES 硬编码)----------
NODES_TABLE       = os.environ.get("DYNAMODB_NODES_TABLE", "sandbox_nodes")
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
# 最近一次回收检测/疏散计划(供 GET /reclaim/status 观测;injected 供测试注入)
_RECLAIM_STATE: dict = {"detected": False, "signal": None, "at": None,
                        "plan": None, "evacuated": False, "injected": None}

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

os.makedirs(SBX_BASE, exist_ok=True)


# ---------- tap 网络 ----------

def _setup_tap(tap_idx: int) -> tuple[str, str, str]:
    """建 tap + /30 子网。返回 (tap_name, host_ip, guest_ip)。"""
    tap      = f"fctap{tap_idx}"
    host_ip  = f"172.18.{tap_idx}.1"
    guest_ip = f"172.18.{tap_idx}.2"
    host_if  = _host_iface()
    # 幂等清理同名残留 tap:suspend/destroy 后 tap 设备不会自动消失,
    # 若 tap_idx 被复用(暖池 resume、tap_idx 回收重分配),残留的旧 tap 会让
    # FC snapshot/load 打开设备时报 "Resource busy (os error 16)" → resume 失败。
    # 先删再建,保证设备干净。
    subprocess.run(["ip", "link", "del", tap], stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "tuntap", "add", tap, "mode", "tap"],
                   stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "addr", "add", f"{host_ip}/30", "dev", tap],
                   stderr=subprocess.DEVNULL)
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


def _teardown_tap(tap: str) -> None:
    subprocess.run(["ip", "link", "del", tap], stderr=subprocess.DEVNULL)


def _host_iface() -> str:
    if HOST_IFACE:
        return HOST_IFACE
    r = subprocess.run("ip route | awk '/default/{print $5;exit}'",
                       shell=True, capture_output=True, text=True)
    return r.stdout.strip() or "eth0"


# ---------- Firecracker 启动(jailer 包裹) ----------

def _start_fc(sandbox_id: str, rootfs: str, tap: str, cpu: int,
               mem_mib: int, kernel: str, env: dict,
               guest_ip: str = "", host_ip: str = "") -> tuple[int, str]:
    """
    用 jailer 启动 Firecracker,返回 (pid, api_sock)。
    jailer 把 FC 进程放进独立 cgroup + chroot + seccomp,防止逃逸。
    """
    d    = f"{SBX_BASE}/{sandbox_id}"
    sock = f"{d}/api.sock"
    log  = f"{d}/vm.log"
    os.makedirs(d, exist_ok=True)

    try:
        os.remove(sock)
    except FileNotFoundError:
        pass

    # jailer 参数:每个沙盒独立 uid(从 tap_idx 派生,避免 uid 碰撞)
    # jailer 会把 FC 进程 chroot 到 /srv/jailer/firecracker/<id>/root/
    # 注意:rootfs 和 kernel 需在 jailer chroot 内可见 → 用 --bind-path 或预先 cp
    # POC 阶段用裸 FC(无 jailer chroot 复杂度);生产切换时去掉 USE_BARE_FC
    USE_BARE_FC = os.environ.get("USE_BARE_FC", "1") == "1"
    if not USE_BARE_FC and os.path.exists(JAILER_BIN):
        cmd = [
            JAILER_BIN,
            "--id",          sandbox_id,
            "--exec-file",   FC_BIN,
            "--uid",         str(3000 + _tap_idx_from_d(d)),
            "--gid",         str(3000 + _tap_idx_from_d(d)),
            "--chroot-base-dir", SBX_BASE,
            "--",
            "--api-sock",    sock,
        ]
    else:
        cmd = [FC_BIN, "--api-sock", sock]

    with open(log, "w") as lf:
        # nosemgrep: dangerous-subprocess-use-tainted-env-args -- cmd 为固定 list(jailer/FC 二进制路径来自环境配置,非请求体);env 仅作为 boot_args 注入 guest,不参与 host 命令拼接
        proc = subprocess.Popen(cmd, stdout=lf, stderr=lf)

    if not _wait_sock(sock, timeout=30.0):
        proc.kill()
        raise RuntimeError("firecracker API socket 未就绪")

    # 配置 VM（JuiceFS 模式：通过 boot_args 把 Redis/S3 地址注入 guest init）
    # 里程碑 B: 注入 guest 网络(SBX_IP/SBX_GW),让 init 配成 node-agent 期望的
    # 172.18.{tap_idx}.2,从而宿主能 SSH 到 guest 做 exec。
    net_args = f"SBX_IP={guest_ip} SBX_GW={host_ip} " if guest_ip and host_ip else ""
    if JUICEFS_ENABLED and JUICEFS_REDIS_ADDR and JUICEFS_BUCKET:
        jfs_env = (
            f"JFS_REDIS={JUICEFS_REDIS_ADDR} "
            f"JFS_BUCKET={JUICEFS_BUCKET} "
            f"JFS_NAME={JUICEFS_FS_NAME} "
            f"AWS_REGION={AWS_REGION} "
        )
        boot_args = f"console=ttyS0 reboot=k panic=1 pci=off init=/sbin/sbxinit {net_args}{jfs_env}"
    else:
        boot_args = f"console=ttyS0 reboot=k panic=1 pci=off init=/sbin/sbxinit {net_args}"

    _fc(sock, "PUT", "/boot-source", {
        "kernel_image_path": kernel,
        "boot_args": boot_args,
    })
    _fc(sock, "PUT", "/drives/rootfs", {
        "drive_id": "rootfs", "path_on_host": rootfs,
        "is_root_device": True, "is_read_only": False,
    })
    # track_dirty_pages=True: 开启脏页跟踪,是 Diff 增量快照的前提。
    # 不开则 PUT /snapshot/create {Diff} 会失败 → 方案C 疏散退化成全量 Full(慢一个量级)。
    _fc(sock, "PUT", "/machine-config",
        {"vcpu_count": cpu, "mem_size_mib": mem_mib, "track_dirty_pages": True})
    _fc(sock, "PUT", "/network-interfaces/eth0", {"iface_id": "eth0", "host_dev_name": tap})
    # vsock: host UDS = {d}/v.sock, guest CID=3, port=2222 供 exec 使用。
    # exec 主通道走 vsock(不依赖 guest 网络/sshd)，SSH 仅兜底。
    # 快照含 vsock 设备配置,resume 时由 op_resume 先 os.remove(v.sock) 避免 "Address in use"。
    vsock_path = f"{d}/v.sock"
    try:
        _fc(sock, "PUT", "/vsock", {"vsock_id": "vsock0", "guest_cid": 3, "uds_path": vsock_path})
    except Exception:
        pass  # vsock 配置失败不阻断 VM 启动(exec 回退 SSH)
    _fc(sock, "PUT", "/actions", {"action_type": "InstanceStart"})

    return proc.pid, sock


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

def _fc(sock: str, method: str, path: str, body=None, timeout: int = 15) -> dict:
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
    last_err: Exception | None = None
    for attempt in range(retries):
        r = subprocess.run(
            ["aws", "s3", "sync", local_dir, s3_prefix,
             "--region", AWS_REGION, "--only-show-errors"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return
        last_err = RuntimeError(
            f"aws s3 sync rc={r.returncode}: {r.stderr.strip()[:500]}")
        if attempt < retries - 1:
            time.sleep(2 ** attempt)  # nosemgrep: arbitrary-sleep -- 上传重试退避
    raise last_err or RuntimeError("s3 upload failed")


def _s3_download(s3_prefix: str, local_dir: str) -> None:
    """同步从 S3 拉三件套到 local_dir(resume 前调用)。"""
    if not s3_prefix:
        return
    os.makedirs(local_dir, exist_ok=True)
    subprocess.run(
        ["aws", "s3", "sync", s3_prefix, local_dir,
         "--region", AWS_REGION, "--quiet"],
        check=True,
    )


# ---------- 操作实现 ----------

def op_create(body: dict) -> dict:
    sid      = body["id"]
    tap_idx  = int(body["tap_idx"])
    cpu      = int(body.get("cpu", 2))
    mem_mib  = int(body.get("mem_mib", 4096))
    kernel   = body.get("kernel", "/opt/sbx/vmlinux")
    env      = body.get("env", {})

    d = f"{SBX_BASE}/{sid}"
    os.makedirs(d, exist_ok=True)

    # CoW 复制基础 rootfs 到沙盒目录(src 是全局基础镜像,dst 是沙盒私有副本)
    dest_rootfs = f"{d}/rootfs.ext4"
    subprocess.run(["cp", "--reflink=auto", ROOTFS, dest_rootfs], check=True)

    tap, host_ip, guest_ip = _setup_tap(tap_idx)

    pid, sock = _start_fc(sid, dest_rootfs, tap, cpu, mem_mib, kernel, env,
                          guest_ip=guest_ip, host_ip=host_ip)

    with _LOCK:
        _VMS[sid] = {
            "state":   "running",
            "pid":     pid,
            "sock":    sock,
            "tap":     tap,
            "tap_idx": tap_idx,
            "ip":      guest_ip,
            "dir":     d,
        }
    return {"state": "running", "ip": guest_ip}


def op_destroy(body: dict) -> dict:
    sid = body["id"]
    with _LOCK:
        vm = _VMS.pop(sid, None)
    if vm:
        if vm.get("pid"):
            # os.kill 而非 subprocess(slim 镜像无 /bin/kill)
            try:
                os.kill(int(vm["pid"]), signal.SIGTERM)
            except (ProcessLookupError, ValueError, TypeError):
                pass
        _teardown_tap(vm.get("tap", ""))
        shutil.rmtree(f"{SBX_BASE}/{sid}", ignore_errors=True)
    return {"deleted": True}


def op_snapshot_base(body: dict) -> dict:
    """
    方案C 预热:在 sandbox 运行期打一次 Full base 快照(不 kill VMM,打完继续跑)。
    目的:spot 疏散时才能走 Diff(只写脏页),而 Diff 的前提是已有 base。
    创建后由控制面异步调用一次;off 关键路径(~16s 无所谓)。
    """
    sid      = body["id"]
    snap_dir = body["snapshot_local_path"]
    with _LOCK:
        vm = _VMS.get(sid)
    if not vm:
        raise KeyError(sid)
    os.makedirs(snap_dir, exist_ok=True)
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
    return {"base_created": True, "base_snapshot_time_s": round(dt, 3),
            "base_mem_bytes": os.path.getsize(base_mem)}


def op_suspend(body: dict) -> dict:
    sid       = body["id"]
    snap_dir  = body["snapshot_local_path"]
    s3_prefix = body.get("s3_prefix", "")

    with _LOCK:
        vm = _VMS.get(sid)
    if not vm:
        raise KeyError(sid)

    os.makedirs(snap_dir, exist_ok=True)
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

    # snapshot/create 超时:内存越大越久;多个并发共享 EBS 带宽会显著拉长。留足 600s。
    SNAP_TIMEOUT = 600

    t0 = time.monotonic()
    snap_type = "diff"
    if not has_base:
        # 无 base:Full 快照,同时保留一份作 base(供本 sandbox 后续 Diff)
        snap_type = "full"
        _fc(sock, "PUT", "/snapshot/create", {
            "snapshot_type": "Full",
            "snapshot_path": diff_snap,
            "mem_file_path": diff_mem,
        }, timeout=SNAP_TIMEOUT)
        import shutil as _sh
        _sh.copy2(diff_snap, base_snap)
        _sh.copy2(diff_mem,  base_mem)
    else:
        # 有 base:Diff 快照(只写自 base 以来的脏页)
        try:
            _fc(sock, "PUT", "/snapshot/create", {
                "snapshot_type": "Diff",
                "snapshot_path": diff_snap,
                "mem_file_path": diff_mem,
            }, timeout=SNAP_TIMEOUT)
        except Exception:
            # Diff 失败(如未开 track_dirty_pages)→ 降级 Full
            snap_type = "full-fallback"
            _fc(sock, "PUT", "/snapshot/create", {
                "snapshot_type": "Full",
                "snapshot_path": diff_snap,
                "mem_file_path": diff_mem,
            }, timeout=SNAP_TIMEOUT)
    dt = time.monotonic() - t0

    # 方案C:快照写在持久状态 EBS 上(snap_dir),spot 终止后卷幸存,
    # 故【不传 S3】——删掉最慢的 S3 传输,是 120s 窗口内跑满 50 个的关键。
    # snapshot/create 同步完成即已落 EBS,数据已持久 → 可安全 kill VMM。
    # kill VMM,释放 RAM。用 os.kill 而非 subprocess(slim 镜像无 /bin/kill)
    try:
        os.kill(int(vm["pid"]), signal.SIGTERM)
    except (ProcessLookupError, ValueError, TypeError):
        pass
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

    # 跨机恢复靠 EBS detach/attach(控制面/运维编排),node-agent 不主动上传。
    # (非方案C 场景若显式要求 upload_s3,保留 S3 上传作兜底路径。)
    if s3_prefix and body.get("upload_s3", False):
        _s3_upload_sync(snap_dir, s3_prefix)

    return {
        "snapshot_type": snap_type,
        "snapshot_create_time_s": round(dt, 3),
        "mem_file_bytes": mem_apparent,
        "mem_actual_bytes": mem_actual,
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
    pat = re.escape(SBX_BASE.encode()) + rb"/[A-Za-z0-9._\-]+/v\.sock"
    return sorted({m.decode("utf-8", "ignore") for m in re.findall(pat, blob)})


def op_resume(body: dict) -> dict:
    sid        = body["id"]
    snap_dir   = body["snapshot_local_path"]
    rootfs     = body["rootfs_path"]          # 统一路径约定
    tap_idx    = int(body["tap_idx"])
    s3_prefix  = body.get("s3_prefix", "")

    # 兜底:若本地无快照文件且传了 s3_prefix,从 S3 拉回。
    # 注:方案C 从不往 S3 上传快照(见 op_suspend 的 upload_s3 分支),控制面传下来的
    # s3_prefix 恒为空 → 这段兜底当前【不会触发】,为未来可选的 S3 归档预留。
    # 现实的跨机恢复靠持久状态 EBS 卷幸存 + attach 到新节点(卷已 attach 则本地就有快照)。
    if not os.path.exists(f"{snap_dir}/vm.snapshot") and s3_prefix:
        _s3_download(s3_prefix, snap_dir)

    d = f"{SBX_BASE}/{sid}"
    os.makedirs(d, exist_ok=True)

    # 快照来源目录:暖池 claim 时 snap_dir=SBX_BASE/{warm_id}/snap,其父目录
    # (warm_id 目录)是快照固化的 vsock UDS 所在(v.sock 路径写死在快照里,
    # load 时 FC 会重新绑定它)。当 sid(real_id)≠ 快照来源 id 时,vsock 仍绑
    # 在来源目录,需清理来源目录的 v.sock,并在 sid 目录建 symlink 供 exec 用。
    src_dir = os.path.dirname(snap_dir)
    vsock_bound = f"{src_dir}/v.sock"   # 按目录约定推测的 UDS 路径
    # 权威来源:直接从快照里抠出固化的 vsock UDS 路径(暖池来源 ≠ sid 目录时,
    # 仅靠上面的目录约定清不掉真实路径 → "Address in use")。合并成待清理集合。
    stale_vsocks = {f"{d}/v.sock", vsock_bound}
    stale_vsocks.update(_vsock_uds_in_snapshot(f"{snap_dir}/vm.snapshot"))

    # rootfs 准备:方案C 下 rootfs 就在状态 EBS 的 {sid}/rootfs.ext4(随卷迁移,含装的软件)。
    # 回退:本地已有→直接用;快照目录里有→复制;都没有→基础镜像 CoW。
    if not os.path.exists(rootfs):
        snap_rootfs = f"{snap_dir}/rootfs.ext4"
        if os.path.exists(snap_rootfs):
            shutil.copy2(snap_rootfs, rootfs)
        else:
            subprocess.run(["cp", "--reflink=auto", ROOTFS, rootfs], check=True)

    # 内存镜像准备:若本次是 Diff 快照(存在 base.mem),需先把 diff 脏页合并到 base。
    #   Diff 快照的 vm.mem 只含脏页,不能独立 load(实测:仅 diff.mem load 失败/内存不全)。
    base_mem = f"{snap_dir}/vm.mem.base"
    diff_mem = f"{snap_dir}/vm.mem"
    mem_backend_path = diff_mem
    merge_time = 0.0
    if os.path.exists(base_mem) and os.path.exists(diff_mem):
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

    sock = f"{d}/api-resume.sock"
    try:
        os.remove(sock)
    except FileNotFoundError:
        pass

    # resume 前清理旧 vsock socket(快照含 vsock 设备,残留的 v.sock 会导致
    # "Address in use" → snapshot load 失败)。这是 Firecracker 快照恢复已知坑。
    # 清理集合含:sid 目录、目录约定路径、以及从快照抠出的固化真实路径(暖池来源)。
    for stale in stale_vsocks:
        try:
            os.remove(stale)
        except FileNotFoundError:
            pass

    with open(f"{d}/vm-resume.log", "w") as lf:
        # nosemgrep: dangerous-subprocess-use-tainted-env-args -- 固定 list:FC_BIN 来自环境配置,sock 为本地派生路径,无用户输入
        proc = subprocess.Popen([FC_BIN, "--api-sock", sock], stdout=lf, stderr=lf)

    if not _wait_sock(sock):
        proc.kill()
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
    tap, _, guest_ip = _setup_tap(tap_idx)

    # load 用 mem_backend_path:有 base(Diff 快照)时 = 上面合并出的 merged;
    # 无 base(Full 快照,如暖池首份)时 = vm.mem 本身。合并只发生在存在 base 时,
    # 是正确性所需(见上方 always-merge 说明),而非亚秒级恢复的阻碍。
    t0 = time.monotonic()
    _fc(sock, "PUT", "/snapshot/load", {
        "snapshot_path": f"{snap_dir}/vm.snapshot",
        "mem_backend":   {"backend_path": mem_backend_path, "backend_type": "File"},
        # track_dirty_pages 不随快照保存 → load 时必须显式再设 True,
        # 否则本次 resume 后的实例无法再打 Diff(多代接力断链)。FC 官方文档明确要求。
        "track_dirty_pages": True,
        "resume_vm":     True,
    })
    dt = time.monotonic() - t0

    # ---- P0: merged 转正为新 base(多代接力 + 存储减半)----
    # FC 语义(官方文档):load 时重置脏页位图 → resume 后的 Diff 基准 = 本次 load 的完整内存镜像。
    # 因此 merged 必须成为下一轮 Diff 的 base。同时旧 base/diff 作废,删除以省空间。
    # ⚠️ merged 正被 FC mmap 当运行内存,不能删/不能动它的 inode → 用 os.replace 原子改名(inode 不变)。
    if mem_backend_path.endswith("vm.mem.merged"):
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

    with _LOCK:
        _VMS[sid] = {
            "state":   "running",
            "pid":     proc.pid,
            "sock":    sock,
            "tap":     tap,
            "tap_idx": tap_idx,
            "ip":      guest_ip,
            "dir":     d,
        }

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
            "net_fix_ok": net_fix_ok,
            "juicefs_mode": JUICEFS_ENABLED}


def op_exec(body: dict) -> dict:
    """
    在 running 沙盒内执行命令。优先级:
      1. vsock UDS(不依赖 guest 网络，优先)
      2. TAP 网络 SSH(兜底，需 rootfs 内 sshd)
    """
    sid = body["id"]
    cmd = body.get("cmd", "echo no-cmd")
    timeout = int(body.get("timeout", 60))

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
    with _LOCK:
        vm = _VMS.get(sid)
    if not vm:
        raise KeyError(sid)
    return {"state": vm["state"], "ip": vm.get("ip", ""), "pid": vm.get("pid")}


def op_health() -> dict:
    mem = _free_mem_mib()
    with _LOCK:
        count = len(_VMS)
    return {"node_id": NODE_ID, "free_mem_mib": mem, "vm_count": count}


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
    from datetime import datetime, timezone
    with _LOCK:
        vm_count = len(_VMS)
    item = {
        "node_id":      {"S": NODE_ID},
        "ip":           {"S": _advertise_ip()},
        "free_mem_mib": {"N": str(_free_mem_mib())},
        "vm_count":     {"N": str(vm_count)},
        "last_seen":    {"S": datetime.now(timezone.utc).isoformat()},
    }
    subprocess.run(
        ["aws", "dynamodb", "put-item",
         "--table-name", NODES_TABLE,
         "--region", AWS_REGION,
         "--item", json.dumps(item)],
        check=True, capture_output=True, text=True,
    )


def start_heartbeat_loop() -> None:
    def _loop():
        import sys
        while True:
            try:
                _heartbeat_once()
            except Exception as e:
                # 心跳失败不阻断执行面,但打到 stderr 便于排障(勿静默吞)
                print(f"[heartbeat] failed: {e}", file=sys.stderr, flush=True)
            time.sleep(HEARTBEAT_EVERY_S)  # nosemgrep: arbitrary-sleep -- 心跳周期
    threading.Thread(target=_loop, daemon=True).start()


# ---------- 启动自恢复(P0-1:重建 _VMS,防重启后状态漂移) ----------

def _recover_vms() -> int:
    """
    启动时扫 SBX_BASE 下各沙盒目录,对仍有存活 FC api socket 的重建 _VMS。
    探测不到的目录跳过(交给控制面 reconcile 标 orphaned)。返回恢复数量。
    """
    recovered = 0
    if not os.path.isdir(SBX_BASE):
        return 0
    for sid in os.listdir(SBX_BASE):
        d = f"{SBX_BASE}/{sid}"
        if not os.path.isdir(d):
            continue
        # create 用 api.sock,resume 用 api-resume.sock —— 依次探测
        for sock_name in ("api.sock", "api-resume.sock"):
            sock = f"{d}/{sock_name}"
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
            with _LOCK:
                _VMS[sid] = {
                    "state": state,
                    "pid":   None,   # 重启后无法拿回原 pid(destroy 会尽力 kill)
                    "sock":  sock,
                    "tap":   f"fctap{_tap_idx_guess(sid)}",
                    "ip":    "",
                    "dir":   d,
                }
            recovered += 1
            break
    return recovered


def _tap_idx_guess(sid: str) -> int:
    """无内存态时无法精确知道 tap_idx;返回 1 占位(仅用于 destroy 尽力 teardown)。"""
    return 1


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
    # 疏散耗时粗估:满载 ~1.3GB/个 Diff、单卷 1000MB/s,实测 50 个并发 ~80s。
    est_s = round(len(sids) * 1.3 + 20, 1)
    mode  = "REAL" if RECLAIM_AUTO_EVACUATE else "DRY-RUN"
    plan  = {"node": NODE_ID, "signal": signal, "count": len(sids),
             "sandboxes": sids, "est_evac_s": est_s, "mode": mode}
    _RECLAIM_STATE.update({"detected": True, "signal": signal,
                           "at": _now_iso(), "plan": plan})
    print(f"[reclaim] SIGNAL={signal.get('type')} → evacuate {len(sids)} sandboxes "
          f"on {NODE_ID} (mode={mode}, est~{est_s}s): {sids}",
          file=sys.stderr, flush=True)
    if not RECLAIM_AUTO_EVACUATE:
        print("[reclaim] DRY-RUN: 不实际疏散。设 RECLAIM_AUTO_EVACUATE=1 开启真疏散。",
              file=sys.stderr, flush=True)
        return plan
    # REAL 疏散:逐个打 Diff 快照(方案C 落持久 EBS)。批量并发由控制面侧限流更合适,
    # 这里节点自救走串行/尽力而为,保证内存先落盘幸存。
    ok = 0
    for sid in sids:
        try:
            op_suspend({"id": sid, "snapshot_local_path": f"{SBX_BASE}/{sid}/snap"})
            ok += 1
        except Exception as e:
            print(f"[reclaim] evacuate {sid} failed: {e}", file=sys.stderr, flush=True)
    plan["evacuated_ok"] = ok
    _RECLAIM_STATE["evacuated"] = True
    print(f"[reclaim] REAL evacuation done: {ok}/{len(sids)}", file=sys.stderr, flush=True)
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


# ---------- HTTP handler ----------

def _check_caller_allowed(client_ip: str) -> bool:
    """校验来源 IP 是否在 ALLOWED_CALLER_CIDR 白名单内。白名单为空则允许所有（仅适合内网隔离环境）。"""
    if not ALLOWED_CALLER_CIDR:
        return True
    import ipaddress
    try:
        addr = ipaddress.ip_address(client_ip)
        for cidr in ALLOWED_CALLER_CIDR.split(","):
            cidr = cidr.strip()
            if cidr and addr in ipaddress.ip_network(cidr, strict=False):
                return True
    except ValueError:
        pass
    return False


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_): pass

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            n = 0
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _check_access(self) -> bool:
        client_ip = self.client_address[0]
        if not _check_caller_allowed(client_ip):
            self._send(403, {"error": "forbidden", "hint": f"caller {client_ip} not in ALLOWED_CALLER_CIDR"})
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        # /health 对所有来源开放（存活探针）
        if path != "/health" and not self._check_access():
            return
        try:
            if path == "/health":
                return self._send(200, op_health())
            if path == "/reclaim/status":
                return self._send(200, _RECLAIM_STATE)
            parts = path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "vm":
                return self._send(200, op_get(parts[1]))
            self._send(404, {"error": "not found"})
        except KeyError:
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        if not self._check_access():
            return
        path = urlparse(self.path).path
        body = self._body()
        try:
            if path == "/vm/create":
                return self._send(200, op_create(body))
            if path == "/vm/destroy":
                return self._send(200, op_destroy(body))
            if path == "/vm/snapshot_base":
                return self._send(200, op_snapshot_base(body))
            if path == "/vm/suspend":
                return self._send(200, op_suspend(body))
            if path == "/vm/resume":
                return self._send(200, op_resume(body))
            if path == "/vm/exec":
                return self._send(200, op_exec(body))
            # Block 1 测试:注入一个回收信号,立即算疏散计划(EKS 节点非 spot,用它验证链路)。
            if path == "/reclaim/simulate":
                sig = {"type": body.get("type", "spot-termination"),
                       "action": body.get("action", "terminate"),
                       "time": body.get("time", _now_iso()),
                       "injected": True}
                _RECLAIM_STATE["detected"] = False  # 允许重复测试
                return self._send(200, _evacuate_local(sig))
            # 清除检测态(测试用:恢复后重置,让 watch loop 可再次触发)
            if path == "/reclaim/reset":
                _RECLAIM_STATE.update({"detected": False, "signal": None, "at": None,
                                       "plan": None, "evacuated": False, "injected": None})
                return self._send(200, {"reset": True})
            self._send(404, {"error": "not found"})
        except KeyError:
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})


if __name__ == "__main__":
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

    print(f"node-agent [{NODE_ID}] 在 {LISTEN_HOST}:{LISTEN_PORT} "
          f"(advertise: {_advertise_ip()}, "
          f"allowed callers: {ALLOWED_CALLER_CIDR or 'all — set ALLOWED_CALLER_CIDR in production'})")
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
