import heapq
import math
import sys

from simkit import Scheduler, SchedulerError

from runtime_predictor import RuntimePredictor


class Record:

    __slots__ = ("view", "cls", "pool", "version", "where",
                 "work",
                 "in_committed",
                 "spare_only",
                 "inst_id", "inst_type", "bench_at_assign", "start", "pred",
                 "pred_end")

    def __init__(self, view, cls, pool):
        self.view = view
        self.cls = cls
        self.pool = pool
        self.version = 0
        self.where = "on_time"
        self.work = 0.0
        self.in_committed = False
        self.spare_only = False
        self.inst_id = None
        self.inst_type = None
        self.bench_at_assign = None
        self.start = None
        self.pred = None
        self.pred_end = None


class MyScheduler(Scheduler):
    IDLE_STARTUP_FACTOR = 1.0
    SCALE_DOWN_MARGIN = 1.2
    H_MAX = 14400
    H_MIN = 60
    RATE_TAU_S = 600
    Z_WARM = 1.28
    B_WARM = 0
    BURST_CHECK_GAP_S = 10
    COOLDOWN_S = 600
    MARKETS = ("spot", "on_demand")
    CAP_AWARE = True
    DO_NOT_SERVE = True
    DNS_MARGIN = 0.05
    MONTH_S = 30 * 86400
    CLEAR_BACKLOG_WITHIN_S = 3600
    COMPACT_MIN_STALE = 1000
    RUNTIME_MARGIN = 1.15
    USE_LEARNED_RUNTIMES = True
    LOOKAHEAD = 4
    SLACK_TIE_S = 60
    CHECK_CONSISTENCY_HOURLY = True
    CHECK_INVARIANTS_AT_END = True

    def __init__(self):
        self.pending = {}
        self.running = {}
        self.queues = {}
        self.pool_count = {}
        self.routing = {}
        self.pool_order = []
        self.ready = {}
        self.fallback = set()
        self.cooldown = {}
        self.client_start = {}
        self.committed = {}
        self.dns = {}
        self.unrunnable = {}
        self.idle_since = {}
        self.seq = 0
        self.heap_entries = 0
        self.stats = dict(arrived=0, assigned=0, completed=0, dropped=0, requeued=0,
                          assign_errors=0, launches=0, launch_failures=0,
                          assigned_to_noticed=0, stale_pops=0, compactions=0,
                          max_pending=0, max_heap_entries=0, unknown_events=0,
                          consistency_checks=0, late_moves=0, late_returns=0,
                          tier1=0, tier2=0, tier3=0, tier4=0,
                          releases=0, releases_held_by_target=0, requeued_by_release=0,
                          releases_not_immediate=0, late_retests=0, burst_checks=0,
                          overflow_assigns=0, overrides=0, repooled=0, routing_recomputes=0,
                          notice_terminations=0, noticed_violations=0, spot_rejections=0,
                          fallback_launches=0, cooldowns=0,
                          became_certain=0, cap_limited_decisions=0,
                          dns_marked=0, dns_reversed=0, dns_dropped=0, spare_only_marked=0,
                          dns_assigned=0, unrunnable_parked=0, unrunnable_admitted=0,
                          unrunnable_dropped=0)
        self.invariant_errors = []
        self.consistency_errors = []
        self.ticks = 0
        self.next_hourly = 3600.0
        self.max_queue_s = None
        self.predictor = RuntimePredictor()
        self.late_index = {}
        self.coverage = {}
        self.work = {}
        self.rate = {}
        self.lc_cache = {}
        self.last_burst_check = {}

    def on_start(self, ctx):
        self._ctx = ctx
        self._cfg = ctx.config
        for cid in ctx.config["clients"]:
            self.client_start[cid] = 0.0
            self.committed[cid] = 0.0
        self._now = ctx.now
        self.add_pools(ctx)

    def add_pools(self, ctx):
        self.max_queue_s = float(ctx.config["max_queue_s"])
        for it in ctx.config["instance_types"]:
            for m in self.MARKETS:
                if (it, m) not in self.pool_count:
                    self.pool_count[(it, m)] = 0
                    self.ready[(it, m)] = {}
                    self.pool_order.append((it, m))

    @staticmethod
    def price_per_slot_s(cfg, t, market):
        it = cfg["instance_types"][t]
        price = it["spot_usd_per_hr"] if market == "spot" else it["on_demand_usd_per_hr"]
        return price / it["slots"] / 3600.0

    def slot_cost(self, cfg, mean_s, t, market):
        return mean_s * self.price_per_slot_s(cfg, t, market)

    def spot_cost(self, cfg, t, r):
        it = cfg["instance_types"][t]
        lam = it["spot_interruptions_per_hr"] / 3600.0
        c_spot = self.price_per_slot_s(cfg, t, "spot")
        if lam <= 0 or r <= 0:
            return c_spot * r
        x = lam * r
        if x > 700.0:
            return math.inf
        return c_spot * math.expm1(x) / lam

    @staticmethod
    def market_usable(cfg, t, market):
        it = cfg["instance_types"][t]
        return (it["spot_capacity"] if market == "spot" else it["on_demand_quota"]) > 0

    def spot_ok(self, cfg, rec, t, now):
        if not self.market_usable(cfg, t, "spot"):
            return False
        r = self.upper_runtime(cfg, rec, t)
        return self.spot_cost(cfg, t, r) < self.price_per_slot_s(cfg, t, "on_demand") * r

    @staticmethod
    def runnable_types(cfg, qtype):
        row = cfg["runtime_matrix_s"].get(qtype, {})
        return [t for t in cfg["instance_types"] if t in row]

    def class_routing(self, cfg, cls):
        r = self.routing.get(cls)
        if r is None:
            row = cfg["runtime_matrix_s"][cls[1]]
            types = self.runnable_types(cfg, cls[1])
            allowed = [t for t in types
                       if self.typical_runtime(cfg, cls, t, upper=True) <= cls[2]]
            if not allowed:
                allowed = [min(types, key=row.__getitem__)]
            now = self._now
            options = []
            for t in allowed:
                up = self.typical_runtime(cfg, cls, t, upper=True)
                for m in self.MARKETS:
                    usable = self.market_usable(cfg, t, m) and self.cooldown.get((t, m), -1) <= now
                    cost = (self.spot_cost(cfg, t, up) if m == "spot"
                            else self.price_per_slot_s(cfg, t, m) * up)
                    options.append((not usable, cost, row[t], m != "spot", (t, m)))
            r = self.routing[cls] = (allowed, min(options)[4])
        return r

    def pool_for(self, cfg, rec, now):
        allowed, (t, market) = self.class_routing(cfg, rec.cls)
        if now + self.upper_runtime(cfg, rec, t) > rec.view.deadline_s:
            row = cfg["runtime_matrix_s"][rec.view.query_type]
            faster = [x for x in self.runnable_types(cfg, rec.view.query_type) if row[x] < row[t]
                      and now + self.upper_runtime(cfg, rec, x) <= rec.view.deadline_s]
            if faster:
                self.stats["overrides"] += 1
                t = min(faster, key=lambda x: (self.slot_cost(cfg, self.mean_runtime(cfg, rec, x), x, market),
                                               row[x]))
        if market == "spot" and not self.spot_ok(cfg, rec, t, now):
            market = "on_demand"
        return (t, market)

    def recompute_routing(self, ctx):
        self.routing.clear()
        self.lc_cache.clear()
        self.stats["routing_recomputes"] += 1
        cfg, now = ctx.config, ctx.now
        for rec in self.pending.values():
            new = self.pool_for(cfg, rec, now)
            if new != rec.pool:
                self.pool_count[rec.pool] -= 1
                self.pool_count[new] += 1
                rec.pool = new
                self.stats["repooled"] += 1
        self.rebuild_work(ctx)

    def enqueue(self, rec):
        rec.where = "on_time"
        self.pending[rec.view.id] = rec
        self.pool_count[rec.pool] += 1
        self.work_add(rec)
        self.stats["max_pending"] = max(self.stats["max_pending"], len(self.pending))
        self.push_entry(rec)

    def push_entry(self, rec):
        rec.version += 1
        self.seq += 1
        heaps = self.queues.get(rec.cls)
        if heaps is None:
            heaps = self.queues[rec.cls] = {"on_time": [], "late": []}
        key = rec.view.deadline_s if rec.where == "on_time" else rec.view.arrival_s + self.max_queue_s
        heapq.heappush(heaps[rec.where], (key, self.seq, rec.view.id, rec.version))
        self.heap_entries += 1
        self.stats["max_heap_entries"] = max(self.stats["max_heap_entries"], self.heap_entries)
        if self.heap_entries > 2 * len(self.pending) + self.COMPACT_MIN_STALE:
            self.compact_heaps()

    def counts_for_scaling(self, cfg, rec):
        if rec.where == "dns" or rec.spare_only:
            return False
        return rec.cls[3] > 0 or cfg["clients"][rec.view.client_id]["drop_penalty_usd"] > 0

    def work_add(self, rec):
        cfg = self._cfg
        rec.work = self.mean_runtime(cfg, rec, rec.pool[0]) if self.counts_for_scaling(cfg, rec) else 0.0
        if rec.work:
            w = self.work.setdefault((rec.cls, rec.pool), [0.0, 0.0, 0, 0])
            i = 0 if rec.where == "on_time" else 1
            w[i] += rec.work
            w[i + 2] += 1

    def work_remove(self, rec):
        if rec.work:
            w = self.work[(rec.cls, rec.pool)]
            i = 0 if rec.where == "on_time" else 1
            w[i + 2] -= 1
            w[i] = w[i] - rec.work if w[i + 2] else 0.0
            rec.work = 0.0

    def rebuild_work(self, ctx):
        self.work = {}
        for rec in self.pending.values():
            self.work_add(rec)

    def is_valid(self, entry):
        rec = self.pending.get(entry[2])
        return rec is not None and rec.version == entry[3]

    def compact_heaps(self):
        total = 0
        for cls, heaps in list(self.queues.items()):
            for name in ("on_time", "late"):
                live = [e for e in heaps[name] if self.is_valid(e)]
                heapq.heapify(live)
                heaps[name] = live
                total += len(live)
            if not heaps["on_time"] and not heaps["late"]:
                del self.queues[cls]
        self.heap_entries = total
        self.stats["compactions"] += 1

    def pop_valid(self, heap):
        while heap:
            entry = heapq.heappop(heap)
            self.heap_entries -= 1
            if self.is_valid(entry):
                return entry
            self.stats["stale_pops"] += 1
        return None

    def clean_head(self, heap):
        while heap and not self.is_valid(heap[0]):
            heapq.heappop(heap)
            self.heap_entries -= 1
            self.stats["stale_pops"] += 1
        return heap[0] if heap else None

    def mean_runtime(self, cfg, rec, t):
        bench = cfg["runtime_matrix_s"][rec.view.query_type][t]
        return bench * self.predictor.m_hat(rec.view) if self.USE_LEARNED_RUNTIMES else bench

    def upper_runtime(self, cfg, rec, t):
        bench = cfg["runtime_matrix_s"][rec.view.query_type][t]
        if self.USE_LEARNED_RUNTIMES:
            return bench * self.predictor.m_up(rec.view)
        return bench * self.RUNTIME_MARGIN

    @staticmethod
    def fastest_type(cfg, rec):
        row = cfg["runtime_matrix_s"][rec.view.query_type]
        return min(MyScheduler.runnable_types(cfg, rec.view.query_type), key=row.__getitem__)

    def ready_slot_can_serve(self, cfg, rec, t, now):
        return now + self.upper_runtime(cfg, rec, t) <= rec.view.deadline_s

    def launch_can_help(self, cfg, rec, t, now):
        it = cfg["instance_types"][t]
        return now + it["join_s"] + it["ready_s"] + self.upper_runtime(cfg, rec, t) <= rec.view.deadline_s

    def feasible(self, cfg, rec, t, now):
        return (self.ready_slot_can_serve(cfg, rec, t, now)
                or (t == self.fastest_type(cfg, rec)
                    and now + self.mean_runtime(cfg, rec, t) <= rec.view.deadline_s))

    def is_late(self, cfg, rec, now):
        return now + self.mean_runtime(cfg, rec, self.fastest_type(cfg, rec)) > rec.view.deadline_s

    def m_drop(self, cfg):
        startup = max(it["join_s"] + it["ready_s"] for it in cfg["instance_types"].values())
        return 2 * (startup + cfg["tick_interval_s"])

    def cap_room(self, cid):
        cfg = self._ctx.config
        c = cfg["clients"].get(cid)
        if c is None:
            return float("inf")
        active = max(0.0, cfg["duration_s"] - self.client_start.get(cid, 0.0)) / self.MONTH_S
        cap = c["monthly_penalty_cap_usd"] * min(1.0, active)
        charged = self._ctx.client_stats(cid)["miss_penalty_usd"]
        return cap - charged - self.committed.get(cid, 0.0)

    def capped_penalty(self, cid, penalty):
        if not self.CAP_AWARE or penalty <= 0:
            return penalty
        e = min(penalty, max(0.0, self.cap_room(cid)))
        if e < penalty:
            self.stats["cap_limited_decisions"] += 1
        return e

    def own_capped_penalty(self, rec):
        p = rec.cls[3]
        if not self.CAP_AWARE or p <= 0:
            return p
        room = self.cap_room(rec.view.client_id) + (p if rec.in_committed else 0.0)
        e = min(p, max(0.0, room))
        if e < p:
            self.stats["cap_limited_decisions"] += 1
        return e

    def effective_penalty(self, rec):
        return 0.0 if rec.where == "late" else self.capped_penalty(rec.view.client_id, rec.cls[3])

    def class_penalty(self, cls):
        return self.capped_penalty(cls[0], cls[3])

    def lower_runtime(self, cfg, rec, t):
        bench = cfg["runtime_matrix_s"][rec.view.query_type][t]
        if self.USE_LEARNED_RUNTIMES:
            return bench * self.predictor.m_lo(rec.view)
        return bench / self.RUNTIME_MARGIN

    def is_certain(self, cfg, rec, now):
        if rec.start is not None:
            return now > rec.view.deadline_s
        return now + self.lower_runtime(cfg, rec, self.fastest_type(cfg, rec)) > rec.view.deadline_s

    def set_committed(self, rec, flag):
        if rec.in_committed == flag or rec.cls[3] <= 0:
            return
        rec.in_committed = flag
        cid = rec.view.client_id
        self.committed[cid] = self.committed.get(cid, 0.0) + (rec.cls[3] if flag else -rec.cls[3])
        if flag:
            self.stats["became_certain"] += 1

    def serve_decision(self, cfg, rec, now):
        if not self.DO_NOT_SERVE or not self.is_certain(cfg, rec, now):
            return "serve"
        d = cfg["clients"][rec.view.client_id]["drop_penalty_usd"]
        e_late = self.own_capped_penalty(rec)
        allowed = self.class_routing(cfg, rec.cls)[0]
        pairs = [(t, m) for t in allowed for m in self.MARKETS if self.market_usable(cfg, t, m)]
        if not pairs:
            pairs = [(t, m) for t in allowed for m in self.MARKETS]
        c = min(self.slot_cost(cfg, self.mean_runtime(cfg, rec, t), t, m) for t, m in pairs)
        if e_late >= d + self.DNS_MARGIN:
            return "dns"
        if e_late + c > d + self.DNS_MARGIN:
            return "spare"
        return "serve"

    def apply_serve_decision(self, cfg, rec, now):
        decision = self.serve_decision(cfg, rec, now)
        if decision == "dns":
            self.mark_dns(rec)
            return True
        spare = decision == "spare"
        if spare != rec.spare_only:
            self.work_remove(rec)
            rec.spare_only = spare
            self.work_add(rec)
            if spare:
                self.stats["spare_only_marked"] += 1
        return False

    def mark_dns(self, rec):
        self.work_remove(rec)
        rec.where = "dns"
        rec.spare_only = False
        rec.version += 1
        self.set_committed(rec, False)
        self.dns[rec.view.id] = rec
        self.stats["dns_marked"] += 1

    def reconsider_dns(self, ctx):
        if not self.dns:
            return
        cfg, now = ctx.config, ctx.now
        for qid, rec in list(self.dns.items()):
            if self.pending.get(qid) is not rec:
                del self.dns[qid]
                continue
            if self.serve_decision(cfg, rec, now) == "dns":
                continue
            del self.dns[qid]
            rec.where = "late" if self.is_late(cfg, rec, now) else "on_time"
            self.set_committed(rec, self.is_certain(cfg, rec, now))
            rec.spare_only = self.serve_decision(cfg, rec, now) == "spare"
            self.work_add(rec)
            self.push_entry(rec)
            if rec.where == "late":
                self.late_index.setdefault(self.predictor.template_key(rec.view), set()).add(qid)
            self.stats["dns_reversed"] += 1

    def tier_of(self, cfg, rec, t, now, m_drop):
        cid = rec.view.client_id
        d = cfg["clients"][cid]["drop_penalty_usd"]
        if rec.spare_only:
            return 4, (rec.view.arrival_s,)
        e = self.effective_penalty(rec)
        ttd = rec.view.arrival_s + self.max_queue_s - now
        if d > 0 and ttd <= m_drop:
            return 1, (ttd, d / max(self.mean_runtime(cfg, rec, t), 1e-9))
        if e > 0 and self.feasible(cfg, rec, t, now):
            slack = rec.view.deadline_s - now - self.upper_runtime(cfg, rec, t)
            return 2, (slack, e / self.mean_runtime(cfg, rec, t))
        if d > 0 and (e == 0 or rec.where == "late"):
            return 3, (ttd, d / max(self.mean_runtime(cfg, rec, t), 1e-9))
        if e == 0 and d == 0:
            return 4, (rec.view.arrival_s,)
        return None

    def move_to_late(self, rec):
        self.work_remove(rec)
        rec.where = "late"
        self.work_add(rec)
        self.push_entry(rec)
        self.late_index.setdefault(self.predictor.template_key(rec.view), set()).add(rec.view.id)
        self.stats["late_moves"] += 1

    def serving_pool(self, inst):
        return (inst.instance_type, "spot") if inst.id in self.fallback else (inst.instance_type, inst.market)

    def slot_ok(self, cfg, rec, t, market, window, now):
        if window is not None and self.upper_runtime(cfg, rec, t) > window:
            return False
        if market == "spot" and not self.spot_ok(cfg, rec, t, now):
            return False
        return True

    def select(self, ctx, slot, inst=None):
        cfg, now = ctx.config, ctx.now
        t = slot[0]
        market = inst.market if inst is not None else slot[1]
        window = (inst.terminate_at_s - now) if inst is not None and inst.terminate_at_s is not None else None
        direct = {slot, (t, "on_demand")} if inst is not None and inst.id in self.fallback else {slot}
        m_drop = self.m_drop(cfg)
        cands = []
        popped = []
        waits = {}

        def consider(rec, entry, source):
            if rec.where == "late" and self.is_certain(cfg, rec, now):
                self.set_committed(rec, True)
                if self.apply_serve_decision(cfg, rec, now):
                    return
            if not self.slot_ok(cfg, rec, t, market, window, now):
                if market == "spot" and rec.pool == slot:
                    self.stats["spot_rejections"] += 1
                    self.repool(cfg, rec, now)
                return
            tk = self.tier_of(cfg, rec, t, now, m_drop)
            if tk is not None and (rec.pool in direct or self.may_overflow(ctx, rec, slot, waits)):
                cands.append((tk[0], tk[1], entry[1], rec, entry, source))

        for cls, heaps in self.queues.items():
            if t not in self.class_routing(cfg, cls)[0]:
                continue
            on_time, late = heaps["on_time"], heaps["late"]
            taken = 0
            while taken < self.LOOKAHEAD:
                entry = self.pop_valid(on_time)
                if entry is None:
                    break
                rec = self.pending[entry[2]]
                if self.is_late(cfg, rec, now):
                    self.move_to_late(rec)
                    continue
                popped.append((on_time, entry))
                taken += 1
                consider(rec, entry, None)
            head = self.clean_head(late)
            if head is not None:
                consider(self.pending[head[2]], head, late)
        best = self.best_candidate(cands)
        for heap, entry in popped:
            if best is None or entry is not best[4]:
                heapq.heappush(heap, entry)
                self.heap_entries += 1
        if best is None:
            return None
        if best[5] is not None:
            heapq.heappop(best[5])
            self.heap_entries -= 1
        self.stats[f"tier{best[0]}"] += 1
        if best[3].pool not in direct:
            self.stats["overflow_assigns"] += 1
        return best[3]

    def repool(self, cfg, rec, now):
        new = self.pool_for(cfg, rec, now)
        if new != rec.pool:
            self.work_remove(rec)
            self.pool_count[rec.pool] -= 1
            rec.pool = new
            self.pool_count[new] += 1
            self.work_add(rec)

    def may_overflow(self, ctx, rec, slot, waits):
        cfg, now = ctx.config, ctx.now
        pool = rec.pool
        for iid in self.ready[pool]:
            inst = ctx.instances.get(iid)
            if (inst is not None and inst.can_accept and inst.free_slots > 0
                    and inst.terminate_at_s is None
                    and self.slot_ok(cfg, rec, pool[0], inst.market, None, now)
                    and self.feasible(cfg, rec, pool[0], now)):
                return False
        if pool not in waits:
            waits[pool] = self.expected_wait(ctx, pool)
        if rec.view.deadline_s - now - self.upper_runtime(cfg, rec, pool[0]) >= waits[pool]:
            return False
        e = self.effective_penalty(rec)
        extra = (self.slot_cost(cfg, self.mean_runtime(cfg, rec, slot[0]), slot[0], slot[1])
                 - self.slot_cost(cfg, self.mean_runtime(cfg, rec, pool[0]), pool[0], pool[1]))
        return extra < e

    def expected_wait(self, ctx, pool):
        cfg, now = ctx.config, ctx.now
        t = pool[0]
        first_end = first_ready = None
        slots = 0
        for inst in ctx.instances.values():
            if self.serving_pool(inst) != pool or inst.draining or inst.terminate_at_s is not None:
                continue
            slots += inst.slots
            if inst.state == "ready":
                for qid in inst.running:
                    rec = self.running.get(qid)
                    if rec is not None and rec.pred_end is not None:
                        first_end = rec.pred_end if first_end is None else min(first_end, rec.pred_end)
            else:
                it = cfg["instance_types"][t]
                ready_at = inst.launched_s + it["join_s"] + it["ready_s"]
                first_ready = ready_at if first_ready is None else min(first_ready, ready_at)
        if first_end is not None:
            t_first = first_end - now
        elif first_ready is not None:
            t_first = first_ready - now
        else:
            t_first = self.startup(cfg, t) + cfg["tick_interval_s"]
        work = sum(w[0] + w[1] for (cls, p), w in self.work.items() if p == pool)
        if slots == 0:
            slots = cfg["instance_types"][t]["slots"]
        return max(0.0, t_first) + work / slots

    def best_candidate(self, cands):
        if not cands:
            return None
        tier = min(c[0] for c in cands)
        group = [c for c in cands if c[0] == tier]
        if tier in (1, 3):
            return min(group, key=lambda c: (-c[1][1], c[1][0], c[2]))
        if tier == 4:
            return min(group, key=lambda c: (c[1][0], c[2]))
        least = min(c[1][0] for c in group)
        close = [c for c in group if c[1][0] <= least + self.SLACK_TIE_S]
        return min(close, key=lambda c: (-c[1][1], c[1][0], c[2]))

    def retest_late(self, ctx, keys=None):
        cfg, now = ctx.config, ctx.now
        for key in list(self.late_index) if keys is None else keys:
            qids = self.late_index.get(key)
            if not qids:
                continue
            for qid in list(qids):
                rec = self.pending.get(qid)
                if rec is None or rec.where != "late":
                    qids.discard(qid)
                    continue
                self.stats["late_retests"] += 1
                if not self.is_late(cfg, rec, now):
                    qids.discard(qid)
                    self.set_committed(rec, False)
                    if rec.spare_only:
                        self.work_remove(rec)
                        rec.spare_only = False
                    self.work_remove(rec)
                    rec.where = "on_time"
                    self.work_add(rec)
                    self.push_entry(rec)
                    self.stats["late_returns"] += 1
                else:
                    self.set_committed(rec, self.is_certain(cfg, rec, now))
                    if self.apply_serve_decision(cfg, rec, now):
                        qids.discard(qid)
            if not qids:
                del self.late_index[key]

    def try_assign(self, ctx, rec, inst):
        qid = rec.view.id
        window = None if inst.terminate_at_s is None else inst.terminate_at_s - ctx.now
        if window is not None and self.upper_runtime(ctx.config, rec, inst.instance_type) > window:
            self.stats["noticed_violations"] += 1
            self.push_entry(rec)
            return False
        if rec.where == "dns":
            self.stats["dns_assigned"] += 1
            return False
        if self.pending.get(qid) is not rec or not inst.can_accept:
            self.stats["assign_errors"] += 1
            if self.pending.get(qid) is rec:
                self.push_entry(rec)
            return False
        del self.pending[qid]
        self.pool_count[rec.pool] -= 1
        self.work_remove(rec)
        try:
            ctx.assign(qid, inst.id)
        except SchedulerError as e:
            self.stats["assign_errors"] += 1
            print(f"assign failed for query {qid} on {inst.id}: {e}", file=sys.stderr)
            if ctx.query(qid) is not None:
                self.enqueue(rec)
            else:
                self.stats["unknown_events"] += 1
            return False
        rec.inst_id = inst.id
        rec.inst_type = inst.instance_type
        rec.bench_at_assign = ctx.config["runtime_matrix_s"][rec.view.query_type][inst.instance_type]
        p = self.predictor
        rec.pred = (p.observations(rec.view), p.m_hat(rec.view), p.m_up(rec.view))
        rec.pred_end = ctx.now + self.mean_runtime(ctx.config, rec, inst.instance_type)
        rec.start = ctx.now
        self.running[qid] = rec
        self.idle_since.pop(inst.id, None)
        self.stats["assigned"] += 1
        if inst.terminate_at_s is not None:
            self.stats["assigned_to_noticed"] += 1
        return True

    def dispatch(self, ctx, pool):
        if not self.pending:
            return
        for iid in list(self.ready[pool]):
            inst = ctx.instances.get(iid)
            if inst is None or not inst.can_accept:
                continue
            while inst.free_slots > 0:
                rec = self.select(ctx, pool, inst)
                if rec is None:
                    break
                if not self.try_assign(ctx, rec, inst):
                    break
            if not self.pending:
                return

    def on_arrival(self, ctx, query):
        self.stats["arrived"] += 1
        penalty = float(ctx.config["clients"][query.client_id]["penalty_per_miss_usd"][query.query_type])
        self.admit(ctx, query, penalty)

    def admit(self, ctx, query, penalty):
        cfg = ctx.config
        if not self.runnable_types(cfg, query.query_type):
            self.unrunnable[query.id] = (query, penalty)
            self.stats["unrunnable_parked"] += 1
            return
        cls = (query.client_id, query.query_type, query.sla_s, penalty)
        rec = Record(query, cls, None)
        rec.pool = self.pool_for(cfg, rec, ctx.now)
        self.note_arrival(cls, ctx.now)
        self.enqueue(rec)
        self.dispatch(ctx, rec.pool)
        for t in self.class_routing(cfg, cls)[0]:
            for m in self.MARKETS:
                if rec.view.id in self.pending and (t, m) != rec.pool:
                    self.dispatch(ctx, (t, m))
        if rec.view.id in self.pending and self.is_latency_critical(cfg, cls):
            if ctx.now - self.last_burst_check.get(rec.pool, -1e18) >= self.BURST_CHECK_GAP_S:
                self.last_burst_check[rec.pool] = ctx.now
                self.stats["burst_checks"] += 1
                self.scale_up(ctx, rec.pool, self.pool_target(ctx, rec.pool, self.busy_split(ctx)))

    def on_complete(self, ctx, query, instance_id, runtime_s):
        rec = self.running.pop(query.id, None)
        if rec is None:
            self.stats["unknown_events"] += 1
        else:
            self.set_committed(rec, False)
            self.learn(ctx, rec, runtime_s)
        self.stats["completed"] += 1
        inst = ctx.instances.get(instance_id)
        if inst is None:
            return
        if inst.terminate_at_s is not None and not inst.running:
            self.stats["notice_terminations"] += 1
            ctx.terminate(instance_id)
            return
        self.dispatch(ctx, self.serving_pool(inst))
        if not inst.running:
            self.idle_since[instance_id] = ctx.now

    def learn(self, ctx, rec, runtime_s):
        result = self.predictor.observe(rec.view, runtime_s, rec.bench_at_assign)
        if result is None:
            return
        key, before, after = result
        m = runtime_s / rec.bench_at_assign
        n, m_hat, m_up = rec.pred
        bucket = "0" if n == 0 else ("1-4" if n < 5 else "5+")
        c = self.coverage.setdefault(bucket, [0, 0, 0, 0.0, 0.0])
        c[0] += 1
        c[1] += m <= m_up
        c[2] += m <= self.RUNTIME_MARGIN
        c[3] += abs(m / m_hat - 1.0)
        c[4] += abs(m - 1.0)
        if after < before and self.USE_LEARNED_RUNTIMES:
            self.retest_late(ctx, (key,))

    def coverage_report(self):
        out = {}
        for b in ("0", "1-4", "5+"):
            if b in self.coverage:
                n, up, fixed, err, err_b = self.coverage[b]
                out[b] = dict(queries=n, within_m_up=round(up / n, 4), within_1_15=round(fixed / n, 4),
                              mean_abs_err_learned=round(err / n, 4), mean_abs_err_benchmark=round(err_b / n, 4))
        return out

    def on_query_dropped(self, ctx, query):
        if self.unrunnable.pop(query.id, None) is not None:
            self.stats["unrunnable_dropped"] += 1
            self.stats["dropped"] += 1
            return
        rec = self.pending.pop(query.id, None)
        if rec is None:
            self.stats["unknown_events"] += 1
        else:
            self.pool_count[rec.pool] -= 1
            self.work_remove(rec)
            self.set_committed(rec, False)
            if self.dns.pop(query.id, None) is not None:
                self.stats["dns_dropped"] += 1
        self.stats["dropped"] += 1

    def on_instance_ready(self, ctx, instance):
        pool = self.serving_pool(instance)
        self.ready[pool][instance.id] = True
        self.idle_since[instance.id] = ctx.now
        self.dispatch(ctx, pool)

    def on_spot_notice(self, ctx, instance, terminate_at_s):
        if instance.state != "ready" or not instance.running:
            self.stats["notice_terminations"] += 1
            ctx.terminate(instance.id)

    def on_instance_terminated(self, ctx, instance, reason, requeued):
        self.ready[self.serving_pool(instance)].pop(instance.id, None)
        self.fallback.discard(instance.id)
        self.idle_since.pop(instance.id, None)
        if reason == "scheduler" and requeued:
            self.stats["requeued_by_release"] += len(requeued)
        for view in requeued:
            rec = self.running.pop(view.id, None)
            if rec is None or view.id in self.pending:
                self.stats["unknown_events"] += 1
                continue
            rec.inst_id = rec.inst_type = rec.bench_at_assign = rec.start = rec.pred = rec.pred_end = None
            rec.pool = self.pool_for(ctx.config, rec, ctx.now)
            self.stats["requeued"] += 1
            self.enqueue(rec)
        if reason != "end_of_simulation":
            for pool in self.pool_order:
                self.dispatch(ctx, pool)
            affected = {self.pending[v.id].pool for v in requeued if v.id in self.pending}
            if affected:
                busy = self.busy_split(ctx)
                for pool in self.pool_order:
                    if pool in affected:
                        self.scale_up(ctx, pool, self.pool_target(ctx, pool, busy))

    def on_config_change(self, ctx, event):
        self._cfg = ctx.config
        self._now = ctx.now
        self.add_pools(ctx)
        self.cooldown.clear()
        if event.get("type") == "client_added":
            self.client_start[event["client_id"]] = ctx.now
            self.committed.setdefault(event["client_id"], 0.0)
        self.retest_late(ctx)
        self.reconsider_dns(ctx)
        for qid, (view, penalty) in list(self.unrunnable.items()):
            if self.runnable_types(ctx.config, view.query_type):
                del self.unrunnable[qid]
                self.stats["unrunnable_admitted"] += 1
                self.admit(ctx, view, penalty)
        self.recompute_routing(ctx)

    def on_tick(self, ctx):
        self._now = ctx.now
        self.ticks += 1
        self.reconsider_dns(ctx)
        for rec in self.running.values():
            if not rec.in_committed and ctx.now > rec.view.deadline_s:
                self.set_committed(rec, True)
        if ctx.now >= self.next_hourly - 1e-6:
            self.next_hourly = (math.floor(ctx.now / 3600.0 + 1e-9) + 1) * 3600.0
            self.retest_late(ctx)
            self.recompute_routing(ctx)
            if self.CHECK_CONSISTENCY_HOURLY:
                self.record_consistency(ctx)
        for pool in self.pool_order:
            self.dispatch(ctx, pool)
        busy = self.busy_split(ctx)
        target = {pool: self.pool_target(ctx, pool, busy) for pool in self.pool_order}
        for pool in self.pool_order:
            self.scale_up(ctx, pool, target[pool])
        self.scale_down_idle(ctx, target)

    def note_arrival(self, cls, now):
        r = self.rate.get(cls)
        if r is None:
            self.rate[cls] = [1.0 / self.RATE_TAU_S, now]
        else:
            r[0] = r[0] * math.exp(-(now - r[1]) / self.RATE_TAU_S) + 1.0 / self.RATE_TAU_S
            r[1] = now

    def arrival_rate(self, cls, now):
        r = self.rate.get(cls)
        return r[0] * math.exp(-(now - r[1]) / self.RATE_TAU_S) if r else 0.0

    def typical_runtime(self, cfg, cls, t, upper=False):
        bench = cfg["runtime_matrix_s"][cls[1]][t]
        if not self.USE_LEARNED_RUNTIMES:
            return bench * (self.RUNTIME_MARGIN if upper else 1.0)
        p = self.predictor
        return bench * (p.class_upper(cls[0], cls[1]) if upper else p.class_mean(cls[0], cls[1]))

    @staticmethod
    def startup(cfg, t):
        it = cfg["instance_types"][t]
        return it["join_s"] + it["ready_s"]

    def is_latency_critical(self, cfg, cls):
        lc = self.lc_cache.get(cls)
        if lc is None:
            sla, penalty = cls[2], self.class_penalty(cls)
            allowed = self.class_routing(cfg, cls)[0]
            lc = penalty > 0 and all(
                sla < self.startup(cfg, t) + self.typical_runtime(cfg, cls, t, upper=True) for t in allowed)
            self.lc_cache[cls] = lc
        return lc

    def busy_split(self, ctx):
        cfg = ctx.config
        out = {pool: [0, 0] for pool in self.pool_order}
        for inst in ctx.instances.values():
            b = out.setdefault(self.serving_pool(inst), [0, 0])
            for qid in inst.running:
                rec = self.running.get(qid)
                if (inst.terminate_at_s is not None and rec is not None and rec.pred_end is not None
                        and rec.pred_end <= inst.terminate_at_s):
                    continue
                b[0 if rec is not None and self.is_latency_critical(cfg, rec.cls) else 1] += 1
        return out

    def horizons(self, ctx, cls, pool):
        cfg, now = ctx.config, ctx.now
        t = pool[0]
        su = self.startup(cfg, t)
        if self.class_penalty(cls) > 0:
            h_on = cls[2] - self.typical_runtime(cfg, cls, t, upper=True) - su - cfg["tick_interval_s"]
        else:
            h_on = self.H_MAX
        heaps = self.queues.get(cls)
        oldest = None
        if heaps is not None:
            for name in ("on_time", "late"):
                head = self.clean_head(heaps[name])
                if head is not None:
                    a = self.pending[head[2]].view.arrival_s
                    oldest = a if oldest is None or a < oldest else oldest
        if oldest is None:
            h_late = self.H_MAX
        else:
            h_late = oldest + self.max_queue_s - now - self.m_drop(cfg) - su
        clamp = lambda h: min(self.H_MAX, max(self.H_MIN, h))
        return clamp(h_on), clamp(h_late)

    def pool_target(self, ctx, pool, busy, paid_only=False):
        cfg, now = ctx.config, ctx.now
        busy_lc, busy_other = busy.get(pool, [0, 0])
        need = 0.0
        for cls in self.rate:
            if self.is_latency_critical(cfg, cls) and self.class_routing(cfg, cls)[1] == pool:
                r = self.arrival_rate(cls, now) * self.typical_runtime(cfg, cls, pool[0])
                if r > 1e-9:
                    need += r + self.Z_WARM * math.sqrt(r) + self.B_WARM
        queued = 0.0
        for (cls, wpool), (on_s, late_s, on_n, late_n) in self.work.items():
            if wpool != pool or not (on_n or late_n) or (paid_only and self.class_penalty(cls) == 0):
                continue
            h_on, h_late = self.horizons(ctx, cls, pool)
            if self.is_latency_critical(cfg, cls):
                queued += (on_s + late_s) / h_late
            else:
                queued += on_s / h_on + late_s / h_late
        return busy_other + max(busy_lc, need) + queued

    def scale_up(self, ctx, pool, target):
        t, market = pool
        cfg = ctx.config
        slots = cfg["instance_types"][t]["slots"]
        now = ctx.now
        if self.cooldown.get(pool, -1) <= now:
            for _ in range(max(0, math.ceil((target - self.capacity_slots(ctx, pool)) / slots - 1e-9))):
                if self.launch(ctx, t, market) is None:
                    self.cooldown[pool] = now + self.COOLDOWN_S
                    self.stats["cooldowns"] += 1
                    break
        if market == "spot" and self.cooldown.get(pool, -1) > now:
            paid = self.pool_target(ctx, pool, self.busy_split(ctx), paid_only=True)
            missing = paid - self.capacity_slots(ctx, pool)
            if missing > 0 and self.cooldown.get((t, "on_demand"), -1) <= now:
                for _ in range(math.ceil(missing / slots - 1e-9)):
                    iid = self.launch(ctx, t, "on_demand")
                    if iid is None:
                        self.cooldown[(t, "on_demand")] = now + self.COOLDOWN_S
                        self.stats["cooldowns"] += 1
                        break
                    self.fallback.add(iid)
                    self.stats["fallback_launches"] += 1

    def launch(self, ctx, it, market):
        iid = ctx.launch(it, market)
        self.stats["launches" if iid is not None else "launch_failures"] += 1
        return iid

    def capacity_slots(self, ctx, pool):
        return sum(i.slots for i in ctx.instances.values()
                   if not i.draining and i.terminate_at_s is None and self.serving_pool(i) == pool)



    def idle_timeout(self, cfg, it):
        t = cfg["instance_types"][it]
        return self.IDLE_STARTUP_FACTOR * (t["join_s"] + t["ready_s"])

    def scale_down_idle(self, ctx, target):
        cfg, now = ctx.config, ctx.now
        min_bill = cfg["billing"].get("minimum_s", 0)
        eligible = {}
        for iid, since in self.idle_since.items():
            inst = ctx.instances.get(iid)
            if (inst is not None and not inst.running
                    and now - since >= self.idle_timeout(cfg, inst.instance_type)
                    and now - inst.launched_s >= min_bill):
                eligible.setdefault(self.serving_pool(inst), []).append((since, iid))
        for pool, machines in eligible.items():
            have = self.capacity_slots(ctx, pool)
            keep = math.ceil(target.get(pool, 0.0) * self.SCALE_DOWN_MARGIN)
            ordered = sorted(machines)
            t = pool[0]
            for n, (since, iid) in enumerate(ordered):
                slots = ctx.instances[iid].slots
                if have - slots < keep:
                    self.stats["releases_held_by_target"] += len(ordered) - n
                    break
                ctx.terminate(iid)
                if iid in ctx.instances:
                    self.stats["releases_not_immediate"] += 1
                self.ready[pool].pop(iid, None)
                self.idle_since.pop(iid, None)
                self.stats["releases"] += 1
                have -= slots

    def on_end(self, ctx):
        if self.CHECK_INVARIANTS_AT_END:
            self.invariant_errors = self.check_invariants(ctx)
            for e in self.invariant_errors:
                print(f"INVARIANT FAILED: {e}", file=sys.stderr)

    def record_consistency(self, ctx):
        self.stats["consistency_checks"] += 1
        for e in self.check_consistency(ctx):
            if len(self.consistency_errors) < 20:
                self.consistency_errors.append(f"t={ctx.now:.0f}: {e}")

    def check_consistency(self, ctx):
        errors = []
        both = self.pending.keys() & self.running.keys()
        if both:
            errors.append(f"{len(both)} queries in both pending and running")
        recount = dict.fromkeys(self.pool_count, 0)
        for rec in self.pending.values():
            recount[rec.pool] += 1
        if recount != self.pool_count:
            errors.append(f"pool_count {self.pool_count} != recount {recount}")
        entries = valid = 0
        for cls, heaps in self.queues.items():
            for name, heap in heaps.items():
                entries += len(heap)
                for e in heap:
                    if self.is_valid(e):
                        valid += 1
                        rec = self.pending[e[2]]
                        if (rec.cls, rec.where) != (cls, name):
                            errors.append(f"query {e[2]} has a valid entry in the wrong heap")
        if entries != self.heap_entries:
            errors.append(f"heap_entries {self.heap_entries} != actual {entries}")
        n_dns = sum(1 for r in self.pending.values() if r.where == "dns")
        if valid != len(self.pending) - n_dns:
            errors.append(f"{valid} valid heap entries for {len(self.pending) - n_dns} queued records")
        if n_dns != len(self.dns) or any(self.pending.get(q) is not r for q, r in self.dns.items()):
            errors.append(f"dns index ({len(self.dns)}) does not match dns records ({n_dns})")
        recount = {}
        for rec in self.pending.values():
            if rec.work:
                w = recount.setdefault((rec.cls, rec.pool), [0.0, 0.0, 0, 0])
                i = 0 if rec.where == "on_time" else 1
                w[i] += rec.work
                w[i + 2] += 1
        for key, w in self.work.items():
            r = recount.get(key, [0.0, 0.0, 0, 0])
            if w[2:] != r[2:] or abs(w[0] - r[0]) > 1e-6 * max(1.0, r[0]) or abs(w[1] - r[1]) > 1e-6 * max(1.0, r[1]):
                errors.append(f"work totals {key}: {w} != recount {r}")
        missing = set(recount) - set(self.work)
        if missing:
            errors.append(f"work totals missing for {len(missing)} (class, pool) keys")
        expect = {}
        for rec in list(self.pending.values()) + list(self.running.values()):
            if rec.in_committed:
                expect[rec.view.client_id] = expect.get(rec.view.client_id, 0.0) + rec.cls[3]
        for cid, v in self.committed.items():
            if abs(v - expect.get(cid, 0.0)) > 1e-6:
                errors.append(f"committed[{cid}] = {v} != recount {expect.get(cid, 0.0)}")
        engine_running = {qid for inst in ctx.instances.values() for qid in inst.running}
        if engine_running != self.running.keys():
            errors.append(f"running mismatch: engine {len(engine_running)}, ours {len(self.running)}")
        if any(ctx.query(qid) is None for qid in self.pending):
            errors.append("a pending record is unknown to the engine")
        return errors

    def check_invariants(self, ctx):
        s = self.stats
        errors = list(self.consistency_errors)
        errors += [f"end of run: {e}" for e in self.check_consistency(ctx)]
        if self.pending or self.running or self.unrunnable:
            errors.append(f"{len(self.pending)} pending, {len(self.running)} running and "
                          f"{len(self.unrunnable)} parked records left")
        if s["arrived"] != s["completed"] + s["dropped"]:
            errors.append(f"arrived {s['arrived']} != completed {s['completed']} + dropped {s['dropped']}")
        engine_done = sum(ctx.client_stats(c)["completed"] for c in ctx.config["clients"])
        engine_dropped = sum(ctx.client_stats(c)["dropped"] for c in ctx.config["clients"])
        if (engine_done, engine_dropped) != (s["completed"], s["dropped"]):
            errors.append(f"engine counts {engine_done}/{engine_dropped} != ours {s['completed']}/{s['dropped']}")
        if any(n != 0 for n in self.pool_count.values()):
            errors.append(f"pool counts not zero: {self.pool_count}")
        if any(self.is_valid(e) for hs in self.queues.values() for h in hs.values() for e in h):
            errors.append("valid heap entry left after the run")
        if s["max_heap_entries"] > 2 * s["max_pending"] + self.COMPACT_MIN_STALE + 1:
            errors.append(f"heap grew to {s['max_heap_entries']} entries")
        for key in ("assign_errors", "unknown_events", "requeued_by_release", "releases_not_immediate",
                    "noticed_violations", "dns_assigned"):
            if s[key]:
                errors.append(f"{key} = {s[key]}")
        return errors