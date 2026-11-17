import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from simkit.api import Instance, QueryView
from my_scheduler import MyScheduler, Record

with open(os.path.join(ROOT, "scenarios/dev_steady/config.json")) as _f:
    BASE = json.load(_f)

POOL = ("mem.xlarge", "spot")
LATE_NOW = 800_000.0
QUEUE_NOW = 700_000.0


class FakeCtx:
    def __init__(self, cfg, sched, now):
        self.config, self.sched, self.now = cfg, sched, now
        self.instances, self.charged, self.terminated = {}, {}, []

    def client_stats(self, cid):
        return dict(miss_penalty_usd=self.charged.get(cid, 0.0))

    def launch(self, it, market="on_demand"):
        return None

    def terminate(self, iid, drain=True):
        inst = self.instances.pop(iid)
        self.terminated.append(iid)
        self.sched.on_instance_terminated(self, inst, "scheduler", [])

    def assign(self, qid, iid):
        self.instances[iid].running[qid] = self.now

    def query(self, qid):
        rec = self.sched.pending.get(qid) or self.sched.running.get(qid)
        return rec.view if rec else None


def _scheduler():
    s = MyScheduler()
    s.CHECK_CONSISTENCY_HOURLY = s.CHECK_INVARIANTS_AT_END = False
    s.USE_LEARNED_RUNTIMES = False
    return s


class LateQuerySetup:
    def __init__(self, cfg=None):
        self.s = _scheduler()
        self.ctx = FakeCtx(cfg or copy.deepcopy(BASE), self.s, LATE_NOW)
        self.s.on_start(self.ctx)
        self.n = 0

    def late(self, client, qtype, past_deadline_s=600.0):
        self.n += 1
        c = self.ctx.config["clients"][client]
        sla = float(c["sla_s"][qtype])
        v = QueryView(
            self.n,
            client,
            qtype,
            "t",
            LATE_NOW - sla - past_deadline_s,
            sla
        )
        rec = Record(
            v,
            (client, qtype, sla, float(c["penalty_per_miss_usd"][qtype])),
            POOL
        )
        self.s.enqueue(rec)
        self.s.move_to_late(rec)
        return rec

    def machine(self):
        inst = Instance("m1", "mem.xlarge", "spot", 16, LATE_NOW - 3600, 0.0)
        inst.state, inst.ready_s = "ready", LATE_NOW - 3000
        self.ctx.instances["m1"] = inst
        self.s.ready[POOL]["m1"] = True
        return inst


class QueueSetup:
    def __init__(self, cfg=None):
        self.s = _scheduler()
        self.ctx = FakeCtx(cfg or copy.deepcopy(BASE), self.s, QUEUE_NOW)
        self.s.on_start(self.ctx)
        self.n = 0

    def add(self, client, qtype, age_s=0.0, pool=None):
        self.n += 1
        c = self.ctx.config["clients"][client]
        v = QueryView(
            self.n,
            client,
            qtype,
            "t",
            self.ctx.now - age_s,
            float(c["sla_s"][qtype])
        )
        rec = Record(
            v,
            (client, qtype, v.sla_s, float(c["penalty_per_miss_usd"][qtype])),
            None
        )
        rec.pool = pool or self.s.pool_for(self.ctx.config, rec, self.ctx.now)
        self.s.enqueue(rec)
        return rec

    def take(self, pool):
        rec = self.s.select(self.ctx, pool)
        if rec is not None:
            del self.s.pending[rec.view.id]
            self.s.pool_count[rec.pool] -= 1
            self.s.work_remove(rec)
            self.s.set_committed(rec, False)
        return rec