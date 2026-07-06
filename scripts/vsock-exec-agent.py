#!/usr/bin/env python3
"""
Guest 内的 vsock exec 代理 —— FC exec 通道的 guest 端。

在 microVM guest 内以后台常驻方式运行(由 /sbin/sbxinit 启动)。
监听 AF_VSOCK 端口 2222，收到一行 JSON 请求后在 guest 内执行命令，
把结果以 JSON 返回。host 端(node-agent op_exec)通过 Firecracker vsock UDS
先发 "CONNECT 2222\\n" 握手，再收发这段协议。

协议(每连接一次请求）：
  host → guest:  {"cmd": "<shell command>"}\\n
  guest → host:  {"rc": <int>, "stdout": "<str>", "stderr": "<str>"}\\n

不依赖 guest 网络配置（绕开 tap IP / sshd），只要 kernel 支持 AF_VSOCK。
CI kernel vmlinux-5.10.223 内建 CONFIG_VSOCKETS/CONFIG_VIRTIO_VSOCKETS。
"""
import json
import socket
import subprocess
import sys

# guest 侧 CID 由 Firecracker 配置为 3；监听端口与 node-agent 约定为 2222
VSOCK_PORT = 2222


def _handle(conn: socket.socket) -> None:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            return
        buf += chunk
    try:
        req = json.loads(buf.decode(errors="replace").strip())
        cmd = req.get("cmd", "")
    except Exception as e:  # 请求解析失败也回一个结构化错误
        conn.sendall((json.dumps({"rc": -1, "stdout": "",
                                  "stderr": f"bad request: {e}"}) + "\n").encode())
        return

    try:
        p = subprocess.run(["/bin/bash", "-c", cmd],
                           capture_output=True, text=True, timeout=300)
        resp = {"rc": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
    except subprocess.TimeoutExpired:
        resp = {"rc": -1, "stdout": "", "stderr": "command timed out (300s)"}
    except Exception as e:
        resp = {"rc": -1, "stdout": "", "stderr": str(e)}
    conn.sendall((json.dumps(resp) + "\n").encode())


def main() -> int:
    try:
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    except AttributeError:
        print("[vsock-exec-agent] AF_VSOCK not supported by this Python/kernel",
              file=sys.stderr)
        return 1
    # VMADDR_CID_ANY = 0xFFFFFFFF
    s.bind((socket.VMADDR_CID_ANY, VSOCK_PORT))
    s.listen(8)
    print(f"[vsock-exec-agent] listening on vsock port {VSOCK_PORT}", flush=True)
    while True:
        try:
            conn, _ = s.accept()
        except Exception:
            continue
        try:
            _handle(conn)
        except Exception:
            pass
        finally:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
