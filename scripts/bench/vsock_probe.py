#!/usr/bin/env python3
"""在 node-agent Pod 内运行：直连各沙盒的 Firecracker vsock UDS，测 guest 交互延迟。

每轮对每个沙盒发一次 `true`（guest agent fork bash 执行），记录往返时间。
排除了控制面/HTTP/跨地域网络，只剩 宿主调度 + vsock + guest 调度 的延迟，
用于衡量 CPU 超分下"空闲沙盒还能不能及时响应"。

用法(stdin 传入本文件):
  kubectl exec <node-agent-pod> -- python3 - --prefix m6 --range 1:257 --rounds 5 < vsock_probe.py
输出一行 JSON：p50/p90/p99/max(ms)、失败数。
"""
import argparse
import json
import socket
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor


def once(uds: str, timeout: float) -> float:
    t0 = time.monotonic()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(uds)
        s.sendall(b"CONNECT 2222\n")
        ack = b""
        while b"\n" not in ack:
            c = s.recv(64)
            if not c:
                raise RuntimeError("no ack")
            ack += c
        s.sendall(b'{"cmd": "true"}\n')
        r = b""
        while b"\n" not in r:
            c = s.recv(65536)
            if not c:
                raise RuntimeError("eof")
            r += c
        if not json.loads(r.split(b"\n", 1)[0]).get("rc") == 0:
            raise RuntimeError("rc!=0")
        return (time.monotonic() - t0) * 1000
    finally:
        s.close()


def pct(v, p):
    v = sorted(v)
    return round(v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))], 1) if v else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--range", required=True)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=30)
    a = ap.parse_args()
    lo, hi = (int(x) for x in a.range.split(":"))
    uds = [f"/var/lib/sbx/{a.prefix}-{i:04d}/v.sock" for i in range(lo, hi)]
    lat, fails = [], 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        for _ in range(a.rounds):
            for res in ex.map(lambda u: _safe(u, a.timeout), uds):
                if res is None:
                    fails += 1
                else:
                    lat.append(res)
    print(json.dumps({"n": len(lat), "fails": fails, "wall_s": round(time.time() - t0, 2),
                      "p50_ms": pct(lat, 50), "p90_ms": pct(lat, 90), "p99_ms": pct(lat, 99),
                      "max_ms": round(max(lat), 1) if lat else None,
                      "mean_ms": round(statistics.mean(lat), 1) if lat else None}))


def _safe(u, timeout):
    try:
        return once(u, timeout)
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
