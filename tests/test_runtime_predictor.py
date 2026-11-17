import copy
import json
import math
import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from simkit.api import Instance, QueryView
from runtime_predictor import RuntimePredictor
from my_scheduler import MyScheduler, Record

BASE = json.load(open(os.path.join(ROOT, "scenarios/dev_steady/config.json")))


def view(qid, template="T1", client="acme_retail", qtype="filtered_agg", arrival=0.0, sla=900.0):
    return QueryView(qid, client, qtype, template, arrival, sla)


class TestPredictor(unittest.TestCase):
    def test_no_data_uses_prior_one_and_wide_margin(self):
        p = RuntimePredictor()
        v = view(1)
        self.assertEqual(p.m_hat(v), 1.0)
        self.assertAlmostEqual(p.m_up(v), 1 + 1.65 * math.sqrt(1.01), places=9)

    def test_divides_by_benchmark_at_assignment(self):
        p = RuntimePredictor()
        p.observe(view(1), runtime_s=480.0, bench_at_assign=240.0)
        self.assertEqual(p.tmpl[("acme_retail", "filtered_agg", "T1")], [1, 2.0])

    def test_shrinkage_towards_class_prior(self):
        p = RuntimePredictor()
        for i in range(3):
            p.observe(view(i, "A"), 2.0 * 240, 240.0)
        self.assertAlmostEqual(p.m_hat(view(9, "A")), (6.0 + 2 * 2.0) / 5)
        self.assertAlmostEqual(p.m_hat(view(9, "B")), 2.0)
        p.observe(view(10, "B"), 0.5 * 240, 240.0)
        cls_mean = (2.0 * 3 + 0.5) / 4
        self.assertAlmostEqual(p.m_hat(view(11, "B")), (0.5 + 2 * cls_mean) / 3)

    def test_clamp(self):
        p = RuntimePredictor()
        p.observe(view(1, "X"), 1e9, 1.0)
        p.observe(view(2, "Y"), 1e-9, 1.0)
        self.assertEqual(p.tmpl[("acme_retail", "filtered_agg", "X")][1], 50.0)
        self.assertEqual(p.tmpl[("acme_retail", "filtered_agg", "Y")][1], 0.02)

    def test_margin_tends_to_within_template_spread(self):
        p = RuntimePredictor()
        for i in range(200):
            p.observe(view(i, "W"), (0.92 if i % 2 else 1.08) * 100, 100.0)
        v = view(999, "W")
        ratio = p.m_up(v) / p.m_hat(v)
        self.assertTrue(1.12 < ratio < 1.15, ratio)

    def test_ignores_missing_benchmark(self):
        p = RuntimePredictor()
        self.assertIsNone(p.observe(view(1), 100.0, None))
        self.assertEqual(p.tmpl, {})


class FakeCtx:

    def client_stats(self, cid):
        return dict(miss_penalty_usd=getattr(self, "charged", {}).get(cid, 0.0))

    def __init__(self, cfg, sched):
        self.config, self.sched, self.now = cfg, sched, 50_000.0
        self.instances = {}

    def assign(self, qid, iid):
        self.instances[iid].running[qid] = self.now

    def launch(self, it, market="on_demand"):
        return None

    def query(self, qid):
        rec = self.sched.pending.get(qid) or self.sched.running.get(qid)
        return rec.view if rec else None


class TestSchedulerFeedsPredictor(unittest.TestCase):
    def setUp(self):
        self.s = MyScheduler()
        self.s.CHECK_CONSISTENCY_HOURLY = self.s.CHECK_INVARIANTS_AT_END = False
        self.ctx = FakeCtx(copy.deepcopy(BASE), self.s)
        self.s.on_start(self.ctx)
        inst = Instance("m1", "mem.xlarge", "spot", 16, 0.0, 0.0)
        inst.state, inst.ready_s = "ready", 1.0
        self.ctx.instances["m1"] = inst
        self.inst = inst

    def start(self, qid, template="T1"):
        v = view(qid, template, arrival=self.ctx.now, sla=900.0)
        rec = Record(v, ("acme_retail", "filtered_agg", 900.0, 0.5), ("mem.xlarge", "spot"))
        self.s.enqueue(rec)
        self.assertTrue(self.s.try_assign(self.ctx, rec, self.inst))
        return rec

    def finish(self, rec, runtime_s):
        self.inst.running.pop(rec.view.id, None)
        self.s.on_complete(self.ctx, rec.view, "m1", runtime_s)

    def test_runtime_update_during_the_run_does_not_distort_learning(self):
        rec = self.start(1)
        self.assertEqual(rec.bench_at_assign, 130.0)
        self.ctx.config["runtime_matrix_s"]["filtered_agg"]["mem.xlarge"] = 260.0
        self.finish(rec, 1.5 * 130.0)
        self.assertEqual(self.s.predictor.tmpl[("acme_retail", "filtered_agg", "T1")], [1, 1.5])
        self.s.USE_LEARNED_RUNTIMES = True
        nxt = Record(view(2), ("acme_retail", "filtered_agg", 900.0, 0.5), ("mem.xlarge", "spot"))
        self.assertAlmostEqual(self.s.mean_runtime(self.ctx.config, nxt, "mem.xlarge"), 260.0 * 1.5)

    def test_coverage_log_uses_the_estimate_made_at_start(self):
        rec = self.start(1)
        self.assertEqual(rec.pred[0], 0)
        self.finish(rec, 1.0 * 130.0)
        report = self.s.coverage_report()
        self.assertEqual(report["0"]["queries"], 1)
        self.assertEqual(report["0"]["within_m_up"], 1.0)

    def test_late_query_returns_on_time_when_its_template_estimate_falls(self):
        s, ctx = self.s, self.ctx
        s.USE_LEARNED_RUNTIMES = True
        for qid in (1, 2):
            self.finish(self.start(qid, "H"), 4.0 * 130.0)
        v = view(10, "H", arrival=ctx.now - 500.0, sla=900.0)
        waiting = Record(v, ("acme_retail", "filtered_agg", 900.0, 0.5), ("mem.xlarge", "spot"))
        s.enqueue(waiting)
        self.assertTrue(s.is_late(ctx.config, waiting, ctx.now))
        s.move_to_late(waiting)
        self.assertEqual(waiting.where, "late")
        for qid in range(100, 140):
            self.finish(self.start(qid, "H"), 1.0 * 130.0)
        self.assertEqual(waiting.where, "on_time")
        self.assertGreaterEqual(s.stats["late_returns"], 1)
        self.assertEqual(s.check_consistency(ctx), [])


def fresh(p, v):
    n, total = p.tmpl.get(p.template_key(v), (0, 0.0))
    mh = (total + p.K * p.prior(v.client_id, v.query_type)) / (n + p.K)
    s = p.spread(v)
    return mh, mh * (1.0 + p.Z * s), mh / (1.0 + p.Z * s)


class TestCache(unittest.TestCase):
    def setUp(self):
        self.views = [QueryView(i, c, t, f"{c}-{t}-{k}", 0.0, 60.0)
                      for i, (c, t, k) in enumerate((c, t, k) for c in ("a", "b")
                                                    for t in ("dashboard", "report") for k in range(4))]

    def check_all(self, p):
        for v in self.views:
            mh, up, lo = fresh(p, v)
            self.assertEqual(p.m_hat(v), mh)
            self.assertEqual(p.m_up(v), up)
            self.assertEqual(p.m_lo(v), lo)

    def test_cached_equals_fresh_after_random_observations(self):
        rng = random.Random(7)
        p = RuntimePredictor()
        self.check_all(p)
        for _ in range(500):
            v = rng.choice(self.views)
            p.m_up(v)
            p.observe(v, rng.uniform(1, 100), rng.uniform(5, 50))
        self.check_all(p)

    def test_observation_invalidates_its_type_only(self):
        p = RuntimePredictor()
        dash = [v for v in self.views if v.query_type == "dashboard"]
        rep = [v for v in self.views if v.query_type == "report"]
        for v in self.views:
            p.m_up(v)
        rep_before = {v.id: p._cache[p.template_key(v)] for v in rep}
        p.observe(dash[0], 30.0, 10.0)
        for v in rep:
            self.assertIs(p._cache[p.template_key(v)], rep_before[v.id])
            self.assertEqual(p._cache[p.template_key(v)][0], p.version.get("report", 0))
        for v in dash[1:]:
            self.assertNotEqual(p._cache[p.template_key(v)][0], p.version["dashboard"])
        self.check_all(p)

    def test_observe_returns_before_and_after_estimates(self):
        p = RuntimePredictor()
        v = self.views[0]
        before = p.m_hat(v)
        key, b, a = p.observe(v, 20.0, 10.0)
        self.assertEqual(b, before)
        self.assertEqual(a, fresh(p, v)[0])


if __name__ == "__main__":
    unittest.main()