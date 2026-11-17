# Cost-Aware Query Scheduler

This is my scheduler implementation for the Revelio Labs take-home assignment.

The main goal of the scheduler is to decide which machines to use (machine type, spot or on-demand, and how many) and where to run waiting queries. It tries to make as much profit as possible while keeping compute costs, SLA penalties, and dropped-query penalties low.

The profit is calculated as:

```
profit = revenue - compute cost - SLA miss penalties (after per-client caps) - drop penalties
```

Compared to the better of the two provided baselines (`CheapestFirst`), my scheduler makes:

- **+$3,190.71 (+27.8%)** on `dev_steady`
- **+$3,264.48 (+31.5%)** on `dev_changes`

I have explained how the scheduler works, the results I got, and why I made these decisions in [`NOTES.md`](NOTES.md).

## What is in this repository

| Path | Description |
| --- | --- |
| `my_scheduler.py` | The main scheduler implementation (`MyScheduler`). Uses only the Python standard library. |
| `runtime_predictor.py` | Learns how long each query template takes to run. The scheduler uses it when making decisions. |
| `NOTES.md` | Explains the results, last month's analysis (Part 1), the approach and trade-offs (Part 3), and testing. |
| `tests/` | Contains 123 unit tests. `tests/support.py` contains shared test fixtures. |
| `analysis/01_bill_analysis.ipynb` to `analysis/05_config_analysis.ipynb` | Notebooks for Part 1, covering last month's bill, instances, fleet, queries, and configuration. |
| `analysis/HISTORICAL_FINDINGS.md` | Brings the Part 1 findings together, with links to the notebook each result came from. |
| `analysis/EXPERIMENT_RESULTS.md` | Contains the simulation experiments I ran to decide on the final settings. |
| `analysis/client_costs.py` | Calculates per-client costs, penalties, and profit using the simulation logs. |
| `analysis/labs_upgrade.py` | Contains the `labs_ds` upgrade analysis used in the NOTES recommendation. |

## What is not included

The simulator and its data were provided with the assignment, so I have not included them in this repository.

You will need these folders to run everything:

| Folder | Used for |
| --- | --- |
| `simkit/` | Running the scheduler and unit tests |
| `scenarios/` (`dev_steady`, `dev_changes`) | Running the scheduler |
| `history/last_month/` | Running the Part 1 notebooks |

You will need a copy of the original assignment folder (`scheduler-takehome/`) to get these files.

## Requirements

- **Python 3.10 or later:** Needed to run the scheduler, tests, and the two analysis scripts. No extra packages are needed because they use the Python standard library.
- **For the notebooks only:** You will need `jupyter`, `pandas`, `numpy`, and `matplotlib`.

## Setup

All the files and folders need to be in the same directory. This means the files in this repository should sit next to `simkit/`, `scenarios/`, and `history/`.

The easiest way to set this up is to copy the three folders from the original assignment into this repository.

```bash
git clone https://github.com/EyobaMulubrhan/revelio-cost-aware-query-scheduler
cd revelio-cost-aware-query-scheduler

# Replace this path with the location of the assignment folder
cp -r /path/to/scheduler-takehome/simkit     .
cp -r /path/to/scheduler-takehome/scenarios  .
cp -r /path/to/scheduler-takehome/history    .
```

After copying, your folder structure should look like this:

```
revelio-cost-aware-query-scheduler/
├── my_scheduler.py
├── runtime_predictor.py
├── NOTES.md
├── README.md
├── tests/
│   ├── support.py
│   ├── test_config_robustness.py
│   ├── test_dispatch_tiers.py
│   ├── test_do_not_serve.py
│   ├── test_penalty_caps.py
│   ├── test_routing_and_overflow.py
│   ├── test_runtime_predictor.py
│   ├── test_scale_down.py
│   ├── test_scale_up.py
│   └── test_spot_handling.py
├── analysis/
│   ├── 01_bill_analysis.ipynb
│   ├── 02_instance_log_analysis.ipynb
│   ├── 03_fleet_timeseries_analysis.ipynb
│   ├── 04_query_log_analysis.ipynb
│   ├── 05_config_analysis.ipynb
│   ├── client_costs.py
│   ├── labs_upgrade.py
│   ├── EXPERIMENT_RESULTS.md
│   └── HISTORICAL_FINDINGS.md
├── simkit/        ← from the assignment
├── scenarios/     ← from the assignment
└── history/       ← from the assignment
```

**Another option:** You can also copy the files from this repository into the original assignment folder. If you choose this option, skip this `README.md` because the assignment folder already has its own.

Run all the commands below from this folder.

If you get an error like `No module named simkit` or `No such file or directory`, first check that you are in the correct directory and that you copied all the required folders.

## 1. Run the unit tests

Run:

```bash
PYTHONHASHSEED=0 python3 -m unittest discover -s tests -p "test_*.py"
```

**Expected result:** You should see `Ran 123 tests` followed by `OK`. The tests should finish in less than a second.

The tests use a fake simulator context to check the scheduler's behaviour and rules separately.

They cover:

- Priority tiers
- Scaling up and down
- Query routing and overflow
- Spot handling and the spot-cost formula
- Runtime prediction
- Penalty caps
- Do-not-serve rules
- Configuration changes

You can find more details about what each test file covers in section 10 of [`NOTES.md`](NOTES.md).

## 2. Run the full-month simulations

Run both scenarios:

```bash
PYTHONHASHSEED=0 python3 -m simkit.run --scenario scenarios/dev_steady  --scheduler my_scheduler.py:MyScheduler
PYTHONHASHSEED=0 python3 -m simkit.run --scenario scenarios/dev_changes --scheduler my_scheduler.py:MyScheduler
```

Each simulation runs for 30 days and takes around 1.5 minutes. On the machines I tested, the runs took between 82 and 95 seconds. The assignment allows up to 10 minutes.

You will also see progress printed once per simulated day.

### Expected results

The scheduler is deterministic, so the results should match these figures to the cent.

| | `dev_steady` | `dev_changes` |
| --- | ---: | ---: |
| **Profit** | **$14,666.36** | **$13,617.73** |
| Revenue | $18,000.00 | $18,333.33 |
| Compute | $2,919.84 | $3,628.11 |
| Miss penalties (after caps) | $413.80 | $1,067.50 |
| Drop penalties | $0.00 | $20.00 |

### Checks during the simulation

The scheduler also checks its own bookkeeping every simulated hour. At the end of each run, it checks that every query finished exactly once and that the final counts match the simulator.

A successful run should not print any extra warning messages.

If something goes wrong, you may see a message starting with `INVARIANT FAILED` or `assign failed`.

The simulation logs are saved in:

```
results/<scenario>/MyScheduler/
```

### Compare the results with the provided baselines

The assignment README contains the baseline results. If you want to run them yourself and compare, use:

```bash
PYTHONHASHSEED=0 python3 -m simkit.run --scenario scenarios/dev_steady  --scheduler simkit.baselines:CheapestFirst
PYTHONHASHSEED=0 python3 -m simkit.run --scenario scenarios/dev_steady  --scheduler simkit.baselines:FastestFirst
PYTHONHASHSEED=0 python3 -m simkit.run --scenario scenarios/dev_changes --scheduler simkit.baselines:CheapestFirst
PYTHONHASHSEED=0 python3 -m simkit.run --scenario scenarios/dev_changes --scheduler simkit.baselines:FastestFirst
```

**For a quicker test:** Add `--days 2` to any of the commands above. This runs only two days instead of the full month and takes a few seconds.

## 3. Reproduce the per-client results (optional)

After running both simulations, you can also reproduce the per-client figures using the logs they generated.

Run:

```bash
python3 analysis/client_costs.py results/dev_steady/MyScheduler scenarios/dev_steady
python3 analysis/labs_upgrade.py results/dev_changes/MyScheduler results/dev_steady/MyScheduler
```

The two scripts do different things.

`client_costs.py` shows each client's allocated and direct compute costs, penalties, profit, and how much of its runtime was spent on spot instances.

For `dev_steady`, you should see 53.2% utilisation and the following figures for `trial_pool`:

- Allocated cost: $458.89
- Direct cost: $210.39
- Runtime on spot: 99.6%

`labs_upgrade.py` looks at the `adhoc` queries for `labs_ds` after its upgrade on day 20.

The expected result is:

```
4,305 of 22,372 labs adhoc queries missed (19.2%)
```

## 4. Open the Part 1 notebooks (optional)

If you want to go through the historical analysis yourself, first install the notebook dependencies:

```bash
uv pip install jupyter pandas numpy matplotlib
```

Then open the notebooks:

```bash
jupyter notebook analysis/
```

Run them in order, starting from `01` and going up to `05`.

Each notebook can find `history/last_month/` automatically, whether you run it from the main project folder or from `analysis/`.

The main findings are also summarised in [`analysis/HISTORICAL_FINDINGS.md`](analysis/HISTORICAL_FINDINGS.md) and section 2 of [`NOTES.md`](NOTES.md).

## How the scheduler works (short version)

Here is a quick overview of the main decisions the scheduler makes. The full explanation and the reasoning behind each decision are in [`NOTES.md`](NOTES.md).

**Routing:** Queries are grouped by client, type, SLA, and penalty. Each group is assigned to the cheapest machine type and market (spot or on-demand) that can still meet its SLA.

**Dispatch:** Whenever a machine slot becomes free, the scheduler looks at the waiting queries and gives priority to the one with the most money at stake. There are four priority levels:

1. Queries that are about to be dropped.
2. Queries that can still finish before their deadline.
3. Late queries that can still avoid a drop penalty.
4. Queries with no remaining penalty, which can use otherwise idle slots.

**Scaling:** Every minute, the scheduler calculates how many machines each pool needs. It considers:

- The work currently running.
- Extra warm capacity for queries with very short SLAs.
- Waiting queries and how much time they have left.

Machines that stay idle are released after their own start-up time.

**Spot instances:** Spot instances are not treated as free or automatically cheaper. The scheduler calculates their expected cost, including the cost of restarts after interruptions, and only uses them when they are cheaper than on-demand instances.

**Penalty caps:** Each client has a monthly penalty cap. Once that cap is reached, additional SLA misses no longer cost anything. At that point, the scheduler stops spending extra money trying to prevent those misses.

This is the short version of how everything fits together. [`NOTES.md`](NOTES.md) goes into more detail about each part and explains the measurements and reasoning behind the final settings.