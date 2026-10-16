"""
Warm Pool — 对外隐藏冷启动,让 create 永远秒级完成。

预先 suspend 一批空白沙盒,create = resume + 注入配置(~秒级),对外隐藏冷启动。

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
from sandbox_api.observability import log_event, record_loop, register_loop

POOL_SIZE    = int(os.environ.get("WARM_POOL_SIZE", "5"))
REFILL_EVERY = int(os.environ.get("WARM_POOL_REFILL_S", "30"))
SNAPSHOT_SETTLE_S = float(os.environ.get("WARM_SNAPSHOT_SETTLE_S", "1"))
_POOL_PLACEMENT_ENABLED = os.environ.get(
    "POOL_PLACEMENT_ENABLED", "1"
).lower() in ("1", "true")
_POOL_CANDIDATE = os.environ.get(
    "WARM_POOL_POOL",
    os.environ.get("DEFAULT_CREATE_POOL", "protected"),
).strip().lower()
WARM_POOL_POOL = (
    _POOL_CANDIDATE
    if _POOL_PLACEMENT_ENABLED and _POOL_CANDIDATE in ("protected", "spot")
    else None
)

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

    @property
    def placement_pool(self) -> str | None:
        return WARM_POOL_POOL

    def can_claim(self, requested_pool: str | None) -> bool:
        return requested_pool == self.placement_pool

    def claim(
        self,
        real_id: str,
        spec: SandboxSpec,
        pool: str | None = None,
    ) -> bool:
        """
        尝试从暖池取一个沙盒并 resume,填充真实配置。
        成功返回 True(调用方跳过冷建);失败返回 False(调用方走冷建)。
        """
        if not self.can_claim(pool):
            return False

        # Journal the source before the node side effect. If the operator dies
        # after resume but before the DDB migration, the retry reuses this
        # exact warm VM instead of consuming another one.
        real_record = db.get(real_id) or {}
        warm_id = str(real_record.get("warm_source_id", ""))
        if not warm_id:
            warm_id = db.claim_warm_item(self._driver_name, pool) or ""
            if not warm_id:
                return False

        record = db.get(warm_id)
        if not record:
            db.force_update(
                real_id,
                {"warm_source_id": "", "runtime_operation": ""},
            )
            return False
        if (record.get("pool") or None) != pool:
            return False

        db.force_update(
            real_id,
            {
                "warm_source_id": warm_id,
                "runtime_operation": "warm_resume",
                "node": record.get("node", ""),
                "tap_idx": int(record.get("tap_idx", 0) or 0),
            },
        )

        def finish(driver_fields: dict) -> None:
            # Migrate the warm runtime fields to the already-created real
            # placeholder. Clear the journal only after the projection is
            # complete; then remove the consumed source.
            migrate = {
                k: v for k, v in record.items()
                if k not in (
                    "id", "pool_state", "tenant_id",
                    "created_at", "updated_at",
                )
            }
            migrate.update({
                "state": "running",
                "warm_source_id": "",
                "runtime_operation": "",
                **driver_fields,
            })
            db.force_update(real_id, migrate)
            db.delete(warm_id)

        try:
            # 用 real_id 注册 VM(后续 exec/suspend 按 real_id 路由),
            # 但从 warm_id 的快照/rootfs 恢复(本地快照在 warm_id 目录)。
            driver_fields = self._driver.resume(real_id, record, snapshot_id=warm_id)
            finish(driver_fields)
            return True
        except Exception as e:
            # A lost HTTP response can arrive after node-agent already made
            # the VM running. Probe the journaled node and retry the now
            # idempotent resume once before treating the source as damaged.
            try:
                journal = db.get(real_id) or {}
                if self._driver.get_runtime_state(
                    real_id, journal
                ) == "running":
                    driver_fields = self._driver.resume(
                        real_id, record, snapshot_id=warm_id
                    )
                    finish(driver_fields)
                    return True
            except Exception:
                pass
            # resume 失败:该 warm 快照/VM 可能已损坏,删掉避免反复领到坏实例;
            # 打印异常(不静默吞)——否则暖池全回退冷建时无从排查(可观测性)。
            log_event(
                "error", "warm_pool_claim_failed",
                sandbox_id=warm_id, error_type=type(e).__name__,
            )
            db.force_update(
                real_id,
                {
                    "warm_source_id": "",
                    "runtime_operation": "",
                    "node": "",
                    "tap_idx": 0,
                },
            )
            db.delete(warm_id)
            return False

    # ------------------------------------------------------------------
    # replenish — 后台补充暖池水位
    # ------------------------------------------------------------------

    def replenish(self) -> dict[str, int]:
        current = db.count_warm(self._driver_name, self.placement_pool)
        need    = POOL_SIZE - current
        stats = {"created": 0, "errors": 0}
        if need <= 0:
            return stats

        for _ in range(need):
            warm_id = f"warm-{uuid.uuid4().hex[:8]}"
            try:
                # 1. driver 层创建 VM
                driver_fields = self._driver.create(
                    warm_id, _BASE_SPEC, pool=self.placement_pool
                )
                # 2. 写入 DB(replenish 绕过了 app.py create_sandbox,需手动 put)
                db.put({
                    "id":         warm_id,
                    "tenant_id":  "__pool__",
                    "state":      "running",
                    "driver":     self._driver_name,
                    "pool_state": "running",
                    "pool":       self.placement_pool or "",
                    "updated_at": db._utcnow(),
                    **driver_fields,
                })
                # 3. 等 guest init 和 vsock agent 确实可用后再快照。刚收到
                # InstanceStart 就暂停会固化 early-boot 状态，在 nested KVM 上
                # 恢复后可能立即退出。
                record = db.get(warm_id) or {}
                rc, _, stderr = self._driver.exec(warm_id, record, "true")
                if rc != 0:
                    raise RuntimeError(f"warm guest readiness failed: rc={rc}, stderr={stderr}")
                if SNAPSHOT_SETTLE_S > 0:
                    time.sleep(SNAPSHOT_SETTLE_S)

                # 4. suspend → 快照
                snap_info = self._driver.suspend(warm_id, record)
                # 5. 标记为 warm
                db.force_update(warm_id, {
                    "pool_state":  "warm",
                    "state":       "warm",
                    "driver":      self._driver_name,
                    "snapshot_s3": snap_info.get("snapshot_s3", ""),
                })
                stats["created"] += 1
            except Exception as exc:
                stats["errors"] += 1
                log_event(
                    "error", "warm_pool_replenish_failed",
                    sandbox_id=warm_id, error_type=type(exc).__name__,
                )
                try:
                    db.delete(warm_id)
                except Exception:
                    pass
        return stats

    def start_replenish_loop(self, is_leader=None) -> None:
        """
        后台补充暖池水位。
        is_leader: 可选的无参 callable,返回 True 才补充。多副本控制面下由
        Reconciler 的 leader 门控注入,避免多副本重复补池互相打架(gap P1-4)。
        None(默认)= 单副本/测试,始终补充。
        """
        register_loop("warm_pool", REFILL_EVERY)

        def _loop():
            while True:
                try:
                    if is_leader is None or is_leader():
                        stats = self.replenish()
                        record_loop(
                            "warm_pool",
                            "error" if stats["errors"] else "success",
                        )
                    else:
                        record_loop("warm_pool", "skipped")
                except Exception as exc:
                    record_loop("warm_pool", "error")
                    log_event(
                        "error", "background_loop_failed",
                        loop="warm_pool", error_type=type(exc).__name__,
                    )
                time.sleep(REFILL_EVERY)  # nosemgrep: arbitrary-sleep -- 暖池补充周期

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
