#!/usr/bin/env python3
"""Spot 快照加速实验(在目标节点的 runner Pod 内运行,BENCH_LOCAL=1)。

periodic  周期性原地 checkpoint:N 个沙盒写满 --dirty-mib 后以 --rewrite-mbps 持续随机改写,
          开/关 --period 秒的后台 checkpoint,稳态 --observe 秒后触发真实 Spot checkpoint 路径。
template  内存模板共享 base:每个沙盒 = --context-mib 共享 context + --private-mib 私有脏页;
          --source cold(冷启动 + 各自 Full base,base 之后才加载 context)或
          --source template(从模板拉起,context 已在模板里);
          --no-base 时冷启动沙盒不打 base(Spot 在 base 之前到来的最坏情况)。

每次运行结果追加一行 JSON 到 --out;触发前冻结全部 memtouch 并记录 sha256,事后抽样 resume 比对。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import overcommit_bench as ob  # noqa: E402
import spot_bench as sb  # noqa: E402

PY = "/usr/local/bin/python3"


def sig_cmd(name: str, sig: str) -> str:
    return (f"{PY} -c \"import json,os,signal;os.kill(json.load(open('/run/{name}.ready'))['pid'],"
            f"signal.{sig})\"")


def dev_written_bytes(dev: str) -> int:
    """/proc/diskstats 第 10 列 = 写入扇区数(512B)。dev 例 nvme2n1。"""
    for line in open("/proc/diskstats"):
        f = line.split()
        if f[2] == dev:
            return int(f[9]) * 512
    return 0


def device_of(path: str) -> str:
    out = sb.pod_exec("local", sb.host(f"findmnt -n -o SOURCE --target {path}"))
    src = out.strip().splitlines()[-1] if out.strip() else ""
    if src.startswith("/dev/"):
        return os.path.basename(os.path.realpath(src))
    # ZFS dataset → pool 设备
    pool = src.split("/")[0]
    if pool:
        out = sb.pod_exec("local", sb.host(f"zpool list -v -H -P {pool}"))
        for line in out.splitlines()[1:]:
            field = line.split()[0] if line.split() else ""
            if field.startswith("/dev/"):
                # 分区(nvme2n1p1)的写入同样计入整盘,取整盘设备名便于和 EBS 指标对照
                return os.path.basename(os.path.realpath(field)).rstrip("0123456789").rstrip("p") \
                    if "nvme" in field and "p" in os.path.basename(field) else os.path.basename(os.path.realpath(field))
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("periodic", "template"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--count", type=int, required=True)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--mem", type=int, default=2048)
    ap.add_argument("--layout", default="", help='如 "zfs:120";默认 xfs')
    ap.add_argument("--verify", type=int, default=3)
    # periodic
    ap.add_argument("--dirty-mib", type=int, default=1200)
    ap.add_argument("--rewrite-mbps", type=float, default=5)
    ap.add_argument("--hot-pct", type=float, default=100, help="改写集中的热区比例(%%),100 = 整块均匀随机")
    ap.add_argument("--period", type=float, default=0, help="checkpoint 周期(秒),0 = 不做")
    ap.add_argument("--observe", type=float, default=90)
    ap.add_argument("--ckpt-concurrency", type=int, default=4, help="后台 checkpoint 并发数")
    # template
    ap.add_argument("--source", choices=("cold", "template"), default="cold")
    ap.add_argument("--template", default="agentctx")
    ap.add_argument("--context-mib", type=int, default=800)
    ap.add_argument("--private-mib", type=int, default=400)
    ap.add_argument("--no-base", action="store_true")
    a = ap.parse_args()

    layout = sb.parse_layout(a.layout or f"xfs:{a.count}", a.count)
    idxs = list(range(a.start, a.start + a.count))
    sids = [f"{a.prefix}-{i:04d}" for i in idxs]
    out = open(a.out, "a")
    rec: dict = {"ts": ob.now(), "mode": a.mode, "tag": a.tag, "count": a.count,
                 "layout": a.layout or f"xfs:{a.count}", "args": vars(a)}

    def log(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    mounts = [f"zfs create -p -o mountpoint=/var/lib/sbx/{s} sbxpool/sbx/{s}"
              for s, k in zip(sids, layout) if k == "zfs"]
    if mounts:
        sb.pod_exec("local", sb.host(" && ".join(mounts)))
    state_dev = device_of(f"/var/lib/sbx/{sids[0]}" if mounts else "/var/lib/sbx")
    rec["state_device"] = state_dev
    b64 = base64.b64encode((HERE / "memtouch.py").read_bytes()).decode()

    with ob.port_forward("local") as url:
        agent = ob.Agent(url, ob.auth_secret())

        def call(path, body, timeout=300):
            for attempt in range(3):  # 连接级错误(code=-1)重试;业务错误不重试
                code, res = agent.call("POST", path, body, timeout=timeout)
                if code != -1:
                    break
                time.sleep(2)
            if code != 200:
                raise RuntimeError(f"{path} {body.get('id')} -> {code} {res}")
            return res

        def ex(sid, cmd, timeout=60):
            return call("/vm/exec", {"id": sid, "cmd": cmd, "timeout": timeout}, timeout + 60)

        def launch_memtouch(sid, name, mb, extra=""):
            ex(sid, f"test -f /root/memtouch.py || echo {b64} | base64 -d > /root/memtouch.py; "
                    f"setsid nohup {PY} /root/memtouch.py --name {name} --mb {mb} --pattern mixed "
                    f"--random-pct 20 {extra} > /run/{name}.log 2>&1 < /dev/null & sleep 0.1; echo ok", 30)

        def wait_ready(name, ids, budget=900):
            pending, t0 = set(ids), time.monotonic()
            while pending and time.monotonic() - t0 < budget:
                time.sleep(5)
                done = ob.pmap(lambda s: (s, agent.call("POST", "/vm/exec", {
                    "id": s, "cmd": f"test -f /run/{name}.ready && echo R", "timeout": 10}, 60)[1].get("stdout", "")),
                    sorted(pending), 32)
                pending -= {s for s, o in done if "R" in o}
            if pending:
                raise RuntimeError(f"{name} not ready on {len(pending)}")
            return round(time.monotonic() - t0, 1)

        try:
            # ---------- 建沙盒 ----------
            t0 = time.monotonic()
            if a.mode == "template" and a.source == "template":
                created = ob.pmap(lambda i: call("/vm/create_from_template", {
                    "id": f"{a.prefix}-{i:04d}", "tap_idx": i, "template": a.template}), idxs, 16)
                rec["create_from_template"] = ob.summarize([c["create_s"] for c in created])
                rec["guest_net_reconfigured"] = sum(1 for c in created if c.get("guest_net_reconfigured"))
            else:
                ob.pmap(lambda i: call("/vm/create", {"id": f"{a.prefix}-{i:04d}", "tap_idx": i, "cpu": 1,
                                                      "mem_mib": a.mem, "kernel": "/opt/sbx/vmlinux"}), idxs, 16)
            rec["create_s"] = round(time.monotonic() - t0, 1)
            if not (a.mode == "template" and a.source == "template"):
                time.sleep(20)
                if not a.no_base:
                    w0 = dev_written_bytes(state_dev)
                    t0 = time.monotonic()
                    ob.pmap(lambda s: call("/vm/snapshot_base", {"id": s, "snapshot_local_path": f"/var/lib/sbx/{s}/snap"}, 900),
                            sids, 4)
                    rec["base_s"] = round(time.monotonic() - t0, 1)
                    rec["base_written_gib"] = round((dev_written_bytes(state_dev) - w0) / 2**30, 2)
            log(f"created {a.count}: create_s={rec['create_s']} base_s={rec.get('base_s')} base_written={rec.get('base_written_gib')}GiB")

            # ---------- 负载 ----------
            if a.mode == "periodic":
                ob.pmap(lambda s: launch_memtouch(s, "mt", a.dirty_mib, f"--rewrite-mbps {a.rewrite_mbps} --hot-pct {a.hot_pct}"), sids, 32)
                rec["fill_s"] = wait_ready("mt", sids)
                names = ["mt"]
            else:
                if a.source == "cold":
                    # context 在 base 之后加载(真实 agent:沙盒启动后才读入上下文)
                    ob.pmap(lambda s: launch_memtouch(s, "ctx", a.context_mib, "--seed 42"), sids, 32)
                    wait_ready("ctx", sids)
                ob.pmap(lambda s: launch_memtouch(s, "priv", a.private_mib), sids, 32)
                rec["fill_s"] = wait_ready("priv", sids)
                names = ["priv"]
            log(f"workload ready fill_s={rec['fill_s']}")
            rec["host_after_fill"] = {k: v for k, v in ob.sample("local", 3).items()
                                     if k in ("fc_count", "fc_rss_mib", "mem_available_mib", "cached_mib", "anon_mib")}

            # ---------- 周期 checkpoint(稳态)----------
            ck = []
            if a.mode == "periodic":
                stop = threading.Event()

                def scheduler(worker: int, workers: int):
                    # 错峰:VM 按 worker 分片,每个 VM 目标每 period 秒 checkpoint 一次
                    mine = sids[worker::workers]
                    gap = a.period / max(1, len(mine))
                    i = 0
                    while not stop.is_set():
                        s = mine[i % len(mine)]
                        t1 = time.monotonic()
                        try:
                            r = call("/vm/checkpoint", {"id": s}, 300)
                            ck.append({"id": s, "t": round(time.time(), 2), "wall_s": round(time.monotonic() - t1, 3),
                                       "paused_s": r.get("paused_s"), "ok": True})
                        except Exception as e:  # noqa: BLE001
                            ck.append({"id": s, "ok": False, "error": str(e)[:200]})
                        i += 1
                        rest = gap - (time.monotonic() - t1)
                        if rest > 0:
                            stop.wait(rest)

                th = None
                ths = []
                w0, t0 = dev_written_bytes(state_dev), time.monotonic()
                if a.period > 0:
                    ths = [threading.Thread(target=scheduler, args=(w, a.ckpt_concurrency), daemon=True)
                           for w in range(a.ckpt_concurrency)]
                    for th in ths:
                        th.start()
                    th = ths[0]
                    # 先让每个 VM 至少完成一轮(首轮要写入填充期的全部脏页),再观测稳态
                    while len([c for c in ck if c.get("ok")]) < len(sids) and time.monotonic() - t0 < 1800:
                        time.sleep(2)
                    rec["first_round_s"] = round(time.monotonic() - t0, 1)
                    rec["first_round_written_gib"] = round((dev_written_bytes(state_dev) - w0) / 2**30, 2)
                    w0, t0, n0 = dev_written_bytes(state_dev), time.monotonic(), len(ck)
                time.sleep(a.observe)
                rec["steady_state"] = {
                    "seconds": round(time.monotonic() - t0, 1),
                    "background_write_mib_s": round((dev_written_bytes(state_dev) - w0) / 2**20 / (time.monotonic() - t0), 1),
                }
                if th:
                    stop.set()
                    for t in ths:
                        t.join(timeout=300)
                    steady = [c for c in ck[n0:] if c.get("ok")]
                    rec["steady_state"]["checkpoints"] = len(steady)
                    rec["steady_state"]["paused_s"] = ob.summarize([c["paused_s"] for c in steady])
                    last = {}
                    for c in ck:
                        if c.get("ok"):
                            last[c["id"]] = c["t"]
                    now_t = time.time()
                    rec["checkpoint_age_at_trigger_s"] = ob.summarize([now_t - last.get(s, now_t - 9999) for s in sids])
                rec["checkpoint_errors"] = [c for c in ck if not c.get("ok")][:5]
                log(f"steady state: {rec['steady_state']} first_round_s={rec.get('first_round_s')}")

            # ---------- 冻结 + 触发 Spot checkpoint ----------
            ob.pmap(lambda s: ex(s, f"{sig_cmd(names[0], 'SIGUSR2')}; for i in $(seq 1 120); do test -f /run/{names[0]}.h1 && break; sleep 0.5; done", 120), sids, 32)
            h1 = {s: json.loads(ex(s, f"cat /run/{names[0]}.h1", 10)["stdout"])["sha256"] for s in sids[: a.verify]}
            sb.pod_exec("local", sb.host("sync"), timeout=900)
            agent.call("POST", "/reclaim/reset", {}, timeout=30)
            w0, t0 = dev_written_bytes(state_dev), time.monotonic()
            code, plan = agent.call("POST", "/reclaim/simulate", {"type": "spot-termination", "action": "terminate",
                                                                   "checkpoint_only": True}, timeout=200)
            rec["trigger_to_done_s"] = round(time.monotonic() - t0, 2)
            rec["spot_written_gib"] = round((dev_written_bytes(state_dev) - w0) / 2**30, 2)
            results = plan.get("results", [])
            rec["plan"] = {k: plan.get(k) for k in ("phase", "evacuated_ok", "failed", "wall_clock_s",
                                                    "total_actual_bytes", "snapshot_concurrency")}
            rec["per_vm_s"] = ob.summarize([r["elapsed_s"] for r in results if r.get("ok")])
            rec["errors"] = sorted({r.get("error", "")[:160] for r in results if not r.get("ok")})[:5]
            rec["snapshot_types"] = sorted({r.get("snapshot_type", "") for r in results if r.get("ok")})
            log(f"SPOT: ok={rec['plan']['evacuated_ok']}/{a.count} wall={rec['plan']['wall_clock_s']}s "
                f"written={rec['spot_written_gib']}GiB per_vm={rec['per_vm_s']} types={rec['snapshot_types']} errors={rec['errors']}")
            agent.call("POST", "/reclaim/reset", {}, timeout=30)

            # ---------- 抽样恢复校验 ----------
            verify = []
            for s in sids[: a.verify]:
                i = int(s.rsplit("-", 1)[1])
                code, res = agent.call("POST", "/vm/resume", {"id": s, "snapshot_local_path": f"/var/lib/sbx/{s}/snap",
                                                              "rootfs_path": f"/var/lib/sbx/{s}/rootfs.ext4", "tap_idx": i}, 300)
                h2 = None
                if code == 200:
                    ex(s, f"rm -f /run/{names[0]}.h2; {sig_cmd(names[0], 'SIGUSR1')}; for i in $(seq 1 120); do test -f /run/{names[0]}.h2 && break; sleep 0.5; done", 90)
                    try:
                        h2 = json.loads(ex(s, f"cat /run/{names[0]}.h2", 10)["stdout"])["sha256"]
                    except Exception:  # noqa: BLE001
                        h2 = None
                verify.append({"id": s, "resume_code": code, "match": h2 == h1.get(s), "error": res.get("error")})
            rec["verify"] = verify
            log(f"verify: {[v['match'] for v in verify]} {[v['error'] for v in verify if v['error']]}")
        except Exception as exc:  # noqa: BLE001
            rec["error"] = f"{type(exc).__name__}: {exc}"[:1000]
            log(f"ERROR {rec['error']}")
        finally:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            ob.pmap(lambda s: agent.call("POST", "/vm/destroy", {"id": s}, timeout=120), sids, 32)
    cleanup = [(f"zfs destroy -f sbxpool/sbx/{s}; rmdir /var/lib/sbx/{s}" if k == "zfs" else f"rm -rf /var/lib/sbx/{s}")
               for s, k in zip(sids, layout)]
    sb.pod_exec("local", sb.host("; ".join(cleanup) + "; true"), timeout=900)
    return 0 if "error" not in rec else 1


if __name__ == "__main__":
    sys.exit(main())
