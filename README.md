# Sigma Performance & Cost Advisor

Find the Sigma workloads driving Snowflake cost, measure whether existing
materializations are actually serving reads, and recommend the least expensive option
that still meets latency and freshness goals. Analysis is read-only; remediation is
explicit and opt-in.

It answers: *"How do we keep Sigma fast while spending less—and which materializations
should we keep, retune, investigate, remove, or add?"*

> Works as a [Claude Code / Agent skill](https://docs.claude.com/en/docs/claude-code)
> (via `SKILL.md`) **and** as a standalone CLI (Python 3 stdlib + the `snow` CLI). The
> cost-analysis SQL also runs natively in **Snowflake / Cortex Code** — see below.

<p align="center"><i>Sample report (synthetic data): <a href="example/sample-report.html"><code>example/sample-report.html</code></a></i></p>

## How it works

Sigma stamps every warehouse query's `QUERY_TAG` with `Sigma Σ {sourceUrl, email, kind}`.
The advisor joins that metadata to:

- `QUERY_ATTRIBUTION_HISTORY` for real per-query compute credits;
- `ACCESS_HISTORY` to match materialization-created objects to subsequent reads;
- `WAREHOUSE_METERING_HISTORY` for warehouse idle context; and
- Sigma schedules, elements, and lineage for control and dependency safeguards.

Every opportunity includes evidence, confidence, performance impact, savings range, and
a validation step. A materialization is never marked safe to remove from refresh cost
alone; removal requires a measured no-materialization baseline.

## Decisions
- **Keep** — observed reads justify refresh cost and cached p95 meets the target.
- **Retune** — useful, but refreshing more often than usage/source updates require.
- **Investigate** — low observed utilization, control risk, incomplete lineage, or no read
  evidence in this input path.
- **Remove candidate** — only after a measured counterfactual stays within the latency SLO.
- **Add candidate** — repeated live workload where refresh TCO is likely lower.
- **Optimize query/model** — lower the per-run floor before adding more caching.
- **Consolidate into data model** — the same logic is built or queried in several
  workbooks; build it once in a data model and repoint the elements.

Every decision carries a High/Medium/Low impact tier and short fix steps written for a
workbook owner. Served reads include exports and screenshots as well as interactive queries.

Observed read matching currently covers workbook materializations. Data-model
materializations remain `Investigate` until their schedule and downstream-consumer
lineage are resolved; the advisor does not label them unused from missing workbook matches.

## Intake
Configure the decision constraints in `advisor-config.json`:
1. **Latency SLO** and acceptable slowdown.
2. **Freshness requirement** (real-time disables add-materialization advice).
3. **Primary goal**: cost, latency, or performance per dollar.
4. **Minimum monthly savings** and utilization thresholds.

**$/credit is auto-detected** from `SNOWFLAKE.ORGANIZATION_USAGE` (effective rate); only
asked if that's unavailable. Persist answers in `advisor-config.json` (see
`advisor-config.example.json`) and pass `--config`.

## Quick start (CLI)
Requires Python 3.8+ (stdlib only) and the [`snow` CLI](https://docs.snowflake.com/en/developer-guide/snowflake-cli/index)
with read access to `SNOWFLAKE.ACCOUNT_USAGE`.

```bash
# analyze (read-only)
python3 scripts/analyze.py --conn <snow-conn> --config advisor-config.json --out out

# Optional Sigma enrichment (schedules, controls, lineage)
export SIGMA_BASE_URL=https://<region-api-host>
export SIGMA_CLIENT_ID=... SIGMA_CLIENT_SECRET=...
python3 scripts/enrich.py --inv out/inventory.json

# report (or one report per workbook owner + owners.csv)
python3 scripts/render-html.py --inv out/inventory.json --out out/report.html --customer "Acme"
python3 scripts/render-html.py --inv out/inventory.json --by-owner out/owners --customer "Acme"

# measure realized savings 2+ weeks after changes (re-run analyze + enrich into out2 first)
python3 scripts/compare.py --before out/inventory.json --after out2/inventory.json --out out/compare
python3 scripts/render-savings.py --compare out/compare/compare.json --out out/savings.html --customer "Acme"
python3 scripts/render-savings.py --compare out/compare/compare.json --by-owner out/savings-owners

# remediate (opt-in) — needs Sigma REST creds:
eval "$(/path/to/get-token.sh)"          # sets SIGMA_API_TOKEN + SIGMA_BASE_URL
python3 scripts/materialize.py list   --workbook <workbookId>                        # find the elementId
python3 scripts/materialize.py create --workbook <workbookId> --element-id <elementId> --cron "0 0 * * *" --dry-run
python3 scripts/materialize.py run    --workbook <workbookId> --sheet-id <sheetId>    # refresh + poll
python3 scripts/materialize.py delete --workbook <workbookId> --element-id <elementId> # destructive, confirms unless --yes
```

## Running in Snowflake / Cortex Code
The cost analysis is portable SQL — no external CLI needed, and the data never leaves the
account:

1. Run the four files in `sql/`, setting their `days`, `org`, and `min_runs` variables.
2. Export the result sets into one JSON bundle:
   `{"workload":[],"coverage":[],"materializations":[],"warehouses":[]}`.
3. Render the bundle:
   ```bash
   python3 scripts/analyze.py --from-rows bundle.json --goal performance_per_dollar --out out
   python3 scripts/render-html.py --inv out/inventory.json --out out/report.html --customer "Acme"
   ```

The heavy lifting stays in-warehouse while Sigma enrichment runs only where REST access
is available. Include `"duplicates": []` from `sql/sigma_duplicates.sql` in the bundle to
get duplicate-work findings.

## Without Snowflake access
Teams whose users can't query `ACCOUNT_USAGE` have two other paths. A central team can
also run the Snowflake path and send each owner their own report with `--by-owner`.

```bash
# 1. Cost table: an export from any internal cost tool (CSV or JSON)
#    columns: workbook_id | data_model_id | object, credits | cost,
#    optional element_id, date, query_count, avg_sec, p95_sec, kind, owner_email
python3 scripts/analyze.py --from-cost-table costs.csv --credit-price 3 --out out

# 2. Sigma only: duplicate logic, schedules, refreshes/day, owners (no cost data)
python3 scripts/scan_workbooks.py --out scan --name-contains "Sales"
python3 scripts/analyze.py --sigma-only --scan scan/scan.json --out out
```

Neither path can see served reads, so materializations go to **Investigate** rather than
Retune or Remove. Add `--scan scan.json` to the Snowflake path too: a materialized
element's compiled SQL only reads its own write-back table, so duplicate logic behind
materializations is found from the workbook definition instead.

## Scripts
| File | Purpose |
|---|---|
| `scripts/analyze.py` | cost/performance evidence (any input path) → v2 inventory + Markdown report |
| `scripts/cost_model.py` | pure keep/retune/investigate/remove/add/consolidate decision engine |
| `scripts/scan_workbooks.py` | read-only Sigma API scan for duplicate logic, schedules, owners |
| `scripts/enrich.py` | read-only Sigma schedule/control/lineage/owner enrichment |
| `scripts/materialize.py` | explicit schedule CRUD and refresh actions (`--dry-run`) |
| `scripts/render-html.py` | `inventory.json` → `report.html`, or per-owner reports |
| `scripts/compare.py` | before/after inventories → realized savings + follow-through (`compare.json`, CSVs, `COMPARE.md`) |
| `scripts/render-savings.py` | `compare.json` → `savings.html` (what was saved, what was acted on, by owner, still open) |
| `sql/*.sql` | workload, coverage, materialization ROI, duplicates, and warehouse context |
| `example/make-sample.py` | regenerate the synthetic example |

## Privacy & safety
Analysis (`analyze.py`, `enrich.py`, `render-html.py`) is read-only by construction.
Remediation is explicit and opt-in: `materialize.py create` / `update` / `delete` manage
a schedule's cron cadence and `materialize.py run` triggers an on-demand refresh — all four
only act when invoked with a specific `--element-id` or `--sheet-id` and object ID.
`delete` is destructive (cancels all future runs, no undo) and prompts for confirmation
unless you pass `--yes`. See [`PRIVACY.md`](PRIVACY.md). The committed
[`example/`](example/) is **fully synthetic** (fictional "Northwind Trading Co.") — no real
data. Real outputs are customer-confidential; `.gitignore` keeps `*-out/` and creds out of
git.

## Contributing
This repository is public. Never commit customer names, people, internal tool names, real
table or account names, or numbers from a real account, including in commit messages and PR
text. Tests and examples use synthetic data only. Enable the denylist hooks once per clone:

```bash
git config core.hooksPath .githooks
```

The hooks check staged changes and commit messages against a private term list kept
**outside** the repository (`~/.config/sigma-performance-advisor/denylist.txt`, or
`$SPA_DENYLIST`), so the list itself is never published. Run
`.githooks/check-denylist.sh --tree` to sweep every tracked file, or `--file <path>` on a PR
body before posting it.

## License
MIT — see [`LICENSE`](LICENSE).
