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
import signal
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
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
    _fc(sock, "PUT", "/machine-config", {"vcpu_count": cpu, "mem_size_mib": mem_mib})
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

def _s3_upload(local_dir: str, s3_prefix: str) -> None:
    """异步把 local_dir 里的快照三件套上传到 s3_prefix。"""
    if not s3_prefix:
        return

    def _do():
        subprocess.run(
            ["aws", "s3", "sync", local_dir, s3_prefix,
             "--region", AWS_REGION, "--quiet"],
            check=False,
        )

    threading.Thread(target=_do, daemon=True).start()


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

    # 快照策略：纯 Full。
    # （曾试过 Diff 只 dump 脏页，但 Firecracker 在 snapshot load/resume 后会重置
    #  脏页位图 → "suspend→resume→再 suspend" 场景下 diff 退化成接近 full，
    #  叠加 overlay 开销后反而更慢。对本用例 Full 更简单可预测。真正的提速是
    #  换带本地 NVMe 的机型，把落盘从 EBS gp3 的 ~81MB/s 提到 GB/s 级。）
    full_snap = f"{snap_dir}/vm.snapshot"      # 设备/vCPU 状态文件（小，几十KB）
    full_mem  = f"{snap_dir}/vm.mem"           # 完整内存镜像

    SNAP_TIMEOUT = 300  # gp3 上 2GB Full 可能 ~90s

    t0 = time.monotonic()
    _fc(sock, "PUT", "/snapshot/create", {
        "snapshot_type": "Full",
        "snapshot_path": full_snap,
        "mem_file_path": full_mem,
    }, timeout=SNAP_TIMEOUT)
    dt = time.monotonic() - t0

    # kill VMM,释放 RAM。用 os.kill 而非 subprocess(slim 镜像无 /bin/kill)
    try:
        os.kill(int(vm["pid"]), signal.SIGTERM)
    except (ProcessLookupError, ValueError, TypeError):
        pass
    time.sleep(0.2)  # nosemgrep: arbitrary-sleep -- 等 VMM 退出释放 vm.mem 文件句柄后再读大小

    mem_size = os.path.getsize(f"{snap_dir}/vm.mem")

    with _LOCK:
        vm["state"] = "suspended"
        vm["pid"]   = None

    # 上传 S3：
    #   方案 A（默认）：三件套 = vm.mem + vm.snapshot + rootfs.ext4
    #   方案 B（JuiceFS）：两件套 = vm.mem + vm.snapshot（workspace 已在 S3，无需复制磁盘）
    if JUICEFS_ENABLED:
        # 方案 B：只上传内存快照，不含 rootfs（快照更小，跨机更轻量）
        _s3_upload(snap_dir, s3_prefix)
    else:
        # 方案 A：rootfs 一起上传（保证跨机 resume 的完整一致性）
        rootfs_src = f"{SBX_BASE}/{sid}/rootfs.ext4"
        rootfs_dst = f"{snap_dir}/rootfs.ext4"
        if os.path.exists(rootfs_src) and rootfs_src != rootfs_dst:
            subprocess.run(["cp", "--reflink=auto", rootfs_src, rootfs_dst],
                           stderr=subprocess.DEVNULL)
        _s3_upload(snap_dir, s3_prefix)

    return {
        "snapshot_create_time_s": round(dt, 3),
        "mem_file_bytes": mem_size,
    }


def op_resume(body: dict) -> dict:
    sid        = body["id"]
    snap_dir   = body["snapshot_local_path"]
    rootfs     = body["rootfs_path"]          # 统一路径约定
    tap_idx    = int(body["tap_idx"])
    s3_prefix  = body.get("s3_prefix", "")

    # 若本地无快照文件，从 S3 拉回
    if not os.path.exists(f"{snap_dir}/vm.snapshot") and s3_prefix:
        _s3_download(s3_prefix, snap_dir)

    d = f"{SBX_BASE}/{sid}"
    os.makedirs(d, exist_ok=True)

    if JUICEFS_ENABLED:
        # 方案 B：rootfs 不在快照里，从基础镜像 CoW 复制（/workspace 数据在 S3，resume 后自动重连）
        if not os.path.exists(rootfs):
            subprocess.run(["cp", "--reflink=auto", ROOTFS, rootfs], check=True)
    else:
        # 方案 A：rootfs 在快照三件套里，从 S3 拉回
        snap_rootfs = f"{snap_dir}/rootfs.ext4"
        if not os.path.exists(rootfs) and os.path.exists(snap_rootfs):
            shutil.copy2(snap_rootfs, rootfs)

    sock = f"{d}/api-resume.sock"
    try:
        os.remove(sock)
    except FileNotFoundError:
        pass

    # resume 前清理旧 vsock socket(快照含 vsock 设备,残留的 v.sock 会导致
    # "Address in use" → snapshot load 失败)。这是 Firecracker 快照恢复已知坑。
    try:
        os.remove(f"{d}/v.sock")
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
    try:
        os.remove(f"{d}/v.sock")
    except FileNotFoundError:
        pass

    # vm.mem 始终是最新的完整内存（Diff 的脏页已在 suspend 侧叠加进去），
    # 因此 resume 直接 load vm.mem，无需在恢复路径上做合并 → resume 保持亚秒级。
    t0 = time.monotonic()
    _fc(sock, "PUT", "/snapshot/load", {
        "snapshot_path": f"{snap_dir}/vm.snapshot",
        "mem_backend":   {"backend_path": f"{snap_dir}/vm.mem", "backend_type": "File"},
        "resume_vm":     True,
    })
    dt = time.monotonic() - t0

    # 重建 tap
    tap, _, guest_ip = _setup_tap(tap_idx)

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

    # 方案 B：JuiceFS resume 后，guest 内的 FUSE 连接会自动重连 S3
    # （JuiceFS daemon 随内存快照一起恢复，重连是 JuiceFS 内部行为）
    # 如果连接未自动恢复，需在 guest 内执行 remount（极少数情况）：
    #   juicefs umount /workspace && juicefs mount <redis_addr> /workspace -d
    # 这里不主动 remount，依赖 JuiceFS 自动重连机制。

    return {"restore_time_s": round(dt, 4), "ip": guest_ip,
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
            if path == "/vm/suspend":
                return self._send(200, op_suspend(body))
            if path == "/vm/resume":
                return self._send(200, op_resume(body))
            if path == "/vm/exec":
                return self._send(200, op_exec(body))
            self._send(404, {"error": "not found"})
        except KeyError:
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})


if __name__ == "__main__":
    print(f"node-agent [{NODE_ID}] 在 {LISTEN_HOST}:{LISTEN_PORT} "
          f"(allowed callers: {ALLOWED_CALLER_CIDR or 'all — set ALLOWED_CALLER_CIDR in production'})")
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
