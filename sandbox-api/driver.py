"""
Sandbox driver abstraction — 统一接口,后端可插拔。
控制面 API 层只依赖这里的 Protocol,不直接碰 kubectl / Firecracker REST。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class UnsupportedOperation(Exception):
    """Driver 不支持某个操作时抛出,HTTP 层转 501。"""


@dataclass
class ServiceSpec:
    port: int
    protocol: str = "tcp"       # tcp | udp
    autostop: bool = False      # 字段占位,唤醒代理下阶段实现
    autostart: bool = False


@dataclass
class SandboxSpec:
    image: str
    cpu: int
    mem_mib: int
    env: dict[str, str] = field(default_factory=dict)
    services: list[ServiceSpec] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)


@dataclass
class Capabilities:
    suspend_resume: bool   # FC=True, Kata v1=False
    warm_pool: bool
    migrate: bool          # 跨机迁移


@runtime_checkable
class SandboxDriver(Protocol):
    def capabilities(self) -> Capabilities: ...

    def create(self, sandbox_id: str, spec: SandboxSpec) -> dict:
        """
        启动沙盒,返回 driver 专属字段写回 DynamoDB:
          FC:   {"node": instance_id, "guest_ip": ..., "tap_idx": N}
          Kata: {"pod_name": ..., "node": nodeName}
        """
        ...

    def destroy(self, sandbox_id: str, record: dict) -> None: ...

    def suspend(self, sandbox_id: str, record: dict) -> dict:
        """
        暂停+快照,返回 {"snapshot_s3": "s3://..."}.
        不支持时 raise UnsupportedOperation.
        """
        ...

    def resume(self, sandbox_id: str, record: dict) -> dict:
        """
        从快照恢复,返回 {"node": ..., "guest_ip": ...}.
        不支持时 raise UnsupportedOperation.
        """
        ...

    def exec(self, sandbox_id: str, record: dict, cmd: str) -> tuple[int, str, str]:
        """在沙盒内执行命令,返回 (rc, stdout, stderr)。"""
        ...

    def get_runtime_state(self, sandbox_id: str, record: dict) -> str:
        """
        实时查 VMM/Pod 存活状态,用于健康对账。
        返回: "running" | "stopped" | "unknown"
        """
        ...
