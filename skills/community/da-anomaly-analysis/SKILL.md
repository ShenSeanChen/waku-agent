---
name: da-anomaly-analysis
description: Investigate a metric anomaly (DAU, GMV, conversion, retention) — why it dropped or spiked. Use for "why did X drop", "anomaly analysis", "metric attribution", "what caused this change".
---

## Instructions

Follow this sequence: define the anomaly, validate the data, narrow the source, investigate internal causes, then investigate external causes. Confirm that the change is real before explaining it.

1. Define the anomaly. State the metric definition, time window, comparison baseline, start time, duration, absolute and relative change, business magnitude, and affected scope.
2. Validate the data. Check source data, instrumentation, ETL, latency, metric definitions, scope changes. Stop business attribution until data and normal periodicity are cleared.
3. Narrow the source. Start with a business equation or funnel, then drill down by mutually exclusive and collectively exhaustive groups such as time, region, channel, platform, version, product, and user segment. Iterate between formula decomposition and dimension drill-down.
4. Quantify contribution. Choose the method from the mathematical relationship:

- Additive total, S = sum(s_i): use delta_i = s_i_now - s_i_base; contribution share is delta_i / delta_S.
- Weighted rate, Y = sum(w_i * y_i): decompose each group into quality w_i_base * delta_y_i, structure delta_w_i * (y_i_base - Y_base), and interaction delta_w_i * delta_y_i.
- Multiplicative metric, S = A * B * ...: use relative-change, log, or Shapley decomposition and apply one consistent interaction-allocation rule. Report contribution values, directions, and shares. If total change is near zero or opposing effects offset, emphasize values because shares may be unstable, negative, or above 100%.

5. Investigate internal causes. Form falsifiable hypotheses around product releases, algorithms, supply, campaigns, budgets, targeting, configuration, outages, performance, compatibility, and reporting. For each hypothesis, specify the change record, timing, affected scope, expected mechanism, validation metric, and control.
6. Investigate external causes. Check market trends, competitors, policy, public events, holidays, weather, and seasonality. Validate with industry data, historical periods, unaffected groups, or comparable markets.
7. Deliver the decision. Summarize the primary and secondary drivers, evidence strength, unresolved hypotheses, immediate mitigation, long-term prevention, owner, and recovery monitoring.

## Edge cases

| Situation | Do |
|---|---|
| The metric or baseline is ambiguous | Ask one clarifying question before diagnosing |
| Overall movement conflicts with segment movements | Check weights and Simpson's paradox using quality, structure, and interaction effects |
