"""
Warm Pool — 对外隐藏冷启动,让 create 永远秒级完成。

预先 suspend 一批空白沙盒,create = resume + 注入配置(~秒级),对外隐藏冷启动。

使用方式:
  在控制面启动时 start_replenish_loop() 开始后台补充。
  create_sandbox() 调 WarmPool.claim() 先尝试从池子拿,没有再冷建。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid

from sandbox_api import db
from sandbox_api.driver import SandboxSpec, ServiceSpec

POOL_SIZE    = int(os.environ.get("WARM_POOL_SIZE", "5"))
REFILL_EVERY = int(os.environ.get("WARM_POOL_REFILL_S", "30"))

# 暖池用的基础镜像和规格(与真实沙盒保持一致)
_BASE_SPEC = SandboxSpec(
    image   = os.environ.get("SANDBOX_IMAGE", ""),
    cpu     = int(os.environ.get("WARM_CPU", "2")),
    mem_mib = int(os.environ.get("WARM_MEM_MIB", "4096")),
)


class WarmPool:
    def __init__(self, driver_name: str, driver):
        self._driver_name = driver_name
        self._driver      = driver
        self._lock        = threading.Lock()

    # ------------------------------------------------------------------
    # claim — create 时先尝试从池子拿
    # ------------------------------------------------------------------

    def claim(self, real_id: str, spec: SandboxSpec) -> bool:
        """
        尝试从暖池取一个沙盒并 resume,填充真实配置。
        成功返回 True(调用方跳过冷建);失败返回 False(调用方走冷建)。
        """
        warm_id = db.claim_warm_item(self._driver_name)
        if not warm_id:
            return False

        record = db.get(warm_id)
        if not record:
            return False

        try:
            # 用 real_id 注册 VM(后续 exec/suspend 按 real_id 路由),
            # 但从 warm_id 的快照/rootfs 恢复(本地快照在 warm_id 目录)。
            driver_fields = self._driver.resume(real_id, record, snapshot_id=warm_id)
            # 迁移到 real_id:create_sandbox 已先 put 了一条 id=real_id 的占位记录
            # (state=creating),故这里必须用 force_update 覆盖,不能再 db.put
            # ——db.put 带 attribute_not_exists(id) 条件写,对已存在的 real_id 会抛
            # ConditionalCheckFailedException,导致 claim 静默回退冷建(暖池形同虚设)。
            # 迁移暖池实例的运行态字段(快照/tap/node/guest_ip)到占位记录上。
            # 排除 updated_at:force_update 内部固定 SET updated_at=:now,
            # 若 migrate 里也带 updated_at 会导致 UpdateExpression 路径重叠报错。
            migrate = {
                k: v for k, v in record.items()
                if k not in ("id", "pool_state", "tenant_id", "created_at", "updated_at")
            }
            migrate.update({"state": "running", **driver_fields})
            db.force_update(real_id, migrate)
            db.delete(warm_id)
            return True
        except Exception as e:
            # resume 失败:该 warm 快照/VM 可能已损坏,删掉避免反复领到坏实例;
            # 打印异常(不静默吞)——否则暖池全回退冷建时无从排查(可观测性)。
            print(f"[warm_pool] claim resume failed for {warm_id}, "
                  f"fallback to cold create: {e!r}", file=sys.stderr, flush=True)
            db.delete(warm_id)
            return False

    # ------------------------------------------------------------------
    # replenish — 后台补充暖池水位
    # ------------------------------------------------------------------

    def replenish(self) -> None:
        current = db.count_warm(self._driver_name)
        need    = POOL_SIZE - current
        if need <= 0:
            return

        for _ in range(need):
            warm_id = f"warm-{uuid.uuid4().hex[:8]}"
            try:
                # 1. driver 层创建 VM
                driver_fields = self._driver.create(warm_id, _BASE_SPEC)
                # 2. 写入 DB(replenish 绕过了 app.py create_sandbox,需手动 put)
                db.put({
                    "id":         warm_id,
                    "tenant_id":  "__pool__",
                    "state":      "running",
                    "driver":     self._driver_name,
                    "pool_state": "running",
                    "updated_at": db._utcnow(),
                    **driver_fields,
                })
                # 3. suspend → 快照
                record = db.get(warm_id) or {}
                snap_info = self._driver.suspend(warm_id, record)
                # 4. 标记为 warm
                db.force_update(warm_id, {
                    "pool_state":  "warm",
                    "state":       "warm",
                    "driver":      self._driver_name,
                    "snapshot_s3": snap_info.get("snapshot_s3", ""),
                })
            except Exception:
                try:
                    db.delete(warm_id)
                except Exception:
                    pass

    def start_replenish_loop(self, is_leader=None) -> None:
        """
        后台补充暖池水位。
        is_leader: 可选的无参 callable,返回 True 才补充。多副本控制面下由
        Reconciler 的 leader 门控注入,避免多副本重复补池互相打架(gap P1-4)。
        None(默认)= 单副本/测试,始终补充。
        """
        def _loop():
            while True:
                try:
                    if is_leader is None or is_leader():
                        self.replenish()
                except Exception:
                    pass
                time.sleep(REFILL_EVERY)  # nosemgrep: arbitrary-sleep -- 暖池补充周期

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
