"""
Reconciler — level-triggered 状态对账(P0-1)。

背景:node-agent 的 _VMS 是进程内内存 dict,重启即丢句柄;S3 上传/节点宕机等
也会让 DynamoDB 状态与真实运行态漂移。本模块周期性对账并自动修正 + 告警。

设计要点:
  - level-triggered:每 tick 重新拉全量 desired(DynamoDB)vs observed(node-agent
    实况 + 活节点表),幂等修正。丢一次消息下 tick 自愈,不依赖事件。
  - leader 门控:多副本控制面下只有持 leader 锁的实例跑对账,避免重复/打架
    (同一门控也用于暖池补充,见 app.py)。
  - 处置力度(第一版,用户已定):自动修正状态 + 告警,不自动跨机重调度。
      · 死节点上的 suspended 沙盒 → 标 needs_reschedule(快照在 S3,留 P1 重调度)
      · 死节点上的 running 等       → 标 orphaned + 回收 tap_idx
      · 节点活但 runtime 已不存在   → 标 orphaned + 回收 tap_idx
    所有写走 update_state(带 prev_state 条件写,天然防与 API 路径并发)。
"""
from __future__ import annotations

import os
import socket
import threading
import time
import uuid

from sandbox_api import db

# 对账关注的"活跃"状态(终态 destroying/failed/orphaned/needs_reschedule 不管)
_ACTIVE_STATES = ["running", "suspended", "suspending", "resuming"]

RECONCILE_EVERY   = int(os.environ.get("RECONCILE_EVERY_S", "20"))
LEADER_LOCK_ID    = os.environ.get("LEADER_LOCK_ID", "reconciler")
LEADER_TTL_S      = int(os.environ.get("LEADER_TTL_S", "30"))
NODE_TTL_S        = int(os.environ.get("NODE_TTL_S", "90"))


class Reconciler:
    """对账循环 + leader 选举。持一个 driver 引用做 runtime 探针。"""

    def __init__(self, driver):
        self._driver      = driver
        # 唯一 owner id:主机名 + 随机后缀,区分同主机多副本
        self._owner       = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self._is_leader   = False
        self._rvn: int | None = None

    # ------------------------------------------------------------------
    # leader 门控
    # ------------------------------------------------------------------

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    def _refresh_leadership(self) -> None:
        rvn = db.acquire_leader_lock(LEADER_LOCK_ID, self._owner, LEADER_TTL_S)
        self._is_leader = rvn is not None
        self._rvn       = rvn

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def start_loop(self) -> None:
        def _loop():
            while True:
                try:
                    self._refresh_leadership()
                    if self._is_leader:
                        self.reconcile_once()
                except Exception:
                    # 对账失败不能让线程死掉,下 tick 重试
                    pass
                time.sleep(RECONCILE_EVERY)  # nosemgrep: arbitrary-sleep -- 对账周期

        threading.Thread(target=_loop, daemon=True).start()

    # ------------------------------------------------------------------
    # 单次对账(可单测直接调)
    # ------------------------------------------------------------------

    def reconcile_once(self) -> dict:
        """返回本次处置计数,便于测试/可观测。"""
        stats = {"checked": 0, "orphaned": 0, "needs_reschedule": 0, "ok": 0}

        active_ids = {n["node_id"] for n in db.list_active_nodes(NODE_TTL_S)}
        # node-agent 心跳里 node_id 可能是 hostname,而沙盒 record["node"] 存的是
        # 传给 driver 的节点标识(IP 或 ip:port)。两者都纳入活集合以兼容。
        active_ips = {n.get("ip", "") for n in db.list_active_nodes(NODE_TTL_S)}
        active     = active_ids | active_ips | {""}  # 空串:未记录 node 的暖池占位不误杀

        for rec in db.list_by_states(_ACTIVE_STATES):
            stats["checked"] += 1
            sid   = rec["id"]
            state = rec["state"]
            node  = rec.get("node", "")
            # 暖池占位(tenant __pool__)不参与用户沙盒对账,交给 warm_pool 自管
            if rec.get("tenant_id") == "__pool__" or rec.get("pool_state"):
                stats["ok"] += 1
                continue

            node_alive = node in active or _host_of(node) in active

            if not node_alive:
                # 死节点:suspended 快照在 S3 → 可留待重调度;其他态无法恢复 → orphaned
                if state == "suspended":
                    self._mark(sid, "needs_reschedule", state,
                               reason="node_down", node=node)
                    stats["needs_reschedule"] += 1
                else:
                    self._orphan(sid, state, rec, reason="node_down")
                    stats["orphaned"] += 1
                continue

            # 节点活:探真实 runtime 状态对账
            runtime = self._driver.get_runtime_state(sid, rec)
            if state == "running" and runtime in ("unknown", "stopped"):
                # DynamoDB 说 running 但节点上已无此 VM → 漂移
                self._orphan(sid, state, rec, reason=f"runtime_{runtime}")
                stats["orphaned"] += 1
            else:
                stats["ok"] += 1

        return stats

    # ------------------------------------------------------------------
    # 处置动作(幂等 + prev_state 条件写防并发)
    # ------------------------------------------------------------------

    def _mark(self, sid: str, new_state: str, prev_state: str,
              reason: str, node: str = "") -> None:
        try:
            db.update_state(sid, new_state, prev_state,
                            {"reconcile_reason": reason})
            db.write_event(sid, "reconciled", prev_state,
                           {"new_state": new_state, "reason": reason, "node": node})
        except Exception:
            # 条件写失败 = 状态已被 API 路径改动,放弃本次(下 tick 重判)
            pass

    def _orphan(self, sid: str, prev_state: str, rec: dict, reason: str) -> None:
        self._mark(sid, "orphaned", prev_state, reason=reason,
                   node=rec.get("node", ""))
        # 回收泄漏资源:tap_idx(release_tap_idx 现为 no-op,预留将来真正回收)
        if rec.get("tap_idx"):
            try:
                db.release_tap_idx(rec["tap_idx"])
            except Exception:
                pass


def _host_of(node: str) -> str:
    """从 "ip:port" 取 ip;无端口则原样返回。"""
    return node.split(":", 1)[0] if node else node
