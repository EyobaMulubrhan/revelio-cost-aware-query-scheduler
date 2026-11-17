# Historical Findings: Last Month (`history/last_month/`)

## Introduction

This document explains what happened during the last month's simulation and what the historical data tells us about the workload, machine usage, costs, and service-level performance.

The analysis is based on five notebooks in the `analysis/` directory. Each notebook focuses on a different part of the historical data:

- `01_bill_analysis.ipynb` examines `bill.csv` and explains how much the fleet cost to run.
- `02_instance_log_analysis.ipynb` examines `instance_log.csv` and looks at machine lifecycles, startup time, execution time, and idle time.
- `03_fleet_timeseries_analysis.ipynb` examines `fleet_timeseries.csv` and shows how machine utilisation and queues changed over time.
- `04_query_log_analysis.ipynb` examines `query_log.csv.gz` and provides information about queries, waiting times, execution times, service-level agreement performance, and client profitability.
- `05_config_analysis.ipynb` examines `config.json` and compares machine prices, runtimes, and other configuration settings.

All timestamps in this analysis are elapsed simulation seconds. The README defines `t = 0` as Monday at 00:00 US/Eastern. I use that convention when describing weekdays, weekends, and times of day.

The findings below distinguish between what the historical data directly shows and what I conclude from those observations. Recommendations for the scheduler are presented separately from the historical facts.

## 1. Executive Summary

The main finding from the historical analysis is that the fleet spent a large amount of money keeping machines available, but that capacity was not always available in the places where the workload needed it.

The simulation generated $18,000 in revenue. Compute spending was $14,883.50, capped service-level agreement penalties were $1,450.10, and drop penalties were $0. After these costs and penalties, the historical profit was approximately $1,666.

The fleet consisted of two `std.small` machines, six `std.large` machines, and six `mem.xlarge` machines. All machines were on-demand and remained running from the beginning of the simulation until the end. This meant the fleet cost $494.40 for every full day, regardless of how many queries arrived. The simulation also continued for 2.5 hours beyond the 30-day period while the remaining queries finished, adding another $51.50 in compute cost.

Across the month, the fleet used only 26.3% of its paid slot-time for query execution. Approximately $10,550, or 71% of the compute bill, was allocated to slots that were ready but not executing queries. Only about $4,331, or 29%, was allocated to query execution.

However, the problem was not simply that the entire fleet had too many machines. The `mem.xlarge` pool, which handled long-running `adhoc` and `report` queries, was frequently full during weekday afternoons. At the same time, the `std.large` pool had many unused slots. In the busiest periods, the `mem.xlarge` pool had as many as 2,989 queries waiting while `std.large` still had around 45 idle slots.

This difference also appeared in service-level performance. The most serious issue was `labs_ds / adhoc`, which missed its 30-minute service-level agreement 78.9% of the time. Its median waiting time was approximately 2.25 hours. Most misses across the workload were caused by waiting in the queue rather than by the time needed to execute the query.

The analysis also showed that the workload was not equally profitable across clients. `acme_retail` and `finpay` generated clear profits. `labs_ds` cost more to serve than the revenue it generated, even when only its direct execution cost was considered. `trial_pool` generated no revenue but still consumed a meaningful amount of compute capacity.

Finally, the query log showed that queries using the same SQL template tend to have similar runtimes. An online predictor based on completed queries from the same template achieved a median error of around 5-6%, compared with 11-46% when using only the benchmark runtime. This provides a useful opportunity for making better scheduling decisions.

Overall, the historical data points to a scheduler that needs to respond to workload demand, not simply keep a fixed fleet running. It also needs to consider how much work is waiting, how long each query is expected to take, which client submitted it, and how much time remains before its deadline.

## 2. Historical Compute Spending

The first thing I examined was how much the existing fleet cost to operate and whether that spending changed with the amount of incoming work.

The total compute bill for the month was $14,883.50, covering 10,115 instance-hours. The largest part of this cost came from `mem.xlarge`. The six `mem.xlarge` machines accounted for 4,335 instance-hours and cost $11,791.20, which represented 79.2% of the total compute bill.

The six `std.large` machines also accounted for 4,335 instance-hours, but their total cost was much lower at $2,947.80, or 19.8% of the bill. The two `std.small` machines accounted for 1,445 instance-hours and cost only $144.50, around 1% of the total.

All machines were billed at on-demand prices. There was no spot usage during the historical simulation.

I also checked whether the billing records agreed with the machine configuration and instance logs. Each bill row matched the instance-hours multiplied by the configured machine price. The total cost calculated from the instance log also matched the bill to the cent. This gives me confidence that the compute spending figures are consistent across the available data.

The fleet's combined running cost was $20.60 per hour. Since all machines stayed on throughout the month, this produced a daily cost of $494.40 for each full day.

What makes this important is that the amount of incoming work changed considerably, but the compute bill did not respond to those changes. Weekdays received approximately 30,000 to 34,000 queries per day, while weekends received only around 10,800 to 12,300. Despite this difference, the fleet continued to cost the same amount each day.

As a result, the cost per 1,000 queries was much higher at weekends. It was approximately $15.50 on weekdays and around $40-$46 on weekends.

The last day also shows why it is important to account for the simulation's actual ending time. The simulation continued for 2.5 hours, or 9,000 seconds, beyond the 30-day period while the remaining queries completed. The static fleet was still running during this time, adding $51.50 to the compute bill.

**What this tells me**

The historical fleet did not adjust its spending according to demand. It paid for the same capacity during busy weekday periods and quieter weekends.

This does not mean that every machine should be stopped whenever it is idle. Some capacity needs to remain available for queries with very short deadlines. However, keeping every machine running for the entire month was expensive because the fleet did not reduce its capacity when demand dropped.

A scheduler that can start and terminate machines according to expected workload has an opportunity to reduce this spending.

## 3. Machine Utilisation and Idle Spending

After understanding the total compute bill, I looked at how much of the paid machine capacity was actually used.

It is important to distinguish between machine time and slot-time. Machine time measures how long an instance is billed for. Slot-time accounts for the number of query slots available on that machine during the billed period. Since different machine types have different numbers of slots, slot-time gives a better view of how much capacity was purchased and used.

The fleet accumulated 10,115 billed machine-hours. When the number of slots on each machine is included, this becomes 106,930 slot-hours of paid capacity.

That slot-time can be divided into three parts.

First, 18.6 slot-hours were spent during startup. These slots were being billed but were not yet ready to execute queries.

Second, 28,125 slot-hours were spent executing queries.

The remaining 78,787 slot-hours were spent in the ready but idle state. These slots were available to accept work but were not executing queries.

These three figures add up to the total paid slot-time.

When utilisation is calculated using the total busy slot-time divided by the total paid slot-time, the fleet's weighted utilisation is 26.3%.

The individual machine types had different utilisation levels. `std.small` had only 0.9% utilisation, `std.large` had 15.2%, and `mem.xlarge` had 32.9%.

The difference between these machine types matters. A simple average of the utilisation values in the CSV gives 20.7%, but that is not the correct fleet-wide figure. It gives the same weight to a machine with two slots as it does to a machine with sixteen slots. The weighted calculation accounts for the capacity of each machine.

I also checked the busy slot-time recorded in the instance log against the sum of query execution time in the query log. The difference was less than 0.2 seconds per machine, which provides a useful cross-check between the two datasets.

To understand how much of the compute bill went towards execution and how much went towards idle capacity, I allocated each machine's cost across its paid slot-seconds.

Across the entire fleet, approximately $2.86 was allocated to startup, $4,330.98 to query execution, and $10,549.65 to ready but idle capacity. This means that around 70.9% of the compute bill was allocated to ready idle slots, while 29.1% was allocated to execution.

The `mem.xlarge` machines accounted for most of both execution spending and idle spending. Their execution cost was approximately $3,882.54, while $7,906.08 was allocated to ready idle capacity. The `std.large` machines had approximately $447.18 in execution cost and $2,500.35 in idle cost. The `std.small` machines contributed very little execution cost, with most of their allocated cost going towards idle capacity.

**What this tells me**

The fleet paid for much more capacity than it used for query execution. However, the idle-cost calculation is an accounting allocation, not a direct calculation of how much money could have been saved.

For example, if one slot out of sixteen is busy, the other fifteen slots are counted as idle. But the machine cannot be rented as fifteen separate slots. It must be rented as a complete machine.

Also, some spare capacity is necessary. Queries such as `lookup` and `dashboard` have very short deadlines, so the scheduler needs some machines ready before those queries arrive. Spare capacity can also help absorb sudden increases in traffic.

Even with these considerations, the historical time patterns show that a large amount of idle capacity accumulated during nights and weekends, when query arrivals were much lower.

The main opportunity is therefore not to remove all spare capacity. It is to keep enough capacity for urgent work while reducing machines that are not needed during quieter periods.

## 4. Query Workload Composition

Next, I examined the query log to understand who was submitting queries and how much work each client generated.

There were 785,567 queries in total. Every query completed successfully, and there were no dropped or rejected queries. Each query also completed in one attempt.

At first, the query counts suggest that `acme_retail` and `finpay` generated most of the workload. `acme_retail` submitted 339,965 queries, representing 43.3% of all queries. `finpay` submitted 330,358, representing 42.1%.

`labs_ds` submitted 65,237 queries, or 8.3% of the total, while `trial_pool` submitted 50,007, or 6.4%.

However, query counts do not tell the full story. The amount of compute work required by each query type is very different.

For example, `finpay` submitted 300,128 `lookup` queries. These queries made up a large part of the total query count but used only a relatively small amount of slot-time. Its other 30,230 queries were `filtered_agg`.

`acme_retail` mainly submitted `dashboard` queries, with 299,513 in total, followed by 39,929 `filtered_agg` queries and 523 reports.

The workload from `labs_ds` was very different. It submitted 59,684 `adhoc` queries and 5,553 reports. Although it represented only 8.3% of all queries, it consumed 18,535 slot-hours of work.

`trial_pool` submitted 29,990 dashboard queries and 20,017 `adhoc` queries, using 3,452 slot-hours.

When I grouped the workload by query count, `lookup` and `dashboard` represented around 80% of all queries. But when I grouped it by compute work, the picture changed completely. `adhoc` accounted for 46.6% of the work, `report` accounted for 34.7%, and `filtered_agg` accounted for 17.7%. Together, these three query types represented approximately 99% of the workload by slot-time.

**What this tells me**

A scheduler should not make capacity decisions based only on how many queries are arriving.

A large number of short queries may require less capacity than a smaller number of long-running queries. If the scheduler only counts arrivals, it may underestimate the resources needed for `adhoc`, `report`, and `filtered_agg` workloads.

For this reason, the amount of estimated work, measured in slot-seconds, is a more useful signal for deciding when to add or remove capacity.

## 5. Traffic Patterns

After looking at the types of queries, I examined when they arrived. This helped explain why the fleet could have a large amount of idle capacity overall and still experience serious queues during particular periods.

The historical data shows a clear difference between weekdays and weekends. Weekdays averaged approximately 31,510 queries per day, while weekends averaged 11,544. There was no clear upward or downward trend across the four weeks.

Within weekdays, arrivals also changed throughout the day. The quietest period was around 01:00-03:00, when there were approximately 260 arrivals per hour. The busiest periods were around 10:00 and 13:00-14:00, reaching as many as 2,740 arrivals per hour. That is roughly ten times the quiet-period volume.

Different query types followed different patterns.

`lookup` queries from `finpay` were distributed throughout the day and continued to arrive during weekends. This workload was less concentrated in office hours.

`dashboard` and `filtered_agg` queries were mainly submitted during office hours, from approximately 07:00 to 17:00.

`adhoc` queries increased from around 11:00, reached their highest levels around 13:00-14:00, and remained high until approximately 19:00.

Reports also followed different patterns depending on the client. `acme_retail` submitted around 17 reports per day at approximately 02:00. In contrast, `labs_ds` submitted reports in batches between 12:00 and 13:59 on weekdays.

The data also shows that traffic was not always smooth. Looking at arrivals in one-minute intervals, the median was 11 queries, the 99th percentile was 65, and the maximum was 153 queries in a minute.

During the busiest 1% of minutes, `acme_retail / dashboard` represented 69% of arrivals, compared with 38% across the full workload. This means short bursts were particularly concentrated in dashboard traffic.

Interestingly, the largest burst occurred on day 14 around 09:00, with approximately 130-144 dashboard queries arriving per minute. This burst did not cause any misses.

That result is important because it shows that a high number of arrivals does not automatically lead to poor performance. The amount of work already waiting, the available capacity, and the type of queries all affect whether a burst becomes a problem.

A different situation occurred on day 24, a Thursday. At around 10:00, `acme_retail` submitted 581 `filtered_agg` queries in one hour. A typical hour had around 183, so this was approximately 3.2 times the usual volume. The client also submitted around twice its usual number of dashboard queries.

During this period, the `std.large` pool became full. Dashboard queries then waited behind `filtered_agg` jobs that could take around 240 seconds to execute. As a result, 3,242 of the month's 3,243 dashboard misses occurred on that single day.

The `adhoc` workload provides another example of why current arrivals alone are not enough to predict waiting time. During hours with 50-100 `adhoc` arrivals, the median wait ranged from zero to approximately 4.8 hours. The difference depended on how much work had already accumulated in the queue.

Looking at the fleet-wide queue over time, the queue was empty in 73% of the five-minute snapshots. However, on almost every weekday, a queue began building around 12:30. It generally remained for 8-14 hours, reached its highest levels in the late afternoon or evening, and drained by approximately 02:00.

The 95th percentile of the queue size was 1,353 queries, and the maximum was 2,989. Weekends did not show the same queue pattern.

**What this tells me**

The workload has predictable daily and weekly patterns, but it also includes sudden bursts that cannot be handled by looking only at average arrivals.

A scheduler can use the recurring weekday afternoon pattern to prepare capacity before demand increases. It should also monitor the amount of work already waiting, because two periods with similar arrival rates can have very different waiting times.

The day-14 and day-24 events show why capacity planning must consider query type and existing backlog, not just the number of incoming queries.

## 6. Service-Level Agreement Performance

The next part of the analysis was to understand how often queries missed their service-level agreements and why those misses happened.

Out of 785,567 completed queries, 57,568 missed their service-level agreements. This represents a miss rate of 7.33%.

The misses were not distributed evenly across clients or query types.

For `acme_retail`, dashboard queries had a 10-second service-level agreement. There were 3,179 misses, giving a miss rate of 1.06% and $317.90 in uncapped penalties.

Its `filtered_agg` queries had a 15-minute service-level agreement. There were 346 misses, or 0.87%, resulting in $173 in uncapped penalties. Its reports had a six-hour agreement and recorded no misses.

For `finpay`, the `lookup` queries had a 1.5-second agreement. Only 12 queries missed it, representing 0.004% of lookup traffic and $1.20 in uncapped penalties. Its `filtered_agg` queries had a 10-minute agreement, with 116 misses and $58 in uncapped penalties.

The largest problem was `labs_ds / adhoc`. These queries had a 30-minute service-level agreement, but 47,084 of them missed it. That represents a miss rate of 78.9% and $9,416.80 in uncapped penalties. The `labs_ds` reports had a 12-hour agreement and recorded no misses.

`trial_pool / adhoc` also had a high miss rate. Its two-hour agreement was missed by 6,767 queries, or 33.8%. However, this client had no revenue or penalty cost in the historical profit calculation. Its dashboard queries had a one-minute agreement and recorded 64 misses, or 0.21%.

The reason for these misses is just as important as the total number.

The analysis found that 95.2% of misses happened because the query waited too long before execution. Another 2.5% happened because the query's execution time alone exceeded its agreement. The remaining 2.4% involved both waiting and execution time.

Most waiting-related misses came from `adhoc` queries, with the day-24 dashboard event also contributing significantly.

This means that the main problem was not simply that the machines were too slow. Many queries were waiting too long to start.

The penalty calculation also needs to account for each client's cap. Before applying caps, the total penalties were $9,966.90. However, `labs_ds` had a penalty cap of $900. Its uncapped penalties reached $9,416.80, but only $900 was owed under the contract.

`acme_retail` had $490.90 in uncapped penalties against a $3,600 cap, so the full amount was owed. `finpay` had $59.20 against a $2,400 cap, so it also owed the full amount. `trial_pool` had no penalties.

After applying the caps, total penalties were $1,450.10. There were no drop penalties because no queries were dropped or rejected.

The analysis also examined how much of each client's service-level agreement was used. Most client and query-type combinations finished at the median within 20-35% of their allowed time. Reports generally finished at around 24-28% of their agreements, even after waiting.

`labs_ds / adhoc` was the clear exception. Its median completion time was approximately 4.8 times its 30-minute agreement.

**What this tells me**

The scheduler needs to consider the full time a query spends in the system. A query's deadline includes both its waiting time and its execution time.

The historical results show that reducing execution time alone would not solve most of the missed agreements. The scheduler also needs to control queue growth, avoid allowing urgent work to wait behind long-running jobs, and consider how much time remains before each query's deadline.

## 7. Client Profitability

After examining query volume, compute usage, and service-level penalties, I looked at how profitable each client was.

There are two cost figures worth distinguishing.

The first is direct execution cost. This charges a client only for the slot-time its queries actually used, based on the average price per slot-second of the pool. It does not include the cost of idle capacity, so it is a lower-bound estimate.

The second is fully allocated cost. This distributes each pool's total compute cost across clients according to their busy slot-seconds. The fully allocated costs add up to the total compute bill.

Using these calculations, `acme_retail` generated $9,000 in revenue. Its direct execution cost was approximately $436, while its fully allocated compute cost was approximately $2,345. It had $490.90 in capped service-level penalties and no drop penalties. After fully allocated compute cost and penalties, its profit was approximately $6,164. Using direct cost instead, its profit was approximately $8,073.

`finpay` generated $6,000 in revenue. Its direct cost was approximately $159, and its fully allocated cost was approximately $1,186. It had $59.20 in capped penalties and no drop penalties. Its fully allocated profit was approximately $4,755, while its direct-cost profit was approximately $5,782.

`labs_ds` generated $3,000 in revenue, but its workload was much more expensive to execute. Its direct cost alone was approximately $3,151. Its fully allocated cost was approximately $9,569. It also reached its $900 penalty cap. This resulted in a fully allocated loss of approximately $7,469. Even when idle capacity is excluded and only direct execution cost is considered, it still lost approximately $1,051.

`trial_pool` generated no revenue. Its direct cost was approximately $585, while its fully allocated cost was approximately $1,783. It had no penalties, but its fully allocated result was still a loss of approximately $1,783.

Across all clients, revenue was $18,000, fully allocated compute cost was approximately $14,884, and capped penalties were $1,450.10. This produced the historical profit of approximately $1,666.

The amount of compute work used by each client also helps explain these results. `labs_ds` used 81% of the busy slot-time in the `mem.xlarge` pool. `trial_pool` used 15% of the busiest pool. `acme_retail` used 64% of the `std.large` work, while `finpay` used 35% of it. `finpay` also used all of the `std.small` workload.

**What this tells me**

The clients do not contribute equally to the business result.

`acme_retail` and `finpay` are profitable under both cost calculations. `labs_ds` is different because its workload is expensive enough that it loses money even before idle capacity is included. `trial_pool` also consumes resources without generating revenue.

This matters for scheduling because the scheduler is not only deciding how to execute queries. It is also deciding how to use limited and costly capacity across clients with different commercial value and service-level agreements.

The historical data supports treating paying clients differently from `trial_pool` when capacity is limited. It also raises a separate commercial question about whether the `labs_ds` contract adequately covers the cost of the workload it requires.

## 8. Machine-Type Bottlenecks

The overall fleet utilisation figure might suggest that the system had more than enough capacity. However, looking at the machine pools separately shows why that conclusion would be misleading.

The `mem.xlarge` pool handled `adhoc` and `report` queries. It was full in 26.5% of the five-minute snapshots. In 99.7% of snapshots where more than 1,000 queries were waiting, this pool was also full.

When the pool had even one free slot, its median queue was zero. This indicates that the queue was closely related to the availability of execution slots in this pool.

The `std.large` pool handled `dashboard` and `filtered_agg` queries. It was full in only 0.2% of snapshots, mainly during the day-24 surge between approximately 09:50 and 11:10.

The `std.small` pool handled `lookup` queries. It was never full and had only 0.9% utilisation.

The historical data also showed that no more than one pool was full at the same time. At the five worst snapshots on day 28 around 20:30, all 96 `mem.xlarge` slots were busy, while `std.large` still had between 43 and 46 of its 48 slots free.

The queue composition explains which workloads were contributing to this pressure. `labs_ds / adhoc` accounted for 79% of accumulated waiting, `trial_pool / adhoc` accounted for 16%, and `labs_ds / report` accounted for the remaining 5%.

I also reconstructed the queue from the query log and compared it with the `pending_queries` values in the fleet time series. The reconstructed values matched in 8,669 of 8,670 snapshots. The remaining snapshot differed by one query.

**What this tells me**

The main congestion problem was concentrated in the `mem.xlarge` pool. The fleet did not have every machine type running out of capacity at the same time.

The historical routing policy assigned each query type to its cheapest on-demand machine and maintained one FIFO queue per type. It did not allow queries to overflow into another machine pool.

As a result, `adhoc` queries could remain waiting for `mem.xlarge` capacity while `std.large` had many free slots.

The benchmark runtime for `adhoc` on `std.large` was 1,125 seconds. That is within the 30-minute `labs_ds` agreement for runtime multipliers up to 1.6, assuming the query can start without excessive waiting.

This suggests that a scheduler could reduce congestion by allowing some queries to use another machine type when their predicted runtime still fits their deadline.

## 9. Runtime and Cost Trade-offs

The next question was whether the cheapest machine for a query type was also the fastest, and whether choosing a faster machine would always be worth the extra cost.

To compare machine types, I calculated the benchmark cost per query using the runtime, hourly machine price, and number of slots. This estimates the cost of the slot-time used by a query. It is a lower-bound cost when a machine is not fully occupied.

The results show that the cheapest and fastest machine are not always the same.

For `lookup`, `std.small` is the cheapest option, while `mem.xlarge` is the fastest. Choosing the fastest option costs approximately 2.27 times as much as the cheapest option.

For `dashboard`, `std.large` is the cheapest and `mem.xlarge` is the fastest. The fastest option costs approximately 1.60 times as much.

For `filtered_agg`, `std.large` is also the cheapest, while `mem.xlarge` is the fastest. However, the cost difference is smaller, with the fastest option costing approximately 1.08 times as much.

For `adhoc` and `report`, `mem.xlarge` is both the cheapest and fastest option under the benchmark comparison.

The configured on-demand price per slot-hour is $0.05 for `std.small`, $0.085 for `std.large`, and $0.17 for `mem.xlarge`.

A `mem.xlarge` slot therefore costs twice as much as a `std.large` slot. It is only cheaper when it runs a query type more than twice as fast. That is the case for `adhoc`, which runs around 2.5 times faster, and `report`, which runs around three times faster.

This explains why sending every query to the fastest machine would not be a good cost strategy. For short queries, the increase in machine price can be greater than the benefit of faster execution.

Spot pricing is approximately 65-68% cheaper across all machine types. This changes the absolute cost of running queries, but it does not change the relative ranking of machine types in the benchmark comparison.

The historical routing policy followed the cheapest-on-demand mapping exactly. The median runtimes observed in the query log were consistent with the benchmark runtime of the assigned machine. However, the 90th percentile runtimes were higher than the benchmarks, ranging from around 25% above benchmark for `lookup` to around 150% above benchmark for `adhoc`.

I also examined whether each query type could meet its service-level agreement on different machines if it started immediately.

Some combinations were not suitable even before waiting time was considered. For example, `report` on `std.small` could not meet the six-hour `acme_retail` agreement at the benchmark runtime, because the largest runtime multiplier that would fit was only 0.48.

Similarly, `adhoc` on `std.small` could not meet the 30-minute `labs_ds` agreement unless its runtime multiplier was 0.6 or lower. `filtered_agg` on `std.small` could not meet the 10-minute `finpay` agreement unless its multiplier was 0.92 or lower.

For `labs_ds / adhoc`, `std.large` could meet the 30-minute agreement for runtime multipliers up to 1.6, while `mem.xlarge` could handle multipliers up to 4, assuming the query started immediately.

**What this tells me**

The scheduler should not always choose the cheapest machine or always choose the fastest machine.

It needs to consider the expected runtime of the individual query, its deadline, and how much waiting time it is likely to experience. If a query can meet its deadline on a cheaper machine, there is no reason to pay for a more expensive one. But if the cheaper pool is congested or the query is too large, using a faster machine may be necessary.

## 10. Query-Template Opportunities

The query log contains another useful piece of information: many queries repeat the same SQL template.

The analysis identified 11,537 distinct templates. Each template belonged to exactly one client and one query type.

The amount of repetition varied. The median number of uses per template was 26. `acme_retail` dashboard templates were repeated more often, with a median of 173 uses. `adhoc` templates were less repetitive, with a median of nine uses for `labs_ds` and six for `trial_pool`.

I then looked at how much runtime varied across queries.

Across an entire query type, runtime multiplier variability was relatively high for some types. The coefficient of variation ranged from 0.17 for `lookup` to 0.81 for `adhoc`.

However, when queries were grouped by template, the variability was much lower. Within a template, the coefficient of variation was approximately 0.08 across every query type.

This means that queries belonging to the same template tend to have more consistent runtimes than queries grouped only by their general type.

To test whether this could help with runtime prediction, I compared two approaches.

The first approach used only the benchmark runtime. The second used the mean runtime multiplier from queries with the same template that had already completed before the new query arrived.

The template-based predictor had a median error of 5.6% for `lookup`, 5.4% for `dashboard`, 5.5% for `filtered_agg`, 5.7% for `adhoc`, and 5.6% for `report`.

Using only the benchmark runtime produced higher median errors. The errors were 11.2% for `lookup`, 14.1% for `dashboard`, 36.7% for `filtered_agg`, 46.5% for `adhoc`, and 19.1% for `report`.

The predictor had historical information available for 98.5% of queries overall. Availability was slightly lower for `adhoc` and `report`, at around 93-94%.

This approach is possible without using hidden information. The engine calculates runtime using the configured runtime matrix multiplied by a query-specific runtime multiplier. The scheduler can access the runtime matrix through `ctx.config`. When a query completes, `on_complete` provides the actual runtime and machine information. The scheduler can use these values to calculate the multiplier and update a running mean for that template.

**What this tells me**

The template history provides a useful way to estimate query runtimes more accurately than the benchmark alone.

A scheduler can use these estimates when deciding where to run a query, whether it can meet its deadline, and how much work is waiting in each machine pool.

For a template that has not been seen before, the benchmark runtime can still be used as a fallback. As more queries complete, the scheduler can update its estimate.

## 11. Machine Lifecycle and Startup Considerations

A scheduler that starts and stops machines also needs to consider how long machines take to become ready.

The historical instance log shows that startup time differs significantly by machine type.

A `std.small` machine takes 135 seconds to start, a `std.large` takes 240 seconds, and a `mem.xlarge` takes 570 seconds. During startup, the machine is billed but cannot execute queries.

At on-demand prices, one launch costs approximately $0.004 for `std.small`, $0.045 for `std.large`, and $0.43 for `mem.xlarge` during startup.

Each historical machine started only once, so the total startup cost for the month was just $2.86. A scheduler that scales machines more frequently will pay startup cost each time it launches a machine.

The query log also shows why startup time matters for scheduling. A query that triggers a new machine cannot use that machine immediately. It must wait until the machine has completed startup.

The historical machines were never terminated by the scheduler. They all remained active until the end of the simulation, including during the 2.5-hour tail after the 30-day period.

The configuration also defines a minimum billing duration of 60 seconds. This means that even a machine that runs for a very short time is billed for at least one minute.

**What this tells me**

For queries with very short service-level agreements, such as `lookup` and `dashboard`, starting a machine after the query arrives may be too late. The scheduler needs to keep some capacity ready before those queries arrive.

For longer agreements, such as the 30-minute to 12-hour workloads, startup time is less significant compared with the total allowed time. Those pools can be scaled more reactively, although it is still useful to prepare capacity before predictable increases in demand.

The weekday rise around 12:00-13:00 provides an opportunity to start additional machines before the afternoon workload builds up.

## 12. Spot Market Observations

There was no spot usage during the historical month. All machines were on-demand, and every query completed in one attempt. Therefore, the historical data does not provide direct evidence of how the workload behaves when machines are interrupted.

The spot-related findings in this section come from the configuration and the engine implementation, not from observed spot events in the historical logs.

The configured spot prices are $0.035 per hour for `std.small`, $0.22 per hour for `std.large`, and $0.95 per hour for `mem.xlarge`. These prices are approximately 65-68% lower than their corresponding on-demand prices.

However, spot machines can be interrupted. The configured mean time between interruptions is 50 hours for `std.small`, 25 hours for `std.large`, and 12.5 hours for `mem.xlarge`. The configuration also provides a 120-second interruption notice.

According to the engine implementation, interruptions are random and follow an exponential distribution. When an interruption happens, a running query restarts from the beginning.

Using the benchmark runtimes and configured interruption rates, the estimated probability that a query is interrupted while running is very small for `lookup` and `dashboard`. It is approximately 0.3% for `filtered_agg`, around 1-1.7% for `adhoc`, and higher for `report`.

For reports, the estimated interruption probability is approximately 11% on `mem.xlarge`, 16% on `std.large`, and 22% on `std.small`.

The `scenarios/dev_changes` configuration also shows that spot market conditions can become less favourable during the simulation. For example, the spot price and interruption rate for `mem.xlarge` can increase during the month.

**What this tells me**

Spot capacity can reduce compute spending, but the discount needs to be considered alongside the risk of interruption and the time required to restart a query.

Short queries and queries with enough deadline margin may be suitable for spot machines. Longer queries with tight deadlines need more protection because an interruption can cause them to lose their previous execution time.

The scheduler should read current spot prices and interruption conditions from `ctx.config` rather than assuming that spot will always be cheaper by the same amount.

## 13. Scheduler Design Implications

The historical analysis shows several problems that the scheduler needs to address. These are recommendations based on the findings, rather than facts directly observed in the historical simulation.

**1. Scale according to the amount of work in each pool.**

The scheduler should estimate queued and running work in slot-seconds rather than relying only on query counts or total fleet utilisation. The `mem.xlarge` pool was overloaded while other pools still had spare capacity. Scaling decisions should therefore be made separately for each pool.

Long-query capacity should increase before the weekday afternoon workload arrives and decrease during quieter nights and weekends. Idle machines should also be released when they are no longer needed, including at the end of the simulation.

**2. Keep a small amount of capacity ready for queries with short deadlines.**

`lookup` and `dashboard` queries have deadlines measured in seconds. Since machines take time to start, the scheduler should keep enough warm capacity available to handle this work without waiting for a new machine to become ready.

Longer-running workloads can be scaled more reactively because their service-level agreements allow more time for startup.

**3. Avoid making short-deadline queries wait behind long-running queries.**

The day-24 event showed how a shared FIFO queue can cause problems. Dashboard queries with a three-second benchmark runtime waited behind `filtered_agg` jobs that took around 240 seconds.

The scheduler should consider query deadlines and expected execution times when deciding which query to run next. Short-deadline queries should not be delayed unnecessarily by much longer work.

**4. Make machine decisions for individual queries, not only query types.**

Queries of the same type can have different runtime requirements. The template-based predictor provides a way to estimate the runtime of an individual query.

The scheduler should choose the cheapest machine on which the query is expected to finish before its deadline, taking both waiting time and execution time into account.

It should also allow queries to use another machine type when their usual pool is congested. For example, an `adhoc` query could run on `std.large` if the `mem.xlarge` pool is full and its predicted runtime still fits the deadline.

**5. Consider the penalty and remaining time when prioritising work.**

Not every missed query has the same financial impact. The scheduler should consider the penalty associated with a query and how much time remains before its deadline.

When capacity is limited, paying clients should generally take priority over `trial_pool`. Work with longer agreements, such as reports with six- to twelve-hour deadlines, can also yield to more urgent work with a 30-minute deadline when necessary.

**6. Use spot capacity when there is enough time to handle interruptions.**

Spot machines offer a significant price discount, but interrupted queries restart from the beginning.

The scheduler can consider spot for short queries, reports with sufficient deadline margin, and `trial_pool` work. It should also respond to interruption notices by draining or replacing affected capacity.

Long-running queries with tight deadlines, such as `labs_ds / adhoc`, need more protection. They can remain on on-demand machines or use spot only when the deadline calculation shows enough room for the risk.

**7. Learn runtime estimates from completed queries.**

The historical analysis showed that queries using the same template have relatively consistent runtimes.

The scheduler can calculate a runtime multiplier from completed queries and maintain a running estimate for each template. For templates without history, it can fall back to the benchmark runtime.

These estimates should continue to update as more queries complete. They should also be reconsidered when configuration changes affect machine runtimes.

**8. Track client penalty caps.**

The scheduler can use `ctx.client_stats()` to check how much of each client's penalty cap has already been reached.

Once a client reaches its cap, additional misses no longer increase the penalty cost in the profit calculation. This can affect scheduling decisions, but it should be a deliberate choice because prioritising profit after a cap has been reached can reduce service quality for that client.

**9. Read changing settings from the configuration.**

The scheduler should use `ctx.config` for machine prices, runtime information, spot conditions, and client contracts. It should also handle `on_config_change`.

The hidden scenarios may change prices, interruption rates, machine capacity, runtimes, clients, and service-level agreements. The scheduler should respond to those changes rather than relying on values observed in the historical month.

**10. Consider whether the client contracts match the cost of the workload.**

The `labs_ds` workload loses money even when only direct execution cost is counted. This suggests that its current contract does not cover the cost of the workload at the required service level.

Possible commercial changes include increasing the price, relaxing the `adhoc` service-level agreement, or offering a lower-cost best-effort or spot-based tier.

Another option raised by the historical analysis is to limit `trial_pool` to spare or spot capacity. This is a possible policy recommendation, rather than a requirement from the historical data. Under the final scheduler, `trial_pool` already runs approximately 99% on spot. The measured results are documented in section 9 of `NOTES.md`.

## 14. Limitations and Assumptions

There are several limitations to keep in mind when interpreting these findings.

First, the historical analysis covers one month under one scheduling policy. That policy used a fixed fleet, cheapest-on-demand routing, and FIFO queues. The data shows what happened under those conditions, but it cannot directly show what would have happened under a different scheduler. Statements about alternative policies are therefore interpretations based on the observed workload and machine behaviour.

Second, no spot machines were used during the historical month. The spot-related findings come from the configuration and engine code, not from observed interruptions or restarts. Actual spot performance under a different scheduling policy may vary.

Third, the idle-cost and client-cost calculations are accounting allocations. Idle cost is distributed across slot-time, while client cost is allocated according to busy slot-time. These figures help explain how the bill was used, but they are not direct measures of how much money could be saved by removing a particular machine or changing a client's allocation.

Fourth, the fleet time series uses five-minute snapshots. Events shorter than five minutes may not appear in the snapshots, and measured episode durations are therefore accurate only to within approximately five minutes.

Fifth, query counts are not a reliable measure of compute demand on their own. A single report can use as much slot-time as approximately 20,000 lookup queries. This is why the analysis considers slot-time as well as query volume.

Sixth, the weekday and time-of-day labels rely on the README's definition of `t = 0` as Monday at 00:00 US/Eastern. Daylight-saving time is not modelled in these calculations.

Seventh, the simulation continued for 2.5 hours beyond the 30-day period. Revenue and penalty caps use the 30-day duration, as defined in `engine.py`, while compute spending includes the additional time required to finish the remaining queries.

Eighth, the template predictor was evaluated using queries from this historical month. Its accuracy may be different in hidden scenarios, particularly if the workload mix or runtime matrix changes.

Ninth, the percentile calculations use pandas interpolation. The engine's `summary.json` uses nearest-rank percentiles, so small differences between the two sets of results are expected.

Finally, the runtime multipliers were calculated from `run_s / benchmark`. This is exact given the engine's runtime formula. However, the scheduler can only calculate the actual multiplier after a query has completed, so it must use previously completed queries to estimate the runtime of new ones.

## Conclusion

The historical analysis shows that the main challenge was not simply a lack of machines. The fleet had a large amount of paid idle capacity, but that capacity was not always available in the machine pool where the workload needed it.

The fixed fleet continued to cost the same amount during busy weekdays and quiet weekends. Meanwhile, long-running queries accumulated in the `mem.xlarge` queue, causing many `labs_ds / adhoc` queries to miss their service-level agreements. Some of this pressure could have been handled differently because other machine pools still had spare capacity.

The workload also showed that query counts alone are not enough to make scheduling decisions. Different query types require very different amounts of execution time, and repeated SQL templates provide useful information for estimating that time.

These findings lead to the main direction for the scheduler: manage capacity according to estimated work, keep enough machines ready for urgent queries, route queries based on their predicted runtime and deadlines, and use the available machine and client configuration when making cost decisions.

The goal is to reduce unnecessary compute spending without creating avoidable queues or missing service-level agreements.