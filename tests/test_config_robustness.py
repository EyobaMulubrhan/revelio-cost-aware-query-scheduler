import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from simkit.api import QueryView      # noqa: E402



class TestHourlyByClock(unittest.TestCase):

    def run_ticks(self, interval, hours):
        from support import LateQuerySetup as Setup
        st = Setup()
        st.ctx.config["tick_interval_s"] = interval
        fired = []
        orig = st.s.recompute_routing

        def spy(ctx):
            fired.append(ctx.now)
            return orig(ctx)
        st.s.recompute_routing = spy
        t = 0.0
        while t < hours * 3600:
            t += interval
            st.ctx.now = t
            st.s.on_tick(st.ctx)
        return fired

    def test_60s_ticks_fire_on_the_hour(self):
        self.assertEqual(self.run_ticks(60, 5), [3600.0 * k for k in range(1, 6)])

    def test_300s_ticks_fire_hourly_not_every_5_hours(self):
        self.assertEqual(self.run_ticks(300, 5), [3600.0 * k for k in range(1, 6)])

    def test_ticks_longer_than_an_hour_fire_every_tick(self):
        self.assertEqual(self.run_ticks(7200, 6), [7200.0, 14400.0, 21600.0])


class TestPartialRuntimeRow(unittest.TestCase):

    def setup(self, row):
        from support import LateQuerySetup as Setup, LATE_NOW as NOW
        st = Setup()
        cfg = st.ctx.config
        cfg["runtime_matrix_s"]["export"] = dict(row)                 
        cfg["clients"]["acme_retail"]["sla_s"]["export"] = 1800
        cfg["clients"]["acme_retail"]["penalty_per_miss_usd"]["export"] = 1.0
        st.s.on_config_change(st.ctx, {"type": "runtime_update", "query_type": "export",
                                       "changes": dict(row)})
        st.n += 1
        v = QueryView(1000 + st.n, "acme_retail", "export", "ac-ex-1", NOW, 1800.0)
        st.s.on_arrival(st.ctx, v)                                   
        return st, v

    def test_only_types_with_a_runtime_are_used(self):
        st, v = self.setup({"mem.xlarge": 260.0})                     
        s, cfg = st.s, st.ctx.config
        rec = s.pending[v.id]
        self.assertEqual(s.runnable_types(cfg, "export"), ["mem.xlarge"])
        self.assertEqual(s.class_routing(cfg, rec.cls)[0], ["mem.xlarge"])
        self.assertEqual(rec.pool[0], "mem.xlarge")
        self.assertEqual(s.fastest_type(cfg, rec), "mem.xlarge")
        self.assertEqual(s.mean_runtime(cfg, rec, "mem.xlarge"), 260.0)  
        self.assertIsNone(s.select(st.ctx, ("std.small", "spot")))       
        self.assertIsNone(s.select(st.ctx, ("std.large", "on_demand")))
        inst = st.machine()                                             
        self.assertIs(s.select(st.ctx, ("mem.xlarge", "spot"), inst), rec)
        self.assertTrue(s.try_assign(st.ctx, rec, inst))               
        self.assertEqual(rec.bench_at_assign, 260.0)
        self.assertIn(v.id, s.running)
        self.assertEqual(s.check_consistency(st.ctx), [])

    def test_full_rows_unchanged(self):
        st, _ = self.setup({"mem.xlarge": 260.0})
        cfg = st.ctx.config
        for qt in ("lookup", "dashboard", "filtered_agg", "adhoc", "report"):
            self.assertEqual(st.s.runnable_types(cfg, qt), list(cfg["instance_types"]))

    def test_no_runnable_type_parks_the_query_safely(self):
        st, v = self.setup({})                                        
        s = st.s
        self.assertNotIn(v.id, s.pending)
        self.assertIn(v.id, s.unrunnable)
        self.assertEqual(s.stats["arrived"], 1)
        self.assertEqual(s.stats["unrunnable_parked"], 1)
        inst = st.machine()
        self.assertIsNone(s.select(st.ctx, ("mem.xlarge", "spot"), inst))  
        self.assertEqual(s.check_consistency(st.ctx), [])
        s.on_query_dropped(st.ctx, v)                                 
        self.assertEqual(s.unrunnable, {})
        self.assertEqual(s.stats["dropped"], 1)
        self.assertEqual(s.stats["unknown_events"], 0)
        self.assertEqual(s.stats["arrived"], s.stats["completed"] + s.stats["dropped"])

    def test_parked_query_is_admitted_when_a_runtime_appears(self):
        st, v = self.setup({})
        s, cfg = st.s, st.ctx.config
        cfg["runtime_matrix_s"]["export"]["std.large"] = 480.0
        s.on_config_change(st.ctx, {"type": "runtime_update", "query_type": "export",
                                    "changes": {"std.large": 480.0}})
        self.assertEqual(s.unrunnable, {})
        self.assertIn(v.id, s.pending)
        self.assertEqual(s.pending[v.id].pool[0], "std.large")
        self.assertEqual(s.pending[v.id].cls[3], 1.0)                
        self.assertEqual(s.stats["arrived"], 1)                       
        self.assertEqual(s.check_consistency(st.ctx), [])


if __name__ == "__main__":
    unittest.main()
