#!/usr/bin/env python3
"""Guest 内负载发生器：按指定模式占用/改写内存、可选烧 CPU，并支持快照前后内存完整性校验。

在 microVM guest 内经 vsock exec 后台启动（python3 标准库即可，无第三方依赖）：

  setsid nohup python3 /root/memtouch.py --mb 2048 --pattern mixed \
      >/run/memtouch.log 2>&1 </dev/null &

模式（--pattern）：
  zero    写满 0x00（页被真正触碰、宿主分配物理页，但内容全零 → 压缩/零页消除的最好情况）
  mixed   取 guest 内 /usr 下真实文件内容（.py/.so/文本）循环拼接，按 --random-pct 掺随机字节
          （近似代码 agent 的堆/缓存，可压缩但不全零）
  random  os.urandom（不可压缩，最坏情况）

信号：
  SIGUSR2  冻结：停止改写，计算整块内存 sha256 写 /run/memtouch.h1（快照前做）
  SIGUSR1  校验：重算 sha256 写 /run/memtouch.h2（恢复后做，与 h1 比对）
状态文件：
  /run/memtouch.ready  填充完成后写入 {"mb":..,"fill_s":..,"pattern":..}
"""
import argparse
import hashlib
import json
import mmap
import os
import random
import signal
import sys
import threading
import time

MiB = 1 << 20
PAGE = 4096
STATE_DIR = "/run"


def _source_chunks(limit_bytes):
    """收集 guest 内真实文件内容作为 mixed 模式的素材（最多 limit_bytes）。"""
    out, total = [], 0
    for root in ("/usr/lib/python3.12", "/usr/local/lib/python3.12", "/usr/lib", "/usr/share"):
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                p = os.path.join(dirpath, name)
                try:
                    if os.path.islink(p) or os.path.getsize(p) < 512:
                        continue
                    with open(p, "rb") as f:
                        data = f.read(256 * 1024)
                except OSError:
                    continue
                out.append(data)
                total += len(data)
                if total >= limit_bytes:
                    return out
    return out or [b"fallback-content-" * 4096]


def _fill(buf, size, pattern, random_pct, seed):
    rng = random.Random(seed)
    if pattern == "mixed":
        corpus = b"".join(_source_chunks(64 * MiB))
        clen = len(corpus)
    off = 0
    while off < size:
        n = min(MiB, size - off)
        if pattern == "zero":
            buf[off:off + n] = bytes(n)
        elif pattern == "random":
            buf[off:off + n] = os.urandom(n)
        else:
            start = rng.randrange(0, max(1, clen - n))
            chunk = bytearray(corpus[start:start + n].ljust(n, b"\0"))
            if random_pct > 0:
                # 按页掺入随机字节，模拟堆上的指针/哈希/压缩数据
                for p in range(0, n, PAGE):
                    if rng.random() * 100 < random_pct:
                        chunk[p:p + PAGE] = os.urandom(min(PAGE, n - p))
            buf[off:off + n] = chunk
        off += n


class Worker:
    def __init__(self, args):
        self.args = args
        self.size = args.mb * MiB
        self.buf = mmap.mmap(-1, self.size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
        self.frozen = threading.Event()
        self.lock = threading.Lock()

    def digest(self):
        h = hashlib.sha256()
        mv = memoryview(self.buf)
        for off in range(0, self.size, 8 * MiB):
            h.update(mv[off:off + 8 * MiB])
        mv.release()
        return h.hexdigest()

    def on_freeze(self, *_):
        self.frozen.set()
        with self.lock:
            d = self.digest()
        _write_state("memtouch.h1", {"sha256": d, "ts": time.time()})

    def on_verify(self, *_):
        with self.lock:
            d = self.digest()
        _write_state("memtouch.h2", {"sha256": d, "ts": time.time()})

    def rewrite_loop(self):
        """以 --rewrite-mbps 的速率持续改写随机页（产生脏页），直到被冻结。"""
        rate = self.args.rewrite_mbps * MiB
        if rate <= 0:
            return
        pages = self.size // PAGE
        rng = random.Random(self.args.seed + 1)
        per_tick = max(1, int(rate / PAGE / 10))  # 每 100ms 改写的页数
        while not self.frozen.is_set():
            t0 = time.time()
            with self.lock:
                for _ in range(per_tick):
                    p = rng.randrange(pages) * PAGE
                    self.buf[p:p + 8] = os.urandom(8)  # 改 8 字节即让整页变脏
            dt = time.time() - t0
            if dt < 0.1:
                time.sleep(0.1 - dt)


def _cpu_burn(stop_at):
    x = b"x" * MiB
    while time.time() < stop_at:
        hashlib.sha256(x).digest()


def _write_state(name, obj):
    tmp = os.path.join(STATE_DIR, name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, os.path.join(STATE_DIR, name))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=int, default=1024)
    ap.add_argument("--pattern", choices=("zero", "mixed", "random"), default="mixed")
    ap.add_argument("--random-pct", type=float, default=20.0,
                    help="mixed 模式下替换为随机内容的页比例(%%)")
    ap.add_argument("--rewrite-mbps", type=float, default=0.0)
    ap.add_argument("--cpu-procs", type=int, default=0, help="并行烧 CPU 的进程数")
    ap.add_argument("--cpu-seconds", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=int.from_bytes(os.urandom(4), "big"))
    args = ap.parse_args()

    w = Worker(args)
    signal.signal(signal.SIGUSR2, w.on_freeze)
    signal.signal(signal.SIGUSR1, w.on_verify)

    t0 = time.time()
    _fill(w.buf, w.size, args.pattern, args.random_pct, args.seed)
    fill_s = time.time() - t0
    _write_state("memtouch.ready", {"mb": args.mb, "pattern": args.pattern,
                                    "random_pct": args.random_pct, "fill_s": round(fill_s, 2),
                                    "pid": os.getpid(), "ts": time.time()})
    print(f"[memtouch] filled {args.mb} MiB pattern={args.pattern} in {fill_s:.1f}s", flush=True)

    if args.cpu_procs > 0 and args.cpu_seconds > 0:
        stop_at = time.time() + args.cpu_seconds
        pids = []
        for _ in range(args.cpu_procs):
            pid = os.fork()
            if pid == 0:
                _cpu_burn(stop_at)
                os._exit(0)
            pids.append(pid)

    threading.Thread(target=w.rewrite_loop, daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
