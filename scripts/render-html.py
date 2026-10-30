#!/usr/bin/env python3
"""inventory.json -> customer-facing Sigma performance-and-cost report.
   python3 scripts/render-html.py --inv out/inventory.json --out out/report.html --customer "Acme"
   python3 scripts/render-html.py --inv out/inventory.json --out out/mine.html --owner analyst@example.com
   python3 scripts/render-html.py --inv out/inventory.json --by-owner out/owners   # one report per owner + owners.csv
"""
import argparse, csv, html, json, datetime, os


def esc(s):
    return html.escape(str(s), quote=True)


def num(x, fmt=",.2f", dash="-"):
    try:
        return format(float(x), fmt)
    except (TypeError, ValueError):
        return dash


def owner_of(email):
    return (email or "").strip().lower()


def group_owners(g):
    return {owner_of(m.get("OWNER_EMAIL")) for m in g.get("MEMBERS", []) if owner_of(m.get("OWNER_EMAIL"))}


def obj_label(o):
    return (o or "").replace("/workbook/", "wb: ").replace("/data-model/", "dm: ").replace("/report/", "rpt: ")


def obj_link(o, url):
    lab = esc(obj_label(o))
    return f"<a href='{esc(url)}' target='_blank' rel='noopener'>{lab}</a>" if url else lab


def steps_html(steps):
    if not steps:
        return ""
    return "<ol class='steps'>" + "".join(f"<li>{esc(x)}</li>" for x in steps) + "</ol>"


def impact_pill(imp):
    if imp not in ("High", "Medium", "Low"):
        return ""
    return f"<span class='imp {imp.lower()}' title='Impact tier'>{imp} impact</span> "


def savings_range(r):
    return (f"${num(r.get('SAVINGS_LOW'), ',.0f')} to ${num(r.get('SAVINGS_HIGH'), ',.0f')}/mo "
            f"(expected ${num(r.get('SAVINGS_EXPECTED'), ',.0f')})")


def has_cost(g, evidence):
    if evidence == "sigma-only":
        return False
    return bool((g.get("TOTAL_CREDITS") or 0) > 0 or (g.get("SAVINGS_HIGH") or 0) > 0
                or any((m.get("CREDITS") or 0) > 0 for m in g.get("MEMBERS", [])))


def downstream_html(g):
    n = g.get("DOWNSTREAM_COUNT") or 0
    if n <= 0:
        return ""
    names = ", ".join(esc(d.get("NAME") or d.get("ELEMENT_ID") or "") for d in g.get("DOWNSTREAM_ELEMENTS") or [])
    return f"<div class='why'>{esc(n)} downstream elements follow this logic: {names}</div>"


def dup_section(groups, evidence):
    if not groups:
        return ""
    out = ("<h2>Duplicate work</h2><p class='note'>The same logic is built or queried in more than one "
           "place. Consolidation savings overlap the per-element estimates above, so they are reported "
           "separately and never added to the headline savings.</p>")
    low = [g for g in groups if g.get("LOW_VALUE")]
    for g in [g for g in groups if not g.get("LOW_VALUE")]:
        mem = "".join(
            f"<tr><td class='obj'>{obj_link(m.get('OBJECT'), m.get('SAMPLE_URL'))}</td>"
            f"<td>{esc(m.get('ELEMENT', ''))}</td><td class='num'>{esc(m.get('RUNS', ''))}</td>"
            f"<td class='num'>{num(m.get('CREDITS'), '.4f')}</td>"
            f"<td class='num'>{num(m.get('REFRESHES_PER_DAY'), '.1f')}</td>"
            f"<td>{esc(m.get('OWNER_EMAIL') or '')}</td></tr>" for m in g.get("MEMBERS", []))
        base = ", ".join(f"<code>{esc(b)}</code>" for b in g.get("BASE_TABLES", [])) or "not resolved"
        money = (f"Savings range <b>{esc(savings_range(g))}</b>" if has_cost(g, evidence)
                 else "<b>No cost data</b> for this group (ranked by refreshes and member count)")
        out += (f"<div class='card'><h3>{impact_pill(g.get('IMPACT'))}{esc(g.get('GROUP_ID', ''))} "
                f"<span class='pill monitor'>{esc(g.get('MATCH', ''))}</span> "
                f"<span class='note'>{esc(g.get('MEMBER_COUNT', len(g.get('MEMBERS', []))))} members · "
                f"source: {esc(g.get('SOURCE', ''))}"
                + (f" · confidence: {esc(g['CONFIDENCE'])}" if g.get("CONFIDENCE") else "") + "</span></h3>"
                f"<div class='why'>{money}</div><div class='why'>Base tables: {base}</div>"
                + downstream_html(g) +
                f"<table><thead><tr><th>Object</th><th>Element</th><th class='num'>Runs</th>"
                f"<th class='num'>Credits</th><th class='num'>Refreshes/day</th><th>Owner</th></tr></thead>"
                f"<tbody>{mem}</tbody></table>"
                f"<div class='why'>{esc(g.get('WHY', ''))}</div>"
                f"{steps_html(g.get('FIX_STEPS'))}"
                + (f"<div class='why'><b>Validate:</b> {esc(g['VALIDATION'])}</div>" if g.get("VALIDATION") else "")
                + "</div>")
    if low:
        rows = "".join(
            f"<tr><td>{esc(g.get('GROUP_ID', ''))}</td><td>{esc(g.get('MATCH', ''))}</td>"
            f"<td class='num'>{esc(len({m.get('OBJECT') for m in g.get('MEMBERS', [])}))}</td>"
            f"<td class='num'>{esc(g.get('MEMBER_COUNT', len(g.get('MEMBERS', []))))}</td>"
            f"<td class='num'>{num(g.get('TOTAL_CREDITS'), '.4f')}</td></tr>" for g in low)
        out += (f"<div class='card'><h3>{esc(len(low))} more duplicate groups with no measurable repeat cost</h3>"
                "<div class='why'>Often throwaway copies. Archiving unused copies is usually enough.</div>"
                "<table><thead><tr><th>Group</th><th>Match</th><th class='num'>Workbooks</th>"
                "<th class='num'>Elements</th><th class='num'>Credits</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></div>")
    return out


EVIDENCE_FOOTER = {
    "cost-table": ("Read-only analysis of a supplied cost table plus Sigma REST metadata. "
                   "Warehouse views such as <code>QUERY_HISTORY</code> and <code>ACCESS_HISTORY</code> were not used"),
    "sigma-only": ("Read-only scan of Sigma workbooks through the Sigma REST API. No warehouse or cost data "
                   "was used"),
}
EVIDENCE_LABEL = {
    "snowflake": "Snowflake query and attribution history",
    "rows": "Pre-exported Snowflake rows",
    "cost-table": "Supplied cost table",
    "sigma-only": "Sigma workbook scan only (no cost data)",
}


def banner_html(inv):
    ev, notes = inv.get("evidence"), inv.get("evidence_notes") or []
    if not ev and not notes:
        return ""
    lis = "".join(f"<li>{esc(n)}</li>" for n in notes)
    return (f"<div class='banner'><b>Evidence:</b> {esc(EVIDENCE_LABEL.get(ev, ev or 'unspecified'))}"
            + (f"<ul>{lis}</ul>" if lis else "") + "</div>")


def filter_inv(inv, owner):
    """Owner view: candidates and groups for one owner; owner=='' selects the unassigned bucket."""
    o = owner_of(owner)
    cands = [c for c in inv["candidates"] if owner_of(c.get("OWNER_EMAIL")) == o]
    groups = [g for g in inv.get("duplicate_groups", [])
              if (o in group_owners(g) if o else not group_owners(g))]
    return cands, groups


def render(inv, customer, owner=None):
    t = inv.get("totals", {})
    cands = inv["candidates"]
    groups = inv.get("duplicate_groups") or []
    evidence = inv.get("evidence")
    gen = inv.get("generated", "")[:10] or datetime.date.today().isoformat()
    cfg = inv.get("config", {})
    dollars = t.get("dollars")
    coverage = t.get("credit_coverage_pct")
    action_counts = t.get("actions", {})
    sub_extra = ""
    if owner is not None:
        cands, groups = filter_inv(inv, owner)
        price = cfg.get("credit_price") or 0
        cr = sum(c.get("TOTAL_CREDITS", c.get("CREDITS", 0)) or 0 for c in cands)
        sav = sum(c.get("SAVINGS_EXPECTED", 0) or 0 for c in cands)
        label = owner or "unassigned"
        sub_extra = f" · owner: {label}"
        kpis = [
            ("Objects in view", f"{len(cands)}", f"assigned to {label}"),
            ("Compute cost", f"${cr * price:,.0f}" if price else f"{cr:.2f} cr",
             f"{cr:.1f} credits @ ${cfg.get('credit_price','?')}"),
            ("Modeled savings", f"${sav:,.0f}/mo", "owner's objects only"),
        ]
        coverage_note = ("Owner view: figures cover only this owner's objects, not whole-scope "
                         "attribution coverage.")
    else:
        kpis = [
            ("Sigma queries", f"{t.get('queries', 0):,}", f"last {inv['days']} days"),
            ("Compute cost", f"${dollars:,.0f}" if dollars else f"{t.get('credits', 0):.2f} cr",
             f"{t.get('credits', 0):.1f} credits @ ${cfg.get('credit_price','?')}"),
            ("Attribution coverage", f"{coverage:.1f}%" if coverage is not None else "unknown",
             "Sigma credits mapped to a source URL"),
            ("Modeled savings", f"${t.get('estimated_monthly_savings',0):,.0f}/mo",
             f"{sum(action_counts.values())} reviewed opportunities"),
        ]
        coverage_note = (f"Coverage is <b>{esc(coverage if coverage is not None else 'unknown')}%</b> "
                         "of Sigma query credits.")
    cons = t.get("consolidation_savings_expected")
    if owner is not None and groups:
        cons = sum(g.get("SAVINGS_EXPECTED", 0) or 0 for g in groups)
    if cons is not None and groups:
        kpis.append(("Consolidation (separate)", f"${cons:,.0f}/mo",
                     f"{len(groups)} duplicate groups, not in modeled savings"))
    kpi_html = "".join(
        f"<div class='kpi'><div class='kpi-v'>{esc(v)}</div><div class='kpi-l'>{esc(l)}</div>"
        f"<div class='kpi-s'>{esc(s)}</div></div>" for l, v, s in kpis)
    rows = ""
    for i, c in enumerate(cands[:25], 1):
        obj_cell = obj_link(c["OBJECT"], c.get("SAMPLE_URL"))
        rec = c.get("ACTION") or c.get("RECOMMENDATION")
        cls = ("keep" if rec.startswith("Keep") else
               "remove" if rec.startswith("Remove") else
               "retune" if rec.startswith("Retune") else
               "investigate" if rec.startswith("Investigate") else
               "improve" if rec.startswith("Optimize") else
               "add" if rec.startswith("Add") else
               "consolidate" if rec.startswith("Consolidate") else "monitor")
        grp = (f"<div class='why'>Part of duplicate group {esc(c['CONSOLIDATION_GROUP'])}</div>"
               if c.get("CONSOLIDATION_GROUP") else "")
        rows += (f"<tr><td>{i}</td><td class='obj'>{obj_cell}</td><td>{esc(c['ELEMENT'])}</td>"
                 f"<td class='num'>{c.get('RUNS',0)}</td>"
                 f"<td class='num'>{c.get('TOTAL_CREDITS',c.get('CREDITS',0)):.4f}</td>"
                 f"<td class='num'>{c.get('P95_SEC',0):.2f}</td>"
                 f"<td class='num'>{c.get('MATERIALIZATION_RUNS',0)}</td>"
                 f"<td class='num'>{c.get('MATCHED_MATERIALIZED_READS',0)}</td>"
                 f"<td>{impact_pill(c.get('IMPACT'))}<span class='pill {cls}'>{esc(rec)}</span>"
                 f"<div class='why'>{esc(c['WHY'])}</div>"
                 f"{steps_html(c.get('FIX_STEPS'))}{grp}"
                 f"<div class='why'><b>Validate:</b> {esc(c.get('VALIDATION',''))}</div></td></tr>")
    footer = EVIDENCE_FOOTER.get(evidence) or (
        "Read-only analysis: <code>QUERY_HISTORY</code>, <code>QUERY_ATTRIBUTION_HISTORY</code>, "
        "<code>ACCESS_HISTORY</code>, and <code>WAREHOUSE_METERING_HISTORY</code>")
    return PAGE.format(customer=esc(customer), gen=esc(gen), scope=esc(inv["scope"]) + esc(sub_extra),
                       days=inv["days"], kpis=kpi_html, rows=rows, banner=banner_html(inv),
                       kpi_cls=" k5" if len(kpis) == 5 else (" k3" if len(kpis) == 3 else ""),
                       dup=dup_section(groups, evidence), footer=footer, coverage_note=coverage_note)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sigma Performance &amp; Cost — {customer}</title><style>
:root{{--ink:#1a2233;--mut:#5b6779;--line:#e6e9f0;--bg:#f6f8fb;--accent:#2f6f4f;--mat:#2f9e44;--rev:#868e96;}}
*{{box-sizing:border-box}}body{{margin:0;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Arial,sans-serif;color:var(--ink);background:var(--bg)}}
.wrap{{max-width:980px;margin:0 auto;padding:0 28px 80px}}
header{{background:linear-gradient(135deg,#173a2b,#2f9e44);color:#fff;padding:48px 0 96px}}
header .wrap{{padding-bottom:0}}.eyebrow{{text-transform:uppercase;letter-spacing:.14em;font-size:12px;opacity:.85;margin:0}}
header h1{{font-size:30px;margin:8px 0 4px}}header .sub{{opacity:.92}}
.ro{{display:inline-block;margin-top:16px;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.3);padding:5px 12px;border-radius:20px;font-size:12.5px}}
.kpis{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:-52px 0 0}}.kpis.k5{{grid-template-columns:repeat(5,minmax(0,1fr))}}.kpis.k3{{grid-template-columns:repeat(3,minmax(0,1fr))}}
.kpi{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;box-shadow:0 6px 20px rgba(20,30,60,.06)}}
.kpi-v{{font-size:24px;font-weight:700;color:var(--accent);overflow-wrap:anywhere}}.kpi-l{{font-size:13px;font-weight:600;margin-top:2px}}.kpi-s{{font-size:12px;color:var(--mut)}}
h2{{font-size:20px;margin:40px 0 8px;padding-bottom:8px;border-bottom:2px solid var(--line)}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:13px;margin-top:12px}}
th,td{{padding:9px 11px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}
th{{background:#eef3f0;font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}td.obj{{font-weight:600}}
tr:last-child td{{border-bottom:none}}
.pill{{font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px;color:#fff;white-space:nowrap}}
.pill.keep{{background:#2f9e44}}.pill.add,.pill.improve{{background:#1971c2}}
.pill.retune{{background:#e67700}}.pill.investigate{{background:#f08c00}}
.pill.remove{{background:#c92a2a}}.pill.monitor{{background:var(--rev)}}.pill.consolidate{{background:#7048e8}}
.imp{{font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:6px;border:1px solid;white-space:nowrap}}
.imp.high{{color:#c92a2a;border-color:#c92a2a}}.imp.medium{{color:#e67700;border-color:#e67700}}.imp.low{{color:var(--mut);border-color:var(--line)}}
ol.steps{{margin:6px 0 0;padding-left:18px;font-size:12px;color:var(--ink)}}
.banner{{background:#fff8e6;border:1px solid #f3d98b;border-radius:10px;padding:10px 16px;margin-top:22px;font-size:13px}}.banner ul{{margin:4px 0 0;padding-left:20px}}
.why{{color:var(--mut);font-size:11.5px;margin-top:4px}}
.card{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:18px 22px;margin-top:14px}}
.card h3{{margin:0 0 6px;font-size:16px}}.opt{{border-left:4px solid var(--mat);padding-left:14px;margin:12px 0}}
.opt.a{{border-left-color:#1971c2}}code{{background:#eef1f8;padding:1px 5px;border-radius:4px;font-family:"SF Mono",Menlo,Consolas,monospace;font-size:12px}}
.note{{color:var(--mut);font-size:12.5px}}footer{{color:var(--mut);font-size:12.5px;margin-top:36px;border-top:1px solid var(--line);padding-top:14px}}
code{{overflow-wrap:anywhere}}
@media (max-width:860px){{.wrap{{padding:0 16px 60px}}.kpis,.kpis.k5,.kpis.k3{{grid-template-columns:repeat(2,minmax(0,1fr))}}
table{{display:block;overflow-x:auto}}}}
</style></head><body>
<header><div class="wrap"><p class="eyebrow">Sigma · Performance per Dollar</p>
<h1>{customer}</h1><div class="sub">Fast results at the lowest sustainable cost · scope: {scope} · last {days} days</div>
<span class="ro">🔒 Read-only analysis — no changes made to Snowflake or Sigma</span></div></header>
<div class="wrap">
<div class="kpis{kpi_cls}">{kpis}</div>
{banner}
<h2>Recommended decisions</h2>
<p class="note">{coverage_note} Recommendations compare
query cost and latency with observed materialization refreshes and reads. A schedule is never
marked safe to remove from refresh cost alone.</p>
<table><thead><tr><th>#</th><th>Object</th><th>Element</th><th class="num">Runs</th><th class="num">Credits</th>
<th class="num">p95 s</th><th class="num">Refreshes</th><th class="num">Matched reads</th><th>Decision</th></tr></thead>
<tbody>{rows}</tbody></table>
{dup}
<h2>Decision policy</h2>
<div class="card">
<div class="opt a"><h3>Optimize before caching</h3>Fix pruning, spill, joins, calculations,
and warehouse pressure when a single run is intrinsically expensive.</div>
<div class="opt"><h3>Keep, retune, investigate, remove, or add</h3>Use observed served reads per
refresh and p95 latency. Controls or incomplete lineage require review. Removal requires a measured
no-materialization baseline and a rollback plan.</div>
</div>
<footer>{footer}. Prepared for {customer}.</footer>
</div></body></html>"""


def owner_reports(inv, customer, outdir):
    os.makedirs(outdir, exist_ok=True)
    owners = sorted({owner_of(c.get("OWNER_EMAIL")) for c in inv["candidates"] if owner_of(c.get("OWNER_EMAIL"))}
                    | {o for g in inv.get("duplicate_groups", []) for o in group_owners(g)})
    rows = []
    for n, o in enumerate(owners + [""], 1):
        cands, groups = filter_inv(inv, o)
        if not o and not cands and not groups:
            continue
        fn = f"report-{n}.html" if o else "report-unassigned.html"
        with open(os.path.join(outdir, fn), "w") as f:
            f.write(render(inv, customer, owner=o))
        rows.append([o or "unassigned", fn, len(cands), len(groups),
                     round(sum(c.get("SAVINGS_EXPECTED", 0) or 0 for c in cands), 2)])
    with open(os.path.join(outdir, "owners.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["owner_email", "file", "candidates", "groups", "expected_savings"])
        w.writerows(rows)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inv", required=True)
    ap.add_argument("--out", help="output html (required unless --by-owner)")
    ap.add_argument("--customer", default="the organization")
    ap.add_argument("--owner", metavar="EMAIL", help="render only this owner's candidates and groups")
    ap.add_argument("--by-owner", metavar="DIR",
                    help="write one report per owner (report-<n>.html) plus owners.csv into DIR")
    a = ap.parse_args()
    if not a.out and not a.by_owner:
        ap.error("--out is required unless --by-owner is given")
    if a.owner and a.by_owner:
        ap.error("--owner and --by-owner are mutually exclusive")
    inv = json.load(open(a.inv))
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        open(a.out, "w").write(render(inv, a.customer, owner=a.owner))
        print("wrote", a.out)
    if a.by_owner:
        rows = owner_reports(inv, a.customer, a.by_owner)
        print(f"wrote {len(rows)} owner reports + owners.csv to", a.by_owner)


if __name__ == "__main__":
    main()
