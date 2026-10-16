#!/usr/bin/env python3
"""单节点超分真机压测：直接驱动一台节点的 node-agent（HMAC 签名），绕开控制面调度，
测宿主本身能承载多少 microVM、内存/CPU/磁盘超分后的真实表现。

子命令（结果均追加到 --out 指定的 JSONL，每行一条带时间戳的记录）：

  sample                       采一次宿主指标（内存/CPU/PSI/磁盘/FC 进程 RSS/iptables 规则数）
  create  --start N --count M  创建 oc-<idx> 沙盒，tap_idx=idx，记录每个的创建耗时
  ready                        等全部沙盒 vsock exec 可用，记录 exec 往返
  exec    --cmd ...            对选中沙盒并发执行命令，记录 rc/耗时/输出摘要
  push-memtouch                把 scripts/bench/memtouch.py 写进 guest /root/
  cpubench --seconds S         选中沙盒同时跑固定 CPU 负载，返回每个 VM 的吞吐
  destroy                      销毁选中沙盒

沙盒选择：--ids a,b 或 --range START:END（左闭右开，按 idx）。
连接：自动对目标节点的 node-agent Pod 建 kubectl port-forward。
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import contextlib
import hashlib
import hmac
import json
import os
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

NS = "sandbox-system"
HERE = Path(__file__).resolve().parent

SAMPLER = r'''
import json, os, time
def meminfo():
    out = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        out[k] = int(v.split()[0])
    return out
def cpu():
    f = open("/proc/stat").readline().split()[1:]
    return [int(x) for x in f]
def psi(kind):
    try:
        res = {}
        for line in open(f"/proc/pressure/{kind}"):
            parts = line.split()
            res[parts[0]] = {k: float(v) for k, v in (p.split("=") for p in parts[1:] if "=" in p and not p.startswith("total"))}
        return res
    except OSError:
        return {}
def fc_procs():
    n = 0; rss = 0; threads = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            comm = open(f"/proc/{pid}/comm").read().strip()
            if comm != "firecracker":
                continue
            for line in open(f"/proc/{pid}/status"):
                if line.startswith("VmRSS:"):
                    rss += int(line.split()[1])
                elif line.startswith("Threads:"):
                    threads += int(line.split()[1])
            n += 1
        except OSError:
            continue
    return n, rss, threads
c1 = cpu(); t1 = time.time(); time.sleep(float(os.environ.get("SAMPLE_S", "3"))); c2 = cpu(); t2 = time.time()
d = [b - a for a, b in zip(c1, c2)]; tot = sum(d) or 1
names = ["user","nice","system","idle","iowait","irq","softirq","steal","guest","guest_nice"]
cpu_pct = {n: round(100.0 * d[i] / tot, 2) for i, n in enumerate(names) if i < len(d)}
m = meminfo(); n, rss, th = fc_procs()
st = os.statvfs("/var/lib/sbx")
def ipt():
    try:
        import subprocess
        return sum(1 for l in subprocess.run(["iptables","-S","FORWARD"],capture_output=True,text=True).stdout.splitlines() if l.startswith("-A"))
    except Exception:
        return -1
la = open("/proc/loadavg").read().split()
vmstat = {}
for line in open("/proc/vmstat"):
    k, v = line.split()
    if k in ("oom_kill","pswpin","pswpout","pgmajfault","thp_fault_alloc","compact_stall"):
        vmstat[k] = int(v)
print(json.dumps({
  "mem_total_mib": m["MemTotal"]//1024, "mem_available_mib": m["MemAvailable"]//1024,
  "mem_free_mib": m["MemFree"]//1024, "anon_mib": m.get("AnonPages",0)//1024,
  "cached_mib": m.get("Cached",0)//1024, "committed_as_mib": m.get("Committed_AS",0)//1024,
  "anon_huge_mib": m.get("AnonHugePages",0)//1024, "slab_mib": m.get("Slab",0)//1024,
  "pagetables_mib": m.get("PageTables",0)//1024, "dirty_mib": m.get("Dirty",0)//1024,
  "fc_count": n, "fc_rss_mib": rss//1024, "fc_threads": th,
  "cpu_pct": cpu_pct, "loadavg": [float(x) for x in la[:3]], "running_tasks": la[3],
  "psi_cpu": psi("cpu"), "psi_mem": psi("memory"), "psi_io": psi("io"),
  "sbx_disk_used_gib": round((st.f_blocks - st.f_bfree) * st.f_frsize / 2**30, 2),
  "sbx_disk_total_gib": round(st.f_blocks * st.f_frsize / 2**30, 2),
  "iptables_forward_rules": ipt(), "vmstat": vmstat,
}))
'''


def now() -> float:
    return round(time.time(), 3)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def node_agent_pod(node: str) -> str:
    out = subprocess.run(
        ["kubectl", "-n", NS, "get", "pods", "-l", "app=node-agent", "-o", "json"],
        check=True, capture_output=True, text=True,
    ).stdout
    for item in json.loads(out)["items"]:
        if item["spec"].get("nodeName") == node or item["status"].get("hostIP") == node:
            return item["metadata"]["name"]
    raise SystemExit(f"node-agent pod not found on {node}")


def auth_secret() -> str:
    out = subprocess.run(
        ["kubectl", "-n", NS, "get", "secret", "node-agent-auth",
         "-o", "jsonpath={.data.NODE_AGENT_AUTH_SECRET}"],
        check=True, capture_output=True, text=True,
    ).stdout
    return base64.b64decode(out).decode()


@contextlib.contextmanager
def port_forward(pod: str):
    port = free_port()
    proc = subprocess.Popen(
        ["kubectl", "-n", NS, "port-forward", f"pod/{pod}", f"{port}:8002"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.2)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()


class Agent:
    def __init__(self, url: str, secret: str):
        self.url, self.secret = url, secret

    def call(self, method: str, path: str, body=None, timeout: float = 180):
        payload = json.dumps(body).encode() if body is not None else b""
        ts, nonce = str(int(time.time())), uuid.uuid4().hex
        sha = hashlib.sha256(payload).hexdigest()
        canonical = "\n".join(("v1", ts, nonce, method, path, sha)).encode()
        sig = hmac.new(self.secret.encode(), canonical, hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            self.url + path, data=payload if body is not None else None, method=method,
            headers={"Content-Type": "application/json", "X-SBX-Auth-Version": "v1",
                     "X-SBX-Timestamp": ts, "X-SBX-Nonce": nonce,
                     "X-SBX-Content-SHA256": sha, "X-SBX-Signature": sig},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except ValueError:
                return e.code, {"error": raw.decode(errors="replace")[:500]}
        except Exception as e:  # 超时/连接错误也作为结果记录
            return -1, {"error": f"{type(e).__name__}: {e}"[:500]}


def sample(pod: str, seconds: float = 3) -> dict:
    r = subprocess.run(
        ["kubectl", "-n", NS, "exec", pod, "--", "env", f"SAMPLE_S={seconds}",
         "python3", "-c", SAMPLER],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        return {"sample_error": r.stderr[-500:]}
    return json.loads(r.stdout.strip().splitlines()[-1])


def pmap(fn, items, conc):
    with cf.ThreadPoolExecutor(max_workers=max(1, min(conc, len(items) or 1))) as ex:
        return list(ex.map(fn, items))


def pct(values, p):
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))], 3)


def summarize(lat):
    return {"n": len(lat), "p50": pct(lat, 50), "p90": pct(lat, 90),
            "p99": pct(lat, 99), "max": round(max(lat), 3) if lat else None,
            "mean": round(statistics.mean(lat), 3) if lat else None}


def select_ids(args) -> list[str]:
    if args.ids:
        return args.ids.split(",")
    a, b = (int(x) for x in args.range.split(":"))
    return [f"{args.prefix}-{i:04d}" for i in range(a, b)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("sample", "create", "ready", "exec", "push-memtouch",
                                    "cpubench", "destroy"))
    ap.add_argument("--node", required=True, help="k8s node name of target sandbox node")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tag", default="", help="free-form label stored with each record")
    ap.add_argument("--prefix", default="oc")
    ap.add_argument("--ids", default="")
    ap.add_argument("--range", default="0:0")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--count", type=int, default=0)
    ap.add_argument("--cpu", type=int, default=2)
    ap.add_argument("--mem", type=int, default=2048)
    ap.add_argument("--balloon", action="store_true",
                    help="开启 virtio-balloon free_page_reporting")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--sample-every", type=int, default=0,
                    help="create 时每创建多少个采一次宿主指标")
    ap.add_argument("--exec-cmd", default="")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--keep-output", action="store_true")
    args = ap.parse_args()

    pod = node_agent_pod(args.node)
    out = open(args.out, "a")

    def emit(rec: dict):
        rec = {"ts": now(), "cmd": args.cmd, "tag": args.tag, "node": args.node, **rec}
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()
        brief = {k: v for k, v in rec.items() if k not in ("results",)}
        print(json.dumps(brief, ensure_ascii=False)[:1500], flush=True)

    if args.cmd == "sample":
        emit({"host": sample(pod, args.seconds)})
        return 0

    with port_forward(pod) as url:
        agent = Agent(url, auth_secret())

        if args.cmd == "create":
            idxs = list(range(args.start, args.start + args.count))
            results = []
            batch = args.sample_every or len(idxs)
            for off in range(0, len(idxs), batch):
                chunk = idxs[off:off + batch]

                def mk(i):
                    body = {"id": f"{args.prefix}-{i:04d}", "tap_idx": i, "cpu": args.cpu,
                            "mem_mib": args.mem, "kernel": "/opt/sbx/vmlinux"}
                    if args.balloon:
                        body["balloon"] = {"free_page_reporting": True, "deflate_on_oom": True,
                                           "stats_polling_interval_s": 1}
                    t0 = time.monotonic()
                    code, res = agent.call("POST", "/vm/create", body, timeout=args.timeout)
                    return {"id": body["id"], "code": code, "s": round(time.monotonic() - t0, 3),
                            "ip": res.get("ip"), "error": res.get("error")}

                t0 = time.monotonic()
                rs = pmap(mk, chunk, args.concurrency)
                wall = time.monotonic() - t0
                results.extend(rs)
                ok = [r for r in rs if r["code"] == 200]
                rec = {"batch_first": chunk[0], "batch_last": chunk[-1], "ok": len(ok),
                       "failed": len(rs) - len(ok), "wall_s": round(wall, 2),
                       "latency": summarize([r["s"] for r in ok]),
                       "errors": sorted({str(r["error"])[:200] for r in rs if r["code"] != 200})[:5],
                       "cpu": args.cpu, "mem": args.mem, "balloon": args.balloon,
                       "results": rs}
                if args.sample_every:
                    time.sleep(5)
                    rec["host"] = sample(pod)
                emit(rec)
            return 0

        ids = select_ids(args)

        if args.cmd == "ready":
            def probe(sid):
                t0 = time.monotonic()
                code, res = agent.call("POST", "/vm/exec", {"id": sid, "cmd": "cat /run/heartbeat", "timeout": 20},
                                       timeout=60)
                return {"id": sid, "code": code, "rc": res.get("rc"), "s": round(time.monotonic() - t0, 3),
                        "error": res.get("error")}
            rs = pmap(probe, ids, args.concurrency)
            ok = [r for r in rs if r["code"] == 200 and r["rc"] == 0]
            emit({"n": len(ids), "ok": len(ok), "latency": summarize([r["s"] for r in ok]),
                  "failed_ids": [r["id"] for r in rs if r not in ok][:50],
                  "errors": sorted({str(r["error"])[:200] for r in rs if r not in ok})[:5]})
            return 0

        if args.cmd in ("exec", "push-memtouch", "cpubench"):
            if args.cmd == "push-memtouch":
                b64 = base64.b64encode((HERE / "memtouch.py").read_bytes()).decode()
                cmd = f"echo {b64} | base64 -d > /root/memtouch.py && python3 -c 'import ast;ast.parse(open(\"/root/memtouch.py\").read())' && echo pushed"
            elif args.cmd == "cpubench":
                cmd = ("python3 -c \"import hashlib,time;b=b'x'*65536;n=0;t=time.time();"
                       f"e=t+{args.seconds}\nwhile time.time()<e:\n hashlib.sha256(b).digest();n+=1\n"
                       "print(n, round(time.time()-t,3))\"")
            else:
                cmd = args.exec_cmd

            def run(sid):
                t0 = time.monotonic()
                code, res = agent.call("POST", "/vm/exec", {"id": sid, "cmd": cmd, "timeout": args.timeout},
                                       timeout=args.timeout + 60)
                stdout = res.get("stdout", "") or ""
                return {"id": sid, "code": code, "rc": res.get("rc"), "s": round(time.monotonic() - t0, 3),
                        "stdout": stdout if args.keep_output else stdout[-300:],
                        "stderr": (res.get("stderr", "") or "")[-300:], "error": res.get("error")}

            t0 = time.monotonic()
            rs = pmap(run, ids, args.concurrency)
            wall = time.monotonic() - t0
            ok = [r for r in rs if r["code"] == 200 and r["rc"] == 0]
            rec = {"n": len(ids), "ok": len(ok), "wall_s": round(wall, 2),
                   "latency": summarize([r["s"] for r in ok]),
                   "errors": sorted({(str(r["error"]) + "|" + r["stderr"])[:200] for r in rs if r not in ok})[:5],
                   "results": rs}
            if args.cmd == "cpubench":
                iters = []
                for r in ok:
                    try:
                        n, secs = r["stdout"].split()[:2]
                        iters.append(int(n) / float(secs))
                    except ValueError:
                        pass
                rec["iters_per_s"] = summarize(iters)
                rec["iters_per_s_total"] = round(sum(iters), 1)
            emit(rec)
            return 0

        if args.cmd == "destroy":
            def kill(sid):
                code, res = agent.call("POST", "/vm/destroy", {"id": sid}, timeout=args.timeout)
                return {"id": sid, "code": code, "error": res.get("error")}
            rs = pmap(kill, ids, args.concurrency)
            emit({"n": len(ids), "ok": sum(1 for r in rs if r["code"] in (200, 404)),
                  "errors": sorted({str(r["error"])[:200] for r in rs if r["code"] not in (200, 404)})[:5]})
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
