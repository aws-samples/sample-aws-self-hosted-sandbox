#!/usr/bin/env python3
"""vCPU 绑核分配与 vCPU 准入的单元测试(报告 §12)。"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
_TMP = tempfile.TemporaryDirectory()
os.environ.update({"SBX_BASE": _TMP.name})

_SPEC = importlib.util.spec_from_file_location("node_agent_main_pin", _HERE / "main.py")
assert _SPEC and _SPEC.loader
agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(agent)


class TestPinning(unittest.TestCase):
    def setUp(self) -> None:
        agent._PINNED.clear()
        agent._COMMITTED_VCPUS.clear()
        agent._VMS.clear()
        patches = [
            patch.object(agent, "_host_cpus", return_value=[0, 1, 2, 3]),
            patch.object(agent, "VMM_PIN_VCPUS", True),
            patch.object(agent, "VCPU_OVERCOMMIT_RATIO", 2.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_parse_cpu_list(self) -> None:
        self.assertEqual(agent._parse_cpu_list("0-2,5, 7"), {0, 1, 2, 5, 7})
        self.assertEqual(agent._parse_cpu_list(""), set())

    def test_least_loaded_distinct_cpus(self) -> None:
        self.assertEqual(agent._reserve_vcpus("a", 1), [0])
        self.assertEqual(agent._reserve_vcpus("b", 1), [1])
        # 多 vCPU:挑最空的不同 CPU
        self.assertEqual(agent._reserve_vcpus("c", 2), [2, 3])
        # 全部负载 1 后从 0 开始轮转
        self.assertEqual(agent._reserve_vcpus("d", 1), [0])

    def test_reserve_is_idempotent_and_grows(self) -> None:
        self.assertEqual(agent._reserve_vcpus("a", 1), [0])
        self.assertEqual(agent._reserve_vcpus("a", 1), [0])
        # resume 时线程比提示多:保留已有 CPU,补分配
        self.assertEqual(agent._reserve_vcpus("a", 2), [0, 1])
        self.assertEqual(sum(agent._COMMITTED_VCPUS.values()), 2)

    def test_admission_rejects_when_full_and_release_frees(self) -> None:
        # 4 CPU × 2 = 8 vCPU 上限
        for i in range(8):
            agent._reserve_vcpus(f"s{i}", 1)
        with self.assertRaises(agent.VcpuCapacityError):
            agent._reserve_vcpus("extra", 1)
        self.assertNotIn("extra", agent._COMMITTED_VCPUS)
        agent._release_vcpus("s0")
        self.assertEqual(agent._reserve_vcpus("extra", 1), [0])
        usage = agent._vcpu_usage()
        self.assertEqual((usage["vcpu_committed"], usage["vcpu_free"]), (8, 0))

    def test_unlimited_ratio_reports_minus_one(self) -> None:
        with patch.object(agent, "VCPU_OVERCOMMIT_RATIO", 0.0):
            for i in range(20):
                agent._reserve_vcpus(f"s{i}", 1)
            self.assertEqual(agent._vcpu_usage()["vcpu_free"], -1)

    def test_no_pinning_still_counts_vcpus(self) -> None:
        with patch.object(agent, "VMM_PIN_VCPUS", False):
            self.assertEqual(agent._reserve_vcpus("a", 3), [])
        self.assertEqual(agent._COMMITTED_VCPUS["a"], 3)
        self.assertNotIn("a", agent._PINNED)

    def test_failed_operation_releases_reservation(self) -> None:
        def boom():
            raise RuntimeError("launch failed")
        with self.assertRaises(RuntimeError):
            agent._with_vcpu_reservation("a", 1, boom)
        self.assertNotIn("a", agent._COMMITTED_VCPUS)
        self.assertEqual(agent._with_vcpu_reservation("b", 1, lambda: "ok"), "ok")
        self.assertIn("b", agent._COMMITTED_VCPUS)

    def test_pin_threads_sets_single_cpu_per_vcpu(self) -> None:
        calls = []
        with patch.object(agent, "_vcpu_threads", return_value=[(0, 101), (1, 102)]), \
                patch.object(agent.os, "listdir", return_value=["100", "101", "102", "103"]), \
                patch.object(agent.os, "sched_setaffinity", create=True, side_effect=lambda t, c: calls.append((t, c))):
            cpus = agent._pin_vcpu_threads("a", 4242)
        self.assertEqual(cpus, [0, 1])
        # vCPU 线程各占一个 CPU;其余线程(API/设备模拟)放开到全部 CPU
        self.assertEqual(calls, [(101, {0}), (102, {1}), (100, {0, 1, 2, 3}), (103, {0, 1, 2, 3})])

    def test_stop_vmm_releases(self) -> None:
        agent._reserve_vcpus("a", 1)
        with patch.object(agent.os, "kill"):
            agent._stop_vmm("a", {"pid": 99999})
        self.assertNotIn("a", agent._COMMITTED_VCPUS)

    def test_adopt_rebuilds_from_thread_affinity(self) -> None:
        with patch.object(agent, "_vcpu_threads", return_value=[(0, 11), (1, 12)]), \
                patch.object(agent.os, "sched_getaffinity", create=True, side_effect=lambda t: {t - 9}):
            agent._adopt_pinning("a", 4242)
        self.assertEqual(agent._PINNED["a"], [2, 3])
        self.assertEqual(agent._COMMITTED_VCPUS["a"], 2)
        # 新分配避开已被接管的 CPU
        self.assertEqual(agent._reserve_vcpus("b", 2), [0, 1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
