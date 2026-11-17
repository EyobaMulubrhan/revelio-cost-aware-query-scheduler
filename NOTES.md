# NOTES: Cost-Aware Query Scheduler

In this scheduler, I decide which machines should run each query, when to start them, and whether to use spot or on-demand machines. The main goal is to keep the running costs low while making as much profit as possible.

This document covers the three parts of the assignment:
- **Part 1:** Section 2 contains my analysis of last month's data and what I found.
- **Part 3:** Sections 3 and 9 explain how I approached the scheduling problem, the decisions I made, the trade-offs I considered, and my business recommendation.
- **Supporting details:** Sections 4–8 explain how the scheduler works in more detail. Sections 10 and 11 cover testing and performance.

I have also separated the results based on where they came from, so it is clear what each figure represents:
- **Historical results:** Figures from last month's logs in `history/last_month/`.
- **Simulation results:** Results from running a scenario for the full 30 days with `PYTHONHASHSEED=0`.
- **Stress tests:** Results from tests using modified copies of the original scenarios.
- **Unit tests:** Tests that check whether individual parts of the scheduler work as expected.
- **Estimates and judgements:** I have pointed these out wherever I have used them.

All amounts are in US dollars.

## 1. Results
My main goal when building this scheduler was to improve profit without spending too much on compute. I calculated profit using this formula:

**Profit = revenue - compute cost - SLA miss penalties after caps - drop penalties**

The revenue is fixed for each scenario, so it is the same regardless of which scheduler is running. It is $18,000 for `dev_steady` and $18,333.33 for `dev_changes`.
I ran both development scenarios for the full 30 days and compared my results with the two provided baselines: `FastestFirst` and `CheapestFirst`.
On `dev_steady`, my scheduler made **$14,666.36**. `FastestFirst` made $6,385.79, while `CheapestFirst` made $11,475.65.
On `dev_changes`, my scheduler made **$13,617.73**. `FastestFirst` made $6,267.79, while `CheapestFirst` made $10,353.25.
Here is how the costs were distributed.
For `dev_steady`:
- My scheduler spent $2,919.84 on compute and paid $413.80 in miss penalties. It had no drop penalties.
- `FastestFirst` spent $8,318.01 on compute and paid $3,296.20 in miss penalties.
- `CheapestFirst` spent $2,851.85 on compute and paid $3,672.50 in miss penalties.

Neither baseline had any drop penalties.
For `dev_changes`:
- My scheduler spent $3,628.11 on compute, $1,067.50 in miss penalties, and $20.00 in drop penalties.
- `FastestFirst` spent $8,410.64 on compute and paid $3,654.90 in miss penalties.
- `CheapestFirst` spent $3,305.98 on compute and paid $4,674.10 in miss penalties.

Again, neither baseline had any drop penalties.
I also ran the two baselines myself to check the results, and they matched the figures provided in the assignment README.
`CheapestFirst` was the better baseline in both scenarios. Compared with it, my scheduler made an additional **$3,190.71 (27.8%)** on `dev_steady` and **$3,264.48 (31.5%)** on `dev_changes`.
One thing worth pointing out is that my scheduler actually spent slightly more on compute than `CheapestFirst`: 2.4% more on `dev_steady` and 9.7% more on `dev_changes`.
However, it reduced miss penalties by 88.7% and 77.2%, respectively. I calculated these percentages by comparing the two schedulers in each scenario.
So, even though my scheduler spent a little more on machines, the reduction in penalties was much bigger. That is where most of the improvement in profit came from.
There were a few other things I checked while looking at the results:
- On `dev_changes`, the scheduler made 10 deliberate do-not-serve decisions. These resulted in $20 in drop penalties instead of higher miss penalties. There were no such decisions on `dev_steady`.
- Machine utilisation improved from 26.3% with last month's fixed fleet to 53.2% on `dev_steady` and 58.4% on `dev_changes`. I calculated utilisation by comparing the time machine slots were actually busy with the total time we paid for them.
- Each full-month simulation took around 1.5 minutes. On my development machine, `dev_steady` took 86.5 seconds and `dev_changes` took 90.5 seconds. Both are comfortably within the assignment's 10-minute limit.

These are the commands I used for the final simulations:

```
PYTHONHASHSEED=0 python3 -m simkit.run --scenario scenarios/dev_steady  --scheduler my_scheduler.py:MyScheduler
PYTHONHASHSEED=0 python3 -m simkit.run --scenario scenarios/dev_changes --scheduler my_scheduler.py:MyScheduler
```

`my_scheduler.py` imports `runtime_predictor.py`, which handles the runtime estimates used by the scheduler.

## 2. Last month (Part 1: Historical Analysis)
Before building the scheduler, I wanted to understand how the existing fleet was being used, where most of the money was going, and which types of queries were causing the biggest problems.
All the figures in this section come from the analysis notebooks in `analysis/`. I also put the main findings together in `analysis/HISTORICAL_FINDINGS.md`, where I included the full tables and the notebook used for each result.

### How much were we spending?
The total compute bill for last month was **$14,883.50**.
All machines were on-demand, and the same fleet was running throughout the month. It consisted of:
- 2 `std.small`
- 6 `std.large`
- 6 `mem.xlarge`

Most of the money went to `mem.xlarge`. It cost $11,791, which was 79% of the total bill. `std.large` cost $2,948 (20%), while `std.small` cost only $145 (1%).
What stood out to me was that the daily bill stayed at $494.40, regardless of how much traffic came in.
For example, a weekday had around 31,500 queries, while a weekend day had around 11,500. Even with that difference in traffic, the daily cost stayed the same.
The reason was simple: the fleet was fixed, and all the machines kept running throughout the month.

### How much of that capacity were we actually using?
The overall fleet utilisation was only **26.3%**.
This means that out of all the machine capacity we paid for during the month, only about a quarter was actually used to run queries. The remaining capacity was sitting idle, even though the machines were running and ready to accept work.
Looking at the costs:
- About $4,331 (29% of the bill) went towards running queries.
- The remaining $10,550 (71%) was spent keeping capacity available but idle.

To calculate this, I looked at how many seconds the machine slots were available and how many of those seconds were actually spent running queries. I then compared the total busy slot-time with the total slot-time we paid for across the fleet.
These numbers show that a large part of the bill was going towards unused capacity.
However, I would not say that we could simply remove the entire $10,550 worth of idle capacity and save that money. Some spare capacity is necessary, especially for queries with very short service-level agreements. They need to start quickly, and waiting for a new machine to launch could already make them miss their deadline.
I also noticed that having spare capacity in the fleet did not always mean that the capacity was available where it was needed.
The fleet was often idle at night, during weekends, and in the standard machine pools. At the same time, `mem.xlarge` was full in 26.5% of the recorded snapshots. At its busiest point, there were 2,989 queries waiting, even though `std.large` had around 45 free slots.
This showed me that the problem was not simply about having too many or too few machines.
The capacity was not always in the right machine pool when queries needed it. So, the scheduler needed to consider which machine types were needed, how much capacity each pool required, and when that capacity should be available.

### How much did each client cost?
I also looked at how much revenue each client brought in, how much compute they used, and how much they paid in penalties.
Here is what I found:

**acme_retail**

- Revenue: $9,000
- Allocated compute cost: $2,345
- Direct execution cost: $436
- Penalties: $491
- Profit after allocated compute and penalties: approximately **$6,164**

**finpay**

- Revenue: $6,000
- Allocated compute cost: $1,186
- Direct execution cost: $159
- Penalties: $59
- Profit: approximately **$4,755**

**labs_ds**

- Revenue: $3,000
- Allocated compute cost: $9,569
- Direct execution cost: $3,151
- Penalties: $900 after reaching its monthly cap.
- Its penalties would have been $9,417 before applying the cap.
- Profit after allocated compute and penalties: approximately **-$7,469**

**trial_pool**

- Revenue: $0
- Allocated compute cost: $1,783
- Direct execution cost: $585
- Miss penalties: $0
- Profit: **-$1,783**

There are two different compute cost figures here, and it is important to understand the difference.
- **Allocated compute cost:** This includes idle time. I divided each pool's actual bill between clients based on the busy slot-seconds they used in that pool. When added together, the four client figures give the full $14,883 bill.
- **Direct execution compute cost:** This only counts the slot-seconds each client actually used, based on the pool's average price per slot-second. The four figures add up to $4,331.

The difference between the two comes from idle capacity. Since the fleet was only 26% utilised, most of the bill was for machines that were available but not doing any work.
I used allocated compute cost when calculating client profit because I wanted to account for the actual bill.
However, there is something important to keep in mind: these figures are each client's share of the bill. They do not represent how much extra money we would save by removing that client. The fleet was fixed, so removing one client would not have reduced last month's bill.
From these results, `acme_retail` and `finpay` were profitable.
`labs_ds` was losing money even when looking only at its direct execution cost. It used 81% of `mem.xlarge` time, and 79% of its adhoc queries missed their 30-minute service-level agreement.
`trial_pool` was not bringing in any revenue, but it still used 15% of the busiest machine pool.
These findings helped me understand why the scheduler needed to consider both the cost of running queries and the value of the work being served.

### What did the traffic look like?
Another thing I found was that the number of queries alone did not tell the whole story.
Lookups and dashboards made up 80% of all queries, but they used only around 1% of the total slot-time.
The other query types used much more capacity:
- Adhoc queries used 46.6% of slot-time.
- Reports used 34.7%.
- Filtered aggregations used 17.7%.

Together, these three query types accounted for 99% of the actual work.
This told me that sizing machine pools based only on query counts would not work very well. I also needed to consider how long each type of query takes to run.
Traffic also followed some clear patterns:
- Weekdays had around 2.7 times more traffic than weekends.
- Within a day, traffic could vary by about 10 times between 02:00 and the busy periods around 10:00 and 13:00–14:00.
- Different query types had different busy periods. Adhoc queries were busiest from 11:00 into the evening. Acme reports peaked at 02:00 every day, while labs reports were busiest between 12:00 and 14:00 on weekdays.

There were also sudden traffic bursts that did not follow the normal daily pattern. Some minutes had as many as 153 arrivals, mostly dashboards.
One example was day 24. Acme's `filtered_agg` traffic suddenly increased by three times and filled the `std.large` pool.
Dashboards then had to wait behind jobs that took 240 seconds to finish. This caused 3,242 out of the month's 3,243 dashboard misses.
This was important for my scheduler because queries with seconds-level service-level agreements cannot always wait for a machine to become available. Some capacity needs to be ready before they arrive.
They also should not have to wait behind long-running queries.
I found that waiting was responsible for most of the missed deadlines. About 95% of misses came from waiting, mostly labs adhoc queries waiting for `mem.xlarge`. Only 2.5% were caused by the query runtime itself.
I also checked whether queries using the same template had similar runtimes.
Most of them were within about plus or minus 8% of each other. The online per-template runtime estimate had a median error of around 5–6%, compared with 11–47% when using the benchmark alone.
This was one of the reasons I decided to build a runtime predictor instead of relying only on the benchmark values.
Finally, there was no historical spot usage. All the machines last month were on-demand, so I could not use the historical logs to understand how spot interruptions affected the workload. I had to rely on the configuration and simulator for that part.

## 3. Scheduling Approach (Part 3)
After going through the historical data, I focused on three main questions:
1. Where should each query run?
2. Which query should run next?
3. How many machines should be available?

For each of these decisions, I wanted the scheduler to consider both compute costs and the penalties that could be avoided.

### Where should a query run?
I started by grouping queries into classes based on their client, query type, service-level agreement, and penalty.
I did this because two queries of the same type might still have different scheduling requirements depending on who submitted them, their deadlines, and the penalties attached to them.
I wanted the scheduler to take those differences into account instead of treating every query of the same type equally.
For each class, the scheduler checks which machine types can normally finish the query within its deadline. It uses the upper runtime estimate for this check.
From the suitable options, it chooses the machine type and market (spot or on-demand) with the lowest expected compute cost.
Sometimes an individual query takes longer than the usual runtime for its class. If the query's own template estimate shows that it cannot meet its deadline on the selected machine, the scheduler moves it to the cheapest faster machine type that can finish it on time.
I also added overflow handling.
This allows a query to use a free slot in another machine pool when its own pool is busy. However, I only allow this when waiting for its original pool would cause it to miss its deadline and the extra compute cost is lower than the penalty that would be avoided.
I did not want routing decisions to remain fixed after they were first made.
The scheduler recalculates them every hour to check whether the selected machine pools are still the best options. It also recalculates them whenever the configuration changes, because changes to prices, capacity, or other settings can affect which pool is the most suitable or cost-effective.
When routing is recalculated, I also check the queries that are already waiting in queues.
If another pool has become a better option for a waiting query, I move it there instead of leaving it in its original queue.

### Which query should run next?
Whenever a machine slot becomes free, the scheduler needs to decide which waiting query should use it.
I did not want to simply choose the query that arrived first. Instead, I used four priority levels based on the penalties that could still be avoided by running each query.

**1. Queries that are close to their 24-hour drop time**

These queries are close to being dropped if they do not start running.
I prioritise the ones that can avoid the highest drop penalty for every second of machine time they need. If two queries have the same value, I choose the one that is closer to its drop time.

**2. Queries that can still meet their deadline**

These are queries that can still finish within their service-level agreement if they start running now.
I calculate their remaining spare time by subtracting their expected runtime from the time left before their deadline.
I give priority to the queries with the least spare time because they are closer to missing their deadlines.
If two queries have similar spare time, I give priority to the one with the higher miss penalty compared with the machine time it needs.

**3. Queries that can only avoid a drop penalty**

These are queries that have already missed their deadline, or queries that have no remaining miss penalty because their client's penalty cap has already been used up.
However, they must still have a drop penalty to qualify for this level.
I prioritise them in the same way as the first level, based on how much drop penalty they can avoid for every second of machine time they need.

**4. Queries with no remaining penalty to avoid**

These queries have neither a miss penalty nor a drop penalty.
This level also includes queries that the do-not-serve rule allows to run only when a machine slot would otherwise remain unused.
I only run these queries when there is spare capacity. Within this group, I choose the oldest query first.
I also keep late queries in a separate queue from queries that can still meet their deadlines.
This is important because I do not want queries that are already late to get in the way of queries that still have a chance to finish on time.
There are cases where a late query becomes eligible again. For example, its runtime estimate might improve, or the runtime matrix might change.
When that happens and the query can meet its deadline again, I move it back into the on-time queue.

### How many machines should be running?
The scheduler checks how much capacity is needed every minute.
For each machine pool, I consider three things when calculating the target capacity:
1. **Work that is already running.** I need enough slots for the queries that are currently using the pool.

2. **Warm capacity for queries with very short deadlines.** Some queries have service-level agreements of only a few seconds. A newly launched machine might take too long to become ready, so I need some capacity available before those queries arrive.

3. **Waiting queries.** I also consider the work waiting in the queues and how much time those queries can still wait for a new machine.

After calculating the target, the scheduler launches enough machines to reach the required capacity. Machines that are still starting up are also counted when calculating available capacity.
I also needed a way to release machines when they were no longer needed.
A machine must have been idle for at least its own start-up time, including join time and warm-up time. It must also have passed the minimum billing period.
Before releasing it, I check that enough capacity will remain in the pool. I keep an additional 20% capacity above the target to avoid releasing machines too quickly.
The full calculation for deciding when to launch and release machines is explained in section 7.

### Trade-offs I had to consider
While building the scheduler, I tested different settings to see how they affected compute costs, missed deadlines, and overall profit.

**How much warm capacity should be kept?**

One of the decisions I had to make was how much capacity should stay ready for dashboards and other queries with very short deadlines.
I tested a smaller warm buffer, and it performed better in both development scenarios.
On `dev_steady`, the smaller buffer saved around $166 in compute costs while adding about $51 in miss penalties.
I considered this a better result because the extra misses were relatively cheap under the current configuration.
However, my warm capacity calculation does not consider the penalty amount. If these queries had much higher miss penalties, keeping a larger warm buffer could give better results.

**Should the scheduler use spot machines?**

Spot machines are about 65% cheaper than on-demand machines, but they can be interrupted.
When an interruption happens, the query has to start again from the beginning.
I included the expected cost of these restarts when comparing spot and on-demand machines. This makes spot less attractive for longer queries, especially when interruptions happen frequently.

**When should idle machines be released?**

I also tested how long an idle machine should remain running before the scheduler releases it.
Releasing a machine after one start-up time performed better than waiting for two or four start-up times.
Compared with waiting for two start-up times, this improved the total result by $209.63. Compared with waiting for four, it improved the result by $420.42 across the two development scenarios.

**How should penalty caps affect scheduling?**

Each client has a monthly limit on the penalties they can incur for missed queries.
Once that limit is reached, additional misses do not reduce the scheduler's profit.
At that point, the scheduler stops spending extra money to prevent those misses.
This helps maximise the profit measured by the simulation, but it does not necessarily mean the client is receiving good service. I discuss this further in section 9.

**When should the scheduler stop serving a query?**

Another decision was whether it still makes sense to run a query that is certain to miss its deadline.
In some cases, dropping a query costs $2, while serving it late costs $5.
I added a do-not-serve rule for these situations so the scheduler does not spend more on a query than the penalty it would save.
This improves the measured profit, but it also means some queries are deliberately dropped. I discuss the business implications of this decision in section 9.

### What happens when the inputs change?
I did not want the scheduler to depend on the initial configuration staying the same throughout the simulation.
It reads `ctx.config` whenever it makes a decision.
When the configuration changes, `on_config_change` checks late queries again, reconsiders do-not-serve decisions, and recalculates the routing of waiting queries.
I tested several types of changes using full simulations.

**When the fleet changes**

The scheduler recalculates routing using the new prices, quotas, spot capacity, and interruption rates.
It avoids markets that no longer have available capacity.
If a spot launch fails, it waits 10 minutes before trying that market again. During that time, it can use on-demand machines as a fallback for work that is worth paying for.
A machine that has received a spot interruption notice only accepts queries that can finish before it is reclaimed.
I tested these changes in `test_events`, `test_spot_storm`, and `test_outage`.

**When the runtime matrix changes**

The predictor's learned runtimes remain valid because they are stored as multiples of the benchmark.
The scheduler recalculates feasibility and work totals. It also checks late queries again so that a query that can now meet its deadline can return to the on-time queue.
For a new query type, the scheduler only uses machine types with a known runtime. If no runtime is available, the query waits safely.
I tested this in `test_runtime_update`, `test_new_query_type`, and `test_partial_row`.

**When contracts or clients change**

The scheduler uses updated service-level agreements, penalties, caps, and drop penalties in its next decision.
It also adjusts the penalty cap for a new client based on the day that client joins.
I tested these changes in `test_caps`, `test_events`, and `dev_changes`.

**When traffic changes unexpectedly**

The scheduler uses a decaying arrival-rate estimate to decide how much warm capacity is needed.
If a query arrives and there is no free slot, it immediately checks whether more machines are needed.
I also tested a three-day period with no traffic. During that period, the fleet reduced to almost zero and recovered when traffic returned, without any drops.
When there is more work than the available capacity can handle, the priority levels decide which queries get the available slots based on the penalties that can still be avoided.
I tested these situations in `test_burst`, `test_no_traffic_gap`, and `test_overload`.
All these stress tests completed without a crash, and their end-of-run checks passed.
Some tests deliberately created situations where queries had to be dropped, such as machine outages and overload scenarios.
The results and more details about these tests are in section 10.

## 4. Cost and Penalty Model

### How I compare compute costs
When deciding where a query should run, I first estimate how much it would cost on each available machine option.
I calculate the price per slot-second by taking the machine's hourly price, dividing it by the number of slots, and then dividing it by 3,600.
I then multiply that price by the query's expected runtime to estimate its compute cost.
This gives me a consistent way to compare different machine types and markets for the same query.
However, this estimate is only used when comparing machine options. It is not the actual bill.
The simulator charges for the entire machine from the time it is launched until it is terminated, with a minimum billing period of 60 seconds. This means we also pay for idle time.
I account for these wider fleet costs when deciding how many machines to launch and when to release them. I explain this further in section 7.

### How I account for penalty caps
Each client has a monthly cap on the penalties they can receive for missed queries.
I need to consider this when deciding how much it is worth spending to prevent a query from missing its deadline.
I calculate the remaining cap by subtracting penalties that have already been charged and penalties from queries that are already certain to miss.
When evaluating a query, I use whichever amount is smaller: its miss penalty or the remaining cap.
For example, if a query has a $20 miss penalty but only $10 remains under the client's cap, I treat its avoidable penalty as $10.
Once the cap is used up, additional missed queries no longer reduce the simulated profit.
These queries therefore stop competing for priority as avoidable misses, although the scheduler can still serve them before their drop time.
I control this behaviour using `CAP_AWARE`.
When calculating the remaining cap for a query, I exclude that query's own committed penalty. This prevents me from counting its penalty against the cap before I have evaluated it.

### How I identify queries that are certain to miss
Before deciding whether to serve a late query, I first check whether it still has a chance of meeting its deadline.
For a query that is waiting, I check whether it would still miss its deadline even if it started immediately on the fastest available machine.
I use the optimistic lower runtime estimate for this check.
If it would still miss its deadline, I consider it certain to miss.
For a query that is already running, I consider it certain to miss once it continues running past its deadline.
This helps me identify queries where spending more resources can no longer prevent a missed deadline.

### When I decide not to serve a query
I added a `DO_NOT_SERVE` rule to avoid spending more on a query than the penalty that would be charged if it were dropped.
I set `DNS_MARGIN` to $0.05. This gives a small margin when comparing the two costs.
When a waiting query is certain to miss, I compare its capped late penalty with its drop penalty.
For example, an acme report has a $5 penalty if it is served late, but only a $2 penalty if it is dropped.
Since serving it late would cost more, dropping it is the cheaper option from the simulation's point of view.
There are three possible outcomes:

**1. Do not serve the query.**

If its capped late penalty is at least $0.05 higher than its drop penalty, I decide not to spend machine resources on it.

**2. Serve it only when a slot is idle.**

Sometimes serving the query could still make sense if there is spare capacity, but not if the scheduler needs to pay for additional capacity.
In that case, I allow the query to run only when a machine slot would otherwise remain unused.
I make this decision by comparing the late penalty plus the cheapest slot cost against the drop penalty and the $0.05 margin.

**3. Serve it normally.**

If neither of the conditions above applies, the query remains eligible to run normally.
I do not reject these queries immediately using `ctx.reject`.
Instead, they remain pending until the simulator reaches the maximum queue time, which is 24 hours in the development scenarios. The simulator then drops them and charges the drop penalty.
The scheduler can also reconsider this decision.
It checks do-not-serve queries on every tick, whenever the configuration changes, and whenever a query is considered for a machine slot.
For example, if the client's remaining penalty cap changes or the query's runtime estimate improves, serving it might become worthwhile again.
When that happens, the scheduler can allow it to run.

### Results from the simulations
On `dev_changes`, the scheduler made this decision for 10 queries.
All 10 were acme reports. Each had a $2 drop penalty instead of a $5 late penalty.
There were no do-not-serve decisions on `dev_steady`.
The `test_outage` scenario also produced 17 such decisions.
None of these decisions was reversed during a full simulation. I tested whether the scheduler could reverse them through unit tests.
This approach improves the profit measured by the simulation, but deliberately dropping queries can affect the client experience. I discuss those business risks in section 9.

## 5. Spot and On-Demand Handling
Spot machines are cheaper than on-demand machines, but they can be interrupted while a query is running.
When an interruption happens, the query goes back into the queue and has to start again from the beginning.
I wanted the scheduler to account for this lost work when comparing spot and on-demand costs.
Just because spot has a lower hourly price does not automatically mean it is the cheaper option, especially if a query is likely to be interrupted several times.

### How I calculate the spot cost
I estimate how much time a query is expected to occupy a spot slot, including the time lost when it gets interrupted and has to restart.
The simulator uses an interruption rate to determine how frequently spot machines are interrupted.
I represent this rate as `rate`, measured in interruptions per second. If the estimated query runtime is `runtime`, the probability that a spot machine survives long enough to finish the query is:

```
P(survive) = exp(-rate * runtime)
```

If the query gets interrupted, it starts again from the beginning.
Since my implementation allows unlimited retries on spot, I calculate the expected total time spent attempting the query as:

```
Expected spot time = (exp(rate * runtime) - 1) / rate
```

I then calculate the expected spot cost:

```
Expected spot cost = spot price per slot-second * expected spot time
```

In the code, I use `math.expm1()` instead of calculating `exp(x) - 1` directly.
This gives better numerical accuracy when `rate * runtime` is a small value.
If that value is greater than 700, the method returns infinity to avoid numerical overflow.

### Assumptions and limitations
There are a few assumptions behind this calculation:
- It estimates the cost of one query's slot usage, not the full machine bill.
- It assumes the interruption rate and spot price stay the same while the query is running.
- It does not include the start-up cost of a replacement machine.
- It does not include spot interruption notices in the formula. The scheduler handles notices separately by avoiding new work on a machine when the query is unlikely to finish before it is reclaimed. Because of this, the formula can slightly overestimate the actual cost.
- The calculation assumes every retry happens on spot. In practice, a retried query goes through normal routing again. It might run on a different machine type, use a free slot in another pool, or use on-demand capacity. This can also make the formula slightly overestimate the actual cost.

### When I allow a query to use spot
The `spot_ok()` method allows a query to use spot capacity only when two conditions are met:
1. Spot capacity is available and usable.
2. The estimated spot cost is lower than the on-demand cost using the query's upper runtime estimate.

So, I do not choose spot just because its hourly price is lower. I compare the expected cost after taking possible interruptions into account.

### Handling spot interruptions and failed launches
When the simulator sends a spot interruption notice, the scheduler handles the machine based on what it is currently doing.
- If the machine is still starting or is ready but idle, it is released immediately.
- If the machine is already running a query, I allow that query to finish if its predicted completion time is before the machine's reclaim time.
- The machine does not receive new work unless the query is predicted to finish before reclamation. Once it becomes idle, it is released.

If a spot machine launch fails, the scheduler puts that pool into a 600-second cooldown.
During this period, it can use on-demand machines as a fallback for the part of the target capacity needed by queries with penalties.
Other work can wait until spot capacity becomes available again.

### What the experiments showed
I tested different ways of handling spot interruptions in the development scenarios and stress tests.
In an earlier version, the scheduler forced a query to switch to on-demand after two spot interruptions.
This often caused the scheduler to launch a new on-demand machine for a single interrupted query, even when the query could probably have finished on spot.
Removing this forced switch improved profit by:
- $184.39 on `dev_steady`.
- $208.74 on `dev_changes`.

Removing the separate miss-risk term as well improved profit by $214.46 and $870.41, respectively.
In the final implementation, retries are allowed to continue on spot, and the expected-cost calculation accounts for the repeated attempts.
The revised calculation produced the same results, to the cent, as the previous approach in both development scenarios.
The stress tests gave more mixed results:
- **Spot interruption stress test:** With five interruptions per hour, the revised calculation improved average profit by $262.45 across three runs.
- **Extreme overload test:** With one machine per type and market and three interruptions per hour, average profit decreased by $3,307.02 across three runs.
- **Other scenarios:** Configuration changes, outages, contract changes, and traffic bursts showed no change.

The overload result showed me a limitation of the cost calculation.
It estimates the expected cost of running a query on a particular spot machine type, but it does not consider how limited capacity is across the other machine types.
When the system is heavily overloaded, a query might be moved away from spot because its estimated cost is higher, even though the alternative machine type has equally limited capacity.
This can make the overall result worse.
The experiments are documented in `analysis/EXPERIMENT_RESULTS.md`, in sections 3 and 5.

## 6. Runtime Prediction
I added the runtime predictor because the historical data showed that queries using the same template usually had similar runtimes.
The actual runtime of a query is the benchmark runtime from the runtime matrix multiplied by a hidden per-query multiplier, `m`.
The `runtime_predictor.py` file learns this multiplier from queries that have already completed.
For each template, it calculates the average of the observed multipliers.
It also uses the average for the same client and query type, giving that average the weight of two observations (`K = 2`).
If there is no data for a particular template, it falls back to the query type average.
If that is also unavailable, it uses `1`.
The predictor gives the scheduler both an upper and a lower runtime estimate.
For the upper estimate, it multiplies the estimated multiplier by:

```
(1 + 1.65 * spread)
```

This gives an approximate one-sided 95% upper bound, using `Z = 1.65`.
The spread considers two things:
- How much the runtime varies within the template.
- How uncertain the estimate is because there are only a few observations.

Until a template has 30 observations, the default within-template spread is 10%.
For the lower estimate, the predictor divides the estimated multiplier by:

```
(1 + 1.65 * spread)
```

I only use this lower estimate when checking whether a query is certain to miss its deadline.
The scheduler uses the different estimates for different decisions:
- **Mean estimate:** Used for cost calculations, ordering queries, and deciding whether a query is late.
- **Upper estimate:** Used to check whether a query can meet its deadline.

The predictor also caches estimates for each template.
When a new observation for a query type becomes available, the cache is updated.
The cache is only there to make lookups faster. It gives the same values as recalculating the estimates every time.

## 7. Fleet Scaling
The scheduler checks how much capacity is needed every minute.
It also checks immediately when a query with a seconds-level service-level agreement arrives and finds that there is no free slot.
For each machine pool, the target capacity is calculated as:

**target = busy slots + max(busy latency-critical slots, warm need) + queued work ÷ horizon**

There are three main parts to this calculation.

**First: Capacity for queries that are already running.**

The scheduler looks at how many slots are currently being used by running queries. This capacity needs to remain available.

**Second: Warm capacity for latency-critical queries.**

Some queries have miss penalties and service-level agreements that a newly launched machine would not be able to meet.
Examples include dashboards and lookups with deadlines of only a few seconds.
For these queries, I calculate the warm capacity needed using:

**R + 1.28 × sqrt(R)**

Here, `R` is the class's arrival rate multiplied by its mean runtime.
The arrival rate is adjusted using a 600-second time constant.
This is square-root staffing, with a target of about 90% coverage.

**Third: Capacity for queries waiting in the queue.**

The scheduler also considers the work waiting in each queue.
It predicts the runtime of those queries and divides the total work by the amount of time each class can still wait for a new machine.
The horizon used for this calculation ranges from 60 seconds to 4 hours.
After calculating the target, the scheduler launches machines until the pool reaches the required capacity.
It also needs to release machines when they are no longer needed.
A ready machine can be released when:
- It has been idle for at least its own start-up time, including join time and warm-up time.
- It has passed the minimum billing period.
- The pool will still have at least 1.2 times its target capacity after the machine is released.

Queries with no penalty do not cause the scheduler to launch additional machines.
The same applies to queries marked do-not-serve.

## 8. Final Settings and Why I Chose Them
I tested the settings that could have a meaningful effect on the results.
I changed one setting at a time and ran both development scenarios for the full month.
Some settings were selected based on the experiments, while others were engineering decisions that I did not tune.
The results are recorded in `analysis/EXPERIMENT_RESULTS.md`.
The final scheduler's noise band was $51.49 on `dev_steady` and $89.90 on `dev_changes`.
I measured this by comparing the normal run with four runs where I changed the idle timeout by one or two seconds. These changes should not make a real difference to the scheduling behaviour.
Any result difference smaller than these amounts is within the run-to-run noise.
Here is why I kept the final settings.

**Warm capacity: `Z_WARM` and `B_WARM`**

I set these to `1.28` and `0`.
Compared with `(2.33, 1)`, this improved profit by $114.37 and $105.41.
Both improvements were above the noise bands. It also performed better on all seven stress tests.
The setting `(3.09, 2)` performed worse.

**Idle machine release: `IDLE_STARTUP_FACTOR`**

I chose `1`.
This means the scheduler can release a machine after it has been idle for one start-up time.
Waiting for two or four start-up times performed worse by $209.63 and $420.42 in total.

**Arrival-rate adjustment: `RATE_TAU_S`**

I set this to 600 seconds.
Using 120 seconds performed worse by $310.49, while 1,800 seconds was within the noise band.

**Queued-work horizon: `H_MAX`**

I kept the maximum horizon at 4 hours.
I also tested 1 hour and 8 hours, but both were within the noise band. There was no strong reason to change the original setting.

**Queue lookahead: `LOOKAHEAD`**

I kept this at 4.
This means the scheduler checks four queued entries per class during each selection.
Values of 1 and 16 were within the noise band.

**Runtime predictor: `Z`**

I kept this at 1.65.
Values of 1.28 and 2.33 were also within the noise band.

**Ordering queries in priority tier 2**

I tested two ways of ordering avoidable misses.
One used the least remaining slack, while the other used penalty divided by slack.
Both performed similarly, so I kept the least-slack approach.

**Ordering queries in priority tiers 1 and 3**

I used drop penalty per slot-second.
I tested this in an earlier version using a three-day overload scenario.
It lost $3,477.21, compared with a loss of $29,527.42 when using oldest-first.
On the development scenarios, the difference was within the noise band.

**Penalty caps: `CAP_AWARE`**

I kept this enabled.
Turning it off reduced `dev_changes` profit by $79.89 and $85.89 in the comparisons, while `dev_steady` stayed unchanged.
These differences were within the final noise band, so the experiment was not decisive.
I kept it enabled because once a client reaches its penalty cap, additional misses have no extra cost in the simulation.

**Do-not-serve: `DO_NOT_SERVE`**

I also kept this enabled.
Turning it off reduced `dev_changes` profit by $11.44 and $35.95. Both differences were within the noise band.
I kept it enabled because dropping a query can sometimes be cheaper than serving it late.
However, this is also a business decision, not just a technical one. I explain the risks in section 9.

### Other settings
Some of the remaining settings were not tuned through experiments:
- `DNS_MARGIN` is $0.05. I chose a small margin so decisions do not keep changing when the drop and miss costs are almost equal.
- `COOLDOWN_S` is 600 seconds. This is how long the scheduler waits after a failed spot launch.
- `SCALE_DOWN_MARGIN` is 1.2. This controls how much capacity stays above the target when releasing machines.
- `SLACK_TIE_S` is 60 seconds. This is the tier-2 window used to break ties using the penalty.
- `BURST_CHECK_GAP_S`, `CLEAR_BACKLOG_WITHIN_S`, and `H_MIN` are 10 seconds, 3,600 seconds, and 60 seconds. They control burst checks, backlog clearing, and the shortest scaling horizon.
- `RuntimePredictor.K` is 2. The default spreads are 10% within a template and 50% across templates. The 10% default is close to the historical within-template variation of about 8%.

Finally, I kept `CHECK_CONSISTENCY_HOURLY` and `CHECK_INVARIANTS_AT_END` enabled.
These are correctness checks. They help catch problems in the scheduler but do not affect its scheduling decisions.

## 9. Business Trade-offs, Limitations and Recommendation (Part 3)

### Do-not-serve is a business decision
The do-not-serve rule improves the profit measured by the simulation because dropping a query that is certain to be late can cost less than serving it late.
However, there is another side to this decision.
The client does not receive the result at all. Without this rule, the client would at least receive a late result.
The simulation does not measure how this could affect client trust, the perceived reliability of the service, contract terms beyond the penalty, or the risk of losing a client.
Repeatedly dropping queries could affect all of these.
The rule was used on only 10 queries out of about 788,000 in `dev_changes`.
Turning it off reduced profit by at most $35.95, which was within the noise band.
For a real service, I would consider setting `DO_NOT_SERVE = False` or limiting how many queries can be dropped for each client.

### Penalty caps also affect service quality
Penalty caps create a similar problem.
Once a client's penalty cap is used up, additional misses do not affect the simulated profit.
The scheduler then stops spending money to prevent those misses.
This is correct based on the objective in the assignment, but it can create a situation where the client already experiencing the most missed queries receives lower priority.
That is something I would want to consider before using this approach in a real service.

### Other limitations
There are a few other limitations I would keep in mind before using this scheduler in production.

**1. Warm capacity does not consider the size of the miss penalty.**

The warm capacity calculation does not look at how expensive a missed query would be.
If misses for seconds-level queries were expensive, keeping a larger warm buffer could be a better option.
A better approach would be to adjust the buffer based on the capped penalty.

**2. Spot cost does not consider how limited the available capacity is.**

As I showed in section 5, the exact expected-cost calculation performed worse than a simpler calculation in the extreme overload stress test.
The calculation looks at the expected cost of a particular spot machine type, but it does not consider how much capacity is available across the other machine types.

**3. The runtime predictor needs historical data.**

When a new and expensive query template appears, the predictor might not have enough information about how long it takes to run.
This means the first few queries could miss their deadlines, or the scheduler might launch more capacity than necessary.

**4. Do-not-serve decisions were not reversed in the full simulations.**

The scheduler supports reversing these decisions, and I tested that behaviour through unit tests.
However, no reversal happened during the full-month simulations.

**5. Partial-month simulations handle penalty caps differently.**

When running a simulation with `--days N`, the simulator adjusts the penalty caps for the shorter period.
However, the scheduler still assumes a 30-day month.

### Recommendation: Do not offer service-level agreements that the fastest machine cannot meet. Price the labs_ds upgrade by template.
I looked more closely at the `labs_ds` upgrade in `dev_changes` because it showed a problem that scheduling alone cannot solve.
The upgrade happens on day 20.
After the upgrade:
- Monthly revenue increases from $3,000 to $4,000.
- The adhoc service-level agreement changes from 30 minutes to 15 minutes.
- The miss penalty increases from $0.20 to $0.40.

After this change, 19.2% of labs adhoc queries missed their deadlines.
That was 4,305 out of 22,372 queries arriving after day 20.
For comparison, the miss rate over the same period in `dev_steady` was only 3.0%.
I then wanted to understand why so many queries were missing their deadlines.
About 87% of the misses, or 3,731 queries, took longer than 15 minutes on `mem.xlarge`, which was the fastest available machine type.
This means that no scheduling decision could have made those queries meet the new deadline.
The uncapped penalties over those ten days were $1,722.
`labs_ds` reached its $900 monthly penalty cap. In comparison, its penalties were $355.20 for the entire month in `dev_steady`.
Looking at the ten days after the upgrade:
- The upgrade added about $333 in revenue.
- It also added about $545 in penalties.
- Direct compute cost increased by about $108.

The extra compute cost was partly caused by a spot price increase in the same scenario.
Overall, the upgrade was roughly $320 worse over those ten days.
For a full month, I estimate that the upgrade would roughly break even because the penalty cap would limit the additional penalties.
However, this would only happen because the cap absorbs penalties for queries that the scheduler cannot prevent from missing their deadlines.
The figures above come from the final development runs and can be reproduced using `analysis/labs_upgrade.py`.
Based on what I found, I would recommend two things:

**1. Only offer the 15-minute adhoc service-level agreement to suitable query templates.**

I would only offer it for templates whose observed runtime on the fastest machine is comfortably below 15 minutes.
The scheduler already learns the runtime of each template, so we can use that information when deciding which templates qualify.

**2. Price the tighter service-level agreement based on its actual expected cost.**

I would not rely on the penalty cap when deciding how much to charge for the upgrade.
At this miss rate, the uncapped penalties would be about $5,000 per month, compared with only $1,000 in additional revenue.
The main point here is that offering a tighter deadline does not automatically mean the system can meet it. We need to understand the actual runtime of the work before making that promise.

### What happened with trial_pool?
I also looked at `trial_pool` because it does not pay for its usage but still takes up machine capacity.
Last month, with the fixed on-demand fleet:
- Allocated compute cost was $1,783.
- Direct execution compute cost was $585.
- It did not use spot.

With my scheduler, the figures were:

| <br>                          | **dev_steady** | **dev_changes** |
| ----------------------------- | -------------- | --------------- |
| Allocated compute cost        | $458.89        | $435.93         |
| Direct execution compute cost | $210.39        | $225.01         |
| Runtime on spot               | 99.6%          | 99.3%           |

These figures come from `analysis/client_costs.py`.
The script divides each machine's cost between clients based on their busy slot-seconds.
The historical analysis used a different method and divided each pool's cost between clients.
Since the figures come from different months and different fleets, they are not directly comparable.
Although `trial_pool` does not pay and has no miss penalties, it does have a $0.25 drop penalty.
This means that queries that can only avoid a drop penalty are still worth serving.
Because of that, they can still cause the scheduler to launch additional machines.
One option would be to allow `trial_pool` queries to use idle capacity only.
Another option would be to charge the client for its usage.
I have not tested either option, so I have not included them in my final recommendation.

## 10. Testing and Robustness
I tested the scheduler in two main ways:
- Unit tests to check individual rules and parts of the implementation.
- Full simulations to see how everything works together, including when things change unexpectedly.

### Unit tests
There are **123 unit tests**, and all of them pass.
I ran them using:

```
PYTHONHASHSEED=0 python3 -m unittest discover -s tests -p "test_*.py"
```

The tests use a fake simulator context from `tests/support.py` and call the scheduler's own methods.
They cover the main parts of the implementation:
- `test_dispatch_tiers.py` has 22 tests for the priority levels, deadline checks, queries with no value, and drop-penalty ordering.
- `test_scale_down.py` has 8 tests for idle timeouts, billing minimums, and hysteresis.
- `test_scale_up.py` has 11 tests for latency-critical classes, warm capacity, horizons, work totals, and machine launches.
- `test_routing_and_overflow.py` has 11 tests for the routing table, faster-machine overrides, and overflow.
- `test_spot_handling.py` has 23 tests for spot cost calculations, hand-calculated values, the closed-form calculation, zero and extreme interruption rates, Monte Carlo testing of the retry policy, market selection, notices, and launch fallback.
- `test_runtime_predictor.py` has 12 tests for runtime estimates, how the scheduler uses the predictor, and the cache.
- `test_penalty_caps.py` has 9 tests for remaining cap amounts and committed penalties.
- `test_do_not_serve.py` has 20 tests for the do-not-serve decision, query life cycle, the query's own cap room, and when the decision is checked again.
- `test_config_robustness.py` has 7 tests for hourly checks with different tick lengths and incomplete runtime rows.

### Checks during and after simulations
I also added checks that run during the simulation.
They are enabled by default and do not change the scheduling decisions.
`check_consistency()` runs once every simulated hour.
It checks that the queues, indexes, counts, work totals, and committed penalties agree with each other and with the simulator's running queries.
At the end of the run, `check_invariants()` checks that:
- Every query finished exactly once.
- The scheduler's counts match the simulator's counts.
- Nothing is left in the queues.

I also check that three counters stay at zero:
- Assignment errors.
- Unknown events.
- Attempts to start a query on a machine under notice when the query cannot finish before it is reclaimed.

I also tested whether turning off the hourly consistency check would improve performance.
It did not make a measurable difference to the run time.
In a five-day simulation, the measured times were between 17.7 and 19.7 seconds either way.

### Stress tests
I also tested the scheduler using modified copies of the original scenarios.
I wanted to see how it would behave when prices, capacity, traffic, runtimes, and contracts changed.
All 20 runs completed without exceptions.
There were no assignment errors or unknown events, and all end-of-run checks passed.
Here are the main results.

**Configuration changes (`test_events`)**

I tested seven changes over 2.5 days, including changes to prices, capacity, runtimes, contracts, and a new client.
Profit was $17,633.37, with no drops or assignment errors.

**Spot interruptions (`test_spot_storm`)**

I increased spot interruptions to five per hour on all spot types and reduced `std.large` spot capacity for one day.
Profit was $9,167.56, with no drops or assignment errors.
I also ran the scenario with two additional simulator seeds. The profits were $9,160.21 and $9,114.00.
All checks passed.

**Machine outage (`test_outage`)**

I removed `std.large` and `mem.xlarge` capacity for 10 hours.
The scheduler completed with $14,034.02 profit and 17 dropped queries.
There were no assignment errors, and all checks passed.

**Penalty cap changes (`test_caps`)**

I reduced the acme cap to $10, later changed it to $360, and added a new client.
Profit was $18,437.35, with no drops or assignment errors.

**Traffic burst (`test_burst`)**

I increased traffic by three times for one hour.
Profit was $14,622.37, with no drops or assignment errors.

**Runtime changes (`test_runtime_update`)**

I changed the runtime matrix on days 10 and 20.
Profit was $14,559.79, with no drops or assignment errors.

**New query types**

I tested adding a new query type on day 2 using `test_new_query_type`.
Profit was $14,622.86.
I also tested a new query type with a runtime available only on `mem.xlarge` using `test_partial_row`.
Profit was $14,663.76.
Both runs passed all checks.

**Different simulation settings (`test_coarse_tick`)**

I tested 300-second ticks and a one-hour billing minimum.
Profit was $14,544.25, with no drops or assignment errors.

**No traffic (`test_no_traffic_gap`)**

I removed traffic from days 10–13.
The fleet reduced to almost zero and recovered when traffic returned.
Profit was $14,902.02, with no drops or assignment errors.

**No penalty value (`test_zero_value`)**

I set all client penalties to zero.
The scheduler launched no machines and dropped 784,650 queries, as expected, because there was no financial reason to pay for running them.
Profit was $18,000.

**Trial pool changes**

I tested setting the `trial_pool` drop penalty to zero using `test_free_trial`. This gave $14,633.37.
I also removed its traffic using `test_no_trial`, which gave $14,861.30.
Both runs passed all checks.

### Overload tests
I also wanted to understand what happens when the available machines are not enough to handle all the incoming work.
So I ran several overload scenarios with different capacity limits and drop times.

```
test_overload_long
```

I used two machines per type and market, with the normal 24-hour drop limit.
The scheduler made $9,618.57 with no drops.

```
test_overload_short
```

I then ran a three-day version of the extreme overload scenario described below, where clients reach their penalty caps quickly.
The scheduler made -$6,317.63 and dropped 6,203 queries.

```
test_overload
```

For the full-month extreme overload scenario, I used only one machine per type and market, three interruptions per hour, and a 30-minute drop limit.
The scheduler made -$29,475.50 and dropped 39,054 queries.
I also ran this scenario with two additional simulator seeds. The results were -$31,740.28 and -$29,004.18.
These overload scenarios are where the scheduler performs worst.
There is simply much more work than the available machines can handle, so the scheduler cannot prevent all the missed deadlines and dropped queries.
With the spot cost correction, `test_overload` and `test_overload_short` lost between $612 and $5,048 per run in the paired comparisons.
The detailed results are in `analysis/EXPERIMENT_RESULTS.md`.
Overall, these tests gave me confidence that the scheduler can handle configuration changes, runtime changes, outages, traffic bursts, and different simulation settings without crashing or breaking its internal checks.
They also showed me where the scheduler still struggles, especially when the available capacity is extremely limited.

## 11. Performance
I also checked how long the scheduler takes to run and which parts of the implementation use most of the time.
On my development machine, a five-day `dev_changes` simulation took between 18.3 and 18.5 seconds with my scheduler.
The `CheapestFirst` baseline took 2.8 seconds.
For the full-month simulations, `dev_steady` took 86.5 seconds and `dev_changes` took 90.5 seconds when run one after the other.
On a laptop with the same scheduling behaviour, the runs took between 82 and 95 seconds.
Both are comfortably within the assignment's 10-minute limit.
I also profiled a five-day `dev_changes` run to understand where most of the time was going.
About 71% of the total time was spent on dispatch.
Every time a new query arrives or a running query finishes, the scheduler needs to select the next query to run.
The `select()` method was called around 345,000 times.
It checks the head of each class queue and also checks whether a query can use overflow capacity.
Two parts of this process took a noticeable amount of time:
- `may_overflow()` used about 24% of the total time.
- `expected_wait()` used about 14%.

The wait calculation scans the running queries in a pool to find the earliest predicted completion time.
The runtime predictor used about 13% of the total time across 4.4 million estimate lookups.
I decided not to make further performance optimisations for this submission.
The biggest possible improvement would be to maintain an index of predicted completion times for each pool.
However, that index would need to be updated correctly every time a query is assigned, completed, interrupted, or a spot notice is received, and whenever a machine is released.
If the index became outdated, it could change the scheduler's decisions.
The maximum possible time saving would be around 14%.
I did not think the risk of changing scheduling behaviour was worth it for this submission, especially since the current implementation already runs comfortably within the assignment's time limit.
The remaining time is also spread across many smaller operations, so there is no single obvious optimisation that would make a much bigger difference.

## Files
The submission contains:
- `my_scheduler.py`: The main scheduler implementation.
- `runtime_predictor.py`: The runtime predictor used by the scheduler.
- `NOTES.md`: This document, explaining the analysis, approach, results, trade-offs, and testing.
- `README.md`: Instructions for setting up the repository, running the tests and simulations, and checking the expected results.
- `tests/`: The unit tests and shared fixtures in `support.py`.
- `analysis/01_bill_analysis.ipynb` to `05_config_analysis.ipynb`: The notebooks used for Part 1. Run them from the assignment folder.
- `analysis/HISTORICAL_FINDINGS.md`: The historical findings, including which notebook each figure came from.
- `analysis/EXPERIMENT_RESULTS.md`: The experiments I used to choose the final settings.
- `analysis/client_costs.py` and `analysis/labs_upgrade.py`: Scripts used to calculate the client cost and `labs_ds` upgrade figures from the simulation logs.
