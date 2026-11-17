import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from simkit.api import Instance, QueryView
from my_scheduler import MyScheduler, Record
sys.path.insert(0, os.path.join(ROOT, "tests"))
from support import POOL, QUEUE_NOW, QueueSetup

BASE = json.load(open(os.path.join(ROOT, "scenarios/dev_steady/config.json")))
NOW = 600_000.0


class FakeCtx:
    def __init__(self, cfg, sched):
        self.config, self.sched, self.now = cfg, sched, NOW
        self.instances, self.charged = {}, {}

    def client_stats(self, cid):
        return dict(miss_penalty_usd=self.charged.get(cid, 0.0))

    def launch(self, it, market="on_demand"):
        return None

    def assign(self, qid, iid):
        self.instances[iid].running[qid] = self.now

    def query(self, qid):
        rec = self.sched.pending.get(qid) or self.sched.running.get(qid)
        return rec.view if rec else None


class Setup:
    def __init__(self, cfg=None):
        self.s = MyScheduler()
        self.s.CHECK_CONSISTENCY_HOURLY = self.s.CHECK_INVARIANTS_AT_END = False
        self.s.USE_LEARNED_RUNTIMES = False
        self.ctx = FakeCtx(cfg or copy.deepcopy(BASE), self.s)
        self.s.on_start(self.ctx)
        self.n = 0

    def add(self, client, qtype, age_s=0.0):
        self.n += 1
        c = self.ctx.config["clients"][client]
        v = QueryView(self.n, client, qtype, "t", self.ctx.now - age_s, float(c["sla_s"][qtype]))
        rec = Record(v, (client, qtype, v.sla_s, float(c["penalty_per_miss_usd"][qtype])), None)
        rec.pool = self.s.pool_for(self.ctx.config, rec, self.ctx.now)
        self.s.enqueue(rec)
        return rec


class TestCapRoom(unittest.TestCase):
    def test_room_is_cap_minus_charged_minus_committed(self):
        st = Setup()
        cap = BASE["clients"]["acme_retail"]["monthly_penalty_cap_usd"]
        self.assertAlmostEqual(st.s.cap_room("acme_retail"), cap)
        st.ctx.charged["acme_retail"] = cap - 0.25
        self.assertAlmostEqual(st.s.cap_room("acme_retail"), 0.25)
        self.assertAlmostEqual(st.s.capped_penalty("acme_retail", 5.0), 0.25)
        self.assertAlmostEqual(st.s.capped_penalty("acme_retail", 0.1), 0.1)

    def test_client_added_mid_month_is_prorated(self):
        cfg = copy.deepcopy(BASE)
        st = Setup(cfg)
        st.ctx.now = 10 * 86400.0
        cfg2 = copy.deepcopy(cfg)
        cfg2["clients"]["newco"] = copy.deepcopy(cfg["clients"]["finpay"])
        st.ctx.config = cfg2
        st.s.on_config_change(st.ctx, {"type": "client_added", "client_id": "newco", "t_s": 10 * 86400.0})
        cap = cfg["clients"]["finpay"]["monthly_penalty_cap_usd"]
        self.assertAlmostEqual(st.s.cap_room("newco"), cap * 20 / 30)

    def test_cap_cut_and_raise_recomputed_at_every_use(self):
        st = Setup()
        st.ctx.charged["acme_retail"] = 50.0
        cfg = copy.deepcopy(st.ctx.config)
        cfg["clients"]["acme_retail"]["monthly_penalty_cap_usd"] = 10
        st.ctx.config = cfg
        self.assertEqual(st.s.capped_penalty("acme_retail", 0.1), 0.0)
        cfg = copy.deepcopy(cfg)
        cfg["clients"]["acme_retail"]["monthly_penalty_cap_usd"] = 360
        st.ctx.config = cfg
        self.assertEqual(st.s.capped_penalty("acme_retail", 0.1), 0.1)

    def test_switch_off(self):
        st = Setup()
        st.s.CAP_AWARE = False
        st.ctx.charged["acme_retail"] = 1e9
        self.assertEqual(st.s.capped_penalty("acme_retail", 0.1), 0.1)


class TestCommitted(unittest.TestCase):
    def test_transitions_never_double_count(self):
        st = Setup()
        s = st.s
        rec = st.add("acme_retail", "filtered_agg", age_s=899.0)
        self.assertTrue(s.is_certain(st.ctx.config, rec, NOW))
        s.move_to_late(rec)
        s.set_committed(rec, True)
        s.set_committed(rec, True)
        self.assertAlmostEqual(s.committed["acme_retail"], 0.5)
        self.assertAlmostEqual(
            s.cap_room("acme_retail"),
            BASE["clients"]["acme_retail"]["monthly_penalty_cap_usd"] - 0.5
        )
        s.on_query_dropped(st.ctx, rec.view)
        self.assertAlmostEqual(s.committed["acme_retail"], 0.0)
        self.assertEqual(s.check_consistency(st.ctx), [])

    def test_late_sweep_marks_certain_and_selection_too(self):
        st = Setup()
        s = st.s
        rec = st.add("acme_retail", "filtered_agg", age_s=899.0)
        taken = s.select(st.ctx, rec.pool)
        self.assertIs(taken, rec)
        self.assertEqual(rec.where, "late")
        self.assertTrue(rec.in_committed)
        s.push_entry(rec)
        s.set_committed(rec, False)
        s.retest_late(st.ctx)
        self.assertTrue(rec.in_committed)
        self.assertEqual(s.check_consistency(st.ctx), [])

    def test_exhausted_cap_moves_queries_from_tier2_to_tier3(self):
        st = Setup()
        s, ctx = st.s, st.ctx
        dash = st.add("acme_retail", "dashboard")
        m = s.m_drop(ctx.config)
        self.assertEqual(s.tier_of(ctx.config, dash, "std.large", NOW, m)[0], 2)
        ctx.charged["acme_retail"] = 1e6
        self.assertEqual(s.tier_of(ctx.config, dash, "std.large", NOW, m)[0], 3)
        self.assertTrue(s.counts_for_scaling(ctx.config, dash))


class TestCapAccounting(unittest.TestCase):
    def test_committed_released_when_no_longer_certain(self):
        st = QueueSetup()
        s = st.s
        s.USE_LEARNED_RUNTIMES = True
        rec = st.add("acme_retail", "filtered_agg", age_s=899.0, pool=POOL)
        s.move_to_late(rec)
        s.retest_late(st.ctx)
        self.assertTrue(rec.in_committed)
        self.assertAlmostEqual(s.committed["acme_retail"], 0.5)
        st.ctx.config = copy.deepcopy(st.ctx.config)
        st.ctx.config["runtime_matrix_s"]["filtered_agg"]["mem.xlarge"] = 0.01
        s.on_config_change(st.ctx, {"type": "runtime_update", "query_type": "filtered_agg"})
        self.assertFalse(rec.in_committed)
        self.assertEqual(rec.where, "on_time")
        self.assertAlmostEqual(s.committed["acme_retail"], 0.0)
        self.assertEqual(s.check_consistency(st.ctx), [])

    def test_completion_moves_penalty_from_committed_to_charged(self):
        st = QueueSetup()
        s = st.s
        rec = st.add("acme_retail", "filtered_agg", age_s=899.0, pool=POOL)
        s.move_to_late(rec)
        s.set_committed(rec, True)
        inst = Instance("m2", "mem.xlarge", "spot", 16, QUEUE_NOW - 3600, 0.0)
        inst.state, inst.ready_s = "ready", QUEUE_NOW - 3000
        st.ctx.instances["m2"] = inst
        self.assertTrue(s.try_assign(st.ctx, rec, inst))
        self.assertAlmostEqual(s.committed["acme_retail"], 0.5)
        inst.running.pop(rec.view.id)
        st.ctx.charged["acme_retail"] = 0.5
        s.on_complete(st.ctx, rec.view, "m2", 130.0)
        self.assertAlmostEqual(s.committed["acme_retail"], 0.0)
        cap = BASE["clients"]["acme_retail"]["monthly_penalty_cap_usd"]
        self.assertAlmostEqual(s.cap_room("acme_retail"), cap - 0.5)


if __name__ == "__main__":
    unittest.main()