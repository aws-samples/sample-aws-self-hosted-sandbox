"""Multi-signal idle detection for automatic sandbox sleep."""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator


AUTO_SLEEP_IDLE_S = int(os.environ.get("AUTO_SLEEP_IDLE_S", "300"))
ACTIVITY_TOUCH_MIN_S = int(os.environ.get("ACTIVITY_TOUCH_MIN_S", "15"))


@dataclass(frozen=True)
class IdleDecision:
    idle: bool
    idle_seconds: float | None
    signal_age_seconds: float | None
    blockers: tuple[str, ...]
    active_connections: int


class IdleDetector:
    """Combines persisted activity timestamps with live connection signals."""

    def __init__(
        self,
        touch_fn: Callable[[str], None],
        *,
        idle_s: int = AUTO_SLEEP_IDLE_S,
        touch_min_s: int = ACTIVITY_TOUCH_MIN_S,
    ):
        self.idle_s = idle_s
        self.touch_min_s = touch_min_s
        self._touch_fn = touch_fn
        self._last_touch: dict[str, float] = {}
        self._connections: dict[str, int] = {}
        self._lock = threading.Lock()

    def touch(self, sid: str, *, force: bool = False) -> None:
        """Persist activity with per-process write throttling."""
        now = time.monotonic()
        with self._lock:
            last = self._last_touch.get(sid, 0.0)
            if not force and now - last < self.touch_min_s:
                return
            self._last_touch[sid] = now
        try:
            self._touch_fn(sid)
        except Exception:
            # Activity tracking must not break the user request path.
            pass

    @contextmanager
    def connection(self, sid: str) -> Iterator[None]:
        """Register a live tunnel and keep its persisted signal fresh."""
        with self._lock:
            self._connections[sid] = self._connections.get(sid, 0) + 1
        self.touch(sid, force=True)
        try:
            yield
        finally:
            with self._lock:
                remaining = self._connections.get(sid, 1) - 1
                if remaining > 0:
                    self._connections[sid] = remaining
                else:
                    self._connections.pop(sid, None)
            self.touch(sid, force=True)

    def forget(self, sid: str) -> None:
        with self._lock:
            self._last_touch.pop(sid, None)
            self._connections.pop(sid, None)

    def active_connections(self, sid: str) -> int:
        with self._lock:
            return self._connections.get(sid, 0)

    def decide(self, record: dict) -> IdleDecision:
        sid = str(record.get("id", ""))
        connections = self.active_connections(sid)
        blockers: list[str] = []
        if connections:
            blockers.append("active_connection")

        age = self._signal_age(record.get("last_active_at") or record.get("created_at"))
        if age is None:
            blockers.append("activity_signal_missing")
        elif age < self.idle_s:
            blockers.append("recent_activity")

        return IdleDecision(
            idle=not blockers,
            idle_seconds=age,
            signal_age_seconds=age,
            blockers=tuple(blockers),
            active_connections=connections,
        )

    @staticmethod
    def _signal_age(value: object) -> float | None:
        if not value or not isinstance(value, str):
            return None
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
        except (TypeError, ValueError):
            return None
