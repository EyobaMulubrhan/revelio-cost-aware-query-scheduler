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
NOW = 400_000.0


class FakeCtx:

    def client_stats(self, cid):
        return dict(miss_penalty_usd=getattr(self, "charged", {}).get(cid, 0.0))

    def __init__(self, cfg, sched):
        self.config, self.sched, self.now = cfg, sched, NOW
        self.instances, self.launched = {}, []

    def launch(self, it, market="on_demand"):
        self.launched.append(it)
        return None

    def assign(self, qid, iid):
        self.instances[iid].running[qid] = self.now

    def query(self, qid):
        rec = self.sched.pending.get(qid) or self.sched.running.get(qid)
        return rec.view if rec else None


class Setup:
    def __init__(self, cfg=None, learned=False):
        self.s = MyScheduler()
        self.s.CHECK_CONSISTENCY_HOURLY = self.s.CHECK_INVARIANTS_AT_END = False
        self.s.USE_LEARNED_RUNTIMES = learned
        self.ctx = FakeCtx(cfg or copy.deepcopy(BASE), self.s)
        self.s.on_start(self.ctx)
        self.n = 0

    def cls(self, client, qtype):
        c = self.ctx.config["clients"][client]
        return (client, qtype, float(c["sla_s"][qtype]), float(c["penalty_per_miss_usd"][qtype]))

    def add(self, client, qtype, age_s=0.0, template="t", pool=None):
        self.n += 1
        c = self.ctx.config["clients"][client]
        v = QueryView(self.n, client, qtype, template, self.ctx.now - age_s, float(c["sla_s"][qtype]))
        rec = Record(v, self.cls(client, qtype), None)
        rec.pool = ((pool, "spot") if isinstance(pool, str) else pool) or self.s.pool_for(self.ctx.config, rec, self.ctx.now)
        self.s.enqueue(rec)
        return rec

    def machine(self, it, busy=0, ready=True):
        self.n += 1
        iid = f"{it}-{self.n}"
        inst = Instance(iid, it, "spot", self.ctx.config["instance_types"][it]["slots"], NOW - 3600, 0.0)
        if ready:
            inst.state, inst.ready_s = "ready", NOW - 3000
            self.s.ready[(it, "spot")][iid] = True
        for k in range(busy):
            inst.running[10_000 * self.n + k] = NOW - 1
        self.ctx.instances[iid] = inst
        return inst


class TestRoutingTable(unittest.TestCase):
    def test_dev_routing(self):
        st = Setup()
        cfg = st.ctx.config
        r = lambda c, q: st.s.class_routing(cfg, st.cls(c, q))
        self.assertEqual(r("acme_retail", "dashboard"), (["std.small", "std.large", "mem.xlarge"], ("std.large", "spot")))
        self.assertEqual(r("finpay", "lookup")[1], ("std.small", "spot"))
        self.assertEqual(r("labs_ds", "adhoc"), (["std.large", "mem.xlarge"], ("mem.xlarge", "spot")))
        self.assertEqual(r("acme_retail", "report")[1], ("mem.xlarge", "spot"))

    def test_tighter_sla_removes_slow_types(self):
        cfg = copy.deepcopy(BASE)
        cfg["clients"]["labs_ds"]["sla_s"]["adhoc"] = 900
        st = Setup(cfg)
        allowed, preferred = st.s.class_routing(cfg, st.cls("labs_ds", "adhoc"))
        self.assertEqual(allowed, ["mem.xlarge"])
        self.assertEqual(preferred[0], "mem.xlarge")

    def test_price_change_reroutes_and_repools_waiting_queries(self):
        st = Setup()
        rec = st.add("labs_ds", "adhoc")
        self.assertEqual(rec.pool, ("mem.xlarge", "spot"))
        cfg = copy.deepcopy(st.ctx.config)
        cfg["instance_types"]["mem.xlarge"]["spot_usd_per_hr"] = 1.55
        st.ctx.config = cfg
        st.s.on_config_change(st.ctx, {"type": "instance_type_update", "instance_type": "mem.xlarge"})
        self.assertEqual(rec.pool, ("std.large", "spot"))
        self.assertEqual(st.s.stats["repooled"], 1)
        self.assertEqual(st.s.pool_count[("std.large", "spot")], 1)
        self.assertEqual(st.s.check_consistency(st.ctx), [])

    def test_std_large_twice_as_fast_switches_routing(self):
        cfg = copy.deepcopy(BASE)
        for q, row in cfg["runtime_matrix_s"].items():
            row["std.large"] = row["std.large"] / 2
        st = Setup(cfg)
        self.assertEqual(st.s.class_routing(cfg, st.cls("labs_ds", "adhoc"))[1][0], "std.large")
        self.assertEqual(st.s.class_routing(cfg, st.cls("acme_retail", "filtered_agg"))[1][0], "std.large")
        self.assertEqual(st.s.class_routing(cfg, st.cls("acme_retail", "report"))[1], ("std.large", "spot"))


class TestOverride(unittest.TestCase):
    def test_heavy_template_counted_in_faster_pool(self):
        st = Setup(learned=True)
        s, ctx = st.s, st.ctx
        p = s.predictor
        for k in range(300):
            p.observe(QueryView(9000 + k, "labs_ds", "adhoc", "L%d" % (k % 30), 0, 1800.0),
                      (0.75 if k % 2 else 0.85) * 450, 450.0)
        for k in range(6):
            p.observe(QueryView(9500 + k, "labs_ds", "adhoc", "H", 0, 1800.0),
                      (2.0 if k % 2 else 2.2) * 450, 450.0)
        cfg = copy.deepcopy(ctx.config)
        cfg["instance_types"]["mem.xlarge"]["spot_usd_per_hr"] = 1.55
        ctx.config = cfg
        s.on_config_change(ctx, {"type": "instance_type_update", "instance_type": "mem.xlarge"})
        self.assertEqual(s.class_routing(cfg, st.cls("labs_ds", "adhoc"))[1], ("std.large", "spot"))
        light = st.add("labs_ds", "adhoc", template="L1")
        heavy = st.add("labs_ds", "adhoc", template="H")
        self.assertEqual(light.pool, ("std.large", "spot"))
        self.assertEqual(heavy.pool[0], "mem.xlarge")
        self.assertGreaterEqual(s.stats["overrides"], 1)


class TestOverflow(unittest.TestCase):
    def test_overflow_when_waiting_would_miss(self):
        st = Setup()
        s, ctx = st.s, st.ctx
        st.machine("std.large", busy=8)
        free_small = st.machine("std.small", busy=0)
        dash = st.add("acme_retail", "dashboard", age_s=1.0)
        self.assertEqual(dash.pool, ("std.large", "spot"))
        self.assertTrue(s.may_overflow(ctx, dash, ("std.small", "spot"), {}))
        s.dispatch(ctx, ("std.small", "spot"))
        self.assertIn(dash.view.id, free_small.running)
        self.assertEqual(s.stats["overflow_assigns"], 1)

    def test_no_overflow_while_own_pool_has_a_feasible_free_slot(self):
        st = Setup()
        st.machine("std.large", busy=4)
        st.machine("std.small")
        dash = st.add("acme_retail", "dashboard", age_s=1.0)
        self.assertFalse(st.s.may_overflow(st.ctx, dash, ("std.small", "spot"), {}))

    def test_no_overflow_when_waiting_is_fine(self):
        st = Setup()
        own = st.machine("mem.xlarge", busy=16)
        busy_rec = st.add("acme_retail", "report")
        del st.s.pending[busy_rec.view.id]; st.s.pool_count[busy_rec.pool] -= 1; st.s.work_remove(busy_rec)
        busy_rec.pred_end = NOW + 30
        qid = next(iter(own.running))
        st.s.running[qid] = busy_rec
        st.machine("std.large")
        report = st.add("acme_retail", "report", age_s=0.0)
        self.assertEqual(report.pool, ("mem.xlarge", "spot"))
        self.assertFalse(st.s.may_overflow(st.ctx, report, ("std.large", "spot"), {}))

    def test_no_overflow_when_extra_cost_exceeds_penalty(self):
        st = Setup()
        st.machine("std.large", busy=8)
        st.machine("mem.xlarge")
        trial = st.add("trial_pool", "dashboard", age_s=1.0)
        self.assertEqual(trial.pool, ("std.large", "spot"))
        self.assertFalse(st.s.may_overflow(st.ctx, trial, ("mem.xlarge", "spot"), {}))

    def test_slot_of_a_type_the_class_may_not_use_is_never_offered(self):
        cfg = copy.deepcopy(BASE)
        cfg["clients"]["labs_ds"]["sla_s"]["adhoc"] = 900
        st = Setup(cfg)
        st.machine("std.large")
        rec = st.add("labs_ds", "adhoc", age_s=0.0)
        self.assertEqual(rec.pool[0], "mem.xlarge")
        self.assertIsNone(st.s.select(st.ctx, ("std.large", "spot")))
        self.assertIn(rec.view.id, st.s.pending)

    def test_expected_wait_states(self):
        st = Setup()
        s, ctx = st.s, st.ctx
        cfg = ctx.config
        st.add("labs_ds", "adhoc")
        self.assertAlmostEqual(s.expected_wait(ctx, ("mem.xlarge", "spot")), 570 + 60 + 450 / 16)
        inst = st.machine("mem.xlarge", ready=False)
        inst.launched_s = NOW - 100
        self.assertAlmostEqual(s.expected_wait(ctx, ("mem.xlarge", "spot")), 470 + 450 / 16)


if __name__ == "__main__":
    unittest.main()