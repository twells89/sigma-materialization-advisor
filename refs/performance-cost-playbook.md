# Sigma performance-and-cost playbook

The objective is not “remove materializations” or “materialize more.” It is:

> Meet the workload's latency and freshness targets at the lowest sustainable total cost.

## Evidence

| Signal | Source | Meaning |
|---|---|---|
| Query credits / QAS | `QUERY_ATTRIBUTION_HISTORY` | Attributed execution cost; excludes idle and other cost pools |
| p50 / p95 / scan / spill / queue / pruning | `QUERY_HISTORY` | User-path performance and query-efficiency evidence |
| Refresh cost | Sigma `kind=materialization` tags + query attribution | Materialization build/refresh compute |
| Observed served reads | `ACCESS_HISTORY.objects_modified` → `direct_objects_accessed` | Queries that accessed an object built by a materialization job |
| Warehouse idle | `WAREHOUSE_METERING_HISTORY` | Warehouse-level upper bound, not automatically Sigma savings |
| Schedule / controls / lineage | Sigma REST | Cadence, control-risk flag, and dependencies |

Keep unmatched reads explicit. A failed object match can mean bypass, object rotation,
incomplete history, or missing privileges; it is not proof that the schedule is unused.
The current `ACCESS_HISTORY` utilization query matches workbook materializations only;
data-model schedules require consumer-lineage review and remain `Investigate`.

## Decisions

1. **Keep** when observed read utilization is healthy, cached p95 meets the SLO, and
   refresh cost is justified.
2. **Retune** when a schedule is useful but refreshes too frequently for user demand or
   source updates.
3. **Investigate** when utilization is low, controls are present, lineage is incomplete,
   or the workload stays slow despite materialization.
4. **Remove candidate** only when a measured no-materialization window remains within
   the latency allowance and produces material savings. Preserve schedule configuration
   and define rollback before removal.
5. **Add candidate** when repeated live-query cost and latency exceed expected refresh
   compute + storage and the freshness requirement permits snapshots.
6. **Optimize query/model** when the query itself has poor pruning, spill, costly joins,
   excess columns, or expensive calculations. Lower the floor before caching.

## Controls and freshness

- A control targeting a materialized element causes live queries. Prefer controls on
  child elements.
- Current list-elements and lineage APIs reveal control presence but not target
  bindings. Mark control-bearing workbooks `review required`.
- Dynamic date controls can force full refreshes; parameters use initial values.
- Schedule upstream materializations before dependent children.

## Duplicate work and consolidation

The same logic is often rebuilt in many workbooks: copied workbooks, the same join and
aggregation over the same base tables, each with its own materialization schedule. Every copy
pays its own refresh. **Consolidate into data model** means: build that logic once in a data
model, materialize it there, and point the workbook elements at it.

| Match | Evidence | Confidence |
|---|---|---|
| `identical-build` | Materialization jobs whose `select` is identical after normalization | medium |
| `identical-sql` | Same compiled element SQL in several workbooks (Sigma API scan, no cost data) | low |
| `same-base-tables` | Materialized elements that read the identical set of base tables | low |
| `identical-live-query` | Same `query_parameterized_hash` issued from several workbooks | low |

Redundant cost is everything except the most expensive member. Consolidation savings overlap
the per-element retune estimates, so they are reported separately and never added to them.
Retiring the old schedules is a removal, and the removal safeguards apply. Keep the old
schedules until reads move to the data model. When three or more workbooks share the logic, or
tools outside Sigma need it too, a warehouse-side table (a dbt model or a Snowflake dynamic
table) may be the better home.

## Incremental refresh

On Snowflake, Sigma can refresh a materialization incrementally by using dynamic tables. This
requires a connection configured for dynamic tables and change tracking on the source tables
(see the materialization docs). When an element falls back to full refreshes, check these in
order:

1. **Relative date controls** (for example "last 7 days"). They force a full refresh.
2. **Timezone-naive timestamps.** A `TIMESTAMP_NTZ` column the element references can make
   incremental refresh unavailable. Check every referenced column, not only the date key.
   Converting the column to a timezone-aware type in the warehouse table or a warehouse view is
   the reliable fix. A cast done only inside Sigma is unproven, and `DATE` behavior is not
   documented, so test it on one element and confirm with Sigma support before you roll it out.
3. **Non-deterministic functions** and other Snowflake dynamic-table limitations.
4. **Upstream object churn.** Recreating an upstream view or function can turn an incremental
   refresh into a full reinitialize. Look for `REFRESH_ACTION` values of `FULL` or
   `REINITIALIZE` in `DYNAMIC_TABLE_REFRESH_HISTORY`.

## Split refresh (history + recent, unioned)

Pattern: materialize stable history on a slow cadence and recent data on a fast cadence, then
union them. It can cut refresh compute, but it moves correctness onto the builder. Treat it as an
advanced, workload-specific option. It is not a default recommendation, and it is not an
officially endorsed pattern. Use it only when all of these hold:

- **History is truly immutable.** Late-arriving rows, restatements, and backfills older than the
  cutoff stay invisible until the slow side refreshes. If you cannot prove this, don't use the
  pattern.
- **The cutoff is non-overlapping and owned by one side**, for example `< start of current
  month` and `>=` for recent. Otherwise rows at the boundary are double-counted or dropped.
- **A reconciliation plan exists.** Force a periodic full rebuild of the history side, and
  compare row counts and key sums against the source.
- **Controls sit downstream of both materialized inputs.** A control on a materialized element
  runs live.
- **Schedules are ordered.** Recent refreshes after its source loads, and anything built on the
  union runs after both inputs, using dependent schedules where available.
- **The union's read cost is counted.** Every read is a live query over two tables.

Where native incremental refresh is available, prefer it: it picks up changed historical rows
automatically. Split refresh is mainly useful where incremental refresh isn't available.

## Sigma materialization vs a warehouse-side table

| Prefer Sigma materialization | Prefer a dbt model / Snowflake dynamic table |
|---|---|
| Speeding up a Sigma-facing element | Shared or complex transformation logic |
| Owner wants cadence control in Sigma | Non-Sigma consumers need the same table |
| One or two workbooks depend on it | Explicit lag targets or a refresh DAG are needed |

To compare costs fairly, run dynamic-table refreshes on a dedicated warehouse. Measure them with
`DYNAMIC_TABLE_REFRESH_HISTORY` joined to `QUERY_HISTORY`, plus `WAREHOUSE_METERING_HISTORY`.
Don't assume refresh credits appear in `QUERY_ATTRIBUTION_HISTORY`; verify that in your account.

## Measuring impact

Re-run the analysis after changes and compare the two inventories with `scripts/compare.py`,
then render `scripts/render-savings.py`. Rules the report follows:

- **Wait at least two weeks** and keep the same scope and window length. `ACCOUNT_USAGE` lags by
  hours, and one unusual week distorts the result. The report warns when runs are too close.
- **Traffic changes are not savings.** Read savings compare cost per run and exclude objects whose
  read volume moved more than the traffic-shift threshold. Refresh savings (fewer or cheaper
  refreshes) are counted separately and are not affected by read traffic.
- **Follow-through is verified where possible.** Enrich both runs so schedule changes are visible.
  A retune counts as acted on when the cron refreshes less often. A removal counts when the
  schedule is gone. A consolidation counts when member schedules are retired. Without schedule
  data, refresh counts are the fallback, and the report says so. Query optimizations can only be
  inferred from lower cost per run, so they are reported as *improved*, not *acted on*.
- **Savings add up.** Savings tied to actions, inferred improvements, and other changes sum to the
  headline. Each object is credited once.
- **Not observed is not removed.** An object missing from the second run may only have dropped
  below the top-N cut.
- **Coincidence, not proof.** A change and a saving that happen together don't prove the change
  caused it. Mention known outside factors, such as a smaller source table or a seasonal drop.

## Savings math

Keep cost pools separate:

`materialization value = avoided live-query credits - refresh credits - incremental storage`

`warehouse idle opportunity = warehouse compute - attributed query compute`

Report low/expected/high savings. Never add overlapping query, materialization, and idle
estimates into one uncapped number.

## Validation sequence

1. Capture current schedule, freshness, ownership, cost, served reads, and p95.
2. Use a natural failed/paused/pre-schedule window or an explicitly approved benchmark.
3. Compare equivalent query cohorts and business periods.
4. Require owner approval for any mutation.
5. Monitor latency, freshness, and credits through a rollback window.
