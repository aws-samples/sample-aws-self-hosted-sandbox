#!/usr/bin/env python3
"""
裸 Firecracker 控制平面 API(demo)—— 含 Fly 同款 suspend(快照)/resume(恢复)。
跑在 .metal 主机本机(直接调 Firecracker 的 API socket + 管本地 tap/snapshot 文件)。
零外部依赖,纯标准库。这是 "Fly 模式" 的最小控制平面——区别于 Kata-on-EKS 那套(K8s 无 turnkey 快照)。

接口:
  POST   /sandboxes              创建并启动 microVM     -> {id, ip}
  POST   /sandboxes/{id}/suspend 暂停+快照+释放RAM(挂起) -> {snapshot_size, create_time}
  POST   /sandboxes/{id}/resume  从快照秒级恢复          -> {restore_time}
  DELETE /sandboxes/{id}         彻底销毁(回收一切)
  GET    /sandboxes              列出(running/suspended)

跑(在 .metal 主机,需 root): python3 fc_snapshot_api.py   # :8001
"""
import json, os, shutil, socket, subprocess, threading, time, uuid, http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

WORK = "/opt/sbx"               # 内核 vmlinux + 基础 rootfs.ext4 所在
BASE = "/opt/fcapi"             # 每个沙盒的运行时文件
KERNEL = f"{WORK}/vmlinux"
ROOTFS = f"{WORK}/rootfs.ext4"
os.makedirs(BASE, exist_ok=True)

# 内存:{id: {"state":"running|suspended", "pid":..., "sock":..., "tap":..., "ip":..., "idx":N}}
# 服务是多线程(ThreadingHTTPServer),所有对 SB / idx 分配器的读写都必须持有 LOCK。
SB = {}
LOCK = threading.Lock()
_next_idx = 1                   # 单调自增的下一个候选 idx
_free_idx = []                  # destroy 后回收的 idx,优先复用,避免子网无限增长


def alloc_idx():
    """分配一个沙盒网络 idx(决定 tap 名与 /30 子网),线程安全。"""
    global _next_idx
    with LOCK:
        if _free_idx:
            return _free_idx.pop()
        idx = _next_idx
        _next_idx += 1
        return idx


def release_idx(idx):
    """归还 idx 供后续复用,线程安全。"""
    with LOCK:
        _free_idx.append(idx)


def uds_request(sock_path, method, path, body=None, timeout=10):
    """向 Firecracker 的 unix socket 发 HTTP 请求(带超时,避免 VMM hang 住时永久阻塞线程)。"""
    conn = http.client.HTTPConnection("localhost", timeout=timeout)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    conn.sock = s
    try:
        s.connect(sock_path)
        data = json.dumps(body) if body is not None else None
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def wait_for_socket(sock_path, timeout=10.0, interval=0.05):
    """轮询等待 Firecracker 的 API socket 就绪(替代脆弱的 time.sleep)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(sock_path):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(interval)
                    s.connect(sock_path)
                return True
            except OSError:
                pass
        time.sleep(interval)
    return False


def setup_tap(idx):
    """每个沙盒一个 tap + /30 子网。返回 (tap, host_ip, guest_ip)。"""
    tap = f"fctap{idx}"
    host_ip = f"172.18.{idx}.1"; guest_ip = f"172.18.{idx}.2"
    subprocess.run(["ip", "tuntap", "add", tap, "mode", "tap"], stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "addr", "add", f"{host_ip}/30", "dev", tap], stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "link", "set", tap, "up"], stderr=subprocess.DEVNULL)
    hostif = subprocess.run("ip route|awk '/default/{print $5;exit}'", shell=True,
                            capture_output=True, text=True).stdout.strip()
    # nosec B602 / nosemgrep: subprocess-shell-true -- shell=True 仅为 "-C 检查 || -A 添加" 幂等惯用法;
    # idx 为内部分配的 int、hostif 来自本机路由表自动探测,均非用户输入,无注入面。
    subprocess.run(f"iptables -t nat -C POSTROUTING -o {hostif} -j MASQUERADE 2>/dev/null || "
                   f"iptables -t nat -A POSTROUTING -o {hostif} -j MASQUERADE", shell=True)  # nosec B602
    subprocess.run(f"iptables -C FORWARD -i {tap} -o {hostif} -j ACCEPT 2>/dev/null || "
                   f"iptables -A FORWARD -i {tap} -o {hostif} -j ACCEPT", shell=True)  # nosec B602
    return tap, host_ip, guest_ip


def create_sandbox(sid):
    idx = alloc_idx()
    d = f"{BASE}/{sid}"; os.makedirs(d, exist_ok=True)
    rootfs = f"{d}/rootfs.ext4"
    subprocess.run(["cp", "--reflink=auto", ROOTFS, rootfs])
    tap, host_ip, guest_ip = setup_tap(idx)
    sock = f"{d}/api.sock"
    try: os.remove(sock)
    except FileNotFoundError: pass
    # 用 with 打开日志:Popen 会把 fd 复制给子进程,父进程这边的副本随 with 关闭,
    # 避免每次 create 泄漏一个文件句柄(子进程仍持有自己的副本继续写)。
    with open(f"{d}/vm.log", "w") as log:
        pid = subprocess.Popen(["firecracker", "--api-sock", sock], stdout=log, stderr=log).pid
    if not wait_for_socket(sock):
        subprocess.run(["kill", str(pid)], stderr=subprocess.DEVNULL)
        release_idx(idx)
        raise RuntimeError("firecracker API socket 未就绪")
    uds_request(sock, "PUT", "/boot-source",
                {"kernel_image_path": KERNEL,
                 "boot_args": "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/sbxinit"})
    uds_request(sock, "PUT", "/drives/rootfs",
                {"drive_id": "rootfs", "path_on_host": rootfs,
                 "is_root_device": True, "is_read_only": False})
    uds_request(sock, "PUT", "/machine-config", {"vcpu_count": 2, "mem_size_mib": 4096})
    uds_request(sock, "PUT", "/network-interfaces/eth0",
                {"iface_id": "eth0", "host_dev_name": tap})
    uds_request(sock, "PUT", "/actions", {"action_type": "InstanceStart"})
    with LOCK:
        SB[sid] = {"state": "running", "pid": pid, "sock": sock, "dir": d,
                   "tap": tap, "ip": guest_ip, "idx": idx}


def suspend_sandbox(sid):
    """Fly suspend:暂停 VM → Full 快照 → kill 进程释放 RAM。"""
    with LOCK:
        s = SB[sid]          # 不存在则抛 KeyError,由 handler 转 404
        d = s["dir"]
    uds_request(s["sock"], "PATCH", "/vm", {"state": "Paused"})
    t0 = time.time()
    uds_request(s["sock"], "PUT", "/snapshot/create",
                {"snapshot_type": "Full",
                 "snapshot_path": f"{d}/vm.snapshot", "mem_file_path": f"{d}/vm.mem"})
    dt = time.time() - t0
    subprocess.run(["kill", str(s["pid"])], stderr=subprocess.DEVNULL)
    time.sleep(1)
    size = os.path.getsize(f"{d}/vm.mem")
    with LOCK:
        s["state"] = "suspended"; s["pid"] = None
    return dt, size


def resume_sandbox(sid):
    """Fly resume:新 Firecracker 进程 → load 快照 → resume。"""
    with LOCK:
        s = SB[sid]          # 不存在则抛 KeyError,由 handler 转 404
        d = s["dir"]
    sock = f"{d}/api.sock"
    try: os.remove(sock)
    except FileNotFoundError: pass
    with open(f"{d}/vm-resume.log", "w") as log:
        pid = subprocess.Popen(["firecracker", "--api-sock", sock], stdout=log, stderr=log).pid
    if not wait_for_socket(sock):
        subprocess.run(["kill", str(pid)], stderr=subprocess.DEVNULL)
        raise RuntimeError("firecracker API socket 未就绪")
    t0 = time.time()
    uds_request(sock, "PUT", "/snapshot/load",
                {"snapshot_path": f"{d}/vm.snapshot",
                 "mem_backend": {"backend_path": f"{d}/vm.mem", "backend_type": "File"},
                 "resume_vm": True})
    dt = time.time() - t0
    with LOCK:
        s["state"] = "running"; s["pid"] = pid; s["sock"] = sock
    return dt


def destroy_sandbox(sid):
    with LOCK:
        s = SB.pop(sid)      # 不存在则抛 KeyError,由 handler 转 404
    if s.get("pid"):
        subprocess.run(["kill", str(s["pid"])], stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "link", "del", s["tap"]], stderr=subprocess.DEVNULL)
    shutil.rmtree(s["dir"], ignore_errors=True)
    release_idx(s["idx"])    # 归还 idx,避免子网号无限增长


class H(BaseHTTPRequestHandler):
    def _s(self, c, o):
        b = json.dumps(o, ensure_ascii=False, indent=2).encode()
        self.send_response(c); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass

    def do_GET(self):
        if urlparse(self.path).path == "/sandboxes":
            with LOCK:
                listing = [{"id": k, "state": v["state"], "ip": v["ip"]} for k, v in SB.items()]
            self._s(200, {"sandboxes": listing})
        else: self._s(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path.strip("/").split("/")
        try:
            if p == ["sandboxes"]:
                sid = uuid.uuid4().hex[:8]; create_sandbox(sid)
                self._s(201, {"id": sid, "state": "running", "ip": SB[sid]["ip"]})
            elif len(p) == 3 and p[0] == "sandboxes" and p[2] == "suspend":
                dt, size = suspend_sandbox(p[1])
                self._s(200, {"id": p[1], "state": "suspended",
                              "snapshot_create_time_s": round(dt, 3),
                              "mem_file_bytes": size,
                              "note": "RAM 已释放;访问时 /resume 秒级恢复"})
            elif len(p) == 3 and p[0] == "sandboxes" and p[2] == "resume":
                dt = resume_sandbox(p[1])
                self._s(200, {"id": p[1], "state": "running",
                              "restore_time_s": round(dt, 4),
                              "note": "从快照恢复,内存状态精确续上"})
            else: self._s(404, {"error": "not found"})
        except KeyError:
            self._s(404, {"error": "not found"})
        except Exception as e:
            self._s(500, {"error": str(e)})

    def do_DELETE(self):
        p = urlparse(self.path).path.strip("/").split("/")
        if len(p) == 2 and p[0] == "sandboxes":
            try: destroy_sandbox(p[1]); self._s(200, {"id": p[1], "deleted": True})
            except KeyError: self._s(404, {"error": "not found"})
            except Exception as e: self._s(500, {"error": str(e)})
        else: self._s(404, {"error": "not found"})


if __name__ == "__main__":
    print("裸 Firecracker 快照 API 在 http://127.0.0.1:8001")
    ThreadingHTTPServer(("127.0.0.1", 8001), H).serve_forever()
