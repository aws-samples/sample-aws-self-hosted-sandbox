#!/usr/bin/env python3
"""Spot 回收窗口快照真机压测:单节点上 N 个沙盒写入指定内容的脏内存,触发 node-agent
真实的并发 checkpoint 路径(/reclaim/simulate checkpoint_only),测 120s 内能保存多少。

存储布局(--layout,可混合 "xfs:40,fsx:40"):
  xfs  默认:沙盒目录在状态 EBS(XFS reflink)上
  zfs  每个沙盒一个 ZFS dataset,mountpoint=/var/lib/sbx/<sid>(继承 pool 压缩设置)
  fsx  FSx for Lustre 子目录 bind mount 到 /var/lib/sbx/<sid>(走网卡,不占 EBS 带宽)
挂载在宿主 mount ns 内完成(node-agent Pod 特权 + hostPID,nsenter -t 1 -m),
经 /var/lib/sbx 的 Bidirectional 传播对 node-agent 可见。

完整性:触发前对每个 guest 的 memtouch 发 SIGUSR2 记录 sha256(h1);
checkpoint 后对 --verify 个沙盒 resume,发 SIGUSR1 重算(h2),h1==h2 才算保真。
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import overcommit_bench as ob  # noqa: E402

PY = "/usr/local/bin/python3"
# guest(python:3.12-slim)没有 procps/pkill:按 memtouch.ready 里的 pid 发信号
_SIG = PY + " -c \"import json,os,signal;os.kill(json.load(open('/run/memtouch.ready'))['pid'],signal.{})\""
SIGNAL_USR1 = _SIG.format("SIGUSR1")
SIGNAL_USR2 = _SIG.format("SIGUSR2")


def pod_exec(pod: str, script: str, timeout: int = 600) -> str:
    r = subprocess.run(["kubectl", "-n", ob.NS, "exec", "-i", pod, "--", "bash", "-s"],
                       input=script, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"pod exec failed: {r.stderr[-800:]}")
    return r.stdout


def host(cmd: str) -> str:
    """在宿主 mount/pid ns 执行(经 node-agent Pod nsenter)。"""
    return f"nsenter -t 1 -m -u -i -n -p -- bash -c {json.dumps(cmd)}"


def parse_layout(spec: str, count: int) -> list[str]:
    parts = []
    for item in spec.split(","):
        name, _, n = item.partition(":")
        parts += [name] * (int(n) if n else count)
    if len(parts) != count:
        raise SystemExit(f"layout {spec} covers {len(parts)} sandboxes, --count is {count}")
    return parts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--prefix", default="sp")
    ap.add_argument("--count", type=int, required=True)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--mem", type=int, default=2048)
    ap.add_argument("--dirty-mib", type=int, default=1200)
    ap.add_argument("--pattern", choices=("zero", "mixed", "random"), default="mixed")
    ap.add_argument("--random-pct", type=float, default=20)
    ap.add_argument("--layout", default="", help='默认全部 xfs;例 "zfs:60" 或 "xfs:40,fsx:40"')
    ap.add_argument("--zfs-parent", default="sbxpool/sbx")
    ap.add_argument("--fsx-root", default="/mnt/fsx/sbx")
    ap.add_argument("--base-concurrency", type=int, default=4)
    ap.add_argument("--verify", type=int, default=3)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    layout = parse_layout(a.layout or f"xfs:{a.count}", a.count)
    idxs = list(range(a.start, a.start + a.count))
    sids = [f"{a.prefix}-{i:04d}" for i in idxs]
    pod = ob.node_agent_pod(a.node)
    out = open(a.out, "a")
    rec: dict = {"ts": ob.now(), "tag": a.tag, "node": a.node, "count": a.count, "mem_mib": a.mem,
                 "dirty_mib": a.dirty_mib, "pattern": a.pattern, "random_pct": a.random_pct,
                 "layout": a.layout or f"xfs:{a.count}"}

    def log(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    # 1) 存储布局:为非 xfs 沙盒预建挂载点
    mounts = []
    for sid, kind in zip(sids, layout):
        d = f"/var/lib/sbx/{sid}"
        if kind == "zfs":
            mounts.append(f"zfs create -p -o mountpoint={d} {a.zfs_parent}/{sid}")
        elif kind == "fsx":
            mounts.append(f"mkdir -p {a.fsx_root}/{sid} {d} && mount --bind {a.fsx_root}/{sid} {d}")
    if mounts:
        pod_exec(pod, host(" && ".join(mounts)))
        log(f"prepared {len(mounts)} non-xfs sandbox dirs")

    with ob.port_forward(pod) as url:
        agent = ob.Agent(url, ob.auth_secret())

        def call(path, body, timeout=300):
            code, res = agent.call("POST", path, body, timeout=timeout)
            if code != 200:
                raise RuntimeError(f"{path} {body.get('id')} -> {code} {res}")
            return res

        try:
            # 2) 创建
            t0 = time.monotonic()
            ob.pmap(lambda i: call("/vm/create", {"id": f"{a.prefix}-{i:04d}", "tap_idx": i, "cpu": 1,
                                                  "mem_mib": a.mem, "kernel": "/opt/sbx/vmlinux"}),
                    idxs, 16)
            rec["create_s"] = round(time.monotonic() - t0, 1)
            time.sleep(20)  # 与控制面 BASE_SNAPSHOT_DELAY_S 一致:等 guest 启动稳定
            # 3) base 快照(Full),记录 base 文件真实占用
            t0 = time.monotonic()
            bases = ob.pmap(lambda s: call("/vm/snapshot_base", {"id": s, "snapshot_local_path": f"/var/lib/sbx/{s}/snap"}, 900),
                            sids, a.base_concurrency)
            rec["base_s"] = round(time.monotonic() - t0, 1)
            rec["base_each_s"] = ob.summarize([b.get("base_snapshot_time_s", 0) for b in bases])
            log(f"created+base {a.count}: create={rec['create_s']}s base={rec['base_s']}s")

            # 4) 推 memtouch、写脏内存、等填充完成并冻结(记录 h1)
            b64 = base64.b64encode((HERE / "memtouch.py").read_bytes()).decode()
            ob.pmap(lambda s: call("/vm/exec", {"id": s, "timeout": 30, "cmd":
                    # 注意用 ";" 而不是 "&&":"A && B &" 会把 A&&B 整体放进后台子 shell,
                    # 子 shell 持有 exec 的 stdout 管道,guest 单线程 vsock agent 会一直阻塞。
                    f"echo {b64} | base64 -d > /root/memtouch.py; setsid nohup {PY} /root/memtouch.py "
                    f"--mb {a.dirty_mib} --pattern {a.pattern} --random-pct {a.random_pct} "
                    f"> /run/mt.log 2>&1 < /dev/null & sleep 0.1; echo ok"}), sids, 32)
            t0 = time.monotonic()
            pending = set(sids)
            while pending and time.monotonic() - t0 < 900:
                time.sleep(10)
                done = ob.pmap(lambda s: (s, agent.call("POST", "/vm/exec", {"id": s, "cmd": "test -f /run/memtouch.ready && echo R", "timeout": 10}, 60)[1].get("stdout", "")),
                               sorted(pending), 32)
                pending -= {s for s, o in done if "R" in o}
            if pending:
                raise RuntimeError(f"memtouch not ready on {len(pending)} sandboxes")
            rec["fill_s"] = round(time.monotonic() - t0, 1)
            ob.pmap(lambda s: call("/vm/exec", {"id": s, "timeout": 120, "cmd":
                    f"{SIGNAL_USR2}; for i in $(seq 1 100); do test -f /run/memtouch.h1 && break; sleep 0.5; done; cat /run/memtouch.h1"}),
                    sids, 32)
            h1 = {s: json.loads(call("/vm/exec", {"id": s, "cmd": "cat /run/memtouch.h1", "timeout": 10})["stdout"])["sha256"]
                  for s in sids[: a.verify]}
            log(f"dirty fill done in {rec['fill_s']}s; h1 captured for {len(h1)}")

            # 5) 宿主回写静默(排除填充期 page cache 回写干扰)
            pod_exec(pod, host("sync"), timeout=900)
            rec["pre_sample"] = ob.sample(pod, 3)

            # 6) 触发前确认节点上只有本次测试的 VM:/reclaim/simulate 会 checkpoint
            #    本机全部 running VM,混入别人的沙盒既干扰对方也污染数据。
            foreign = pod_exec(pod, "ls /var/lib/sbx | grep -v -E '^(\\.|" + a.prefix + "-)' || true").split()
            if foreign:
                raise RuntimeError(f"refusing to trigger: foreign sandboxes on node: {foreign[:10]}")
            # 7) 触发真实 checkpoint 路径
            agent.call("POST", "/reclaim/reset", {}, timeout=30)
            import threading
            samples = []
            stop = threading.Event()

            def sampler():
                while not stop.is_set():
                    samples.append(ob.sample(pod, 5))

            th = threading.Thread(target=sampler, daemon=True)
            th.start()
            t0 = time.monotonic()
            code, plan = agent.call("POST", "/reclaim/simulate",
                                    {"type": "spot-termination", "action": "terminate", "checkpoint_only": True},
                                    timeout=200)
            rec["trigger_to_done_s"] = round(time.monotonic() - t0, 2)
            stop.set()
            th.join(timeout=30)
            rec["during_samples"] = samples
            results = plan.get("results", [])
            rec["plan"] = {k: plan.get(k) for k in ("phase", "evacuated_ok", "failed", "wall_clock_s",
                                                    "total_actual_bytes", "effective_write_mib_s",
                                                    "snapshot_concurrency", "checkpoint_deadline")}
            rec["per_vm_s"] = ob.summarize([r["elapsed_s"] for r in results if r.get("ok")])
            rec["errors"] = sorted({r.get("error", "")[:160] for r in results if not r.get("ok")})[:5]

            # 7) 逻辑数据量(SEEK_DATA 扫描)与实际占用
            scan = pod_exec(pod, f"""python3 - <<'EOF'
import os, json
logical = alloc = 0
for s in {json.dumps(sids)}:
    p = f"/var/lib/sbx/{{s}}/snap/vm.mem"
    try:
        fd = os.open(p, os.O_RDONLY); size = os.fstat(fd).st_size; off = 0
        while off < size:
            try: ds = os.lseek(fd, off, os.SEEK_DATA)
            except OSError: break
            de = os.lseek(fd, ds, os.SEEK_HOLE); logical += de - ds; off = de
        alloc += os.fstat(fd).st_blocks * 512; os.close(fd)
    except OSError: pass
print(json.dumps({{"diff_logical_bytes": logical, "diff_allocated_bytes": alloc}}))
EOF""")
            rec.update(json.loads(scan.strip().splitlines()[-1]))
            ok, wall = rec["plan"]["evacuated_ok"] or 0, rec["plan"]["wall_clock_s"] or 0
            rec["logical_mib_s"] = round(rec["diff_logical_bytes"] / 2**20 / wall, 1) if wall else None
            log(f"checkpoint: ok={ok}/{a.count} wall={wall}s logical={rec['diff_logical_bytes']/2**30:.1f}GiB "
                f"alloc={rec['diff_allocated_bytes']/2**30:.1f}GiB logical_rate={rec['logical_mib_s']}MiB/s "
                f"per_vm={rec['per_vm_s']} errors={rec['errors']}")
            agent.call("POST", "/reclaim/reset", {}, timeout=30)

            # 8) 抽样恢复 + 内存校验
            verify = []
            for s in sids[: a.verify]:
                i = int(s.rsplit("-", 1)[1])
                t1 = time.monotonic()
                code, res = agent.call("POST", "/vm/resume", {"id": s, "snapshot_local_path": f"/var/lib/sbx/{s}/snap",
                                                              "rootfs_path": f"/var/lib/sbx/{s}/rootfs.ext4",
                                                              "tap_idx": i}, timeout=300)
                rt = round(time.monotonic() - t1, 2)
                h2 = None
                if code == 200:
                    agent.call("POST", "/vm/exec", {"id": s, "cmd": f"rm -f /run/memtouch.h2; {SIGNAL_USR1}; for i in $(seq 1 120); do test -f /run/memtouch.h2 && break; sleep 0.5; done", "timeout": 90}, 150)
                    c2, r2 = agent.call("POST", "/vm/exec", {"id": s, "cmd": "cat /run/memtouch.h2", "timeout": 10})
                    try:
                        h2 = json.loads(r2.get("stdout", ""))["sha256"]
                    except Exception:
                        h2 = None
                verify.append({"id": s, "resume_code": code, "resume_s": rt, "restore_time_s": res.get("restore_time_s"),
                               "match": h2 == h1.get(s), "error": res.get("error")})
            rec["verify"] = verify
            log(f"verify: {verify}")
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"[:1000]
            log(f"ERROR {rec['error']}")
        finally:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            if not a.keep:
                ob.pmap(lambda s: agent.call("POST", "/vm/destroy", {"id": s}, timeout=120), sids, 32)
    if not a.keep:
        cleanup = []
        for sid, kind in zip(sids, layout):
            d = f"/var/lib/sbx/{sid}"
            if kind == "zfs":
                cleanup.append(f"zfs destroy -f {a.zfs_parent}/{sid} 2>/dev/null; rmdir {d} 2>/dev/null")
            elif kind == "fsx":
                cleanup.append(f"umount {d} 2>/dev/null; rm -rf {a.fsx_root}/{sid}; rmdir {d} 2>/dev/null")
            else:
                cleanup.append(f"rm -rf {d}")
        pod_exec(pod, host("; ".join(cleanup) + "; true"), timeout=900)
    return 0 if "error" not in rec else 1


if __name__ == "__main__":
    sys.exit(main())
