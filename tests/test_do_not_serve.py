import copy
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from simkit.api import QueryView
from my_scheduler import Record
from support import BASE, POOL, LATE_NOW as NOW, LateQuerySetup as Setup

CAP = 3600.0


def committed_total(s, cid):
    recs = list(s.pending.values()) + list(s.running.values())
    return sum(r.cls[3] for r in recs if r.in_committed and r.view.client_id == cid)


class TestDecision(unittest.TestCase):

    def test_acme_report_with_cap_room_is_not_served(self):
        st = Setup()
        rec = st.late("acme_retail", "report")
        self.assertEqual(st.s.serve_decision(st.ctx.config, rec, NOW), "dns")

    def test_labs_report_is_spare_capacity_only(self):
        st = Setup()
        rec = st.late("labs_ds", "report")
        self.assertEqual(st.s.serve_decision(st.ctx.config, rec, NOW), "spare")

    def test_cheap_late_work_is_served(self):
        st = Setup()
        for client, qtype in (("labs_ds", "adhoc"), ("acme_retail", "dashboard"),
                              ("finpay", "filtered_agg"), ("trial_pool", "dashboard")):
            rec = st.late(client, qtype)
            self.assertEqual(st.s.serve_decision(st.ctx.config, rec, NOW), "serve", (client, qtype))

    def test_exhausted_cap_means_serve(self):
        st = Setup()
        st.ctx.charged["acme_retail"] = 1e6
        rec = st.late("acme_retail", "report")
        self.assertEqual(st.s.serve_decision(st.ctx.config, rec, NOW), "serve")

    def test_only_certain_misses_are_considered(self):
        st = Setup()
        st.n += 1
        v = QueryView(st.n, "acme_retail", "report", "t", NOW - 21600 + 5000, 21600.0)
        rec = Record(v, ("acme_retail", "report", 21600.0, 5.0), POOL)
        st.s.enqueue(rec)
        self.assertFalse(st.s.is_certain(st.ctx.config, rec, NOW))
        self.assertEqual(st.s.serve_decision(st.ctx.config, rec, NOW), "serve")

    def test_switch_off(self):
        st = Setup()
        st.s.DO_NOT_SERVE = False
        self.assertEqual(st.s.serve_decision(st.ctx.config, st.late("acme_retail", "report"), NOW), "serve")


class TestLifecycle(unittest.TestCase):
    def test_dns_record_leaves_heaps_work_and_committed_and_is_never_assigned(self):
        st = Setup()
        s = st.s
        rec = st.late("acme_retail", "report")
        st.machine()
        self.assertIsNone(s.select(st.ctx, POOL, st.ctx.instances["m1"]))
        self.assertEqual(rec.where, "dns")
        self.assertIn(rec.view.id, s.dns)
        self.assertEqual(rec.work, 0.0)
        self.assertFalse(rec.in_committed)
        self.assertEqual(s.committed["acme_retail"], 0.0)
        self.assertFalse(s.try_assign(st.ctx, rec, st.ctx.instances["m1"]))
        self.assertEqual(s.stats["dns_assigned"], 1)
        self.assertEqual(s.check_consistency(st.ctx), [])

    def test_dns_reversed_when_cap_runs_out(self):
        st = Setup()
        s = st.s
        rec = st.late("acme_retail", "report")
        s.mark_dns(rec)
        st.ctx.charged["acme_retail"] = 1e6
        s.reconsider_dns(st.ctx)
        self.assertEqual(rec.where, "late")
        self.assertNotIn(rec.view.id, s.dns)
        self.assertEqual(s.stats["dns_reversed"], 1)
        self.assertGreater(rec.work, 0.0)
        st.machine()
        self.assertIs(s.select(st.ctx, POOL, st.ctx.instances["m1"]), rec)

    def test_dropped_dns_record_is_cleaned_up(self):
        st = Setup()
        s = st.s
        rec = st.late("acme_retail", "report")
        s.mark_dns(rec)
        s.on_query_dropped(st.ctx, rec.view)
        self.assertNotIn(rec.view.id, s.pending)
        self.assertEqual(s.dns, {})
        self.assertEqual(s.stats["dns_dropped"], 1)
        self.assertEqual(s.check_consistency(st.ctx), [])

    def test_spare_only_is_tier4_and_not_counted_for_scaling(self):
        st = Setup()
        s = st.s
        rec = st.late("labs_ds", "report")
        s.apply_serve_decision(st.ctx.config, rec, NOW)
        self.assertTrue(rec.spare_only)
        self.assertEqual(rec.work, 0.0)
        self.assertEqual(s.tier_of(st.ctx.config, rec, "mem.xlarge", NOW, s.m_drop(st.ctx.config))[0], 4)
        self.assertEqual(s.check_consistency(st.ctx), [])


class TestOwnCommitment(unittest.TestCase):
    def test_own_commitment_near_cap(self):
        st = Setup()
        s = st.s
        st.ctx.charged["acme_retail"] = CAP - 5.0
        rec = st.late("acme_retail", "report")
        s.set_committed(rec, True)
        self.assertEqual(s.cap_room("acme_retail"), 0.0)
        self.assertEqual(s.capped_penalty("acme_retail", 5.0), 0.0)
        self.assertEqual(s.own_capped_penalty(rec), 5.0)
        self.assertEqual(s.serve_decision(st.ctx.config, rec, NOW), "dns")
        self.assertEqual(s.committed["acme_retail"], 5.0)

    def test_own_and_other_commitments(self):
        st = Setup()
        s = st.s
        a, b = st.late("acme_retail", "report"), st.late("acme_retail", "report")
        s.set_committed(a, True)
        s.set_committed(b, True)
        for charged, room, e, decision in ((3588, 2.0, 5.0, "dns"), (3592, -2.0, 3.0, "dns"),
                                           (3594, -4.0, 1.0, "serve")):
            st.ctx.charged["acme_retail"] = float(charged)
            self.assertEqual(s.cap_room("acme_retail"), room)
            self.assertEqual(s.own_capped_penalty(a), e, charged)
            self.assertEqual(s.serve_decision(st.ctx.config, a, NOW), decision, charged)
        self.assertEqual(s.committed["acme_retail"], 10.0)

    def test_uncommitted_query_sees_full_room_minus_others(self):
        st = Setup()
        s = st.s
        other = st.late("acme_retail", "report")
        s.set_committed(other, True)
        st.ctx.charged["acme_retail"] = 3593.0
        rec = st.late("acme_retail", "report")
        self.assertFalse(rec.in_committed)
        self.assertEqual(s.own_capped_penalty(rec), 2.0)

    def test_commitment_released_on_drop(self):
        st = Setup()
        s = st.s
        st.ctx.charged["acme_retail"] = 3000.0
        a, b = st.late("acme_retail", "report"), st.late("acme_retail", "report")
        s.set_committed(a, True)
        s.set_committed(b, True)
        s.on_query_dropped(st.ctx, a.view)
        s.on_query_dropped(st.ctx, a.view)
        self.assertEqual(s.committed["acme_retail"], 5.0)
        self.assertEqual(s.committed["acme_retail"], committed_total(s, "acme_retail"))
        self.assertEqual(s.cap_room("acme_retail"), CAP - 3000.0 - 5.0)
        self.assertEqual(s.check_consistency(st.ctx), [])

    def test_commitment_released_when_estimate_improves(self):
        st = Setup()
        s = st.s
        st.n += 1
        v = QueryView(st.n, "acme_retail", "report", "t", NOW - 21600 + 3000, 21600.0)
        rec = Record(v, ("acme_retail", "report", 21600.0, 5.0), POOL)
        s.enqueue(rec)
        s.move_to_late(rec)
        self.assertTrue(s.is_certain(st.ctx.config, rec, NOW))
        s.set_committed(rec, True)
        cfg = copy.deepcopy(st.ctx.config)
        for t in cfg["runtime_matrix_s"]["report"]:
            cfg["runtime_matrix_s"]["report"][t] = 1000.0
        st.ctx.config = cfg
        s.on_config_change(st.ctx, {"type": "runtime_update", "query_type": "report", "changes": {}})
        self.assertEqual(rec.where, "on_time")
        self.assertFalse(rec.in_committed)
        self.assertEqual(s.committed["acme_retail"], 0.0)
        self.assertEqual(s.check_consistency(st.ctx), [])

    def test_repeated_mark_and_reverse_never_double_counts(self):
        st = Setup()
        s = st.s
        other = st.late("acme_retail", "report")
        s.set_committed(other, True)
        rec = st.late("acme_retail", "report")
        for i in range(5):
            st.ctx.charged["acme_retail"] = 0.0
            s.retest_late(st.ctx)
            self.assertEqual(rec.where, "dns", i)
            self.assertEqual(s.committed["acme_retail"], committed_total(s, "acme_retail"))
            st.ctx.charged["acme_retail"] = 1e6
            s.reconsider_dns(st.ctx)
            s.reconsider_dns(st.ctx)
            self.assertEqual(rec.where, "late", i)
            self.assertEqual(s.committed["acme_retail"], committed_total(s, "acme_retail"))
            self.assertEqual(s.check_consistency(st.ctx), [], i)
        self.assertEqual(s.stats["dns_marked"], 10)
        self.assertEqual(s.stats["dns_reversed"], 10)
        self.assertEqual(s.committed["acme_retail"], 10.0)


class TestReassessmentTiming(unittest.TestCase):
    def test_dns_normal_dns_without_hourly_sweep(self):
        st = Setup()
        s = st.s
        rec = st.late("acme_retail", "report")
        s.mark_dns(rec)
        st.ctx.charged["acme_retail"] = 1e6
        s.on_tick(st.ctx)
        self.assertEqual(rec.where, "late")
        self.assertTrue(rec.in_committed)
        st.ctx.charged["acme_retail"] = 0.0
        inst = st.machine()
        self.assertIsNone(s.select(st.ctx, POOL, inst))
        self.assertEqual(rec.where, "dns")
        self.assertFalse(rec.in_committed)
        self.assertEqual(s.committed["acme_retail"], 0.0)
        self.assertFalse(s.try_assign(st.ctx, rec, inst))
        self.assertEqual(s.check_consistency(st.ctx), [])

    def test_dns_normal_assigned(self):
        st = Setup()
        s = st.s
        rec = st.late("acme_retail", "report")
        s.mark_dns(rec)
        st.ctx.charged["acme_retail"] = 1e6
        s.on_tick(st.ctx)
        inst = st.machine()
        picked = s.select(st.ctx, POOL, inst)
        self.assertIs(picked, rec)
        self.assertTrue(s.try_assign(st.ctx, rec, inst))
        self.assertIn(rec.view.id, s.running)
        self.assertNotIn(rec.view.id, s.pending)
        self.assertNotIn(rec.view.id, s.dns)
        self.assertEqual(s.committed["acme_retail"], 5.0)
        self.assertEqual(s.check_consistency(st.ctx), [])

    def test_config_change_re_decides_queued_late_records(self):
        st = Setup()
        s = st.s
        st.ctx.charged["acme_retail"] = CAP - 1.0
        rec = st.late("acme_retail", "report")
        s.retest_late(st.ctx)
        self.assertEqual(rec.where, "late")
        cfg = copy.deepcopy(st.ctx.config)
        cfg["clients"]["acme_retail"]["drop_penalty_usd"] = 0.10
        st.ctx.config = cfg
        s.on_config_change(st.ctx, {"type": "contract_update", "client_id": "acme_retail",
                                    "changes": {"drop_penalty_usd": 0.10}})
        self.assertEqual(rec.where, "dns")
        self.assertEqual(s.check_consistency(st.ctx), [])

    def test_reassessment_creates_no_duplicate_heap_entries(self):
        st = Setup()
        s = st.s
        rec = st.late("acme_retail", "report")
        for _ in range(3):
            s.mark_dns(rec) if rec.where != "dns" else None
            st.ctx.charged["acme_retail"] = 1e6
            s.reconsider_dns(st.ctx)
            st.ctx.charged["acme_retail"] = 0.0
        live = [e for h in s.queues.values() for name in ("on_time", "late") for e in h[name]
                if s.is_valid(e)]
        self.assertEqual(len(live), 1)
        self.assertEqual(s.check_consistency(st.ctx), [])


if __name__ == "__main__":
    unittest.main()