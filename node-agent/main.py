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
import shutil
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ---------- 配置 ----------
LISTEN_PORT  = int(os.environ.get("NODE_AGENT_PORT", "8002"))
SBX_BASE     = os.environ.get("SBX_BASE", "/var/lib/sbx")       # 统一路径约定
ROOTFS       = os.environ.get("FC_ROOTFS",  "/opt/sbx/rootfs.ext4")  # 基础 rootfs 模板
JAILER_BIN   = os.environ.get("JAILER_BIN", "/usr/local/bin/firecracker-jailer")
FC_BIN       = os.environ.get("FC_BIN",     "/usr/local/bin/firecracker")
HOST_IFACE   = os.environ.get("HOST_IFACE", "")                 # 空则自动探测
AWS_REGION   = os.environ.get("AWS_REGION", "us-east-1")
NODE_ID      = os.environ.get("NODE_ID", socket.gethostname())

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
    subprocess.run(
        f"iptables -t nat -C POSTROUTING -o {host_if} -j MASQUERADE 2>/dev/null || "
        f"iptables -t nat -A POSTROUTING -o {host_if} -j MASQUERADE",
        shell=True,
    )
    subprocess.run(
        f"iptables -C FORWARD -i {tap} -o {host_if} -j ACCEPT 2>/dev/null || "
        f"iptables -A FORWARD -i {tap} -o {host_if} -j ACCEPT",
        shell=True,
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
               mem_mib: int, kernel: str, env: dict) -> tuple[int, str]:
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
        proc = subprocess.Popen(cmd, stdout=lf, stderr=lf)

    if not _wait_sock(sock, timeout=30.0):
        proc.kill()
        raise RuntimeError("firecracker API socket 未就绪")

    # 配置 VM
    _fc(sock, "PUT", "/boot-source", {
        "kernel_image_path": kernel,
        "boot_args": "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/sbxinit",
    })
    _fc(sock, "PUT", "/drives/rootfs", {
        "drive_id": "rootfs", "path_on_host": rootfs,
        "is_root_device": True, "is_read_only": False,
    })
    _fc(sock, "PUT", "/machine-config", {"vcpu_count": cpu, "mem_size_mib": mem_mib})
    _fc(sock, "PUT", "/network-interfaces/eth0", {"iface_id": "eth0", "host_dev_name": tap})
    # vsock: host UDS = {d}/v.sock, guest CID=3, port=2222 供 exec 使用
    vsock_path = f"{d}/v.sock"
    try:
        _fc(sock, "PUT", "/vsock", {"vsock_id": "vsock0", "guest_cid": 3, "uds_path": vsock_path})
    except Exception:
        pass  # vsock 配置失败不阻断 VM 启动
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
        time.sleep(0.05)
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

    pid, sock = _start_fc(sid, dest_rootfs, tap, cpu, mem_mib, kernel, env)

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
            subprocess.run(["kill", str(vm["pid"])], stderr=subprocess.DEVNULL)
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

    # 暂停
    _fc(sock, "PATCH", "/vm", {"state": "Paused"})

    # 快照策略:
    #   首次(无 base Full) → Full 快照(写全量内存,慢但必要)
    #   后续(有 base Full) → Diff 快照(只写脏页,快且小)
    # Firecracker Diff 快照要求:resume 时需同时提供 base mem + diff mem
    base_snap  = f"{snap_dir}/vm.snapshot.base"
    base_mem   = f"{snap_dir}/vm.mem.base"
    diff_snap  = f"{snap_dir}/vm.snapshot"
    diff_mem   = f"{snap_dir}/vm.mem"
    has_base   = os.path.exists(base_mem)

    t0 = time.monotonic()
    if not has_base:
        # 首次:Full 快照,同时保留一份作 base
        _fc(sock, "PUT", "/snapshot/create", {
            "snapshot_type": "Full",
            "snapshot_path": diff_snap,
            "mem_file_path": diff_mem,
        })
        # 复制为 base 供后续 Diff 使用
        import shutil as _sh
        _sh.copy2(diff_snap, base_snap)
        _sh.copy2(diff_mem,  base_mem)
    else:
        # 后续:Diff 快照(只写自上次 Full 以来的脏页)
        try:
            _fc(sock, "PUT", "/snapshot/create", {
                "snapshot_type": "Diff",
                "snapshot_path": diff_snap,
                "mem_file_path": diff_mem,
            })
        except Exception:
            # Diff 失败(如内核不支持)→ 降级 Full
            _fc(sock, "PUT", "/snapshot/create", {
                "snapshot_type": "Full",
                "snapshot_path": diff_snap,
                "mem_file_path": diff_mem,
            })
    dt = time.monotonic() - t0

    # kill VMM,释放 RAM
    subprocess.run(["kill", str(vm["pid"])], stderr=subprocess.DEVNULL)
    time.sleep(0.2)

    mem_size = os.path.getsize(f"{snap_dir}/vm.mem")

    with _LOCK:
        vm["state"] = "suspended"
        vm["pid"]   = None

    # 异步上传 S3(三件套:vm.mem + vm.snapshot + rootfs)
    # rootfs 也同步到 snap_dir 供跨机迁移
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

    # 若本地无快照文件,从 S3 拉回
    if not os.path.exists(f"{snap_dir}/vm.snapshot") and s3_prefix:
        _s3_download(s3_prefix, snap_dir)

    # rootfs 也需在约定路径(Firecracker snapshot 硬编码绝对路径)
    d = f"{SBX_BASE}/{sid}"
    os.makedirs(d, exist_ok=True)
    snap_rootfs = f"{snap_dir}/rootfs.ext4"
    if not os.path.exists(rootfs) and os.path.exists(snap_rootfs):
        shutil.copy2(snap_rootfs, rootfs)

    sock = f"{d}/api-resume.sock"
    try:
        os.remove(sock)
    except FileNotFoundError:
        pass

    with open(f"{d}/vm-resume.log", "w") as lf:
        proc = subprocess.Popen([FC_BIN, "--api-sock", sock], stdout=lf, stderr=lf)

    if not _wait_sock(sock):
        proc.kill()
        raise RuntimeError("firecracker resume socket 未就绪")

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
    return {"restore_time_s": round(dt, 4), "ip": guest_ip}


def op_exec(body: dict) -> dict:
    """
    在 running 沙盒内执行命令。优先级:
      1. TAP 网络 SSH(guest IP 可达时最简单)
      2. vsock UDS(SSH 不可用时兜底)
    """
    sid = body["id"]
    cmd = body.get("cmd", "echo no-cmd")
    timeout = int(body.get("timeout", 60))

    with _LOCK:
        vm = _VMS.get(sid)
    if not vm or vm["state"] != "running":
        raise RuntimeError(f"sandbox {sid} not running")

    # --- 方式 1: TAP SSH ---
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

    # --- 方式 2: vsock via socat ---
    # Firecracker vsock: host 端 UDS = {d}/v.sock, guest 端 CID=3, port=2222
    d     = vm.get("dir", f"{SBX_BASE}/{sid}")
    vsock = f"{d}/v.sock"
    if os.path.exists(vsock):
        try:
            # 通过 socat 把命令发给 guest vsock listener(guest 需运行 socat vsock 服务)
            r = subprocess.run(
                ["socat", "-", f"UNIX-CONNECT:{vsock}"],
                input=f"{cmd}\n", capture_output=True, text=True, timeout=timeout,
            )
            return {"rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr}
        except FileNotFoundError:
            pass  # socat 未安装

    raise RuntimeError(
        f"sandbox {sid}: exec failed (SSH unreachable, vsock not available). "
        "Ensure SSH is configured in rootfs or vsock is enabled."
    )


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

    def do_GET(self):
        path = urlparse(self.path).path
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
    print(f"node-agent [{NODE_ID}] 在 :{LISTEN_PORT}")
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
