# Sigma Performance & Cost Opportunities

_Scope: **account-wide** · last 30 days · 2,500 Sigma queries · 50.00 credits (~$150) · generated 2026-10-29 · **analysis is read-only**_

Attribution coverage: **60.74%** of Sigma query credits map to a source URL. Unattributed/system activity remains a separate cost pool and is not silently dropped.

Evidence: **rows**

Estimated monthly savings (per element): **$31** · Consolidation savings (separate; overlaps the per-element figure): **$11**

## Recommended decisions

| # | Object | Element | Action | Impact | Runs | Credits | p95 s | Refreshes | Matched reads | Confidence |
|---:|---|---|---|---|---:|---:|---:|---:|---:|---|
| 1 | [dm: Sales-Mart-DM-aaa111](https://app.sigmacomputing.com/northwind/data-model/Sales-Mart-DM-aaa111) | (workbook load) | Investigate materialization | Low | 240 | 20.4000 | 4.00 | 0 | 0 | low |
| 2 | [wb: Legacy-Snapshot-iii999](https://app.sigmacomputing.com/northwind/workbook/Legacy-Snapshot-iii999?:displayNodeId=table-main) | table-main | Remove materialization candidate | Medium | 40 | 8.5000 | 1.50 | 30 | 40 | high |
| 3 | [wb: Customer-Overview-jjj000](https://app.sigmacomputing.com/northwind/workbook/Customer-Overview-jjj000?:displayNodeId=orders) | orders | Add materialization candidate | Low | 180 | 4.2000 | 4.50 | 0 | 0 | low |
| 4 | [wb: Ops-Live-Tiles-ccc333](https://app.sigmacomputing.com/northwind/workbook/Ops-Live-Tiles-ccc333?:displayNodeId=kpi-row) | kpi-row | Retune materialization | Low | 80 | 2.8000 | 2.00 | 90 | 4 | medium |
| 5 | [wb: Orders-Daily-Copy-A-mmm111](https://app.sigmacomputing.com/northwind/workbook/Orders-Daily-Copy-A-mmm111?:displayNodeId=master) | master | Keep materialization | Low | 120 | 2.6000 | 1.40 | 30 | 90 | medium |
| 6 | [wb: Orders-Daily-Copy-B-nnn222](https://app.sigmacomputing.com/northwind/workbook/Orders-Daily-Copy-B-nnn222?:displayNodeId=master) | master | Keep materialization | Low | 90 | 2.5000 | 1.40 | 30 | 60 | medium |
| 7 | [wb: Orders-Daily-Copy-C-ooo333](https://app.sigmacomputing.com/northwind/workbook/Orders-Daily-Copy-C-ooo333?:displayNodeId=master) | master | Retune materialization | Low | 40 | 2.2000 | 1.40 | 30 | 25 | medium |
| 8 | [wb: Exec-Revenue-Daily-bbb222](https://app.sigmacomputing.com/northwind/workbook/Exec-Revenue-Daily-bbb222?:displayNodeId=master) | master | Keep materialization | Low | 510 | 2.4000 | 1.30 | 30 | 500 | medium |
| 9 | [wb: Finance-Snapshot-hhh888](https://app.sigmacomputing.com/northwind/workbook/Finance-Snapshot-hhh888?:displayNodeId=master) | master | Investigate materialization | Low | 60 | 1.6000 | 3.00 | 30 | 20 | medium |
| 10 | [wb: Adhoc-Cohort-Explorer-ddd444](https://app.sigmacomputing.com/northwind/workbook/Adhoc-Cohort-Explorer-ddd444?:displayNodeId=cohort-tbl) | cohort-tbl | Optimize query/model | High | 5 | 1.1000 | 18.00 | 0 | 0 | medium |
| 11 | [wb: Marketing-Overview-fff666](https://app.sigmacomputing.com/northwind/workbook/Marketing-Overview-fff666?:displayNodeId=chart-1) | chart-1 | Monitor | Low | 64 | 0.1800 | 0.70 | 0 | 0 | high |
| 12 | [wb: HR-Headcount-ggg777](https://app.sigmacomputing.com/northwind/workbook/HR-Headcount-ggg777?:displayNodeId=table-main) | table-main | Monitor | Low | 22 | 0.0900 | 0.50 | 0 | 0 | high |

### Investigate materialization: /data-model/Sales-Mart-DM-aaa111 / (workbook load)

- Impact: Low
- Why: The data-model workload is repetitive and expensive, but schedule and downstream-consumer utilization are not resolved by the workbook-only materialization evidence pass.
- Performance: Potentially high fan-out; performance impact is unknown.
- Validate: Inspect the data-model schedule and consumer lineage before adding, retuning, or removing materialization.
  1. Open the workbook and check whether the schedule is active, failing, or recently changed.
  2. Check in Sigma who opens the workbook and how often.
  3. If page controls (filters) feed this element, review them: they can bypass the saved copy.
  4. Change nothing until the owner has decided to keep, lower, or remove it.

### Remove materialization candidate: /workbook/Legacy-Snapshot-iii999 / table-main

- Impact: Medium
- Why: Measured no-materialization p95 is 3.20s and remains within the latency allowance; projected monthly savings are about $24.
- Performance: Measured p95 without materialization: 3.20s.
- Validate: Remove only with owner approval, preserve the cron/timezone, and monitor latency and credits with a rollback window.
  1. Confirm with the workbook owner that nobody relies on the saved copy.
  2. Write down the current schedule (cron and timezone) so it can be restored.
  3. Turn the schedule off and watch load time and credits for one to two weeks.
  4. Restore the schedule if the workbook becomes slower than your target.

### Add materialization candidate: /workbook/Customer-Overview-jjj000 / orders

- Impact: Low
- Why: The element is repetitive (180 runs) and non-trivial (4.50s p95, 4.20 query + QAS credits).
- Performance: Likely faster repeat reads; freshness and refresh cost must be measured.
- Validate: Create only after a capped benchmark. Compare avoided live-query credits with refresh compute and storage.
  1. Open the workbook and find the element named in the report: it is opened often and costs a lot each time.
  2. Turn on materialization for that element with a daily schedule at a quiet hour.
  3. After a week, compare load time and credits; keep it only if both improve.

### Retune materialization: /workbook/Ops-Live-Tiles-ccc333 / kpi-row

- Impact: Low
- Why: Only 4 matched read(s) were observed across 90 refresh(es) (0.04 reads/refresh).
- Performance: Expected to preserve cached performance while reducing refresh work; the exact impact requires a cadence experiment.
- Validate: Align cadence to source updates and user access. Measure one full business cycle before considering removal.
  1. Open the workbook, find the element named in the report, and open its materialization schedule.
  2. Ask the data owner how often the underlying data really changes; refresh no more often than that (daily is often enough).
  3. Lower the schedule (for example from hourly to every 4 hours or daily) and write down the old setting first.
  4. Check again in a week or two: the workbook should open just as fast and refresh credits should drop.

### Keep materialization: /workbook/Orders-Daily-Copy-A-mmm111 / master

- Impact: Low
- Why: It serves 90 observed read(s) across 30 refresh(es), and matched-read p95 is 1.10s.
- Performance: Current cached performance meets the configured SLO.
- Validate: Continue measuring refresh cost and matched reads; retune if utilization falls.
  1. No change needed: the saved copy is being used and loads fast.
  2. Look again next month. If fewer people open the workbook, refresh it less often.

### Keep materialization: /workbook/Orders-Daily-Copy-B-nnn222 / master

- Impact: Low
- Why: It serves 60 observed read(s) across 30 refresh(es), and matched-read p95 is 1.10s.
- Performance: Current cached performance meets the configured SLO.
- Validate: Continue measuring refresh cost and matched reads; retune if utilization falls.
  1. No change needed: the saved copy is being used and loads fast.
  2. Look again next month. If fewer people open the workbook, refresh it less often.

### Retune materialization: /workbook/Orders-Daily-Copy-C-ooo333 / master

- Impact: Low
- Why: Only 25 matched read(s) were observed across 30 refresh(es) (0.83 reads/refresh).
- Performance: Expected to preserve cached performance while reducing refresh work; the exact impact requires a cadence experiment.
- Validate: Align cadence to source updates and user access. Measure one full business cycle before considering removal.
  1. Open the workbook, find the element named in the report, and open its materialization schedule.
  2. Ask the data owner how often the underlying data really changes; refresh no more often than that (daily is often enough).
  3. Lower the schedule (for example from hourly to every 4 hours or daily) and write down the old setting first.
  4. Check again in a week or two: the workbook should open just as fast and refresh credits should drop.

### Keep materialization: /workbook/Exec-Revenue-Daily-bbb222 / master

- Impact: Low
- Why: It serves 500 observed read(s) across 30 refresh(es), and matched-read p95 is 1.30s.
- Performance: Current cached performance meets the configured SLO.
- Validate: Continue measuring refresh cost and matched reads; retune if utilization falls.
  1. No change needed: the saved copy is being used and loads fast.
  2. Look again next month. If fewer people open the workbook, refresh it less often.

### Investigate materialization: /workbook/Finance-Snapshot-hhh888 / master

- Impact: Low
- Why: Control targets are not resolved; observed utilization is 20 matched read(s) across 30 refresh(es). Removal safety cannot be inferred.
- Performance: Unknown until control/lineage behavior is verified.
- Validate: Review controls and lineage, then benchmark the element with and without materialization before changing the schedule.
  1. Open the workbook and check whether the schedule is active, failing, or recently changed.
  2. Check in Sigma who opens the workbook and how often.
  3. If page controls (filters) feed this element, review them: they can bypass the saved copy.
  4. Change nothing until the owner has decided to keep, lower, or remove it.

### Optimize query/model: /workbook/Adhoc-Cohort-Explorer-ddd444 / cohort-tbl

- Impact: High
- Why: The workload is expensive per run (p95 18.00s) and shows scan, pruning, or spill pressure. Lower the cost floor before caching it.
- Performance: Expected to reduce both live-query cost and latency.
- Validate: Inspect query insights/profile, filters, joins, projected columns, and pre-aggregation; remeasure before materializing.
  1. Open the element and remove columns it does not show; add a date or category filter if people only look at part of the data.
  2. If several elements repeat the same joins or calculations, move them into one shared data model so the work happens once.
  3. Ask your data team whether the source table is organized (clustered) by the column you filter on most.
  4. Re-run this report next week to see whether cost and load time dropped.

## Duplicate work

Elements that do the same work more than once. Consolidation savings overlap the per-element estimates above and are reported separately.

| Group | Match | Members | Redundant credits | Expected savings $/mo | Impact | Confidence |
|---|---|---:|---:|---:|---|---|
| sf-1 | identical-build | 3 | 4.0000 | 10 | Low | medium |
| sf-2 | same-base-tables | 2 | 1.0000 | 1 | Low | low |
| sf-3 | identical-live-query | 2 | 0.1800 | 0 | Low | low |

_2 group(s) have no measurable repeat cost and are summarized only._

### sf-1: identical-build (3 elements)

- [/workbook/Orders-Daily-Copy-A-mmm111 / master](https://app.sigmacomputing.com/northwind/workbook/Orders-Daily-Copy-A-mmm111?:displayNodeId=master) — 2.0000 credits
- [/workbook/Orders-Daily-Copy-B-nnn222 / master](https://app.sigmacomputing.com/northwind/workbook/Orders-Daily-Copy-B-nnn222?:displayNodeId=master) — 2.0000 credits
- [/workbook/Orders-Daily-Copy-C-ooo333 / master](https://app.sigmacomputing.com/northwind/workbook/Orders-Daily-Copy-C-ooo333?:displayNodeId=master) — 2.0000 credits
- Base tables: NORTHWIND.SALES.CUSTOMERS, NORTHWIND.SALES.ORDERS
- Why: 3 elements build the identical table, so the same work is done 3 times. Roughly 4.00 of 6.00 credits in this window are repeat work (about $12/month at list price). A table in your warehouse (for example a Snowflake dynamic table or dbt model) may fit better if other tools also need this data.
- Validate: Build the shared logic once in a data model, materialize it there, repoint the listed elements, keep the old schedules until reads move, then retire them under the removal safeguards.
  1. Create one data model that holds the shared logic (same source tables, joins, and calculations).
  2. Turn on materialization on that data model at the cadence the most demanding workbook needs.
  3. Repoint each listed workbook element to the data model instead of its own copy.
  4. Keep the old schedules until readers have moved over, then turn them off with the owner's approval.

## Warehouse context

- Compute: 70.0 credits
- Estimated idle: 25.0 credits (35.71%)
- Idle is a warehouse-level upper bound; it is not automatically attributed to Sigma or to any one workbook.

## Method & caveats

- Query cost comes from `QUERY_ATTRIBUTION_HISTORY`; materialization utilization matches `ACCESS_HISTORY.objects_modified` to `direct_objects_accessed`.
- A removal recommendation requires a measured no-materialization baseline. Refresh cost alone is never treated as proof that removal is safe.
- Served reads count every non-materialization query kind (adhoc, export, screenshot, ...) that reads a materialized table.
- Duplicate work is matched by identical materialization builds, identical source-table sets, identical live queries, and (with `--scan`) Sigma metadata; consolidation savings are estimates and are never added to per-element savings.
- Controls can bypass materialization. Unresolved target bindings force manual review.
- `ACCOUNT_USAGE` is delayed and query attribution excludes idle, storage, transfer, cloud services, and most serverless costs.