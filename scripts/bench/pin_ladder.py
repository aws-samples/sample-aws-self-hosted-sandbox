#!/usr/bin/env python3
"""EKS 完整链路下的 vCPU 绑核阶梯压测(报告 §12/§13)。在目标节点的 runner Pod 内运行(BENCH_LOCAL=1)。

  ladder   按 --steps 逐档经 node-agent /vm/create 加沙盒(jailer + tap 网络 + vsock),每档观察 --hold 秒后
           统计:FAIL_ENTRY、存活 VMM unit 数、vCPU 线程绑核情况(单核比例/每 CPU 负载)、抽样 vsock exec
           延迟、宿主内存、node-agent /health 的 vcpu_* 字段
  resume   对已有沙盒中抽样 --count 个做 suspend → resume,检查恢复后仍是单核绑定
  adopt    记录当前绑核表 → 等外部删除本节点 node-agent Pod → 新 Pod 就绪后比对 /health 的 vcpu_committed
  cleanup  销毁 --prefix 开头的全部沙盒

每条结果追加一行 JSON 到 --out。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import overcommit_bench as ob  # noqa: E402
import spot_bench as sb  # noqa: E402

VCPU_RE = re.compile(r"fc_vcpu (\d+)")


def vcpu_affinity() -> dict:
    """扫宿主 /proc(runner 为 hostPID):所有 firecracker 进程的 fc_vcpu 线程及其 Cpus_allowed_list。"""
    per_cpu: Counter = Counter()
    threads = single = 0
    for comm_path in glob.glob("/proc/[0-9]*/comm"):
        try:
            if open(comm_path).read().strip() != "firecracker":
                continue
        except OSError:
            continue
        pid_dir = os.path.dirname(comm_path)
        for task in glob.glob(f"{pid_dir}/task/*"):
            try:
                if not VCPU_RE.fullmatch(open(f"{task}/comm").read().strip()):
                    continue
                allowed = next(l.split(":", 1)[1].strip() for l in open(f"{task}/status")
                               if l.startswith("Cpus_allowed_list"))
            except (OSError, StopIteration):
                continue
            threads += 1
            if re.fullmatch(r"\d+", allowed):
                single += 1
                per_cpu[int(allowed)] += 1
    loads = list(per_cpu.values()) or [0]
    return {"vcpu_threads": threads, "single_cpu_pinned": single,
            "cpus_used": len(per_cpu), "per_cpu_min": min(loads), "per_cpu_max": max(loads)}


def fail_entries(prefix: str) -> int:
    n = 0
    for log in glob.glob(f"/var/lib/sbx/{prefix}-*/vm*.log"):
        try:
            if "KVM_EXIT_FAIL_ENTRY" in open(log, errors="replace").read():
                n += 1
        except OSError:
            pass
    return n


def active_units(prefix: str) -> int:
    out = sb.pod_exec("local", sb.host(
        f"systemctl list-units --no-legend --state=active 'sbx-vmm-{prefix}-*' | wc -l"))
    return int(out.strip().splitlines()[-1] or 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("ladder", "resume", "adopt", "cleanup"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--prefix", default="pin")
    ap.add_argument("--steps", default="256,512,768,1024,1280,1536,1920")
    ap.add_argument("--start", type=int, default=1, help="首个沙盒编号(也是 tap_idx)")
    ap.add_argument("--cpu", type=int, default=1)
    ap.add_argument("--mem", type=int, default=2048)
    ap.add_argument("--hold", type=float, default=120)
    ap.add_argument("--probe", type=int, default=64, help="每档抽样 exec 的沙盒数")
    ap.add_argument("--count", type=int, default=16)
    ap.add_argument("--conc", type=int, default=16)
    a = ap.parse_args()
    out = open(a.out, "a")

    def emit(rec):
        rec = {"ts": ob.now(), "tag": a.tag, "cmd": a.cmd, **rec}
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()
        print(json.dumps(rec, ensure_ascii=False), flush=True)

    with ob.port_forward("local") as url:
        agent = ob.Agent(url, ob.auth_secret())

        def health():
            code, h = agent.call("GET", "/health", None, timeout=30)
            return {k: h.get(k) for k in ("vm_count", "free_mem_mib", "vcpu_committed",
                                           "vcpu_capacity", "vcpu_free", "vcpu_pinning")} if code == 200 else {"code": code}

        def exec_probe(ids):
            def one(sid):
                t0 = time.monotonic()
                code, res = agent.call("POST", "/vm/exec", {"id": sid, "cmd": "echo ok", "timeout": 20}, 60)
                return (time.monotonic() - t0) * 1000, code == 200 and "ok" in (res.get("stdout") or "")
            res = ob.pmap(one, ids, 32)
            lat = [r[0] for r in res if r[1]]
            return {"probed": len(ids), "ok": len(lat), **({"lat_ms": ob.summarize(lat)} if lat else {})}

        def ids_upto(n):
            return [f"{a.prefix}-{i:05d}" for i in range(a.start, a.start + n)]

        if a.cmd == "ladder":
            n = 0
            for target in [int(x) for x in a.steps.split(",")]:
                todo = list(range(a.start + n, a.start + target))
                t0 = time.monotonic()

                def mk(i):
                    code, res = agent.call("POST", "/vm/create", {
                        "id": f"{a.prefix}-{i:05d}", "tap_idx": i, "cpu": a.cpu, "mem_mib": a.mem,
                        "kernel": "/opt/sbx/vmlinux"}, timeout=300)
                    return code, (res.get("reason") or res.get("error") or "")[:120]
                created = ob.pmap(mk, todo, a.conc)
                create_s = round(time.monotonic() - t0, 1)
                errors = Counter(f"{c}:{e}" for c, e in created if c != 200)
                n = target
                time.sleep(a.hold)
                ids = ids_upto(n)
                rec = {"launched": n, "guest_vcpus": n * a.cpu, "create_s": create_s,
                       "create_errors": dict(errors.most_common(5)),
                       "active_units": active_units(a.prefix), "fail_entry": fail_entries(a.prefix),
                       **vcpu_affinity(), "exec": exec_probe(random.sample(ids, min(a.probe, len(ids)))),
                       "health": health(),
                       "host": {k: v for k, v in ob.sample("local", 3).items()
                                if k in ("mem_available_mib", "anon_mib", "fc_count", "fc_rss_mib", "load1")}}
                emit(rec)
                if errors and sum(errors.values()) > len(todo) // 2:
                    emit({"stop": "more than half of the step failed to create"})
                    break

        elif a.cmd == "resume":
            ids = [s for s in ids_upto(10 ** 5) if os.path.isdir(f"/var/lib/sbx/{s}")]
            pick = random.sample(ids, min(a.count, len(ids)))
            ok = []
            for sid in pick:
                i = int(sid.rsplit("-", 1)[1])
                c1, r1 = agent.call("POST", "/vm/suspend", {"id": sid, "snapshot_local_path": f"/var/lib/sbx/{sid}/snap"}, 300)
                c2, r2 = agent.call("POST", "/vm/resume", {"id": sid, "snapshot_local_path": f"/var/lib/sbx/{sid}/snap",
                                                           "rootfs_path": f"/var/lib/sbx/{sid}/rootfs.ext4", "tap_idx": i}, 300)
                ok.append({"id": sid, "suspend": c1, "resume": c2, "err": (r1.get("error") or r2.get("error") or "")[:160]})
            time.sleep(10)
            emit({"resumed": sum(1 for o in ok if o["resume"] == 200), "n": len(pick),
                  "errors": [o for o in ok if o["resume"] != 200][:5], **vcpu_affinity(),
                  "exec": exec_probe(pick), "health": health()})

        elif a.cmd == "adopt":
            # 记录当前状态后,等外部删除本节点 node-agent Pod(本脚本在 runner 内,没有 kubectl):
            # 先等 /health 不可达,再等新 Pod 就绪
            before = {"health": health(), **vcpu_affinity()}
            emit({"phase": "waiting for node-agent restart", "before": before})
            for _ in range(600):
                if agent.call("GET", "/health", None, timeout=5)[0] != 200:
                    break
                time.sleep(1)
            for _ in range(120):
                time.sleep(5)
                if agent.call("GET", "/health", None, timeout=10)[0] == 200:
                    break
            time.sleep(10)
            after = {"health": health(), **vcpu_affinity()}
            emit({"before": before, "after": after})

        else:  # cleanup
            ids = sorted(os.path.basename(p) for p in glob.glob(f"/var/lib/sbx/{a.prefix}-*"))
            res = ob.pmap(lambda s: agent.call("POST", "/vm/destroy", {"id": s}, timeout=120)[0], ids, 32)
            emit({"destroyed": sum(1 for c in res if c == 200), "n": len(ids)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
