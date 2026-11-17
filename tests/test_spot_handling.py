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
from my_scheduler import MyScheduler, Record
sys.path.insert(0, os.path.join(ROOT, "tests"))
from support import POOL, QUEUE_NOW, QueueSetup

BASE = json.load(open(os.path.join(ROOT, "scenarios/dev_steady/config.json")))
NOW = 500_000.0


def tightened():
    cfg = copy.deepcopy(BASE)
    cfg["instance_types"]["mem.xlarge"].update(spot_usd_per_hr=1.55, spot_interruptions_per_hr=0.3)
    return cfg


class FakeCtx:
    def client_stats(self, cid):
        return dict(miss_penalty_usd=getattr(self, "charged", {}).get(cid, 0.0))

    def __init__(self, cfg, sched):
        self.config, self.sched, self.now = cfg, sched, NOW
        self.instances, self.launched, self.terminated = {}, [], []
        self.full = set()

    def launch(self, it, market="on_demand"):
        if (it, market) in self.full:
            return None
        iid = f"{it}-{market}-{len(self.launched) + 1}"
        self.launched.append((it, market))
        self.instances[iid] = Instance(iid, it, market, self.config["instance_types"][it]["slots"], self.now, 0.0)
        return iid

    def terminate(self, iid, drain=True):
        inst = self.instances[iid]
        if inst.state == "ready" and inst.running and drain:
            inst.draining = True
            return
        del self.instances[iid]
        self.terminated.append(iid)
        self.sched.on_instance_terminated(self, inst, "scheduler", [])

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

    def add(self, client, qtype, age_s=0.0):
        self.n += 1
        c = self.ctx.config["clients"][client]
        v = QueryView(self.n, client, qtype, "t", self.ctx.now - age_s, float(c["sla_s"][qtype]))
        rec = Record(v, self.cls(client, qtype), None)
        rec.pool = self.s.pool_for(self.ctx.config, rec, self.ctx.now)
        self.s.enqueue(rec)
        return rec

    def machine(self, it, market="spot", busy=0, state="ready"):
        self.n += 1
        iid = f"{it}-{market}-m{self.n}"
        inst = Instance(iid, it, market, self.ctx.config["instance_types"][it]["slots"], NOW - 3600, 0.0)
        inst.state = state
        if state == "ready":
            inst.ready_s = NOW - 3000
            self.s.ready[(it, market)][iid] = True
            self.s.idle_since[iid] = NOW - 10
        for k in range(busy):
            inst.running[100_000 + 1000 * self.n + k] = NOW - 1
        self.ctx.instances[iid] = inst
        return inst


class TestSpotCost(unittest.TestCase):
    def test_hand_calculated_values(self):
        s = MyScheduler()
        base, tight = BASE, tightened()
        od = lambda cfg, t, r: s.price_per_slot_s(cfg, t, "on_demand") * r
        self.assertAlmostEqual(s.spot_cost(base, "mem.xlarge", 5400), 0.09463, places=5)
        self.assertAlmostEqual(od(base, "mem.xlarge", 5400), 0.25500, places=5)
        self.assertAlmostEqual(s.spot_cost(tight, "mem.xlarge", 5400), 0.18352, places=5)
        self.assertAlmostEqual(s.spot_cost(tight, "std.large", 16200), 0.13559, places=5)
        self.assertAlmostEqual(s.spot_cost(tight, "mem.xlarge", 450), 0.01234, places=5)
        self.assertAlmostEqual(od(tight, "mem.xlarge", 450), 0.02125, places=5)

    def test_no_interruptions_means_plain_price(self):
        s = MyScheduler()
        cfg = copy.deepcopy(BASE)
        cfg["instance_types"]["std.large"]["spot_interruptions_per_hr"] = 0
        self.assertAlmostEqual(s.spot_cost(cfg, "std.large", 1000),
                               s.price_per_slot_s(cfg, "std.large", "spot") * 1000)
        self.assertEqual(s.spot_cost(BASE, "std.large", 0), 0.0)

    def test_frequent_interruptions_make_long_queries_dearer_than_on_demand(self):
        s = MyScheduler()
        cfg = copy.deepcopy(BASE)
        cfg["instance_types"]["mem.xlarge"]["spot_interruptions_per_hr"] = 5.0
        r = 5400
        self.assertGreater(s.spot_cost(cfg, "mem.xlarge", r), s.price_per_slot_s(cfg, "mem.xlarge", "on_demand") * r)
        self.assertLess(s.spot_cost(cfg, "mem.xlarge", 3), s.price_per_slot_s(cfg, "mem.xlarge", "on_demand") * 3)

    def test_matches_closed_form_and_wald_identity(self):
        s = MyScheduler()
        c = s.price_per_slot_s(BASE, "mem.xlarge", "spot")
        for rate in (0.02, 0.08, 0.3, 2.0, 5.0):
            cfg = copy.deepcopy(BASE)
            cfg["instance_types"]["mem.xlarge"]["spot_interruptions_per_hr"] = rate
            lam = rate / 3600.0
            for r in (1.0, 450.0, 5400.0, 16200.0):
                q = math.exp(-lam * r)
                attempt = (1.0 - q) / lam
                expected = c * attempt / q
                self.assertAlmostEqual(s.spot_cost(cfg, "mem.xlarge", r) / expected, 1.0, places=9)
                self.assertAlmostEqual(s.spot_cost(cfg, "mem.xlarge", r),
                                       c * (math.exp(lam * r) - 1.0) / lam, places=12)

    def test_small_rate_tends_to_plain_price_without_cancellation(self):
        s = MyScheduler()
        cfg = copy.deepcopy(BASE)
        cfg["instance_types"]["mem.xlarge"]["spot_interruptions_per_hr"] = 1e-9
        c = s.price_per_slot_s(cfg, "mem.xlarge", "spot")
        x = 1e-9 / 3600.0 * 450.0
        ratio = s.spot_cost(cfg, "mem.xlarge", 450.0) / (c * 450.0)
        self.assertAlmostEqual(ratio, 1.0 + x / 2.0, places=15)
        naive = (math.exp(x) - 1.0) / x
        self.assertGreater(abs(naive - (1.0 + x / 2.0)), 1e-9)

    def test_increasing_in_rate_and_runtime_and_never_below_plain_price(self):
        s = MyScheduler()
        c = s.price_per_slot_s(BASE, "mem.xlarge", "spot")
        prev_rate = None
        for rate in (0.0, 0.02, 0.08, 0.3, 1.0, 5.0):
            cfg = copy.deepcopy(BASE)
            cfg["instance_types"]["mem.xlarge"]["spot_interruptions_per_hr"] = rate
            costs = [s.spot_cost(cfg, "mem.xlarge", r) for r in (10.0, 450.0, 5400.0)]
            self.assertEqual(costs, sorted(costs))
            for r, cost in zip((10.0, 450.0, 5400.0), costs):
                self.assertGreaterEqual(cost, c * r * (1 - 1e-12))
            if prev_rate is not None:
                self.assertTrue(all(a >= b for a, b in zip(costs, prev_rate)))
            prev_rate = costs

    def test_extreme_rate_returns_infinity(self):
        s = MyScheduler()
        cfg = copy.deepcopy(BASE)
        cfg["instance_types"]["mem.xlarge"]["spot_interruptions_per_hr"] = 1e6
        cost = s.spot_cost(cfg, "mem.xlarge", 5400.0)
        self.assertEqual(cost, math.inf)
        self.assertFalse(cost < s.price_per_slot_s(cfg, "mem.xlarge", "on_demand") * 5400.0)

    def test_monte_carlo_of_the_retry_policy(self):
        s = MyScheduler()
        cfg = tightened()
        lam, r = 0.3 / 3600.0, 5400.0
        rng = random.Random(1)
        total, n = 0.0, 20000
        for _ in range(n):
            while True:
                x = rng.expovariate(lam)
                if x >= r:
                    total += r
                    break
                total += x
        c = s.price_per_slot_s(cfg, "mem.xlarge", "spot")
        self.assertAlmostEqual((total / n) / (s.spot_cost(cfg, "mem.xlarge", r) / c), 1.0, delta=0.02)

    def test_spot_or_on_demand_choice_matches_two_attempt_blend(self):
        s = MyScheduler()
        for t in ("std.small", "std.large", "mem.xlarge"):
            for rate in (0.02, 0.08, 0.3, 1.0, 3.0, 5.0):
                cfg = copy.deepcopy(BASE)
                cfg["instance_types"][t]["spot_interruptions_per_hr"] = rate
                for r in (10.0, 450.0, 1125.0, 5400.0, 16200.0, 45000.0):
                    retry = s.spot_cost(cfg, t, r)
                    od = s.price_per_slot_s(cfg, t, "on_demand") * r
                    p = 1.0 - math.exp(-rate / 3600.0 * r)
                    blend = (1 - p * p) * retry + p * p * od if retry != math.inf else math.inf
                    self.assertEqual(retry < od, blend < od, (t, rate, r))


class TestRoutingMarket(unittest.TestCase):
    def test_routing_decisions(self):
        st = Setup(tightened())
        r = lambda c, q: st.s.class_routing(st.ctx.config, st.cls(c, q))[1]
        self.assertEqual(r("acme_retail", "dashboard"), ("std.large", "spot"))
        self.assertEqual(r("acme_retail", "report"), ("std.large", "spot"))
        cfg = tightened()
        cfg["clients"]["labs_ds"]["sla_s"]["adhoc"] = 900
        cfg["clients"]["labs_ds"]["penalty_per_miss_usd"]["adhoc"] = 0.4
        st2 = Setup(cfg)
        self.assertEqual(st2.s.class_routing(cfg, st2.cls("labs_ds", "adhoc"))[1], ("mem.xlarge", "spot"))

    def test_capacity_zero_skips_the_market(self):
        cfg = copy.deepcopy(BASE)
        cfg["instance_types"]["mem.xlarge"]["spot_capacity"] = 0
        st = Setup(cfg)
        self.assertNotEqual(st.s.class_routing(cfg, st.cls("acme_retail", "report"))[1], ("mem.xlarge", "spot"))

    def test_interruptions_alone_do_not_force_on_demand(self):
        st = Setup()
        rec = st.add("acme_retail", "report")
        rec.view.attempts = 2
        self.assertTrue(st.s.spot_ok(st.ctx.config, rec, "mem.xlarge", NOW))
        self.assertEqual(st.s.pool_for(st.ctx.config, rec, NOW), ("mem.xlarge", "spot"))

    def test_query_failing_spot_test_is_not_offered_a_spot_slot_and_is_repooled(self):
        st = Setup()
        rec = st.add("acme_retail", "report")
        self.assertEqual(rec.pool, ("mem.xlarge", "spot"))
        inst = st.machine("mem.xlarge", "spot")
        cfg = copy.deepcopy(st.ctx.config)
        cfg["instance_types"]["mem.xlarge"]["spot_usd_per_hr"] = 3.0
        st.ctx.config = cfg
        self.assertIsNone(st.s.select(st.ctx, ("mem.xlarge", "spot"), inst))
        self.assertEqual(rec.pool, ("mem.xlarge", "on_demand"))
        self.assertEqual(st.s.stats["spot_rejections"], 1)
        self.assertEqual(st.s.check_consistency(st.ctx), [])


class TestNotices(unittest.TestCase):
    def test_notice_while_booting_terminates(self):
        st = Setup()
        inst = st.machine("std.large", state="booting")
        inst.terminate_at_s = NOW + 100
        st.s.on_spot_notice(st.ctx, inst, inst.terminate_at_s)
        self.assertIn(inst.id, st.ctx.terminated)

    def test_notice_on_idle_ready_machine_terminates(self):
        st = Setup()
        inst = st.machine("std.large")
        inst.terminate_at_s = NOW + 120
        st.s.on_spot_notice(st.ctx, inst, inst.terminate_at_s)
        self.assertIn(inst.id, st.ctx.terminated)
        self.assertNotIn(inst.id, st.s.ready[("std.large", "spot")])

    def test_noticed_busy_machine_takes_only_short_queries(self):
        st = Setup()
        inst = st.machine("std.large", busy=4)
        inst.terminate_at_s = NOW + 60
        st.s.on_spot_notice(st.ctx, inst, inst.terminate_at_s)
        self.assertNotIn(inst.id, st.ctx.terminated)
        long_q = st.add("acme_retail", "filtered_agg")
        short_q = st.add("acme_retail", "dashboard")
        self.assertEqual(long_q.pool, ("std.large", "spot"))
        st.s.dispatch(st.ctx, ("std.large", "spot"))
        self.assertIn(short_q.view.id, inst.running)
        self.assertNotIn(long_q.view.id, inst.running)
        self.assertEqual(st.s.stats["noticed_violations"], 0)

    def test_noticed_machine_released_when_idle(self):
        st = Setup()
        inst = st.machine("std.large", busy=0)
        rec = st.add("acme_retail", "dashboard")
        st.s.dispatch(st.ctx, ("std.large", "spot"))
        self.assertIn(rec.view.id, inst.running)
        inst.terminate_at_s = NOW + 60
        inst.running.pop(rec.view.id)
        st.s.on_complete(st.ctx, rec.view, inst.id, 3.0)
        self.assertIn(inst.id, st.ctx.terminated)

    def test_noticed_machines_leave_have_and_busy_rule(self):
        st = Setup()
        pool = ("std.large", "spot")
        inst = st.machine("std.large", busy=0)
        self.assertEqual(st.s.capacity_slots(st.ctx, pool), 8)
        inst.terminate_at_s = NOW + 60
        self.assertEqual(st.s.capacity_slots(st.ctx, pool), 0)
        a, b = st.add("acme_retail", "dashboard"), st.add("acme_retail", "dashboard")
        for rec, end in ((a, NOW + 10), (b, NOW + 500)):
            del st.s.pending[rec.view.id]
            st.s.pool_count[rec.pool] -= 1
            st.s.work_remove(rec)
            rec.pred_end = end
            st.s.running[rec.view.id] = rec
            inst.running[rec.view.id] = NOW
        self.assertEqual(sum(st.s.busy_split(st.ctx)[pool]), 1)


class TestLaunchFallback(unittest.TestCase):
    def test_failed_spot_launch_cools_down_and_falls_back_for_paid_work(self):
        st = Setup()
        s, ctx = st.s, st.ctx
        ctx.full.add(("mem.xlarge", "spot"))
        for k in range(10):
            st.add("labs_ds", "adhoc")
        pool = ("mem.xlarge", "spot")
        s.scale_up(ctx, pool, s.pool_target(ctx, pool, s.busy_split(ctx)))
        self.assertEqual(s.stats["cooldowns"], 1)
        self.assertGreaterEqual(s.stats["fallback_launches"], 1)
        fb = [iid for iid in ctx.instances if iid in s.fallback]
        self.assertTrue(fb and all(ctx.instances[i].market == "on_demand" for i in fb))
        n = len(ctx.launched)
        s.scale_up(ctx, pool, s.pool_target(ctx, pool, s.busy_split(ctx)))
        self.assertEqual(len(ctx.launched), n)
        s.on_config_change(ctx, {"type": "instance_type_update", "instance_type": "mem.xlarge"})
        self.assertEqual(s.cooldown, {})

    def test_zero_penalty_work_waits_for_spot(self):
        st = Setup()
        s, ctx = st.s, st.ctx
        ctx.full.add(("mem.xlarge", "spot"))
        for k in range(10):
            st.add("trial_pool", "adhoc")
        pool = ("mem.xlarge", "spot")
        s.scale_up(ctx, pool, s.pool_target(ctx, pool, s.busy_split(ctx)))
        self.assertEqual(s.stats["fallback_launches"], 0)

    def test_fallback_machine_serves_the_spot_pool(self):
        st = Setup()
        s, ctx = st.s, st.ctx
        inst = st.machine("mem.xlarge", "on_demand")
        s.ready[("mem.xlarge", "on_demand")].pop(inst.id)
        s.fallback.add(inst.id)
        s.ready[("mem.xlarge", "spot")][inst.id] = True
        rec = st.add("labs_ds", "adhoc")
        self.assertEqual(rec.pool, ("mem.xlarge", "spot"))
        self.assertEqual(s.capacity_slots(ctx, ("mem.xlarge", "spot")), 16)
        self.assertEqual(s.capacity_slots(ctx, ("mem.xlarge", "on_demand")), 0)
        s.dispatch(ctx, ("mem.xlarge", "spot"))
        self.assertIn(rec.view.id, inst.running)
        self.assertEqual(s.stats["overflow_assigns"], 0)


class TestSpotPolicy(unittest.TestCase):
    def test_no_risk_term_in_routing(self):
        cfg = copy.deepcopy(BASE)
        cfg["instance_types"]["mem.xlarge"].update(spot_usd_per_hr=1.55, spot_interruptions_per_hr=0.3)
        cfg["clients"]["labs_ds"]["sla_s"]["adhoc"] = 900
        st = QueueSetup(cfg)
        cls = ("labs_ds", "adhoc", 900.0, 0.2)
        self.assertEqual(st.s.class_routing(cfg, cls)[1], ("mem.xlarge", "spot"))

    def test_noticed_machine_still_takes_only_short_queries(self):
        st = QueueSetup()
        inst = Instance("m1", "mem.xlarge", "spot", 16, QUEUE_NOW - 3600, 0.0)
        inst.state, inst.ready_s = "ready", QUEUE_NOW - 3000
        inst.running[999] = QUEUE_NOW - 5
        st.ctx.instances["m1"] = inst
        st.s.ready[POOL]["m1"] = True
        inst.terminate_at_s = QUEUE_NOW + 30
        st.s.on_spot_notice(st.ctx, inst, inst.terminate_at_s)
        long_q = st.add("labs_ds", "adhoc", pool=POOL)
        short_q = st.add("acme_retail", "dashboard", pool=POOL)
        st.s.dispatch(st.ctx, POOL)
        self.assertIn(short_q.view.id, inst.running)
        self.assertNotIn(long_q.view.id, inst.running)
        self.assertEqual(st.s.stats["noticed_violations"], 0)


if __name__ == "__main__":
    unittest.main()