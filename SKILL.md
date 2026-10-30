---
name: sigma-performance-cost-advisor
description: >-
  Keep Sigma fast while lowering Snowflake cost. Attribute compute to Sigma objects,
  measure whether materializations serve observed reads, and recommend keep, retune,
  investigate, remove, add, or optimize-query actions against latency and freshness
  constraints. Read-only analysis; remediation is explicit and opt-in.
user-invocable: true
---

# Sigma Performance & Cost Advisor

> **STATUS: validated live** against Sigma and Snowflake: full cost coverage, schedule
> and lineage reads, `ACCESS_HISTORY` utilization matching, on-demand refresh polling,
> and capped element exports. Analysis is read-only; mutations require explicit commands.

**Read first:** `refs/performance-cost-playbook.md` (decision evidence and safeguards),
`PRIVACY.md` (read-only analysis posture).

## The key idea
Sigma stamps **every** warehouse query's `QUERY_TAG` with `Sigma Σ {json}` containing
`sourceUrl` (org + workbook/data-model + element), `email`, and `kind`. Snowflake's
`QUERY_ATTRIBUTION_HISTORY` gives real credits. `ACCESS_HISTORY` reveals whether workbook
queries read objects built by materialization jobs. Evaluate refresh TCO and observed
performance together; never infer that deletion is safe from refresh cost alone.

## Inputs / access
- **Snowflake** via the `snow` CLI (a connection with read access to
  `SNOWFLAKE.ACCOUNT_USAGE`; `ACCESS_HISTORY` requires the applicable Enterprise
  privileges). SSO (`externalbrowser`) or key-pair both work.
- **Sigma REST** (`SIGMA_BASE_URL` plus a token or client credentials) — for read-only
  schedule/control/lineage enrichment, the workbook scan, and explicit remediation actions.

## Choose the input path — ask first
**Can the person running this query `SNOWFLAKE.ACCOUNT_USAGE`?** Prefer the first path that
applies. Each later path measures less, and the report says what was not measured.

| Path | Command | Measures | Doesn't measure |
|---|---|---|---|
| **Snowflake** (default, most complete) | `analyze.py --conn <snow-conn>` | credits, latency, refresh cost, served reads, duplicate builds | — |
| **Exported rows** (Cortex Code / notebook) | `analyze.py --from-rows bundle.json` | same as Snowflake | — |
| **Cost table** (an internal cost tool's export) | `analyze.py --from-cost-table costs.csv` | credits or cost per workbook/element | served reads, so a materialization is never retuned or removed on this evidence |
| **Sigma only** (no warehouse access) | `scan_workbooks.py` then `analyze.py --sigma-only --scan scan.json` | duplicate logic, schedules, refreshes/day, owners | any cost or latency |

Add `--scan scan.json` to any path to find duplicate logic hidden behind materializations.
A materialized element's compiled SQL reads only its own write-back table, so the scan
fingerprints the element definition from the workbook spec instead.

Cost table columns (CSV or JSON, case-insensitive): one of `workbook_id` / `data_model_id` /
`object`; one of `credits` / `cost`; optional `element_id`, `date`, `query_count`,
`avg_sec`, `p95_sec`, `kind` (`materialization` = refresh cost), `owner_email`.

## Intake — ask before the first run
Persist answers in `advisor-config.json`; do not use blind defaults for a customer.

| Ask | Maps to | Why |
|---|---|---|
| **Latency target?** p95 seconds + acceptable slowdown | recommendation constraint | Prevents cost cuts that make dashboards unusably slow |
| **How fresh?** real-time / ≤1h / ≤1 day / varies | cadence and eligibility | Real-time disables add-materialization advice |
| **Primary goal?** cost / latency / performance per dollar | ranking | Changes which opportunities lead |
| **Minimum savings?** dollars/month | removal threshold | Avoids operational work for immaterial savings |

**$ per credit is auto-detected** — `analyze.py` reads the account's effective rate from
`SNOWFLAKE.ORGANIZATION_USAGE.usage_in_currency_daily` (then `rate_sheet_daily`). Only ask
the customer if detection returns nothing (no `ORGANIZATION_USAGE` access / internal
account); then pass `--credit-price`. The report states whether the rate was
*auto-detected*, *provided*, or *assumed* ($3 default).

Suggested threshold presets:
- **conservative:** `--mat-min-runs 20 --mat-min-avg-sec 5 --mat-min-credits 2`
- **balanced (default):** `--mat-min-runs 12 --mat-min-avg-sec 3 --mat-min-credits 0.5`
- **aggressive:** `--mat-min-runs 8 --mat-min-avg-sec 1.5 --mat-min-credits 0.2`

Persist the answers in `advisor-config.json` (see `advisor-config.example.json`) and pass
`--config advisor-config.json` so re-runs are consistent.

## Phases
1. **Scan (optional, Sigma API only)** — `scripts/scan_workbooks.py` finds duplicate
   logic, schedules, refreshes/day, and owners → `scan.json`. It never stores raw SQL.
2. **Analyze** — full Sigma cost coverage, source-level workload, materialization
   utilization, duplicate work, and warehouse context → schema-v2 `inventory.json` +
   `REPORT.md`.
3. **Enrich** — `scripts/enrich.py` resolves schedules, controls, lineage, and owners.
   Because current APIs do not expose control target bindings, any control-bearing workbook
   is `review required`.
4. **Report** — render decisions with an impact tier and plain fix steps. Use
   `--by-owner DIR` for one report per workbook owner (plus `owners.csv`), so a central team
   can send each owner only their items.
5. **Remediate (opt-in)** — use `materialize.py --dry-run` first. Removal requires a
   measured no-materialization baseline, owner approval, and rollback details.
6. **Measure** — at least two weeks after changes, re-run over the same scope and window
   (enrich both runs), then `scripts/compare.py --before A --after B --out C` and
   `scripts/render-savings.py --compare C/compare.json --out savings.html`. The report shows
   realized savings, which recommendations were acted on (verified from schedule changes where
   possible), savings by owner, and what is still open. Read savings use cost per run, so
   traffic changes are not counted as savings, and refresh savings are counted separately.
   Savings are attributed because they coincide with a change, not proven caused by it.

## Scripts
| Script | Purpose |
|---|---|
| `scripts/analyze.py` | cost/performance evidence (any input path) → v2 inventory + report |
| `scripts/cost_model.py` | pure recommendation, consolidation, impact, and savings logic |
| `scripts/scan_workbooks.py` | read-only Sigma API scan for duplicate logic and schedules |
| `scripts/enrich.py` | read-only Sigma API schedule/control/lineage/owner pass |
| `scripts/materialize.py` | explicit schedule CRUD / refresh with `--dry-run` |
| `scripts/render-html.py` | `inventory.json` → `report.html`, or per-owner reports |
| `scripts/compare.py` | before/after inventories → realized savings + follow-through (`compare.json`, CSVs, `COMPARE.md`) |
| `scripts/render-savings.py` | `compare.json` → `savings.html`, or per-owner savings reports |

Key flags (`analyze.py`): `--conn`, `--from-rows`, `--from-cost-table`, `--sigma-only`,
`--scan`, `--days`, `--org`, `--latency-slo-sec`, `--freshness`,
`--acceptable-slowdown-sec`, `--goal`, `--config`, and `--out`.

## Cortex Code note
The attribution SQL is portable — it runs equally well from Cortex Code / a Snowflake
notebook if a team prefers to keep the analysis in-warehouse (no creds to manage, data
never leaves the account). The Sigma-side mapping and materialization actions need the
Sigma REST API, so the end-to-end loop lives here.

## Non-negotiable safeguards
- Report all Sigma-tagged cost and the attributable percentage; never label a limited
  candidate list as total spend.
- Keep unmatched materialization reads as an explicit unknown bucket.
- Never recommend removal without a measured counterfactual inside the latency allowance.
- Treat warehouse idle as an upper bound, not as Sigma-attributable savings.
- Do not automatically act on controls, semantic changes, warehouse sizing, or deletion.
- Never infer low utilization from missing read evidence: cost-table and Sigma-only
  inputs route materializations to `Investigate`.
- Report consolidation savings separately; they overlap the per-element estimates.
- Don't recommend split refresh (history + recent, unioned) by default; see the playbook's
  prerequisites.
- Real outputs (inventories, scans, reports) are confidential and never go into this
  repository. Examples and tests use synthetic data only.
