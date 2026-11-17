import csv
import gzip
import os
import sys

DAY = 86400.0


def slot_rates(*run_dir):
    slots = {"std.small": 2, "std.large": 8, "mem.xlarge": 16}
    with open(os.path.join(*run_dir, "instance_log.csv")) as f:
        return {
            r["instance_id"]: float(r["cost_usd"]) / (float(r["billed_s"]) * slots[r["instance_type"]])
            for r in csv.DictReader(f)
        }


def direct_cost(*run_dir, rows):
    rate = slot_rates(*run_dir)
    return sum(float(r["run_s"]) * rate[r["instance_id"]] for r in rows)


def adhoc_after_day20(*run_dir):
    rows = []
    with gzip.open(os.path.join(*run_dir, "query_log.csv.gz"), "rt") as f:
        for r in csv.DictReader(f):
            if (
                r["client_id"] == "labs_ds"
                and r["query_type"] == "adhoc"
                and r["status"] == "completed"
                and float(r["arrival_s"]) >= 20 * DAY
            ):
                rows.append(r)
    return rows


if __name__ == "__main__":
    up = adhoc_after_day20(sys.argv[1])
    misses = [r for r in up if r["missed"] == "1"]

    unmeetable = [
        r for r in misses
        if float(r["run_s"]) > 900 and r["instance_type"] == "mem.xlarge"
    ]

    print(
        f"after the upgrade: {len(misses):,} of {len(up):,} labs adhoc queries missed "
        f"({len(misses) / len(up):.1%}); {len(unmeetable):,} of the misses "
        f"({len(unmeetable) / len(misses):.0%}) ran longer than 900 s on mem.xlarge"
    )

    print(f"uncapped adhoc penalty over these days at $0.40: ${0.40 * len(misses):,.2f}")
    print(f"direct compute of these queries: ${direct_cost(sys.argv[1], up):,.2f}")

    if len(sys.argv) > 2:
        ref = adhoc_after_day20(sys.argv[2])
        ref_m = sum(r["missed"] == "1" for r in ref)

        print(
            f"reference run, same days: {ref_m:,} of {len(ref):,} missed ({ref_m / len(ref):.1%}); "
            f"direct compute ${direct_cost(sys.argv[2], ref):,.2f}"
        )