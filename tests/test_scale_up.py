import copy
import json
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from simkit.api import Instance, QueryView
from my_scheduler import MyScheduler, Record

BASE = json.load(open(os.path.join(ROOT, "scenarios/dev_steady/config.json")))
NOW = 300_000.0


class FakeCtx:
    def client_stats(self, cid):
        return dict(miss_penalty_usd=getattr(self, "charged", {}).get(cid, 0.0))

    def __init__(self, cfg, sched):
        self.config, self.sched, self.now = cfg, sched, NOW
        self.instances, self.launched = {}, []

    def launch(self, it, market="on_demand"):
        iid = f"{it}-{len(self.launched) + 1}"
        self.launched.append(it)
        self.instances[iid] = Instance(iid, it, market, self.config["instance_types"][it]["slots"], self.now, 0.0)
        return iid

    def query(self, qid):
        rec = self.sched.pending.get(qid) or self.sched.running.get(qid)
        return rec.view if rec else None


class Setup:
    def __init__(self):
        self.s = MyScheduler()
        self.s.CHECK_CONSISTENCY_HOURLY = self.s.CHECK_INVARIANTS_AT_END = False
        self.s.USE_LEARNED_RUNTIMES = False
        self.ctx = FakeCtx(copy.deepcopy(BASE), self.s)
        self.s.on_start(self.ctx)
        self.n = 0

    def cls(self, client, qtype):
        c = self.ctx.config["clients"][client]
        return (client, qtype, float(c["sla_s"][qtype]), float(c["penalty_per_miss_usd"][qtype]))

    def add(self, client, qtype, age_s):
        self.n += 1
        c = self.ctx.config["clients"][client]
        v = QueryView(self.n, client, qtype, "t", NOW - age_s, float(c["sla_s"][qtype]))
        rec = Record(v, self.cls(client, qtype), None)
        rec.pool = self.s.pool_for(self.ctx.config, rec, NOW)
        self.s.enqueue(rec)
        return rec


class TestLatencyCritical(unittest.TestCase):
    def test_dev_classes(self):
        st = Setup()
        cfg = st.ctx.config
        lc = lambda c, q: st.s.is_latency_critical(cfg, st.cls(c, q))
        self.assertTrue(lc("acme_retail", "dashboard"))
        self.assertTrue(lc("finpay", "lookup"))
        self.assertFalse(lc("trial_pool", "dashboard"))
        self.assertFalse(lc("acme_retail", "filtered_agg"))
        self.assertFalse(lc("labs_ds", "adhoc"))
        self.assertFalse(lc("acme_retail", "report"))


class TestRateAndNeed(unittest.TestCase):
    def test_decayed_rate(self):
        st = Setup()
        c = st.cls("acme_retail", "dashboard")
        for k in range(600):
            st.s.note_arrival(c, NOW - 600 + k)
        lam = st.s.arrival_rate(c, NOW)
        self.assertAlmostEqual(lam, (1 - math.exp(-1)) * 1.0, delta=0.01)
        self.assertAlmostEqual(st.s.arrival_rate(c, NOW + 600), lam * math.exp(-1), places=9)

    def test_warm_capacity_square_root_staffing(self):
        st = Setup()
        c = st.cls("acme_retail", "dashboard")
        st.s.rate[c] = [0.74, NOW]
        pool = st.s.class_routing(st.ctx.config, st.cls("acme_retail", "dashboard"))[1]
        r = 0.74 * BASE["runtime_matrix_s"]["dashboard"][pool[0]]
        need = r + st.s.Z_WARM * math.sqrt(r) + st.s.B_WARM
        target = st.s.pool_target(st.ctx, pool, {pool: [0, 0]})
        self.assertAlmostEqual(target, need, places=9)
        self.assertGreater(need, r)

    def test_warm_capacity_covers_busy_lc_not_added_to_it(self):
        st = Setup()
        c = st.cls("acme_retail", "dashboard")
        st.s.rate[c] = [0.74, NOW]
        pool = st.s.class_routing(st.ctx.config, st.cls("acme_retail", "dashboard"))[1]
        need = st.s.pool_target(st.ctx, pool, {pool: [0, 0]})
        self.assertAlmostEqual(st.s.pool_target(st.ctx, pool, {pool: [3, 0]}), need)
        self.assertAlmostEqual(st.s.pool_target(st.ctx, pool, {pool: [20, 0]}), 20.0)
        self.assertAlmostEqual(st.s.pool_target(st.ctx, pool, {pool: [3, 4]}), need + 4)


class TestHorizonsAndWork(unittest.TestCase):
    def test_horizons(self):
        st = Setup()
        s, ctx = st.s, st.ctx
        h = lambda c, q, pool: s.horizons(ctx, st.cls(c, q), pool)
        self.assertAlmostEqual(h("labs_ds", "adhoc", ("mem.xlarge", "spot"))[0], 1800 - 450 * 1.15 - 570 - 60)
        self.assertEqual(h("acme_retail", "report", ("mem.xlarge", "spot"))[0], 14400)
        self.assertEqual(h("trial_pool", "adhoc", ("mem.xlarge", "spot"))[0], 14400)
        self.assertEqual(h("acme_retail", "dashboard", ("std.large", "spot"))[0], 60)

    def test_late_horizon_follows_oldest_queued_query(self):
        st = Setup()
        st.add("trial_pool", "adhoc", 86400 - 3000)
        su = 90 + 480
        expected = 3000 - st.s.m_drop(st.ctx.config) - su
        self.assertAlmostEqual(st.s.horizons(st.ctx, st.cls("trial_pool", "adhoc"), ("mem.xlarge", "spot"))[1], expected)

    def test_work_totals_follow_every_transition(self):
        st = Setup()
        s, ctx = st.s, st.ctx
        a = st.add("labs_ds", "adhoc", 0)
        b = st.add("labs_ds", "adhoc", 10)
        key = (a.cls, a.pool)
        self.assertEqual(s.work[key][2], 2)
        self.assertAlmostEqual(s.work[key][0], 2 * 450.0)
        s.move_to_late(b)
        self.assertEqual(s.work[key][2:], [1, 1])
        self.assertAlmostEqual(s.work[key][1], 450.0)
        del s.pending[a.view.id]; s.pool_count[a.pool] -= 1; s.work_remove(a)
        s.on_query_dropped(ctx, b.view)
        self.assertEqual(s.work[key], [0.0, 0.0, 0, 0])

    def test_lc_queue_enters_only_the_late_term(self):
        st = Setup()
        pool = st.s.class_routing(st.ctx.config, st.cls("acme_retail", "dashboard"))[1]
        for k in range(20):
            st.add("acme_retail", "dashboard", 1)
        c = st.cls("acme_retail", "dashboard")
        st.s.rate[c] = [0.74, NOW]
        h_late = st.s.horizons(st.ctx, c, pool)[1]
        need = st.s.pool_target(st.ctx, pool, {pool: [0, 0]}) - 20 * 3.0 / h_late
        r = 0.74 * 3.0
        self.assertAlmostEqual(need, r + st.s.Z_WARM * math.sqrt(r) + st.s.B_WARM,
                               places=6)
        self.assertGreater(h_late, 60)

    def test_rebuild_after_runtime_update(self):
        st = Setup()
        st.add("labs_ds", "adhoc", 0)
        cfg = copy.deepcopy(st.ctx.config)
        cfg["runtime_matrix_s"]["adhoc"]["mem.xlarge"] = 300.0
        st.ctx.config = cfg
        st.s.on_config_change(st.ctx, {"type": "runtime_update", "query_type": "adhoc"})
        key = (st.cls("labs_ds", "adhoc"), ("mem.xlarge", "spot"))
        self.assertAlmostEqual(st.s.work[key][0], 300.0)
        self.assertEqual(st.s.check_consistency(st.ctx), [])


class TestLaunches(unittest.TestCase):
    def test_booting_machines_are_not_launched_twice(self):
        st = Setup()
        s, ctx = st.s, st.ctx
        for k in range(40):
            st.add("labs_ds", "adhoc", 0)
        s.on_tick(ctx)
        first = len(ctx.launched)
        self.assertEqual(first, math.ceil(40 * 450 / (1800 - 450 * 1.15 - 570 - 60) / 16))
        s.on_tick(ctx)
        s.on_tick(ctx)
        self.assertEqual(len(ctx.launched), first)

    def test_burst_check_is_rate_limited(self):
        st = Setup()
        s, ctx = st.s, st.ctx
        for k in range(30):
            ctx.now = NOW + k * 0.3
            s.on_arrival(ctx, QueryView(1000 + k, "acme_retail", "dashboard", "t", ctx.now, 10.0))
        self.assertEqual(s.stats["burst_checks"], 1)
        self.assertEqual(len(ctx.launched), 1)
        ctx.now = NOW + 11
        s.on_arrival(ctx, QueryView(2000, "acme_retail", "dashboard", "t", ctx.now, 10.0))
        self.assertEqual(s.stats["burst_checks"], 2)


if __name__ == "__main__":
    unittest.main()