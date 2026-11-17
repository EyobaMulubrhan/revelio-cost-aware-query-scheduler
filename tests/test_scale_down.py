import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from simkit.api import Instance, QueryView
from my_scheduler import MyScheduler, Record

BASE = json.load(open(os.path.join(ROOT, "scenarios/dev_steady/config.json")))
NOW = 100_000.0


class FakeCtx:

    def client_stats(self, cid):
        return dict(miss_penalty_usd=getattr(self, "charged", {}).get(cid, 0.0))

    def __init__(self, cfg, sched):
        self.config, self.sched, self.now = cfg, sched, NOW
        self.instances, self.launched, self.terminated = {}, [], []

    def launch(self, it, market="on_demand"):
        self.launched.append(it)
        return None

    def terminate(self, iid, drain=True):
        inst = self.instances[iid]
        if inst.running and drain:
            inst.draining = True
            return
        del self.instances[iid]
        self.terminated.append(iid)
        self.sched.on_instance_terminated(self, inst, "scheduler", [])

    def query(self, qid):
        rec = self.sched.pending.get(qid) or self.sched.running.get(qid)
        return rec.view if rec else None


class Fleet:
    def __init__(self, cfg=None):
        self.s = MyScheduler()
        self.s.CHECK_CONSISTENCY_HOURLY = self.s.CHECK_INVARIANTS_AT_END = False
        self.s.USE_LEARNED_RUNTIMES = False
        self.ctx = FakeCtx(cfg or copy.deepcopy(BASE), self.s)
        self.s.on_start(self.ctx)
        self.n = 0

    def machine(self, it, launched_ago, idle_for=None, busy=0):
        self.n += 1
        iid = f"{it}-{self.n:03d}"
        inst = Instance(iid, it, "spot", self.ctx.config["instance_types"][it]["slots"], NOW - launched_ago, 0.0)
        inst.state, inst.ready_s = "ready", NOW - launched_ago + 1
        for q in range(busy):
            inst.running[10_000 * self.n + q] = NOW - 5
        self.ctx.instances[iid] = inst
        self.s.ready[(it, "spot")][iid] = True
        if idle_for is not None:
            self.s.idle_since[iid] = NOW - idle_for
        return iid

    def tick(self):
        self.s.on_tick(self.ctx)


class TestScaleDown(unittest.TestCase):
    def test_idle_timeout_is_startup_time(self):
        f = Fleet()
        cfg = f.ctx.config
        self.assertEqual([f.s.idle_timeout(cfg, t) for t in ("std.small", "std.large", "mem.xlarge")],
                         [135, 240, 570])

    def test_released_only_after_its_own_idle_timeout(self):
        f = Fleet()
        young = f.machine("std.large", 3600, idle_for=239)
        old = f.machine("std.large", 3600, idle_for=240)
        mem = f.machine("mem.xlarge", 3600, idle_for=569)
        f.tick()
        self.assertEqual(f.ctx.terminated, [old])
        self.assertIn(young, f.ctx.instances)
        self.assertIn(mem, f.ctx.instances)

    def test_never_released_while_running(self):
        f = Fleet()
        busy = f.machine("std.large", 3600, busy=3)
        f.s.idle_since[busy] = NOW - 10_000
        f.tick()
        self.assertEqual(f.ctx.terminated, [])
        self.assertFalse(f.ctx.instances[busy].draining)

    def test_not_released_before_billing_minimum(self):
        cfg = copy.deepcopy(BASE)
        cfg["instance_types"]["std.small"].update(join_s=5, ready_s=10)
        f = Fleet(cfg)
        young = f.machine("std.small", 59, idle_for=40)
        old = f.machine("std.small", 61, idle_for=40)
        f.tick()
        self.assertEqual(f.ctx.terminated, [old])
        self.assertIn(young, f.ctx.instances)

    def test_hysteresis_keeps_capacity_above_target(self):
        f = Fleet()
        f.machine("std.large", 7200, busy=8)
        f.machine("std.large", 7200, busy=8)
        a = f.machine("std.large", 7200, idle_for=1000)
        b = f.machine("std.large", 7200, idle_for=500)
        f.tick()
        self.assertEqual(f.ctx.terminated, [a])
        self.assertIn(b, f.ctx.instances)
        self.assertEqual(f.s.stats["releases_held_by_target"], 1)

    def test_queued_work_raises_the_target(self):
        f = Fleet()
        idle = f.machine("mem.xlarge", 7200, idle_for=1000)
        for i in range(3):
            v = QueryView(i + 1, "acme_retail", "report", "t", NOW, 21600.0)
            f.s.enqueue(Record(v, ("acme_retail", "report", 21600.0, 5.0), ("mem.xlarge", "spot")))
        f.s.scale_down_idle(f.ctx, {("mem.xlarge", "spot"): 4.5})
        self.assertEqual(f.ctx.terminated, [])
        self.assertIn(idle, f.ctx.instances)

    def test_idle_longest_first_and_one_pool_does_not_hold_another(self):
        f = Fleet()
        newer = f.machine("std.large", 7200, idle_for=300)
        older = f.machine("std.large", 7200, idle_for=900)
        small = f.machine("std.small", 7200, idle_for=900)
        f.s.scale_down_idle(f.ctx, {("std.large", "spot"): 6.0, ("std.small", "spot"): 0.0})
        self.assertEqual(sorted(f.ctx.terminated), sorted([older, small]))
        self.assertIn(newer, f.ctx.instances)

    def test_released_machine_leaves_scheduler_state(self):
        f = Fleet()
        iid = f.machine("std.large", 3600, idle_for=1000)
        f.tick()
        self.assertNotIn(iid, f.s.ready[("std.large", "spot")])
        self.assertNotIn(iid, f.s.idle_since)
        self.assertEqual(f.s.stats["releases"], 1)
        self.assertEqual(f.s.stats["releases_not_immediate"], 0)


if __name__ == "__main__":
    unittest.main()