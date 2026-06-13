"""
Warm Pool — 对外隐藏冷启动,让 create 永远秒级完成。

FC 模式:预先 suspend 一批空白沙盒,create = resume + 注入配置(~7ms)
Kata 模式:交给 kubernetes-sigs/agent-sandbox SandboxWarmPool CRD 管理

使用方式:
  在控制面启动时 start_replenish_loop() 开始后台补充。
  create_sandbox() 调 WarmPool.claim() 先尝试从池子拿,没有再冷建。
"""
from __future__ import annotations

import os
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
        if not self._driver.capabilities().suspend_resume:
            return False   # Kata 模式交给 SandboxWarmPool CRD,这里不干预

        warm_id = db.claim_warm_item(self._driver_name)
        if not warm_id:
            return False

        record = db.get(warm_id)
        if not record:
            return False

        try:
            driver_fields = self._driver.resume(warm_id, record)
            # 迁移到 real_id:排除 pool 相关字段
            new_record = {
                k: v for k, v in record.items()
                if k not in ("id", "pool_state")
            }
            new_record.update({"id": real_id, "state": "running", **driver_fields})
            db.put(new_record)
            db.delete(warm_id)
            return True
        except Exception:
            db.delete(warm_id)
            return False

    # ------------------------------------------------------------------
    # replenish — 后台补充暖池水位
    # ------------------------------------------------------------------

    def replenish(self) -> None:
        if not self._driver.capabilities().suspend_resume:
            return

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

    def start_replenish_loop(self) -> None:
        def _loop():
            while True:
                try:
                    self.replenish()
                except Exception:
                    pass
                time.sleep(REFILL_EVERY)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
