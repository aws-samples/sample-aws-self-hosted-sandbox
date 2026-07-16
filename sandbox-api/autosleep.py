"""
AutoSleeper — 空闲自动休眠扫描 loop(auto-sleep,对齐 fly.io)。

背景:沙盒创建后一直占着节点 RAM,没人用也不会自己睡。本模块周期性扫描 running 沙盒,
把【开启了 autostop 且空闲超过阈值】的自动休眠(打快照进 slept 状态,释放 RAM)。
唤醒是网关侧的事(app.py 的 _ensure_awake:请求打到 slept 沙盒 → 透明 resume),这里只管睡。

设计要点(全部复用现有范式,不造轮子):
  - leader 门控:与 reconcile / 暖池补充共用同一 leader 锁,多副本控制面下只有 leader
    跑扫描,避免重复触发(见 app.py 注入 is_leader=lambda: _reconciler.is_leader)。
  - 处置委托:实际休眠动作走注入的 sleep_fn(app.py 的 auto_sleep_sandbox),复用它的
    lease + prev_state 条件写 + 失败回滚 + 二次空闲校验,与手动 suspend 同一套并发保护。
  - level-triggered:每 tick 重新拉全量 running 沙盒判空闲,幂等;漏一次下 tick 再来。

镜像 warm_pool.py 的 start_replenish_loop 结构(后台 daemon 线程 + is_leader callback)。
"""
from __future__ import annotations

import os
import sys
import threading
import time

from sandbox_api import db

SCAN_EVERY   = int(os.environ.get("AUTO_SLEEP_SCAN_S", "30"))   # 扫描间隔
IDLE_S       = int(os.environ.get("AUTO_SLEEP_IDLE_S", "300"))  # 空闲多久自动 sleep


class AutoSleeper:
    """空闲扫描 loop。持一个 sleep_fn(sid)->(code, body) 委托实际休眠(app.auto_sleep_sandbox)。"""

    def __init__(self, sleep_fn, idle_seconds_fn, autostop_fn):
        # 依赖注入,避免与 app 循环 import:
        #   sleep_fn(sid)        —— 实际休眠(app.auto_sleep_sandbox),含并发保护 + 二次校验
        #   idle_seconds_fn(rec) —— 计算空闲秒数(app._idle_seconds)
        #   autostop_fn(rec)     —— 该沙盒是否 opt-in 自动休眠(app._autostop_enabled)
        self._sleep_fn     = sleep_fn
        self._idle_fn      = idle_seconds_fn
        self._autostop_fn  = autostop_fn

    def scan_once(self) -> dict:
        """扫一遍 running 沙盒,把空闲且 opt-in 的休眠。返回计数,便于测试/可观测。"""
        stats = {"checked": 0, "slept": 0, "skipped": 0}
        for rec in db.list_by_states(["running"]):
            stats["checked"] += 1
            sid = rec["id"]
            # 暖池占位(__pool__)不参与自动休眠,交给 warm_pool 自管
            if rec.get("tenant_id") == "__pool__" or rec.get("pool_state"):
                stats["skipped"] += 1
                continue
            # opt-in:未开 autostop 的沙盒永不自动休眠
            if not self._autostop_fn(rec):
                stats["skipped"] += 1
                continue
            idle = self._idle_fn(rec)
            if idle is None or idle < IDLE_S:
                stats["skipped"] += 1
                continue
            # 空闲达标 → 委托休眠(内部拿 lease 后还会二次校验仍空闲,防竞态)
            try:
                code, _ = self._sleep_fn(sid)
                if code == 200:
                    stats["slept"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                print(f"[autosleep] sleep {sid} failed: {e!r}", file=sys.stderr, flush=True)
                stats["skipped"] += 1
        return stats

    def start_loop(self, is_leader=None) -> None:
        """后台扫描 loop。is_leader:可选无参 callable,返回 True 才扫描(leader 门控)。"""
        def _loop():
            while True:
                try:
                    if is_leader is None or is_leader():
                        self.scan_once()
                except Exception:
                    pass  # 扫描失败不让线程死,下 tick 重试
                time.sleep(SCAN_EVERY)  # nosemgrep: arbitrary-sleep -- 自动休眠扫描周期

        threading.Thread(target=_loop, daemon=True).start()
