"""virtio-balloon 内存回收控制器(纯逻辑,无 I/O,便于单测)。

背景(见 README「内存回收」与 docs/balloon-内存回收-真机测试报告):
  - free page reporting(FPR)只把 guest *空闲列表* 里连续 >=2MiB 的块还给宿主,
    从不让 guest 丢 page cache。AI Agent 负载(装包、clone、读写大量文件)的
    guest 内存大头恰恰是 page cache,FPR 对它无效。
  - 传统气球充气(inflate)才是"强制回收 + 设上限"的杠杆:驱动在 guest 里分配
    并钉住页,迫使 guest 沿 空闲 → 干净缓存 → 脏缓存 → 匿名页 的阶梯自己挑最
    便宜的页交出来。但 virtio-balloon 本身不带任何策略,目标值必须由宿主决定。

本模块就是这个宿主侧策略(参考 oVirt MoM / ESX 的 share-out 思路,以及
Meta senpai 用 PSI 做压力信号的做法):

  触发  宿主 sbx.slice 的 memory.current/memory.max 越过高水位(或 PSI
        some avg10 越过阈值)进入 RECLAIM;降到低水位以下才退出(滞回,
        "回收要狠、归还要慢",避免目标值来回震荡)。
  分摊  只从"guest 自报可用内存 - 保底"里拿;空闲 VM(长时间无 exec)优先,
        同为空闲/活跃时按可回收量从大到小。每 VM 每 tick 最多充 step_mib,
        单 VM 气球不超过 max_ratio × 配置内存;步长随压力线性放大(高水位
        1×,到 slice 上限 max_step_factor×),压力越急回收越快。
  保护  guest 出现 major fault 激增 / swap-in / 可用内存跌破保底一半 → 立即
        放气一步并冷却;发现 deflate_on_oom 触发(actual 自己往回掉)→ 目标
        对齐 actual 并冷却;充气停滞(actual 连续数 tick 不涨)→ 目标对齐
        actual,避免驱动空转(Firecracker 文档的 caveat)。
  归还  压力低于低水位时,每 tick 每 VM 最多放 release_step_mib,慢慢还给 guest。

不可信 guest:气球是 best-effort,统计值由 guest 驱动提供,只作参考。宿主侧的
硬保障仍是 sbx.slice MemoryMax + zram + 每 VM service MemoryMax;本控制器只
在其上做"尽量不让宿主走到 swap/OOM"的优化,绝不依赖它做准入。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

MIB = 1024 * 1024


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class BalloonConfig:
    enabled: bool = True
    interval_s: float = 5.0
    high_ratio: float = 0.80          # 进入 RECLAIM 的 slice 使用率
    low_ratio: float = 0.65           # 退出 RECLAIM / 开始归还的 slice 使用率
    psi_some_avg10: float = 20.0      # slice memory.pressure some avg10(%) 触发阈值
    step_mib: int = 256               # 每 VM 每 tick 最多充气
    release_step_mib: int = 128       # 每 VM 每 tick 最多放气(归还)
    guest_reserve_mib: int = 256      # guest 至少保留的可用内存
    guest_reserve_pct: float = 0.10   # 或配置内存的这个比例(取大)
    max_step_factor: float = 4.0      # 压力越高步长越大:高水位 1×,到 slice 上限 4×
    max_ratio: float = 0.75           # 单 VM 气球上限占配置内存比例
    majflt_per_s: float = 50.0        # guest major fault 速率超过即视为吃紧
    idle_after_s: float = 120.0       # 超过这么久没 exec 视为空闲,优先回收
    cooldown_s: float = 60.0          # 吃紧/OOM 放气后该 VM 暂停充气的时间
    stall_ticks: int = 3              # actual 连续不涨的 tick 数 → 目标对齐 actual
    manual_hold_s: float = 600.0      # 手动设定目标后控制器不接管的时间

    @classmethod
    def from_env(cls) -> "BalloonConfig":
        cfg = cls._from_env_raw()
        default = cls()
        # 直接设置环境变量会绕过 Terraform 的校验:不合法的组合退回默认值,
        # 保证滞回成立(0 < low < high < 1)、比例都在 (0, 1] 内。
        if not (0 < cfg.low_ratio < cfg.high_ratio < 1):
            cfg.low_ratio, cfg.high_ratio = default.low_ratio, default.high_ratio
        if not (0 < cfg.max_ratio <= 1):
            cfg.max_ratio = default.max_ratio
        if not (0 <= cfg.guest_reserve_pct < 1):
            cfg.guest_reserve_pct = default.guest_reserve_pct
        return cfg

    @classmethod
    def _from_env_raw(cls) -> "BalloonConfig":
        return cls(
            enabled=os.environ.get("BALLOON_AUTO_RECLAIM", "1").lower()
            in ("1", "true"),
            interval_s=max(1.0, _env_float("BALLOON_CONTROL_INTERVAL_S", 5.0)),
            high_ratio=_env_float("BALLOON_RECLAIM_HIGH", 0.80),
            low_ratio=_env_float("BALLOON_RECLAIM_LOW", 0.65),
            psi_some_avg10=_env_float("BALLOON_PSI_SOME_AVG10", 20.0),
            step_mib=max(1, _env_int("BALLOON_STEP_MIB", 256)),
            release_step_mib=max(1, _env_int("BALLOON_RELEASE_STEP_MIB", 128)),
            guest_reserve_mib=max(0, _env_int("BALLOON_GUEST_RESERVE_MIB", 256)),
            guest_reserve_pct=_env_float("BALLOON_GUEST_RESERVE_PCT", 0.10),
            max_step_factor=max(1.0, _env_float("BALLOON_MAX_STEP_FACTOR", 4.0)),
            max_ratio=_env_float("BALLOON_MAX_RATIO", 0.75),
            majflt_per_s=_env_float("BALLOON_MAJFLT_PER_S", 50.0),
            idle_after_s=_env_float("BALLOON_IDLE_AFTER_S", 120.0),
            cooldown_s=_env_float("BALLOON_COOLDOWN_S", 60.0),
            stall_ticks=max(1, _env_int("BALLOON_STALL_TICKS", 3)),
            manual_hold_s=_env_float("BALLOON_MANUAL_HOLD_S", 600.0),
        )


@dataclass
class HostMem:
    current_bytes: int                # sbx.slice memory.current
    max_bytes: int                    # sbx.slice memory.max(无限制时用 MemTotal)
    psi_some_avg10: float = 0.0       # sbx.slice memory.pressure some avg10

    @property
    def ratio(self) -> float:
        return self.current_bytes / self.max_bytes if self.max_bytes > 0 else 0.0


@dataclass
class VmMem:
    sid: str
    mem_mib: int                      # 配置内存
    target_mib: int                   # 气球当前目标
    actual_mib: int                   # 气球当前实际
    available_bytes: int | None       # guest MemAvailable(stats 关闭时 None)
    major_faults: int | None = None   # 累计 major fault
    swap_in_bytes: int | None = None  # 累计 swap-in
    idle_s: float = 0.0               # 距上次 exec 的秒数


@dataclass
class VmTrack:
    last_majflt: int | None = None
    last_swap_in: int | None = None
    last_actual: int | None = None
    last_ts: float | None = None
    stall: int = 0
    cooldown_until: float = 0.0
    manual_until: float = 0.0


@dataclass
class ControllerState:
    mode: str = "normal"              # normal | reclaim
    vms: dict[str, VmTrack] = field(default_factory=dict)


@dataclass
class Action:
    sid: str
    target_mib: int
    reason: str                       # reclaim|release|distress|oom|stall


def reserve_mib(vm: VmMem, cfg: BalloonConfig) -> int:
    return max(cfg.guest_reserve_mib, int(vm.mem_mib * cfg.guest_reserve_pct))


def max_balloon_mib(vm: VmMem, cfg: BalloonConfig) -> int:
    return max(0, int(vm.mem_mib * cfg.max_ratio))


def reclaim_step_mib(host: HostMem, cfg: BalloonConfig) -> int:
    """回收步长随压力线性放大(真机实测 guest 驱动 1s 内可充 3GiB,瓶颈在决策周期)。"""
    span = max(1e-6, 1.0 - cfg.high_ratio)
    over = min(1.0, max(0.0, (host.ratio - cfg.high_ratio) / span))
    return int(cfg.step_mib * (1.0 + (cfg.max_step_factor - 1.0) * over))


def update_mode(state: ControllerState, host: HostMem, cfg: BalloonConfig) -> str:
    pressured = host.ratio >= cfg.high_ratio or (
        cfg.psi_some_avg10 > 0 and host.psi_some_avg10 >= cfg.psi_some_avg10
    )
    if state.mode == "normal" and pressured:
        state.mode = "reclaim"
    elif state.mode == "reclaim" and host.ratio < cfg.low_ratio and not (
        cfg.psi_some_avg10 > 0 and host.psi_some_avg10 >= cfg.psi_some_avg10
    ):
        state.mode = "normal"
    return state.mode


def plan(state: ControllerState, host: HostMem, vms: list[VmMem],
         cfg: BalloonConfig, now: float) -> list[Action]:
    """根据宿主压力与各 VM 统计,返回本 tick 要下发的气球目标调整。"""
    mode = update_mode(state, host, cfg)
    live = {vm.sid for vm in vms}
    for sid in list(state.vms):
        if sid not in live:
            state.vms.pop(sid, None)

    actions: dict[str, Action] = {}
    distressed: set[str] = set()

    # ---- 1. 每 VM 的保护性检查(任何模式下都做) ----
    for vm in vms:
        tr = state.vms.setdefault(vm.sid, VmTrack())
        dt = (now - tr.last_ts) if tr.last_ts is not None else None
        majflt_rate = 0.0
        if (dt and dt > 0 and vm.major_faults is not None
                and tr.last_majflt is not None):
            majflt_rate = max(0, vm.major_faults - tr.last_majflt) / dt
        swapped_in = (
            vm.swap_in_bytes is not None and tr.last_swap_in is not None
            and vm.swap_in_bytes > tr.last_swap_in
        )
        reserve = reserve_mib(vm, cfg)
        avail_mib = (vm.available_bytes // MIB
                     if vm.available_bytes is not None else None)

        if now < tr.manual_until:
            pass  # 手动目标保持期:不接管
        elif vm.target_mib > 0 and (
            majflt_rate > cfg.majflt_per_s or swapped_in
            or (avail_mib is not None and avail_mib < reserve // 2)
        ):
            # guest 吃紧:立即放气一步(比归还步长大,救急)并冷却
            new_t = max(0, min(vm.target_mib, vm.actual_mib) - cfg.step_mib)
            actions[vm.sid] = Action(vm.sid, new_t, "distress")
            tr.cooldown_until = now + cfg.cooldown_s
            distressed.add(vm.sid)
        elif (tr.last_actual is not None and vm.target_mib > 0
              and vm.actual_mib < tr.last_actual
              and vm.actual_mib < vm.target_mib):
            # actual 自己往回掉而目标没降 → deflate_on_oom 触发了
            actions[vm.sid] = Action(vm.sid, vm.actual_mib, "oom")
            tr.cooldown_until = now + cfg.cooldown_s
            distressed.add(vm.sid)
        elif vm.target_mib > vm.actual_mib:
            # 充气中:actual 不涨则累计停滞
            if tr.last_actual is not None and vm.actual_mib <= tr.last_actual:
                tr.stall += 1
            else:
                tr.stall = 0
            if tr.stall >= cfg.stall_ticks:
                actions[vm.sid] = Action(vm.sid, vm.actual_mib, "stall")
                tr.stall = 0
                tr.cooldown_until = now + cfg.cooldown_s
                distressed.add(vm.sid)
        else:
            tr.stall = 0

        tr.last_majflt = vm.major_faults
        tr.last_swap_in = vm.swap_in_bytes
        tr.last_actual = vm.actual_mib
        tr.last_ts = now

    # ---- 2. 模式驱动的充/放气 ----
    if mode == "reclaim":
        # 目标:把 slice 压到低水位以下
        need_mib = max(0, int(
            (host.current_bytes - cfg.low_ratio * host.max_bytes) // MIB
        ))
        # 正在充气但还没兑现的部分已在路上,别重复计
        inflight = sum(max(0, vm.target_mib - vm.actual_mib) for vm in vms)
        need_mib = max(0, need_mib - inflight)

        step = reclaim_step_mib(host, cfg)
        candidates = []
        for vm in vms:
            tr = state.vms[vm.sid]
            if (vm.sid in distressed or vm.sid in actions
                    or now < tr.cooldown_until or now < tr.manual_until
                    or vm.available_bytes is None):
                continue  # 没有统计 = 看不到 guest 状态,不冒险充气
            room = min(
                vm.available_bytes // MIB - reserve_mib(vm, cfg),
                max_balloon_mib(vm, cfg) - vm.target_mib,
                step,
            )
            if room <= 0:
                continue
            idle = vm.idle_s >= cfg.idle_after_s
            candidates.append((0 if idle else 1, -room, vm.sid, vm, room))
        candidates.sort()
        for _, _, _, vm, room in candidates:
            if need_mib <= 0:
                break
            take = min(room, need_mib)
            actions[vm.sid] = Action(vm.sid, vm.target_mib + take, "reclaim")
            need_mib -= take
    elif mode == "normal" and host.ratio < cfg.low_ratio:
        # 低于低水位才归还(高低水位之间保持不动 = 滞回)
        for vm in vms:
            tr = state.vms[vm.sid]
            if (vm.sid in actions or vm.target_mib <= 0
                    or now < tr.manual_until):
                continue
            new_t = max(0, vm.target_mib - cfg.release_step_mib)
            actions[vm.sid] = Action(vm.sid, new_t, "release")

    return [a for a in actions.values()]


def parse_stats(raw: dict) -> dict:
    """把 Firecracker GET /balloon/statistics 的返回规整成本模块用的字段。"""
    def _int(key):
        v = raw.get(key)
        return int(v) if isinstance(v, (int, float)) else None

    return {
        "target_mib": _int("target_mib") or 0,
        "actual_mib": _int("actual_mib") or 0,
        "available_bytes": _int("available_memory"),
        "free_bytes": _int("free_memory"),
        "total_bytes": _int("total_memory"),
        "cache_bytes": _int("disk_caches"),
        "major_faults": _int("major_faults"),
        "minor_faults": _int("minor_faults"),
        "swap_in_bytes": _int("swap_in"),
        "swap_out_bytes": _int("swap_out"),
        "oom_kill": _int("oom_kill"),
    }


def parse_psi_some_avg10(text: str) -> float:
    """解析 cgroup v2 memory.pressure 的 'some avg10=..' 字段。"""
    for line in text.splitlines():
        if line.startswith("some "):
            for part in line.split():
                if part.startswith("avg10="):
                    try:
                        return float(part.split("=", 1)[1])
                    except ValueError:
                        return 0.0
    return 0.0
