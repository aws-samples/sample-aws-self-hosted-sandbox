#!/usr/bin/env python3
"""virtio-balloon 内存回收真机测试(在目标 sandbox 节点的 bench-runner Pod 内运行)。

用法(runner Pod 内,BENCH_LOCAL=1;见 scripts/bench/runner.yaml):
  python3 balloon_bench.py cache    --out /bench/out/b1-cache.jsonl
  python3 balloon_bench.py pressure --out /bench/out/b2-pressure-on.jsonl --count 8 --tag on
  python3 balloon_bench.py distress --out /bench/out/b3-distress.jsonl
  python3 balloon_bench.py snapshot --out /bench/out/b4-snapshot.jsonl
  python3 balloon_bench.py cleanup  --prefix bl

场景:
  cache     单 VM:page cache 场景下 FPR 拿不回内存 → 手动充气可以拿回;匿名内存释放后
            FPR 可以拿回(回归)。记录 VM 的宿主 cgroup 内存、guest MemAvailable/Cached。
  pressure  N 个 VM 同时攒 page cache,把 sbx.slice 推过高水位;全程每 2s 采样
            slice 使用率 / swap(zram) / PSI / 控制器模式 / 气球总量 / exec 探测延迟,
            最后统计 guest OOM。--tag on|off 对应控制器开关(由 DaemonSet env 控制)。
  distress  气球已充的 VM 突然要大块匿名内存:验证 guest 不被 OOM、控制器放气。
  snapshot  充气状态下 suspend/resume:内存完整性(memtouch sha256)、Full 快照大小、
            resume 后统计/气球配置仍在。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from overcommit_bench import Agent, auth_secret, now, pmap, port_forward, summarize  # noqa: E402

MEMINFO = "grep -E '^(MemTotal|MemFree|MemAvailable|Cached|Buffers|AnonPages|SwapTotal):' /proc/meminfo"
FILL_CACHE = (
    "set -e; dd if=/dev/urandom of=/root/cachefile bs=1M count={mb} status=none; sync; "
    "cat /root/cachefile >/dev/null; cat /dev/vda >/dev/null 2>&1 || true; echo filled"
)
# 从 ready 文件取 memtouch pid 定向发信号(pkill -f 会匹配到执行命令的 shell 自己)
MT_PID = "$(python3 -c \"import json;print(json.load(open('/run/memtouch.ready'))['pid'])\")"
OOM_COUNT = "dmesg 2>/dev/null | grep -ci 'out of memory' || true"


def parse_meminfo(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1]) // 1024
    return out


class Bench:
    def __init__(self, agent: Agent, out: str, tag: str):
        self.a, self.out, self.tag = agent, out, tag
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def rec(self, kind: str, **fields):
        row = {"ts": now(), "kind": kind, "tag": self.tag, **fields}
        with self._lock, open(self.out, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps(row, ensure_ascii=False)[:400], flush=True)
        return row

    # ---- node-agent helpers ----
    def create(self, sid: str, idx: int, mem: int = 4096, cpu: int = 1,
               balloon: dict | None = None) -> tuple[int, dict, float]:
        body = {"id": sid, "tap_idx": idx, "cpu": cpu, "mem_mib": mem}
        if balloon is not None:
            body["balloon"] = balloon
        t = time.time()
        code, r = self.a.call("POST", "/vm/create", body)
        return code, r, round(time.time() - t, 3)

    def exec(self, sid: str, cmd: str, timeout: int = 300) -> dict:
        t = time.time()
        code, r = self.a.call("POST", "/vm/exec", {"id": sid, "cmd": cmd, "timeout": timeout},
                              timeout=timeout + 30)
        r = dict(r) if isinstance(r, dict) else {"raw": r}
        r["http"] = code
        r["s"] = round(time.time() - t, 3)
        return r

    def wait_ready(self, sid: str, deadline_s: float = 120) -> bool:
        end = time.time() + deadline_s
        while time.time() < end:
            r = self.exec(sid, "echo ok", timeout=10)
            if r.get("http") == 200 and "ok" in (r.get("stdout") or ""):
                return True
            time.sleep(2)
        return False

    def guest_mem(self, sid: str) -> dict:
        return parse_meminfo(self.exec(sid, MEMINFO, timeout=20).get("stdout", ""))

    def balloon(self, sid: str) -> dict:
        code, r = self.a.call("GET", f"/vm/{sid}/balloon")
        return r if code == 200 else {"http": code, **(r or {})}

    def set_balloon(self, sid: str, mib: int, hold_s: int = 600) -> dict:
        code, r = self.a.call("POST", "/vm/balloon", {"id": sid, "amount_mib": mib, "hold_s": hold_s})
        return {"http": code, **(r or {})}

    def status(self) -> dict:
        code, r = self.a.call("GET", "/balloon/status")
        return r if code == 200 else {"http": code}

    def host_mib(self, sid: str) -> int | None:
        st = (self.balloon(sid).get("stats") or {})
        hb = st.get("host_bytes")
        return hb // (1024 * 1024) if isinstance(hb, int) else None

    def snap_vm(self, sid: str, label: str, **extra):
        b = self.balloon(sid)
        st = b.get("stats") or {}
        g = self.guest_mem(sid)
        return self.rec("vm_state", label=label, id=sid,
                        host_mib=(st.get("host_bytes") or 0) // 1048576 if st else None,
                        host_anon_mib=(st.get("host_anon_bytes") or 0) // 1048576 if st else None,
                        host_file_mib=(st.get("host_file_bytes") or 0) // 1048576 if st else None,
                        balloon_target_mib=st.get("target_mib"), balloon_actual_mib=st.get("actual_mib"),
                        guest_available_mib=g.get("MemAvailable"), guest_free_mib=g.get("MemFree"),
                        guest_cached_mib=g.get("Cached"), guest_buffers_mib=g.get("Buffers"),
                        guest_anon_mib=g.get("AnonPages"), **extra)

    def destroy(self, sid: str):
        return self.a.call("POST", "/vm/destroy", {"id": sid})

    def wait_balloon(self, sid: str, target: int, timeout_s: float = 120) -> float:
        t = time.time()
        while time.time() - t < timeout_s:
            st = self.balloon(sid).get("stats") or {}
            if abs((st.get("actual_mib") or 0) - target) <= 4:
                return round(time.time() - t, 2)
            time.sleep(0.5)
        return -1.0


def scenario_cache(b: Bench, args):
    sid = f"{args.prefix}-cache"
    code, r, s = b.create(sid, args.tap_base)
    b.rec("create", id=sid, http=code, s=s, resp=r)
    assert b.wait_ready(sid), "vm not ready"
    cfg = b.balloon(sid).get("config")
    b.rec("balloon_config", id=sid, config=cfg)
    time.sleep(10)
    b.snap_vm(sid, "idle")

    # A. page cache: FPR 无法回收
    r = b.exec(sid, FILL_CACHE.format(mb=args.cache_mb), timeout=600)
    b.rec("fill_cache", id=sid, rc=r.get("rc"), s=r.get("s"), stdout=(r.get("stdout") or "")[-200:])
    b.snap_vm(sid, "cache_filled_t0")
    time.sleep(30)
    b.snap_vm(sid, "cache_filled_t30s_fpr_only")

    # B. 手动充气:目标 = 当前 guest 可用 - 保底 512MiB
    g = b.guest_mem(sid)
    target = max(0, g.get("MemAvailable", 0) - 512)
    t0 = time.time()
    b.rec("inflate_request", id=sid, target_mib=target, resp=b.set_balloon(sid, target))
    took = b.wait_balloon(sid, target, timeout_s=180)
    b.snap_vm(sid, "inflated", inflate_s=took, wall_s=round(time.time() - t0, 2))
    # 放气:页还给 guest,但 guest 不碰就不会重新占用宿主内存
    b.rec("deflate_request", id=sid, resp=b.set_balloon(sid, 0))
    took = b.wait_balloon(sid, 0, timeout_s=180)
    b.snap_vm(sid, "deflated", deflate_s=took)
    # 缓存是否还能正常工作(重新读文件 → 缓存回来)
    r = b.exec(sid, "cat /root/cachefile >/dev/null; echo reread", timeout=300)
    b.snap_vm(sid, "cache_reread", reread_s=r.get("s"))

    # C. 匿名内存释放:FPR 回归
    b.exec(sid, "rm -f /root/cachefile; sync; echo 3 > /proc/sys/vm/drop_caches", timeout=60)
    time.sleep(15)
    b.snap_vm(sid, "dropped_caches_t15s")
    r = b.exec(sid, "python3 -c \"b=bytearray(1536*1024*1024)\n"
                    "for i in range(0,len(b),4096): b[i]=1\nprint('touched')\"", timeout=300)
    b.rec("anon_touch", id=sid, rc=r.get("rc"), s=r.get("s"))
    time.sleep(15)
    b.snap_vm(sid, "anon_freed_t15s")
    b.destroy(sid)


def sampler(b: Bench, stop: threading.Event, probe_sid: str | None, every: float = 2.0):
    while not stop.is_set():
        st = b.status()
        host = st.get("host") or {}
        tot = st.get("totals") or {}
        probe = None
        if probe_sid:
            r = b.exec(probe_sid, "echo p", timeout=30)
            probe = r.get("s") if r.get("http") == 200 else -1
        b.rec("sample", mode=st.get("mode"), host_ratio=st.get("host_ratio"),
              slice_mib=(host.get("current_bytes") or 0) // 1048576,
              slice_usage_mib=(host.get("usage_bytes") or 0) // 1048576,
              slice_anon_mib=(host.get("anon_bytes") or 0) // 1048576,
              slice_max_mib=(host.get("max_bytes") or 0) // 1048576,
              swap_mib=(host.get("swap_bytes") or 0) // 1048576,
              psi_some_avg10=host.get("psi_some_avg10"),
              host_avail_mib=(host.get("host_mem_available_bytes") or 0) // 1048576,
              balloon_target_mib=(tot.get("target") or 0) // 1048576,
              balloon_actual_mib=(tot.get("actual") or 0) // 1048576,
              guest_cache_mib=(tot.get("guest_cache") or 0) // 1048576,
              guest_avail_mib=(tot.get("guest_available") or 0) // 1048576,
              vms=st.get("vms_with_stats"), probe_exec_s=probe,
              actions=len(st.get("recent_actions") or []))
        stop.wait(every)


def scenario_pressure(b: Bench, args):
    ids = [f"{args.prefix}-p{i:02d}" for i in range(args.count)]
    res = pmap(lambda x: (x[1], b.create(x[1], args.tap_base + x[0])), list(enumerate(ids)), 8)
    for sid, (code, r, s) in res:
        b.rec("create", id=sid, http=code, s=s, resp=r)
    ok = [sid for sid, (code, _, _) in res if code == 200]
    pmap(b.wait_ready, ok, 8)
    stop = threading.Event()
    th = threading.Thread(target=sampler, args=(b, stop, ok[0] if ok else None), daemon=True)
    th.start()
    time.sleep(10)
    t0 = time.time()
    fills = pmap(lambda sid: (sid, b.exec(sid, FILL_CACHE.format(mb=args.cache_mb), timeout=900)), ok, len(ok))
    for sid, r in fills:
        b.rec("fill_cache", id=sid, rc=r.get("rc"), s=r.get("s"), http=r.get("http"))
    b.rec("fill_done", wall_s=round(time.time() - t0, 1))
    time.sleep(args.hold_s)
    # 再来一轮:在压力下活跃 VM 重读缓存(模拟持续 I/O)
    act = ok[: max(1, len(ok) // 2)]
    rr = pmap(lambda sid: (sid, b.exec(sid, "cat /root/cachefile >/dev/null; echo r", timeout=900)), act, len(act))
    for sid, r in rr:
        b.rec("reread", id=sid, rc=r.get("rc"), s=r.get("s"), http=r.get("http"))
    time.sleep(args.hold_s)
    # 归还:销毁一半 VM,压力下降 → 控制器慢慢放气
    for sid in ok[len(ok) // 2:]:
        b.destroy(sid)
        b.rec("destroy", id=sid)
    time.sleep(args.release_s)
    stop.set()
    th.join(timeout=60)
    for sid in ok[: len(ok) // 2]:
        oom = b.exec(sid, OOM_COUNT, timeout=20).get("stdout", "").strip()
        b.snap_vm(sid, "final", guest_oom_lines=oom)
    st = b.status()
    b.rec("final_status", status={k: st.get(k) for k in ("mode", "host_ratio", "host", "totals", "recent_actions")})
    lat = [r.get("s") for _, r in fills if r.get("http") == 200]
    b.rec("summary", fill_latency=summarize(lat), ok=len(ok), requested=len(ids))
    for sid in ok[: len(ok) // 2]:
        b.destroy(sid)


def scenario_distress(b: Bench, args):
    sid = f"{args.prefix}-distress"
    code, r, s = b.create(sid, args.tap_base)
    b.rec("create", id=sid, http=code, s=s)
    assert b.wait_ready(sid)
    b.exec(sid, FILL_CACHE.format(mb=args.cache_mb), timeout=600)
    g = b.guest_mem(sid)
    target = max(0, g.get("MemAvailable", 0) - 512)
    # hold_s=0:手动充气后立刻交还控制器管理(验证吃紧放气)
    b.rec("inflate_request", id=sid, target_mib=target, resp=b.set_balloon(sid, target, hold_s=0))
    b.wait_balloon(sid, target, 180)
    b.snap_vm(sid, "inflated")
    # guest 突然要 2.5GiB 匿名内存(大于当前可用)
    r = b.exec(sid, "python3 -c \"import time\nb=bytearray(2560*1024*1024)\n"
                    "for i in range(0,len(b),4096): b[i]=1\nprint('allocated')\ntime.sleep(20)\"",
               timeout=600)
    b.rec("big_alloc", id=sid, rc=r.get("rc"), s=r.get("s"),
          stdout=(r.get("stdout") or "")[-100:], stderr=(r.get("stderr") or "")[-300:])
    b.snap_vm(sid, "after_alloc", guest_oom_lines=b.exec(sid, OOM_COUNT).get("stdout", "").strip())
    st = b.status()
    b.rec("controller_actions", actions=[a for a in (st.get("recent_actions") or []) if a.get("id") == sid])
    b.destroy(sid)


def scenario_snapshot(b: Bench, args):
    snap_root = "/var/lib/sbx"
    for variant in ("no-balloon-inflate", "inflated"):
        sid = f"{args.prefix}-snap-{'i' if variant == 'inflated' else 'n'}"
        code, r, s = b.create(sid, args.tap_base + (1 if variant == "inflated" else 0))
        b.rec("create", id=sid, http=code, s=s)
        assert b.wait_ready(sid)
        b.exec(sid, FILL_CACHE.format(mb=args.cache_mb), timeout=600)
        # memtouch 写入可校验内容
        push = Path(__file__).with_name("memtouch.py").read_text()
        b.exec(sid, "cat > /root/memtouch.py <<'EOF'\n" + push + "\nEOF\necho pushed")
        b.exec(sid, "P=$(command -v python3 || ls /usr/bin/python3* | head -1); setsid nohup $P /root/memtouch.py --mb 512 --pattern mixed "
                    ">/run/memtouch.log 2>&1 </dev/null & sleep 1; echo started")
        for _ in range(60):
            if "ok" in b.exec(sid, "test -f /run/memtouch.ready && echo ok").get("stdout", ""):
                break
            time.sleep(2)
        if variant == "inflated":
            g = b.guest_mem(sid)
            target = max(0, g.get("MemAvailable", 0) - 512)
            b.set_balloon(sid, target)
            b.wait_balloon(sid, target, 180)
        b.exec(sid, f"kill -USR2 {MT_PID}; sleep 5; cat /run/memtouch.h1")
        h1 = b.exec(sid, "cat /run/memtouch.h1").get("stdout", "").strip()
        b.snap_vm(sid, f"{variant}:before_suspend")
        t = time.time()
        code, r = b.a.call("POST", "/vm/suspend", {"id": sid, "snapshot_local_path": f"{snap_root}/{sid}/snap"},
                           timeout=900)
        b.rec("suspend", id=sid, variant=variant, http=code, s=round(time.time() - t, 2), resp=r)
        t = time.time()
        code, r = b.a.call("POST", "/vm/resume", {"id": sid, "snapshot_local_path": f"{snap_root}/{sid}/snap",
                                                 "rootfs_path": f"{snap_root}/{sid}/rootfs.ext4",
                                                 "tap_idx": args.tap_base + (1 if variant == "inflated" else 0)},
                           timeout=900)
        b.rec("resume", id=sid, variant=variant, http=code, s=round(time.time() - t, 2), resp=r)
        b.wait_ready(sid, 60)
        b.exec(sid, f"kill -USR1 {MT_PID}; sleep 5")
        h2 = b.exec(sid, "cat /run/memtouch.h2").get("stdout", "").strip()
        same = bool(h1) and json.loads(h1).get("sha256") == (json.loads(h2).get("sha256") if h2 else None)
        bl = b.balloon(sid)
        b.rec("integrity", id=sid, variant=variant, sha_match=same, h1=h1[:120], h2=h2[:120],
              balloon_config_after_resume=bl.get("config"),
              stats_after_resume=bool(bl.get("stats")))
        b.snap_vm(sid, f"{variant}:after_resume")
        b.destroy(sid)


def scenario_cleanup(b: Bench, args):
    code, r = b.a.call("GET", "/balloon/status")
    for sid in list((r or {}).get("vms", {}).keys()):
        if sid.startswith(args.prefix + "-"):
            b.rec("destroy", id=sid, resp=b.destroy(sid))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("cache", "pressure", "distress", "snapshot", "cleanup"))
    ap.add_argument("--out", default="/bench/out/balloon.jsonl")
    ap.add_argument("--tag", default="")
    ap.add_argument("--prefix", default="bl")
    ap.add_argument("--count", type=int, default=8)
    ap.add_argument("--cache-mb", type=int, default=1200)
    ap.add_argument("--tap-base", type=int, default=300)
    ap.add_argument("--hold-s", type=int, default=60)
    ap.add_argument("--release-s", type=int, default=90)
    args = ap.parse_args()
    with port_forward("local") as url:
        b = Bench(Agent(url, auth_secret()), args.out, args.tag or args.cmd)
        {"cache": scenario_cache, "pressure": scenario_pressure, "distress": scenario_distress,
         "snapshot": scenario_snapshot, "cleanup": scenario_cleanup}[args.cmd](b, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
