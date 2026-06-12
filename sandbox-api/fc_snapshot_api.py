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
import json, os, shutil, socket, subprocess, time, http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

WORK = "/opt/sbx"               # 内核 vmlinux + 基础 rootfs.ext4 所在
BASE = "/opt/fcapi"             # 每个沙盒的运行时文件
KERNEL = f"{WORK}/vmlinux"
ROOTFS = f"{WORK}/rootfs.ext4"
os.makedirs(BASE, exist_ok=True)

# 内存:{id: {"state":"running|suspended", "pid":..., "sock":..., "tap":..., "ip":..., "idx":N}}
SB = {}
NEXT = [1]


def uds_request(sock_path, method, path, body=None):
    """向 Firecracker 的 unix socket 发 HTTP 请求。"""
    conn = http.client.HTTPConnection("localhost")
    conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.sock.connect(sock_path)
    data = json.dumps(body) if body is not None else None
    conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
    r = conn.getresponse(); out = r.read(); conn.close()
    return r.status, out


def setup_tap(idx):
    """每个沙盒一个 tap + /30 子网。返回 (tap, host_ip, guest_ip)。"""
    tap = f"fctap{idx}"
    host_ip = f"172.18.{idx}.1"; guest_ip = f"172.18.{idx}.2"
    subprocess.run(["ip", "tuntap", "add", tap, "mode", "tap"], stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "addr", "add", f"{host_ip}/30", "dev", tap], stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "link", "set", tap, "up"], stderr=subprocess.DEVNULL)
    hostif = subprocess.run("ip route|awk '/default/{print $5;exit}'", shell=True,
                            capture_output=True, text=True).stdout.strip()
    subprocess.run(f"iptables -t nat -C POSTROUTING -o {hostif} -j MASQUERADE 2>/dev/null || "
                   f"iptables -t nat -A POSTROUTING -o {hostif} -j MASQUERADE", shell=True)
    subprocess.run(f"iptables -C FORWARD -i {tap} -o {hostif} -j ACCEPT 2>/dev/null || "
                   f"iptables -A FORWARD -i {tap} -o {hostif} -j ACCEPT", shell=True)
    return tap, host_ip, guest_ip


def create_sandbox(sid):
    idx = NEXT[0]; NEXT[0] += 1
    d = f"{BASE}/{sid}"; os.makedirs(d, exist_ok=True)
    rootfs = f"{d}/rootfs.ext4"
    subprocess.run(["cp", "--reflink=auto", ROOTFS, rootfs])
    tap, host_ip, guest_ip = setup_tap(idx)
    sock = f"{d}/api.sock"
    try: os.remove(sock)
    except FileNotFoundError: pass
    log = open(f"{d}/vm.log", "w")
    pid = subprocess.Popen(["firecracker", "--api-sock", sock], stdout=log, stderr=log).pid
    time.sleep(1)
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
    SB[sid] = {"state": "running", "pid": pid, "sock": sock, "dir": d,
               "tap": tap, "ip": guest_ip, "idx": idx}


def suspend_sandbox(sid):
    """Fly suspend:暂停 VM → Full 快照 → kill 进程释放 RAM。"""
    s = SB[sid]; d = s["dir"]
    uds_request(s["sock"], "PATCH", "/vm", {"state": "Paused"})
    t0 = time.time()
    uds_request(s["sock"], "PUT", "/snapshot/create",
                {"snapshot_type": "Full",
                 "snapshot_path": f"{d}/vm.snapshot", "mem_file_path": f"{d}/vm.mem"})
    dt = time.time() - t0
    subprocess.run(["kill", str(s["pid"])], stderr=subprocess.DEVNULL)
    time.sleep(1)
    size = os.path.getsize(f"{d}/vm.mem")
    s["state"] = "suspended"; s["pid"] = None
    return dt, size


def resume_sandbox(sid):
    """Fly resume:新 Firecracker 进程 → load 快照 → resume。"""
    s = SB[sid]; d = s["dir"]
    sock = f"{d}/api.sock"
    try: os.remove(sock)
    except FileNotFoundError: pass
    log = open(f"{d}/vm-resume.log", "w")
    pid = subprocess.Popen(["firecracker", "--api-sock", sock], stdout=log, stderr=log).pid
    time.sleep(1)
    t0 = time.time()
    uds_request(sock, "PUT", "/snapshot/load",
                {"snapshot_path": f"{d}/vm.snapshot",
                 "mem_backend": {"backend_path": f"{d}/vm.mem", "backend_type": "File"},
                 "resume_vm": True})
    dt = time.time() - t0
    s["state"] = "running"; s["pid"] = pid; s["sock"] = sock
    return dt


def destroy_sandbox(sid):
    s = SB.pop(sid)
    if s.get("pid"):
        subprocess.run(["kill", str(s["pid"])], stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "link", "del", s["tap"]], stderr=subprocess.DEVNULL)
    shutil.rmtree(s["dir"], ignore_errors=True)


class H(BaseHTTPRequestHandler):
    def _s(self, c, o):
        b = json.dumps(o, ensure_ascii=False, indent=2).encode()
        self.send_response(c); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass

    def do_GET(self):
        if urlparse(self.path).path == "/sandboxes":
            self._s(200, {"sandboxes": [
                {"id": k, "state": v["state"], "ip": v["ip"]} for k, v in SB.items()]})
        else: self._s(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path.strip("/").split("/")
        try:
            if p == ["sandboxes"]:
                sid = str(int(time.time()))[-6:]; create_sandbox(sid)
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
        except Exception as e:
            self._s(500, {"error": str(e)})

    def do_DELETE(self):
        p = urlparse(self.path).path.strip("/").split("/")
        if len(p) == 2 and p[0] == "sandboxes":
            try: destroy_sandbox(p[1]); self._s(200, {"id": p[1], "deleted": True})
            except KeyError: self._s(404, {"error": "not found"})
        else: self._s(404, {"error": "not found"})


if __name__ == "__main__":
    print("裸 Firecracker 快照 API 在 http://127.0.0.1:8001")
    ThreadingHTTPServer(("127.0.0.1", 8001), H).serve_forever()
