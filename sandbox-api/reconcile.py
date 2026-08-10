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
from concurrent.futures import ThreadPoolExecutor

from sandbox_api import db

# 对账关注的"活跃"状态(终态 destroying/failed/orphaned/needs_reschedule 不管)
# slept = 自动休眠(与手动 suspended 同样有 S3/EBS 快照),对账时按同等方式处理。
_ACTIVE_STATES = ["running", "suspended", "suspending", "resuming", "slept"]

RECONCILE_EVERY   = int(os.environ.get("RECONCILE_EVERY_S", "20"))
LEADER_LOCK_ID    = os.environ.get("LEADER_LOCK_ID", "reconciler")
LEADER_TTL_S      = int(os.environ.get("LEADER_TTL_S", "30"))
NODE_TTL_S        = int(os.environ.get("NODE_TTL_S", "90"))

# M4:死节点上的沙盒 → 自动跨机重调度(从 S3 权威副本在活节点恢复)。
# 默认开;关掉则只标 needs_reschedule 不自动拉起(回到"仅告警"行为)。
AUTO_RESCHEDULE          = os.environ.get("AUTO_RESCHEDULE_ENABLED", "1").lower() in ("1", "true")
RESCHEDULE_MAX_ATTEMPTS  = int(os.environ.get("RESCHEDULE_MAX_ATTEMPTS", "3"))
# 每 tick 最多重调度多少个(避免单 tick 阻塞过久 → leader 锁 TTL 内跑不完;level-triggered 会跨 tick 排干)
RESCHEDULE_BATCH         = int(os.environ.get("RESCHEDULE_BATCH", "8"))
RESCHEDULE_CONCURRENCY   = int(os.environ.get("RESCHEDULE_CONCURRENCY", "4"))


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
        stats = {"checked": 0, "orphaned": 0, "needs_reschedule": 0, "ok": 0,
                 "rescheduled": 0, "reschedule_failed": 0, "fenced": 0}

        active_nodes = db.list_active_nodes(NODE_TTL_S)
        active_ids = {n["node_id"] for n in active_nodes}
        # node-agent 心跳里 node_id 可能是 hostname,而沙盒 record["node"] 存的是
        # 传给 driver 的节点标识(IP 或 ip:port)。两者都纳入活集合以兼容。
        active_ips = {n.get("ip", "") for n in active_nodes}
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
                # 死节点:能否重调度取决于 S3 是否有【完整快照】(diff+rootfs)。
                #   - suspended/slept:手动挂起/自动休眠,S3 必有完整快照 → needs_reschedule。
                #   - running/suspending/resuming:若已被 spot 疏散(方案A)传全 S3 → 也可恢复;
                #     否则(如节点骤死未疏散,只有创建期的 base)→ orphaned。
                if state in ("suspended", "slept"):
                    self._mark(sid, "needs_reschedule", state,
                               reason="node_down", node=node)
                    stats["needs_reschedule"] += 1
                else:
                    recoverable = False
                    try:
                        recoverable = self._driver.snapshot_complete_in_s3(sid)
                    except Exception:
                        recoverable = False
                    if recoverable:
                        self._mark(sid, "needs_reschedule", state,
                                   reason="node_down_evacuated", node=node)
                        stats["needs_reschedule"] += 1
                    else:
                        self._orphan(sid, state, rec, reason="node_down")
                        stats["orphaned"] += 1
                continue

            # 节点活:探真实 runtime 状态对账
            runtime = self._driver.get_runtime_state(sid, rec)
            if state == "running" and runtime in ("unknown", "stopped"):
                # DynamoDB 说 running 但节点上探不到此 VM。
                #   runtime=unknown(node-agent 不可达,典型是节点正在终止、心跳尚未过期的 TTL 窗口)
                #     且 S3 有完整快照 → 【不要 orphan】,推迟本轮;等节点心跳过期进"死节点"分支
                #     → needs_reschedule,避免把可恢复的沙盒误杀成 orphaned(M4 关键)。
                #   否则(runtime=stopped 明确停;或无完整快照无法跨机恢复)→ orphan。
                if runtime == "unknown":
                    recoverable = False
                    try:
                        recoverable = self._driver.snapshot_complete_in_s3(sid)
                    except Exception:
                        recoverable = False
                    if recoverable:
                        stats["ok"] += 1  # 推迟:留待死节点分支重调度
                        continue
                self._orphan(sid, state, rec, reason=f"runtime_{runtime}")
                stats["orphaned"] += 1
            else:
                stats["ok"] += 1

        # ---- 栅栏:围杀陈旧重复 VM(epoch 对账)----
        # 各活节点心跳上报本节点承载的 {sid: epoch};若某 VM 所在节点 ≠ record.node 且 epoch 不新于
        # record → 是分区愈合后复活的老 VM / 漂移副本 → 下发 destroy,保证一个 sid 全局只有一个活 VM。
        self._fence_stale_vms(active_nodes, stats)

        # ---- M4:重调度阶段 —— 把 needs_reschedule 的沙盒在活节点从 S3 拉起 ----
        # 已 leader 门控(整个 reconcile_once 只有 leader 跑)。每 tick 处理一批,level-triggered 跨 tick 排干。
        if AUTO_RESCHEDULE:
            self._reschedule_pass(stats)

        return stats

    # ------------------------------------------------------------------
    # 栅栏:epoch 对账,围杀陈旧重复 VM
    # ------------------------------------------------------------------

    def _fence_stale_vms(self, active_nodes: list[dict], stats: dict) -> None:
        for node in active_nodes:
            node_ip = node.get("ip", "")
            vms = node.get("vms") or {}
            if not node_ip or not isinstance(vms, dict):
                continue
            for sid, ep in vms.items():
                try:
                    reported_epoch = int(ep)
                except (ValueError, TypeError):
                    reported_epoch = 1
                rec = db.get(sid)
                stale = False
                reason = ""
                if not rec:
                    # 记录已删除,但节点上还有这个 VM → 泄漏,围杀
                    stale, reason = True, "record_gone"
                else:
                    owner = rec.get("node", "")
                    rec_epoch = int(rec.get("epoch", 1))
                    # VM 所在节点不是 record 认定的 owner,且这个 VM 不比 record 新 → 陈旧副本
                    if owner and owner != node_ip and reported_epoch <= rec_epoch:
                        stale, reason = True, f"wrong_node(owner={owner},ep={reported_epoch}<= {rec_epoch})"
                if stale:
                    if self._driver.fence_vm(sid, node_ip):
                        stats["fenced"] = stats.get("fenced", 0) + 1
                        try:
                            db.write_event(sid, "fenced", rec.get("state", "") if rec else "",
                                           {"node": node_ip, "reason": reason})
                        except Exception:
                            pass

    # ------------------------------------------------------------------
    # M4:needs_reschedule → 活节点从 S3 恢复(自动跨机重调度)
    # ------------------------------------------------------------------

    def _reschedule_pass(self, stats: dict) -> None:
        pending = []
        for rec in db.list_by_states(["needs_reschedule"]):
            if rec.get("tenant_id") == "__pool__" or rec.get("pool_state"):
                continue
            pending.append(rec)
            if len(pending) >= RESCHEDULE_BATCH:
                break
        if not pending:
            return
        workers = max(1, min(RESCHEDULE_CONCURRENCY, len(pending)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(self._reschedule_one, pending))
        for ok in results:
            stats["rescheduled" if ok else "reschedule_failed"] += 1

    def _reschedule_one(self, rec: dict) -> bool:
        """把单个 needs_reschedule 沙盒在一台活节点从 S3 恢复。成功 True。
        条件写(needs_reschedule→resuming)天然互斥:多 leader/并发只有一个抢到。"""
        sid      = rec["id"]
        attempts = int(rec.get("reschedule_attempts", 0) or 0)
        try:
            node = self._driver._pick_node()   # 任一活节点
        except Exception:
            return False  # 暂无活节点,下 tick 再试(不计失败,不加 attempts)
        # 抢占式转 resuming;抢不到(已被别的 leader/请求改动)则跳过
        try:
            db.update_state(sid, "resuming", "needs_reschedule")
        except Exception:
            return False
        try:
            new_epoch = int(rec.get("epoch", 1)) + 1   # 栅栏:重调度=新一代放置
            fields = self._driver.resume(sid, rec, force_node=node, epoch=new_epoch)
            db.update_state(sid, "running", "resuming",
                            {**fields, "last_active_at": db._utcnow(),
                             "reschedule_attempts": 0})
            db.write_event(sid, "rescheduled", "needs_reschedule",
                           {"to_node": fields.get("node"), "attempts": attempts + 1})
            return True
        except Exception as e:
            attempts += 1
            if attempts >= RESCHEDULE_MAX_ATTEMPTS:
                # 反复失败(如 S3 快照实际损坏)→ 标 failed,停止无限重试
                db.force_update(sid, {"state": "failed",
                                      "error": f"reschedule failed x{attempts}: {e}"})
                db.write_event(sid, "reschedule_failed", "resuming",
                               {"attempts": attempts, "error": str(e)[:200]})
            else:
                # 回落 needs_reschedule 供下 tick 重试,记录 attempts
                db.force_update(sid, {"state": "needs_reschedule",
                                      "reschedule_attempts": attempts,
                                      "error": str(e)[:200]})
            return False

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
