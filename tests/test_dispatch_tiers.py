import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from simkit.api import QueryView
from my_scheduler import MyScheduler, Record
sys.path.insert(0, os.path.join(ROOT, "tests"))
from support import POOL, QueueSetup

BASE = json.load(open(os.path.join(ROOT, "scenarios/dev_steady/config.json")))
NOW = 200_000.0


class FakeCtx:

    def client_stats(self, cid):
        return dict(miss_penalty_usd=getattr(self, "charged", {}).get(cid, 0.0))

    def __init__(self, cfg):
        self.config = cfg
        self.now = NOW
        self.instances = {}
        self.launched = []

    def launch(self, it, market="on_demand"):
        self.launched.append((it, market))
        return f"{it}-{len(self.launched)}"

    def query(self, qid):
        rec = self.sched.pending.get(qid) or self.sched.running.get(qid)
        return rec.view if rec else None


def make_config():
    cfg = copy.deepcopy(BASE)
    cfg["clients"]["freebie"] = {"monthly_revenue_usd": 0, "sla_s": {"dashboard": 60, "adhoc": 7200},
                                 "penalty_per_miss_usd": {"dashboard": 0.0, "adhoc": 0.0},
                                 "monthly_penalty_cap_usd": 0, "drop_penalty_usd": 0.0}
    cfg["clients"]["missonly"] = {"monthly_revenue_usd": 0, "sla_s": {"dashboard": 10, "adhoc": 1800},
                                  "penalty_per_miss_usd": {"dashboard": 0.1, "adhoc": 0.2},
                                  "monthly_penalty_cap_usd": 100, "drop_penalty_usd": 0.0}
    return cfg


class Book:

    def __init__(self):
        self.ctx = FakeCtx(make_config())
        self.s = MyScheduler()
        self.s.CHECK_CONSISTENCY_HOURLY = self.s.CHECK_INVARIANTS_AT_END = False
        self.s.USE_LEARNED_RUNTIMES = False
        self.s.on_start(self.ctx)
        self.ctx.sched = self.s
        self.next_id = 0

    def add(self, client, qtype, age_s, pool):
        self.next_id += 1
        c = self.ctx.config["clients"][client]
        view = QueryView(self.next_id, client, qtype, "t-%d" % self.next_id, NOW - age_s, float(c["sla_s"][qtype]))
        rec = Record(view, (client, qtype, view.sla_s, float(c["penalty_per_miss_usd"][qtype])),
                     (pool, "spot") if isinstance(pool, str) else pool)
        self.s.enqueue(rec)
        return rec

    def select(self, pool, t=None):
        return self.s.select(self.ctx, (t or pool, "spot"))

    def take(self, pool):
        rec = self.select(pool)
        if rec is not None:
            del self.s.pending[rec.view.id]
            self.s.pool_count[rec.pool] -= 1
            self.s.work_remove(rec)
            self.s.set_committed(rec, False)
        return rec


class TestTiers(unittest.TestCase):
    def test_one_query_per_tier_is_taken_in_tier_order(self):
        b = Book()
        q4 = b.add("freebie", "dashboard", 1000, "std.large")
        q3 = b.add("trial_pool", "dashboard", 1, "std.large")
        q2 = b.add("acme_retail", "dashboard", 1, "std.large")
        q1 = b.add("labs_ds", "adhoc", 86400 - 600, "std.large")
        order = [b.select("std.large") for _ in range(4)]
        self.assertEqual(order, [q1, q2, q3, q4])
        self.assertEqual([b.s.stats[f"tier{i}"] for i in (1, 2, 3, 4)], [1, 1, 1, 1])
        self.assertIsNone(b.select("std.large"))
        self.assertEqual(len(b.s.pending), 4)

    def test_tier1_boundary_is_m_drop(self):
        b = Book()
        m = b.s.m_drop(b.ctx.config)
        self.assertEqual(m, 2 * (570 + 60))
        inside = b.add("labs_ds", "adhoc", 86400 - m, "mem.xlarge")
        b.add("acme_retail", "dashboard", 1, "mem.xlarge")
        self.assertIs(b.select("mem.xlarge"), inside)
        b2 = Book()
        b2.add("labs_ds", "adhoc", 86400 - m - 1, "mem.xlarge")
        dash = b2.add("acme_retail", "dashboard", 1, "mem.xlarge")
        self.assertIs(b2.select("mem.xlarge"), dash)

    def test_tier2_least_slack_first(self):
        b = Book()
        relaxed = b.add("acme_retail", "filtered_agg", 0, "mem.xlarge")
        urgent = b.add("acme_retail", "filtered_agg", 600, "mem.xlarge")
        self.assertIs(b.select("mem.xlarge"), urgent)
        self.assertIs(b.select("mem.xlarge"), relaxed)

    def test_tier2_penalty_per_second_breaks_near_ties(self):
        b = Book()
        agg = b.add("finpay", "filtered_agg", 600 - 130 * 1.15 - 1, "mem.xlarge")
        dash = b.add("acme_retail", "dashboard", 10 - 2.4 * 1.15 - 5, "mem.xlarge")
        self.assertIs(b.select("mem.xlarge"), dash)
        self.assertIs(b.select("mem.xlarge"), agg)

    def test_tier2_outside_window_least_slack_wins(self):
        b = Book()
        report = b.add("acme_retail", "report", 0, "mem.xlarge")
        agg = b.add("finpay", "filtered_agg", 300, "mem.xlarge")
        self.assertIs(b.select("mem.xlarge"), agg)
        self.assertIs(b.select("mem.xlarge"), report)

    def test_late_paid_query_competes_in_tier3_oldest_first(self):
        b = Book()
        trial = b.add("trial_pool", "dashboard", 5, "std.large")
        late = b.add("acme_retail", "dashboard", 30, "std.large")
        self.assertIs(b.select("std.large"), late)
        self.assertEqual(late.where, "late")
        self.assertEqual(b.s.stats["tier3"], 1)
        self.assertIs(b.select("std.large"), trial)

    def test_on_time_paid_work_beats_late_and_drop_only_work(self):
        b = Book()
        b.add("acme_retail", "dashboard", 30, "std.large")
        b.add("trial_pool", "dashboard", 50, "std.large")
        on_time = b.add("acme_retail", "filtered_agg", 0, "std.large")
        self.assertIs(b.select("std.large"), on_time)

    def test_late_record_returns_on_time_after_runtime_update(self):
        b = Book()
        rec = b.add("acme_retail", "filtered_agg", 800, "mem.xlarge")
        b.add("acme_retail", "report", 0, "mem.xlarge")
        b.take("mem.xlarge")
        self.assertEqual(rec.where, "late")
        b.ctx.config["runtime_matrix_s"]["filtered_agg"]["mem.xlarge"] = 50.0
        b.s.on_config_change(b.ctx, {"type": "runtime_update", "query_type": "filtered_agg"})
        self.assertEqual(rec.where, "on_time")
        self.assertEqual(b.s.stats["late_returns"], 1)
        self.assertEqual(b.s.check_consistency(b.ctx), [])

    def test_lookahead_skips_a_head_that_cannot_use_this_slot(self):
        b = Book()
        head = b.add("missonly", "dashboard", 3, "std.small")
        nxt = b.add("missonly", "dashboard", 0, "std.small")
        self.assertIs(b.take("std.small"), nxt)
        self.assertIn(head.view.id, b.s.pending)
        self.assertEqual(b.s.check_consistency(b.ctx), [])

    def test_heap_accounting_after_selection(self):
        b = Book()
        for age in range(0, 40, 2):
            b.add("acme_retail", "dashboard", age, "std.large")
            b.add("trial_pool", "dashboard", age, "std.large")
        for _ in range(15):
            b.take("std.large")
        self.assertEqual(b.s.check_consistency(b.ctx), [])


class TestFeasibility(unittest.TestCase):

    def setUp(self):
        self.b = Book()
        self.cfg = self.b.ctx.config

    def check(self, rec, t, ready, launch):
        s = self.b.s
        self.assertEqual(s.ready_slot_can_serve(self.cfg, rec, t, NOW), ready)
        self.assertEqual(s.launch_can_help(self.cfg, rec, t, NOW), launch)

    def test_filtered_agg_on_mem_xlarge(self):
        self.check(self.b.add("acme_retail", "filtered_agg", 0, "mem.xlarge"), "mem.xlarge", True, True)
        self.check(self.b.add("acme_retail", "filtered_agg", 300, "mem.xlarge"), "mem.xlarge", True, False)
        self.check(self.b.add("acme_retail", "filtered_agg", 800, "mem.xlarge"), "mem.xlarge", False, False)

    def test_dashboard_ready_slot_only(self):
        self.check(self.b.add("acme_retail", "dashboard", 0, "std.large"), "std.large", True, False)

    def test_feasible_fallback_only_on_fastest_type(self):
        s = self.b.s
        rec = self.b.add("acme_retail", "dashboard", 7.5, "mem.xlarge")
        self.assertFalse(s.ready_slot_can_serve(self.cfg, rec, "mem.xlarge", NOW))
        self.assertTrue(s.feasible(self.cfg, rec, "mem.xlarge", NOW))
        rec2 = self.b.add("acme_retail", "dashboard", 6.8, "std.large")
        self.assertFalse(s.feasible(self.cfg, rec2, "std.large", NOW))
        self.assertFalse(s.is_late(self.cfg, rec2, NOW))

    def test_late_uses_fastest_type_mean(self):
        s = self.b.s
        self.assertFalse(s.is_late(self.cfg, self.b.add("acme_retail", "dashboard", 7.5, "std.large"), NOW))
        self.assertTrue(s.is_late(self.cfg, self.b.add("acme_retail", "dashboard", 7.7, "std.large"), NOW))


class TestZeroValueNeverLaunches(unittest.TestCase):
    def test_counts_for_scaling(self):
        b = Book()
        s, cfg = b.s, b.ctx.config
        self.assertFalse(s.counts_for_scaling(cfg, b.add("freebie", "adhoc", 0, "mem.xlarge")))
        self.assertTrue(s.counts_for_scaling(cfg, b.add("trial_pool", "adhoc", 0, "mem.xlarge")))
        self.assertTrue(s.counts_for_scaling(cfg, b.add("missonly", "adhoc", 0, "mem.xlarge")))
        self.assertEqual(s.work.get((("freebie", "adhoc", 7200.0, 0.0), ("mem.xlarge", "spot"))), None)

    def test_arrival_of_zero_value_query_does_not_launch(self):
        b = Book()
        b.s.on_arrival(b.ctx, QueryView(1, "freebie", "adhoc", "t", NOW, 7200.0))
        b.s.on_arrival(b.ctx, QueryView(2, "freebie", "dashboard", "t", NOW, 60.0))
        self.assertEqual(b.ctx.launched, [])
        b.s.on_tick(b.ctx)
        self.assertEqual(b.ctx.launched, [])
        b.s.on_arrival(b.ctx, QueryView(3, "trial_pool", "adhoc", "t", NOW, 7200.0))
        self.assertEqual(b.ctx.launched, [])
        b.s.on_arrival(b.ctx, QueryView(4, "acme_retail", "dashboard", "t", NOW, 10.0))
        self.assertEqual(len(b.ctx.launched), 1)

    def test_tick_ignores_zero_value_backlog(self):
        b = Book()
        for i in range(500):
            b.add("freebie", "adhoc", i, "mem.xlarge")
        b.s.on_tick(b.ctx)
        self.assertEqual(b.ctx.launched, [])
        b.add("trial_pool", "adhoc", 0, "mem.xlarge")
        b.s.on_tick(b.ctx)
        self.assertEqual(len(b.ctx.launched), 1)

    def test_zero_value_runs_on_spare_capacity(self):
        b = Book()
        rec = b.add("freebie", "dashboard", 0, "std.large")
        self.assertIs(b.select("std.large"), rec)


class TestDropValueOrdering(unittest.TestCase):
    def test_tier3_prefers_most_drop_penalty_per_slot_second(self):
        st = QueueSetup()
        st.ctx.charged["acme_retail"] = 1e6
        report = st.add("acme_retail", "report", age_s=3000, pool=POOL)
        dash = st.add("acme_retail", "dashboard", age_s=100, pool=POOL)
        self.assertIs(st.take(POOL), dash)
        self.assertIs(st.take(POOL), report)
        self.assertEqual(st.s.stats["tier3"], 2)

    def test_tier1_prefers_most_drop_penalty_per_slot_second(self):
        st = QueueSetup()
        st.ctx.charged["acme_retail"] = 1e6
        near = 86400 - 600
        report = st.add("acme_retail", "report", age_s=near + 50, pool=POOL)
        dash = st.add("acme_retail", "dashboard", age_s=near, pool=POOL)
        self.assertIs(st.take(POOL), dash)
        self.assertEqual(st.s.stats["tier1"], 1)

    def test_equal_value_density_breaks_ties_by_age(self):
        st = QueueSetup()
        st.ctx.charged["trial_pool"] = 0.0
        younger = st.add("trial_pool", "dashboard", age_s=100, pool=POOL)
        older = st.add("trial_pool", "dashboard", age_s=200, pool=POOL)
        self.assertIs(st.take(POOL), older)
        self.assertIs(st.take(POOL), younger)

    def test_tier2_and_tier4_order_unchanged(self):
        st = QueueSetup()
        relaxed = st.add("acme_retail", "filtered_agg", age_s=0, pool=POOL)
        urgent = st.add("acme_retail", "filtered_agg", age_s=600, pool=POOL)
        self.assertIs(st.take(POOL), urgent)
        self.assertIs(st.take(POOL), relaxed)
        cfg = copy.deepcopy(BASE)
        cfg["clients"]["freebie"] = {"monthly_revenue_usd": 0, "sla_s": {"dashboard": 60, "report": 43200},
                                     "penalty_per_miss_usd": {"dashboard": 0.0, "report": 0.0},
                                     "monthly_penalty_cap_usd": 0, "drop_penalty_usd": 0.0}
        st2 = QueueSetup(cfg)
        old_long = st2.add("freebie", "report", age_s=500, pool=POOL)
        new_short = st2.add("freebie", "dashboard", age_s=10, pool=POOL)
        self.assertIs(st2.take(POOL), old_long)


if __name__ == "__main__":
    unittest.main()