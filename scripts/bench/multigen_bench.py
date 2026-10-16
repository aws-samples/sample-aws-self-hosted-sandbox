#!/usr/bin/env python3
"""多代快照正确性:create → base → [写新内容 → 冻结记 h1 → suspend → resume → 比对 h2] × G 代。

验证 resume 后"完整镜像转正为新 base"、下一代 Diff 仍正确(ZFS inplace / XFS sparse 都适用)。
每代在 guest 里起一个新的 memtouch(新随机种子、新内存区域),并让旧 memtouch 退出,
所以每一代的内存内容都和上一代不同。--layout zfs 时沙盒目录为独立 ZFS dataset。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import overcommit_bench as ob  # noqa: E402
import spot_bench as sb  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--prefix", default="mg")
    ap.add_argument("--count", type=int, default=2)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--generations", type=int, default=3)
    ap.add_argument("--dirty-mib", type=int, default=512)
    ap.add_argument("--layout", choices=("xfs", "zfs"), default="zfs")
    ap.add_argument("--zfs-parent", default="sbxpool/sbx")
    a = ap.parse_args()

    idxs = list(range(a.start, a.start + a.count))
    sids = [f"{a.prefix}-{i:04d}" for i in idxs]
    pod = ob.node_agent_pod(a.node)
    out = open(a.out, "a")
    if a.layout == "zfs":
        sb.pod_exec(pod, sb.host(" && ".join(
            f"zfs create -p -o mountpoint=/var/lib/sbx/{s} {a.zfs_parent}/{s}" for s in sids)))
    b64 = base64.b64encode((HERE / "memtouch.py").read_bytes()).decode()
    results = []
    with ob.port_forward(pod) as url:
        agent = ob.Agent(url, ob.auth_secret())

        def call(path, body, timeout=300):
            code, res = agent.call("POST", path, body, timeout=timeout)
            if code != 200:
                raise RuntimeError(f"{path} {body.get('id')} -> {code} {res}")
            return res

        def ex(sid, cmd, timeout=120):
            return call("/vm/exec", {"id": sid, "cmd": cmd, "timeout": timeout}, timeout + 60)

        try:
            for i, s in zip(idxs, sids):
                call("/vm/create", {"id": s, "tap_idx": i, "cpu": 1, "mem_mib": 2048, "kernel": "/opt/sbx/vmlinux"})
            time.sleep(20)
            for s in sids:
                call("/vm/snapshot_base", {"id": s, "snapshot_local_path": f"/var/lib/sbx/{s}/snap"}, 600)
                ex(s, f"echo {b64} | base64 -d > /root/memtouch.py; echo ok")
            for g in range(1, a.generations + 1):
                for s in sids:
                    # 结束上一代 memtouch(释放其内存),起新一代并等填充完成
                    ex(s, f"{sb.PY} -c \"import json,os,signal;os.kill(json.load(open('/run/memtouch.ready'))['pid'],signal.SIGKILL)\" 2>/dev/null; "
                          "rm -f /run/memtouch.ready /run/memtouch.h1 /run/memtouch.h2; "
                          f"setsid nohup {sb.PY} /root/memtouch.py --mb {a.dirty_mib} --pattern mixed --random-pct 20 "
                          "> /run/mt.log 2>&1 < /dev/null & sleep 0.1; echo ok")
                for s in sids:
                    for _ in range(120):
                        if "R" in ex(s, "test -f /run/memtouch.ready && echo R", 10).get("stdout", ""):
                            break
                        time.sleep(2)
                    ex(s, f"{sb.SIGNAL_USR2}; for i in $(seq 1 100); do test -f /run/memtouch.h1 && break; sleep 0.5; done")
                h1 = {s: json.loads(ex(s, "cat /run/memtouch.h1", 10)["stdout"])["sha256"] for s in sids}
                for i, s in zip(idxs, sids):
                    t0 = time.monotonic()
                    info = call("/vm/suspend", {"id": s, "snapshot_local_path": f"/var/lib/sbx/{s}/snap"}, 600)
                    ts = time.monotonic() - t0
                    t1 = time.monotonic()
                    res = call("/vm/resume", {"id": s, "snapshot_local_path": f"/var/lib/sbx/{s}/snap",
                                              "rootfs_path": f"/var/lib/sbx/{s}/rootfs.ext4", "tap_idx": i}, 600)
                    tr = time.monotonic() - t1
                    ex(s, f"rm -f /run/memtouch.h2; {sb.SIGNAL_USR1}; for i in $(seq 1 120); do test -f /run/memtouch.h2 && break; sleep 0.5; done", 90)
                    h2 = json.loads(ex(s, "cat /run/memtouch.h2", 10)["stdout"])["sha256"]
                    beat = ex(s, "cat /run/heartbeat", 10)["stdout"].strip()
                    r = {"gen": g, "id": s, "layout": a.layout, "snapshot_type": info.get("snapshot_type"),
                         "suspend_s": round(ts, 2), "resume_s": round(tr, 2), "match": h1[s] == h2, "heartbeat": beat}
                    results.append(r)
                    print(json.dumps(r), flush=True)
        finally:
            out.write(json.dumps({"ts": ob.now(), "layout": a.layout, "results": results}) + "\n")
            for s in sids:
                agent.call("POST", "/vm/destroy", {"id": s}, timeout=120)
    if a.layout == "zfs":
        sb.pod_exec(pod, sb.host("; ".join(
            f"zfs destroy -f {a.zfs_parent}/{s}; rmdir /var/lib/sbx/{s}" for s in sids) + "; true"))
    else:
        sb.pod_exec(pod, sb.host("; ".join(f"rm -rf /var/lib/sbx/{s}" for s in sids) + "; true"))
    return 0 if results and all(r["match"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
