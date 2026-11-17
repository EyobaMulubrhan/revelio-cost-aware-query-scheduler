# Experiment results behind the final policy choices

## Introduction

This document explains the experiments I ran while developing the final scheduler and how their results influenced the policy decisions.

Every figure in this document comes from a full simulation run, lasting 30 days unless stated otherwise. The runs used `PYTHONHASHSEED=0` and the project's comparison harness. Each run's full output, including profit, compute costs, query counts, assignment errors, and invariant results, is recorded in the development experiment log. That log is not included in the submission.

The experiments in sections 1 and 2 changed individual constants in `my_scheduler.py` or `runtime_predictor.py`. These tests can be repeated by changing the relevant constant and running the simulation again.

Throughout this document, profit means:

`revenue - compute - miss penalties (after caps) - drop penalties`

I also needed a consistent way to decide whether an experiment was genuinely better. I treated a change as a gain only when all three conditions were met:

- The combined profit improvement across `dev_steady` and `dev_changes` was greater than the noise band.
- Neither development month lost more than its own noise band.
- The change did not cause new drops in the stress scenarios.

If a change did not meet these conditions, I classified it as within noise or worse. This distinction is important because a small difference in profit does not necessarily mean that one policy is better. Simulation results can vary between runs, so I needed to separate meaningful improvements from ordinary variation.

## 1. Measuring the Noise Band

Before comparing policy changes, I first needed to understand how much profit could change even when the scheduler's behaviour was not expected to change meaningfully.

To measure this, I adjusted the idle-release timeout for `mem.xlarge` by small amounts. I tested four variations: minus two seconds, minus one second, plus one second, and plus two seconds. I also included the unmodified scheduler as the reference.

The purpose was to measure how much run-to-run variation appeared when changing a setting that should have little effect on the overall policy.

For `dev_steady`, the five profits were:

- Minus two seconds: $14,666.53
- Minus one second: $14,666.42
- Unmodified: $14,666.36
- Plus one second: $14,615.22
- Plus two seconds: $14,615.04

The difference between the highest and lowest results was $51.49. I used this as the noise band for `dev_steady`.

For `dev_changes`, the corresponding profits were:

- Minus two seconds: $13,621.56
- Minus one second: $13,621.45
- Unmodified: $13,617.73
- Plus one second: $13,531.66
- Plus two seconds: $13,550.56

The difference between the highest and lowest results was $89.90. This became the noise band for `dev_changes`.

Together, the two bands add up to $141.39.

This gave me a more reliable way to judge later experiments. A change that improves profit by a small amount may simply be within normal variation. I therefore avoided treating every positive difference as an improvement.

The policy experiments in section 2 were originally evaluated using an earlier combined noise band of $95.34. In this document, I restate their results against the final combined band of $141.39.

## 2. Testing Policy Settings One at a Time

After measuring the noise, I tested individual scheduler settings on both development scenarios. The goal was to understand which settings had a measurable effect and whether any improvement held across different workload conditions.

I divided the experiments into two rounds.

In round 1, I changed one setting at a time from the original defaults. The reference profits were $14,551.99 for `dev_steady` and $13,512.32 for `dev_changes`.

In round 2, I tested the later settings again, but this time on top of the round-1 winner: the warm-capacity setting of `(1.28, 0)`. The reference profits for this round were $14,666.36 on `dev_steady` and $13,617.73 on `dev_changes`.

The results below describe what happened when each setting was changed.

### Idle-release timeout

The idle-release setting controls how long a machine remains available before the scheduler releases it.

I tested increasing `IDLE_STARTUP_FACTOR` from its default value of 1 to 2 and then to 4.

Changing it from 1 to 2 reduced profit by $86.70 on `dev_steady` and $122.93 on `dev_changes`.

Increasing it further to 4 reduced profit by $234.95 and $185.47, respectively.

Both changes performed worse than the default. Increasing the idle-release factor did not provide a useful improvement, so I kept `IDLE_STARTUP_FACTOR` at 1.

### Warm capacity

Warm capacity determines how much ready capacity the scheduler keeps available rather than waiting until more work arrives before launching machines.

This was the most important positive result from the individual policy experiments.

The original values were `Z_WARM = 2.33` and `B_WARM = 1`. I tested reducing them to `Z_WARM = 1.28` and `B_WARM = 0`.

This increased profit by $114.37 on `dev_steady` and $105.41 on `dev_changes`.

The combined improvement was $219.78, which was greater than the final combined noise band of $141.39. Neither development month lost profit, and the change also performed better on the stress scenarios.

I also tested increasing the warm-capacity values to `(3.09, 2)`. That reduced profit by $172.39 on `dev_steady` and $185.21 on `dev_changes`.

The results showed that keeping more warm capacity was not necessarily better. The lower setting provided a better balance for these workloads.

I therefore adopted `(1.28, 0)` as the final warm-capacity setting.

I also compared this setting against the previous default on the stress scenarios.

On `test_burst`, profit increased from $14,532.47 to $14,622.37, an improvement of $89.90.

On `test_spot_storm`, it increased from $8,550.18 to $8,976.11, an improvement of $425.93.

On `test_events`, it increased from $17,542.75 to $17,633.37, an improvement of $90.62.

On the three-day `test_overload_short` scenario, profit improved from -$3,477.21 to -$3,350.38, a difference of $126.83.

On `test_outage`, it increased from $13,942.10 to $14,034.02, an improvement of $91.92.

On `test_no_traffic_gap`, it increased from $14,850.83 to $14,902.02, an improvement of $51.19.

The largest difference appeared on `test_overload`. Profit improved from -$35,468.46 to -$25,214.55, an increase of $10,253.91.

The warm-capacity change therefore improved both development scenarios and every listed stress scenario. This is why it became part of the final policy.

### Clearing-horizon cap

The clearing horizon controls how far ahead the scheduler considers when estimating whether queued work can be completed.

The default `H_MAX` was four hours. I tested reducing it to one hour and increasing it to eight hours.

In round 1, reducing the horizon to one hour decreased `dev_steady` profit by $20.51 but increased `dev_changes` profit by $10.80.

In round 2, the same change increased profit by $19.48 on `dev_steady` and $8.73 on `dev_changes`.

These differences were within the noise range.

Increasing the horizon to eight hours also did not provide a clear improvement. In round 1, profit decreased by $17.75 on `dev_steady` and $16.73 on `dev_changes`. In round 2, it decreased by $22.00 and $4.74.

Since neither alternative produced a meaningful gain, I kept the four-hour default.

### Arrival-rate time constant

The arrival-rate time constant controls how quickly the scheduler's estimate of incoming traffic responds to changes.

The default `RATE_TAU_S` was 600 seconds. I tested a shorter value of 120 seconds and a longer value of 1,800 seconds.

Reducing it to 120 seconds performed worse in both rounds.

In round 1, profit decreased by $146.97 on `dev_steady` and $163.52 on `dev_changes`. In round 2, it decreased by another $76.54 and $110.28 relative to that round's reference.

Increasing it to 1,800 seconds produced mixed and relatively small results. In round 1, `dev_steady` improved by $33.32 while `dev_changes` decreased by $2.19. In round 2, profit decreased by $13.80 and $73.44.

These results did not establish a meaningful improvement. I kept the original 600-second setting.

### Upper-runtime prediction value

The runtime predictor uses an upper-runtime estimate to make scheduling decisions more cautiously when a query may take longer than its average prediction.

The default value of `RuntimePredictor.Z` was 1.65. I tested reducing it to 1.28 and increasing it to 2.33.

Reducing it to 1.28 increased `dev_steady` profit by $1.93 in round 1 and $9.80 in round 2. However, it reduced `dev_changes` profit by $33.23 and $62.32 in the respective rounds.

Increasing it to 2.33 reduced profit on both development scenarios in both rounds. The reductions ranged from $1.68 to $30.58 in round 1 and from $24.39 to $25.71 in round 2.

None of these differences established a gain beyond the noise range. I therefore kept the default value of 1.65.

### Tier-2 ordering

Tier 2 is one of the scheduler's query-priority groups. I tested whether its ordering should continue to use least remaining slack or instead use penalty divided by the maximum of slack and one second.

The alternative ordering increased profit by $22.77 on `dev_steady` and $21.94 on `dev_changes` in round 1.

However, when tested again in round 2, it reduced profit by $48.83 on `dev_steady` and $49.67 on `dev_changes`.

The results were within the noise range and did not show a consistent advantage for the alternative.

I kept least-slack ordering.

### Drop margin

The drop margin determines how much additional time the scheduler allows before treating a query as a candidate for dropping.

The default `m_drop` was two times the sum of startup time and the simulation tick interval. I tested replacing it with a fixed value of 3,600 seconds.

There was no change in profit on either development scenario in either round.

The reason is that tier 1, which uses this setting, was never used on the development scenarios. Since the change had no effect on these tests, I kept the existing setting.

### Look-ahead

The look-ahead setting controls how far the scheduler considers future work when making its decisions.

The default `LOOKAHEAD` value was 4. I tested reducing it to 1 and increasing it to 16.

Reducing it to 1 increased `dev_steady` profit by $10.14 in round 1 and $10.85 in round 2. However, it reduced `dev_changes` profit by $37.09 and $31.16.

Increasing it to 16 reduced `dev_steady` profit by $37.17 in round 1 and $4.67 in round 2. It increased `dev_changes` profit by $44.11 and $15.90.

Neither alternative showed a consistent improvement across both development scenarios. I kept the default value of 4.

### Cap-aware penalties

The `CAP_AWARE` setting controls whether the scheduler accounts for the fact that a client has a monthly penalty cap.

I tested turning this setting off.

On `dev_steady`, profit did not change in either round. On `dev_changes`, profit decreased by $79.89 in round 1 and $85.89 in round 2.

These reductions were within the final noise band, so the experiments did not prove that keeping cap awareness on produced a measurable profit gain. However, turning it off did not help either.

I kept `CAP_AWARE` enabled because it makes the scheduler's money model consistent with the scoring rule. Once a client's monthly penalty cap has been reached, another missed query does not add to that client's penalty cost.

### Do-not-serve

The `DO_NOT_SERVE` setting allows the scheduler to drop a query when it is certain that the query will miss its deadline and the drop penalty is cheaper than the late penalty.

I tested turning this setting off.

On `dev_steady`, profit did not change in either round. On `dev_changes`, profit decreased by $11.44 in round 1 and $35.95 in round 2.

These differences were within the noise band. The experiment therefore showed that turning the setting off did not improve the results.

I kept `DO_NOT_SERVE` enabled because it follows the scoring rule: when a query is certain to miss its deadline, a drop penalty that is smaller than the late penalty is the cheaper outcome.

### Overall result of the policy-setting experiments

The warm-capacity change was the only setting in this group that met the gain criteria. It improved both development months by more than the final combined noise band and also improved the listed stress scenarios.

The other settings either performed worse or produced differences that were too small or inconsistent to establish a reliable gain.

I therefore adopted the warm-capacity change and kept the other settings at their existing values.

## 3. Spot Policy: Removing the Risk Term and Forced On-Demand Fallback

The next group of experiments examined how the scheduler should choose between spot and on-demand machines.

These tests used an earlier version of the scheduler. That version did not yet include the do-not-serve policy, the warm-capacity change, or later fixes. Therefore, its absolute profit figures are different from those of the final scheduler.

The purpose here was to compare alternative spot policies within the same earlier version.

The original policy included two rules:

- A risk term that increased the estimated cost of using spot capacity.
- A forced fallback to on-demand after two spot interruptions.

I tested removing each rule separately and then removing both.

With both rules enabled, profit was $14,367.00 on `dev_steady` and $12,637.63 on `dev_changes`.

When I removed the risk term but kept the forced on-demand fallback, profit increased to $14,397.07 on `dev_steady` and $13,299.30 on `dev_changes`. These were improvements of $30.07 and $661.67.

When I kept the risk term but removed the forced fallback, profit increased to $14,536.72 and $12,782.27. These were improvements of $169.72 and $144.64.

The strongest result came from removing both rules. Profit increased to $14,581.46 on `dev_steady` and $13,508.04 on `dev_changes`. Compared with the original policy, these were improvements of $214.46 and $870.41.

I also tested removing the forced fallback after the risk term had already been removed. This produced an additional improvement of $184.39 on `dev_steady` and $208.74 on `dev_changes`.

Based on these comparisons, the final spot policy uses neither the risk term nor the forced on-demand fallback.

The important qualification is that these results compare variants of the same earlier scheduler version. They should not be read as direct profit comparisons against the final scheduler.

## 4. Ordering Inside Tiers 1 and 3

I also tested how the scheduler should order queries inside tiers 1 and 3.

This experiment used the earlier scheduler version and the three-day `test_overload` scenario. The scenario had one machine per type and market, with a 30-minute drop limit.

I compared two ordering rules.

The first was oldest-first. Under this policy, the scheduler prioritised the query that had been waiting the longest.

The second prioritised queries with the highest drop penalty per slot-second. This means the scheduler considered how much drop penalty could be avoided relative to the amount of execution capacity needed.

The oldest-first policy produced a profit of -$29,527.42. It incurred $30,364.50 in drop penalties and dropped 18,890 queries.

The penalty-per-slot-second policy performed much better. Profit was -$3,477.21, drop penalties were $4,316.25, and 4,872 queries were dropped.

The profit improvement was substantial in this overload scenario. The policy reduced both the number of dropped queries and the total drop penalties.

I also tested the change on the two development months. It was within noise there, reducing profit by $29.47 on `dev_steady` and $7.16 on `dev_changes`.

The change was therefore adopted because of its clear improvement under the overload stress scenario, even though it did not produce a measurable gain on the development months.

## 5. Correcting the Spot Cost Estimate

One of the later experiments focused on how the scheduler estimates the cost of spot capacity.

The earlier formula assumed that a query would make two spot attempts and then move to on-demand. However, that was not the policy the scheduler actually followed. The scheduler continued retrying on spot until an attempt completed.

I corrected the expected-cost formula so that it represented the actual unlimited-retry policy.

To check the correction, I ran the scheduler before and after the change using paired simulations. Seed copies changed only the simulator's random seed.

The correction did not change either development month.

On `dev_steady`, profit remained $14,666.36. Compute cost, drops, and misses were also unchanged. There were no drops, and 2,509 queries missed their service-level agreements.

On `dev_changes`, profit remained $13,617.73. There were 10 drops and 7,431 misses before and after the correction.

The correction also produced no changes on `test_events`, `test_outage`, `test_caps`, or `test_burst`.

However, the results were different on `test_spot_storm`.

In the original-seed run, profit increased from $8,976.11 to $9,167.56, an improvement of $191.45. Compute cost decreased by $188.55. The number of drops remained zero, while missed queries decreased from 11,222 to 7,404.

I repeated the test with two additional random seeds.

For seed 1, profit increased from $8,900.36 to $9,160.21, a gain of $259.85. Compute cost decreased by $242.85. There were no drops, and misses decreased from 12,003 to 7,963.

For seed 2, profit increased from $8,777.96 to $9,114.00, a gain of $336.04. Compute cost decreased by $500.84. There were no drops, and misses decreased from 11,773 to 8,272.

Across the three paired `test_spot_storm` runs, the average profit improvement was $262.45. All three runs improved.

The result was different on `test_overload`.

In the original-seed run, profit decreased from -$25,214.55 to -$29,475.50, a loss of $4,260.95. Compute cost increased by $110.10. Drops increased from 38,227 to 39,054, and misses increased from 234,308 to 257,637.

For seed 1, profit decreased from -$31,128.58 to -$31,740.28, a loss of $611.70. Compute cost increased by $65.05. Drops changed from 40,038 to 40,037, while misses increased from 237,367 to 262,747.

For seed 2, profit decreased from -$23,955.77 to -$29,004.18, a loss of $5,048.41. Compute cost increased by $77.36. Drops increased from 37,469 to 38,717, and misses increased from 228,317 to 255,003.

Across these three paired `test_overload` runs, the average profit change was a decrease of $3,307.02. All three runs were negative.

The three-day `test_overload_short` result also decreased. Profit changed from -$3,350.38 before the correction to -$6,317.63 after it, a loss of $2,967.25. Compute cost increased by $9.50, drops increased from 4,570 to 6,203, and misses increased from 32,226 to 35,715.

This short-overload comparison was not part of the paired batch. The before figure came from an earlier stress run of the same code, while the after figure came from the final-code run. It should therefore be interpreted separately from the three paired overload comparisons.

### Checking whether the overload result was noise

The overload results needed additional checking because the simulation has substantial variation under extreme congestion.

To estimate the noise in `test_overload`, I used the version before the spot-cost correction and changed the idle-release timeout by plus or minus 1%.

The resulting profits were -$31,208.77 and -$26,124.65, compared with the reference profit of -$25,214.55.

This produced a spread of approximately $6,000 for a single run.

Each of the three paired overload differences was within that spread. However, all three comparisons showed a decrease in profit after the correction.

For that reason, I treated the result as a real loss in this extreme scenario rather than dismissing it as ordinary noise.

### Why the correction affects overload performance

The correction does not change the spot-versus-on-demand choice for a single machine type. A unit test checks that this choice remains identical under both formulas.

The difference appears when the scheduler compares spot capacity on one machine type against an alternative on another machine type inside `class_routing`.

In `test_overload`, each market has only one machine, and machine launches fail frequently. The original-seed runs recorded 5,427 and 6,011 launch cool-downs. This means the scheduler repeatedly has to choose between alternatives across machine types while capacity is severely limited.

With the corrected expected-cost calculation, spot capacity on an interrupted machine type can appear more expensive than it did under the earlier formula. The scheduler may then route more work towards other machine types.

Under normal conditions, that can be a useful cost decision. Under this extreme overload condition, however, the alternative machine types are also short of capacity. Moving work towards them can increase congestion and make the overall result worse.

The important distinction is that the corrected formula represents the retry policy the scheduler actually follows. The earlier formula did not accurately price that policy.

The stress results show a trade-off: the correction improved the spot-storm scenario consistently, but reduced profit in the extreme overload scenario. This is a limitation to keep in mind when interpreting the final policy's behaviour under severe capacity shortages.

## Conclusion

The experiments helped separate settings that produced measurable improvements from those whose differences were within normal simulation variation.

The warm-capacity change was the clearest gain across both development months and all listed stress scenarios. I adopted `Z_WARM = 1.28` and `B_WARM = 0`.

The other individual setting changes did not establish a consistent improvement, so I kept their existing values. I also kept cap awareness and do-not-serve enabled because they make the scheduler's decisions consistent with the scoring rules, even though turning them off did not produce a measurable gain.

The spot-policy experiments supported removing both the risk term and the forced on-demand fallback. The tier-ordering experiment showed that prioritising drop penalty per slot-second can greatly reduce losses under severe overload, even though the change was within noise on the development months.

Finally, the spot-cost correction made the expected-cost calculation match the scheduler's actual unlimited-retry behaviour. It improved all three paired spot-storm runs but reduced profit in all three paired extreme-overload runs. That trade-off is important: a cost estimate that is correct for the implemented retry policy does not guarantee better results in every capacity condition.

Together, these experiments explain the reasoning behind the final policy choices and identify where the scheduler performs well, where changes made a measurable difference, and where extreme workloads remain challenging.