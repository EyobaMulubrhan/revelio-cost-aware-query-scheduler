import csv
import gzip
import json
import os
import sys
from collections import defaultdict


def client_costs(run_dir, scenario_dir):
    with open(os.path.join(scenario_dir, "config.json")) as f:
        slots = {k: v["slots"] for k, v in json.load(f)["instance_types"].items()}
    with open(os.path.join(run_dir, "instance_log.csv")) as f:
        inst = {r["instance_id"]: r for r in csv.DictReader(f)}
    used = defaultdict(lambda: defaultdict(float))
    market_s = defaultdict(lambda: defaultdict(float))
    with gzip.open(os.path.join(run_dir, "query_log.csv.gz"), "rt") as f:
        for r in csv.DictReader(f):
            if r["status"] == "completed":
                used[r["instance_id"]][r["client_id"]] += float(r["run_s"])
                market_s[r["client_id"]][r["market"]] += float(r["run_s"])
    allocated, direct = defaultdict(float), defaultdict(float)
    unallocated = busy = slot_s = 0.0
    for iid, r in inst.items():
        cost, billed, n = float(r["cost_usd"]), float(r["billed_s"]), slots[r["instance_type"]]
        busy += float(r["busy_slot_s"])
        slot_s += billed * n
        total = sum(used[iid].values())
        if total == 0:
            unallocated += cost
        for cid, run in used[iid].items():
            allocated[cid] += cost * run / total
            direct[cid] += run * cost / (billed * n)
    with open(os.path.join(run_dir, "summary.json")) as f:
        summ = json.load(f)
    rows = []
    for cid, c in summ["clients"].items():
        pen = c["miss_penalty_usd"] + c["drop_penalty_usd"]
        spot = market_s[cid].get("spot", 0.0)
        tot = sum(market_s[cid].values())
        rows.append(dict(client=cid, revenue=c["revenue_usd"], allocated=allocated[cid],
                         direct=direct[cid], penalties=pen,
                         profit=c["revenue_usd"] - allocated[cid] - pen,
                         spot_share=spot / tot if tot else 0.0))
    return rows, dict(compute=sum(float(r["cost_usd"]) for r in inst.values()),
                      unallocated=unallocated, utilisation=busy / slot_s if slot_s else 0.0)


if __name__ == "__main__":
    rows, tot = client_costs(sys.argv[1], sys.argv[2])
    print(f"compute ${tot['compute']:,.2f}  (machines that ran nothing: ${tot['unallocated']:,.2f})"
          f"  utilisation {tot['utilisation']:.1%}")
    print(f"{'client':12s} {'revenue':>10s} {'allocated':>10s} {'direct':>9s} {'penalties':>10s}"
          f" {'profit':>10s} {'on spot':>8s}")
    for r in rows:
        print(f"{r['client']:12s} {r['revenue']:10,.2f} {r['allocated']:10,.2f} {r['direct']:9,.2f}"
              f" {r['penalties']:10,.2f} {r['profit']:10,.2f} {r['spot_share']:8.1%}")