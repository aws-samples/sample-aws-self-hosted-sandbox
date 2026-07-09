"""
Sandbox 数据模型与能力声明。

历史上这里是"可插拔 driver"的 Protocol 抽象(Firecracker / Kata 两个后端)。
Kata 因无法快照/恢复(与本平台 spot 疏散核心诉求不符)已移除,现仅剩
FirecrackerDriver 一个实现,故抽象层已拍平——本模块只保留共享的数据类
(SandboxSpec / ServiceSpec / Capabilities)与 UnsupportedOperation。
"""
from __future__ import annotations

from dataclasses import dataclass, field


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
    suspend_resume: bool   # Firecracker=True
    warm_pool: bool
    migrate: bool          # 跨机迁移
