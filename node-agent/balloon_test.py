#!/usr/bin/env python3
"""Unit tests for the virtio-balloon reclaim controller (balloon.py + main glue)."""
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
os.environ.update({"SBX_BASE": _TMP.name, "HOST_CGROUP_ROOT": _TMP.name})

import balloon as B  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("node_agent_main_b", _HERE / "main.py")
assert _SPEC and _SPEC.loader
agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(agent)

GIB = 1024 * B.MIB


def host(ratio: float, psi: float = 0.0) -> B.HostMem:
    return B.HostMem(current_bytes=int(100 * GIB * ratio), max_bytes=100 * GIB,
                     psi_some_avg10=psi)


def vm(sid="a", mem=4096, target=0, actual=0, avail_mib=3000, majflt=0,
       swap_in=0, idle=0.0) -> B.VmMem:
    return B.VmMem(sid=sid, mem_mib=mem, target_mib=target, actual_mib=actual,
                   available_bytes=None if avail_mib is None else avail_mib * B.MIB,
                   major_faults=majflt, swap_in_bytes=swap_in, idle_s=idle)


class TestPlan(unittest.TestCase):
    def setUp(self):
        self.cfg = B.BalloonConfig()
        self.st = B.ControllerState()

    def test_no_pressure_no_action(self):
        self.assertEqual(B.plan(self.st, host(0.5), [vm()], self.cfg, 0), [])
        self.assertEqual(self.st.mode, "normal")

    def test_hysteresis(self):
        B.plan(self.st, host(0.81), [vm()], self.cfg, 0)
        self.assertEqual(self.st.mode, "reclaim")
        # between low and high: stays in reclaim
        B.plan(self.st, host(0.70), [vm()], self.cfg, 5)
        self.assertEqual(self.st.mode, "reclaim")
        B.plan(self.st, host(0.60), [vm()], self.cfg, 10)
        self.assertEqual(self.st.mode, "normal")
        # between low and high from normal: no reclaim, no release (hold)
        acts = B.plan(self.st, host(0.75), [vm(target=512, actual=512)], self.cfg, 15)
        self.assertEqual(self.st.mode, "normal")
        self.assertEqual(acts, [])

    def test_psi_triggers_reclaim(self):
        B.plan(self.st, host(0.5, psi=30.0), [vm()], self.cfg, 0)
        self.assertEqual(self.st.mode, "reclaim")

    def test_reclaim_respects_step_reserve_and_cap(self):
        acts = B.plan(self.st, host(0.95), [vm(avail_mib=3000)], self.cfg, 0)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0].reason, "reclaim")
        self.assertEqual(acts[0].target_mib, B.reclaim_step_mib(host(0.95), self.cfg))
        # little available memory: only above reserve (max(256, 409)=409)
        st = B.ControllerState()
        acts = B.plan(st, host(0.95), [vm(avail_mib=500)], self.cfg, 0)
        self.assertEqual(acts[0].target_mib, 500 - 409)
        # cap at max_ratio * mem
        st = B.ControllerState()
        acts = B.plan(st, host(0.95), [vm(target=3000, actual=3000, avail_mib=900)],
                      self.cfg, 0)
        self.assertEqual(acts[0].target_mib, int(4096 * 0.75))

    def test_step_scales_with_pressure(self):
        cfg = self.cfg
        self.assertEqual(B.reclaim_step_mib(host(0.80), cfg), 256)
        self.assertEqual(B.reclaim_step_mib(host(0.90), cfg), 640)
        self.assertEqual(B.reclaim_step_mib(host(1.00), cfg), 1024)
        self.assertEqual(B.reclaim_step_mib(host(0.50), cfg), 256)
        acts = B.plan(B.ControllerState(), host(1.0), [vm(avail_mib=3800)], cfg, 0)
        self.assertEqual(acts[0].target_mib, 1024)

    def test_no_stats_never_inflated(self):
        acts = B.plan(self.st, host(0.95), [vm(avail_mib=None)], self.cfg, 0)
        self.assertEqual(acts, [])

    def test_idle_first_and_need_bounded(self):
        # need = (0.66-0.65)*100GiB ≈ 1024MiB; step 256 → idle VMs first
        cfg = B.BalloonConfig(high_ratio=0.66, step_mib=1024)
        vms = [vm("busy", avail_mib=3000, idle=0), vm("idle", avail_mib=2000, idle=999)]
        acts = B.plan(self.st, host(0.66), vms, cfg, 0)
        self.assertEqual([a.sid for a in acts], ["idle"])
        self.assertLessEqual(acts[0].target_mib, 1025)

    def test_inflight_not_double_counted(self):
        cfg = B.BalloonConfig(high_ratio=0.66)
        # need ≈ 1024MiB, already 1024MiB in flight → nothing more
        acts = B.plan(self.st, host(0.66), [vm(target=1024, actual=0)], cfg, 0)
        self.assertEqual(acts, [])

    def test_distress_deflates_and_cools_down(self):
        B.plan(self.st, host(0.95), [vm(target=512, actual=512, majflt=0)], self.cfg, 0)
        acts = B.plan(self.st, host(0.95), [vm(target=512, actual=512, majflt=10000)],
                      self.cfg, 5)
        self.assertEqual(acts[0].reason, "distress")
        self.assertEqual(acts[0].target_mib, 512 - self.cfg.step_mib)
        # cooldown: no reclaim on next tick even though pressured
        acts = B.plan(self.st, host(0.95), [vm(target=256, actual=256, majflt=10000)],
                      self.cfg, 10)
        self.assertTrue(all(a.reason != "reclaim" for a in acts))

    def test_swap_in_is_distress(self):
        B.plan(self.st, host(0.5), [vm(target=512, actual=512, swap_in=0)], self.cfg, 0)
        acts = B.plan(self.st, host(0.5), [vm(target=512, actual=512, swap_in=4096)],
                      self.cfg, 5)
        self.assertEqual(acts[0].reason, "distress")

    def test_low_available_is_distress(self):
        acts = B.plan(self.st, host(0.95), [vm(target=1024, actual=1024, avail_mib=100)],
                      self.cfg, 0)
        self.assertEqual(acts[0].reason, "distress")

    def test_deflate_on_oom_detected(self):
        B.plan(self.st, host(0.7), [vm(target=1024, actual=1024)], self.cfg, 0)
        acts = B.plan(self.st, host(0.7), [vm(target=1024, actual=600)], self.cfg, 5)
        self.assertEqual(acts[0].reason, "oom")
        self.assertEqual(acts[0].target_mib, 600)

    def test_stall_aligns_target(self):
        cfg = B.BalloonConfig(stall_ticks=2)
        v = dict(target=2048, actual=300, avail_mib=3000)
        B.plan(self.st, host(0.7), [vm(**v)], cfg, 0)
        B.plan(self.st, host(0.7), [vm(**v)], cfg, 5)
        acts = B.plan(self.st, host(0.7), [vm(**v)], cfg, 10)
        self.assertEqual(acts[0].reason, "stall")
        self.assertEqual(acts[0].target_mib, 300)

    def test_release_is_gentle(self):
        acts = B.plan(self.st, host(0.3), [vm(target=1000, actual=1000)], self.cfg, 0)
        self.assertEqual(acts[0].reason, "release")
        self.assertEqual(acts[0].target_mib, 1000 - self.cfg.release_step_mib)

    def test_manual_hold(self):
        self.st.vms["a"] = B.VmTrack(manual_until=100)
        self.assertEqual(B.plan(self.st, host(0.3), [vm(target=1000, actual=1000)],
                                self.cfg, 50), [])
        acts = B.plan(self.st, host(0.3), [vm(target=1000, actual=1000)], self.cfg, 150)
        self.assertEqual(acts[0].reason, "release")

    def test_forgets_gone_vms(self):
        B.plan(self.st, host(0.5), [vm("x")], self.cfg, 0)
        B.plan(self.st, host(0.5), [], self.cfg, 5)
        self.assertNotIn("x", self.st.vms)


class TestParsers(unittest.TestCase):
    def test_parse_psi(self):
        text = ("some avg10=12.34 avg60=1.00 avg300=0.10 total=1\n"
                "full avg10=3.00 avg60=0 avg300=0 total=0\n")
        self.assertAlmostEqual(B.parse_psi_some_avg10(text), 12.34)
        self.assertEqual(B.parse_psi_some_avg10(""), 0.0)

    def test_parse_stats(self):
        p = B.parse_stats({"target_mib": 5, "actual_mib": 4, "available_memory": 10,
                           "disk_caches": 3, "major_faults": 7})
        self.assertEqual((p["target_mib"], p["actual_mib"], p["available_bytes"],
                          p["cache_bytes"], p["major_faults"]), (5, 4, 10, 3, 7))
        self.assertIsNone(p["swap_in_bytes"])

    def test_config_from_env_rejects_bad_combinations(self):
        with patch.dict(os.environ, {"BALLOON_RECLAIM_HIGH": "0.6",
                                     "BALLOON_RECLAIM_LOW": "0.7",
                                     "BALLOON_MAX_RATIO": "1.5"}):
            cfg = B.BalloonConfig.from_env()
        self.assertEqual((cfg.low_ratio, cfg.high_ratio), (0.65, 0.80))
        self.assertEqual(cfg.max_ratio, 0.75)

    def test_config_from_env(self):
        with patch.dict(os.environ, {"BALLOON_AUTO_RECLAIM": "0",
                                     "BALLOON_RECLAIM_HIGH": "0.9",
                                     "BALLOON_STEP_MIB": "bogus"}):
            cfg = B.BalloonConfig.from_env()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.high_ratio, 0.9)
        self.assertEqual(cfg.step_mib, 256)


class TestGlue(unittest.TestCase):
    def setUp(self):
        agent._VMS.clear()
        agent._VM_OP_LOCKS.clear()
        agent._VM_OP_LOCK_USERS.clear()
        agent._BALLOON_CTL.vms.clear()
        agent._BALLOON_CTL.mode = "normal"
        slice_dir = Path(_TMP.name) / "sbx.slice"
        (slice_dir / "sbx-vmm-a.service").mkdir(parents=True, exist_ok=True)
        (slice_dir / "memory.current").write_text(str(95 * GIB))
        (slice_dir / "memory.max").write_text(str(100 * GIB))
        (slice_dir / "memory.swap.current").write_text("0")
        (slice_dir / "memory.pressure").write_text("some avg10=1.00 avg60=0 avg300=0 total=0\n")
        (slice_dir / "sbx-vmm-a.service" / "memory.current").write_text(str(3 * GIB))
        (slice_dir / "sbx-vmm-a.service" / "memory.stat").write_text(
            f"anon {2 * GIB}\nfile {1 * GIB}\ninactive_file {GIB // 2}\n")
        (slice_dir / "memory.stat").write_text(f"anon {80 * GIB}\nfile {15 * GIB}\n"
                                              f"inactive_file {2 * GIB}\n")

    def _fake_fc(self, calls, stats=None):
        def fake(sock, method, path, body=None, timeout=15):
            calls.append((sock, method, path, body))
            if path == "/balloon/statistics":
                if stats is None:
                    raise RuntimeError("firecracker GET /balloon/statistics → 400")
                return stats
            if path == "/machine-config":
                return {"mem_size_mib": 4096}
            return {}
        return fake

    def test_working_set_excludes_inactive_file(self):
        slice_dir = Path(_TMP.name) / "sbx.slice"
        (slice_dir / "memory.stat").write_text(f"inactive_file {30 * GIB}\n")
        self.assertEqual(agent._host_vmm_memory()["current_bytes"], 65 * GIB)

    def test_tick_reads_cgroup_and_inflates(self):
        agent._VMS["a"] = {"state": "running", "sock": "/s/a",
                           "runtime_unit": "sbx-vmm-a.service", "last_exec_ts": 0}
        calls = []
        stats = {"target_mib": 0, "actual_mib": 0, "available_memory": 3000 * B.MIB,
                 "disk_caches": 2500 * B.MIB, "free_memory": 400 * B.MIB,
                 "major_faults": 0}
        with patch.object(agent, "_fc", self._fake_fc(calls, stats)):
            applied = agent.balloon_tick(now=1000.0)
        self.assertEqual([(a.sid, a.reason) for a in applied], [("a", "reclaim")])
        # working set 93/100 → step = 256 * (1 + 3 * 0.65) = 755
        self.assertIn(("/s/a", "PATCH", "/balloon", {"amount_mib": 755}), calls)
        st = agent._BALLOON_STATE
        self.assertEqual(st["mode"], "reclaim")
        self.assertEqual(st["host"]["source"], "cgroup")
        self.assertEqual(st["vms"]["a"]["host_mib"], 3072)
        self.assertEqual(st["vms"]["a"]["host_anon_mib"], 2048)
        self.assertEqual(st["vms"]["a"]["host_file_mib"], 1024)
        # working set = usage(95G) - inactive_file(2G)
        self.assertEqual(st["host"]["current_bytes"], 93 * GIB)
        self.assertEqual(st["host"]["usage_bytes"], 95 * GIB)
        self.assertEqual(st["vms"]["a"]["guest_cache_mib"], 2500)
        self.assertEqual(agent._VMS["a"]["mem_mib"], 4096)
        # metrics refresh must not raise and must export the new series
        agent._refresh_metrics()
        body, _ = agent.metrics_payload()
        self.assertIn(b"fc_balloon_memory_bytes", body)
        self.assertIn(b"fc_balloon_adjustments_total", body)

    def test_tick_skips_vm_without_stats(self):
        agent._VMS["a"] = {"state": "running", "sock": "/s/a", "mem_mib": 4096}
        calls = []
        with patch.object(agent, "_fc", self._fake_fc(calls, None)):
            applied = agent.balloon_tick(now=1.0)
        self.assertEqual(applied, [])
        self.assertEqual(agent._BALLOON_STATE["vms_without_stats"], 1)
        self.assertFalse(any(c[1] == "PATCH" for c in calls))

    def test_tick_skips_busy_vm(self):
        agent._VMS["a"] = {"state": "running", "sock": "/s/a", "mem_mib": 4096}
        calls = []
        import threading
        held = threading.Event()
        release = threading.Event()

        def holder():
            with agent._vm_lifecycle("a"):
                held.set()
                release.wait(5)
        t = threading.Thread(target=holder)
        t.start()
        held.wait(5)
        try:
            with patch.object(agent, "_fc", self._fake_fc(calls, {"actual_mib": 0})):
                agent.balloon_tick(now=1.0)
        finally:
            release.set()
            t.join()
        self.assertEqual(calls, [])

    def test_long_exec_does_not_block_controller(self):
        # exec 持有 op 锁(vsock 长命令)时,控制器仍要能读统计/调整气球
        agent._VMS["a"] = {"state": "running", "sock": "/s/a", "mem_mib": 4096}
        calls = []
        import threading
        held, release = threading.Event(), threading.Event()

        def holder():
            with agent._vm_operation_lock("a"):
                held.set()
                release.wait(5)
        t = threading.Thread(target=holder)
        t.start()
        held.wait(5)
        try:
            with patch.object(agent, "_fc", self._fake_fc(
                    calls, {"actual_mib": 0, "available_memory": 3000 * B.MIB})):
                agent.balloon_tick(now=1.0)
        finally:
            release.set()
            t.join()
        self.assertTrue(any(c[2] == "/balloon/statistics" for c in calls))

    def test_balloon_locks_pruned_for_gone_vms(self):
        agent._VM_BALLOON_LOCKS.clear()
        agent._VMS["a"] = {"state": "running", "sock": "/s/a", "mem_mib": 4096}
        agent._balloon_lock("a")
        agent._balloon_lock("gone")
        with patch.object(agent, "_fc", self._fake_fc([], None)):
            agent.balloon_tick(now=1.0)
        self.assertIn("a", agent._VM_BALLOON_LOCKS)
        self.assertNotIn("gone", agent._VM_BALLOON_LOCKS)
        # 被其他线程(生命周期操作)持有的锁不能被清理
        import threading
        held, release = threading.Event(), threading.Event()

        def holder():
            with agent._vm_lifecycle("busy-gone"):
                held.set()
                release.wait(5)
        t = threading.Thread(target=holder)
        t.start()
        held.wait(5)
        try:
            agent._prune_balloon_locks()
            self.assertIn("busy-gone", agent._VM_BALLOON_LOCKS)
        finally:
            release.set()
            t.join()
        agent._prune_balloon_locks()
        self.assertNotIn("busy-gone", agent._VM_BALLOON_LOCKS)

    def test_manual_set_holds(self):
        agent._VMS["a"] = {"state": "running", "sock": "/s/a", "mem_mib": 4096}
        calls = []
        with patch.object(agent, "_fc", self._fake_fc(calls)):
            out = agent.op_balloon_set({"id": "a", "amount_mib": 1024, "hold_s": 60})
        self.assertEqual(out["target_mib"], 1024)
        self.assertIn(("/s/a", "PATCH", "/balloon", {"amount_mib": 1024}), calls)
        self.assertGreater(agent._BALLOON_CTL.vms["a"].manual_until, 0)
        with self.assertRaises(agent.RequestValidationError):
            agent.op_balloon_set({"id": "a", "amount_mib": 99999})

    def test_default_balloon_has_stats(self):
        self.assertEqual(agent._DEFAULT_BALLOON["stats_polling_interval_s"], 5)
        self.assertTrue(agent._DEFAULT_BALLOON["free_page_reporting"])
        self.assertTrue(agent._DEFAULT_BALLOON["deflate_on_oom"])


if __name__ == "__main__":
    unittest.main()
