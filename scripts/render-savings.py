#!/usr/bin/env python3
"""compare.json -> customer-facing savings report (what changed, what it saved, what is open).
   python3 scripts/render-savings.py --compare out/compare/compare.json --out out/savings.html --customer "Acme"
   python3 scripts/render-savings.py --compare out/compare/compare.json --out out/mine.html --owner analyst@example.com
   python3 scripts/render-savings.py --compare out/compare/compare.json --by-owner out/owners
Reuses render-html.py's page CSS and helpers. Stdlib only.
"""
import argparse, csv, importlib.util, json, os, re

_spec = importlib.util.spec_from_file_location(
    "render_html", os.path.join(os.path.dirname(os.path.abspath(__file__)), "render-html.py"))
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)
esc, num, owner_of, obj_link = rh.esc, rh.num, rh.owner_of, rh.obj_link

# render-html.py keeps its CSS inside a str.format template (doubled braces); unescape it
# so the two reports share one stylesheet instead of drifting apart.
BASE_CSS = (re.search(r"<style>(.*?)</style>", rh.PAGE, re.S).group(1)
            .replace("{{", "{").replace("}}", "}"))
EXTRA_CSS = """
.pill.acted-on{background:#2f9e44}.pill.partly{background:#74b816}.pill.improved{background:#1971c2}
.pill.reverted{background:#c92a2a}.pill.not-yet{background:#e67700}.pill.not-observed{background:var(--rev)}
td.wrap-text{min-width:220px}
.tbl{overflow-x:auto;max-width:100%}.tbl table{margin-top:0}
.banner.warn{background:#fff3f0;border-color:#f1b5a8}
@media (max-width:860px){.tbl table{display:table;min-width:640px}}
"""
MAX_IDLE_ROWS = 15
ORDER = {"acted-on": 0, "partly": 1, "improved": 2, "reverted": 3, "not-yet": 4, "not-observed": 5}
LABEL = {"acted-on": "Acted on", "partly": "Partly done", "improved": "Improved",
         "reverted": "Reverted", "not-yet": "Not yet", "not-observed": "Not observed"}
COUNTED = ("acted-on", "partly")


def money(x, dec=0):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "-"
    return f"{'-' if x < 0 else ''}${abs(x):,.{dec}f}"


def kpi(label, value, sub):
    return (f"<div class='kpi'><div class='kpi-v'>{esc(value)}</div><div class='kpi-l'>{esc(label)}"
            f"</div><div class='kpi-s'>{esc(sub)}</div></div>")


def table(head, body):
    th = "".join(f"<th{' class=num' if c.startswith('>') else ''}>{esc(c.lstrip('>'))}</th>"
                 for c in head)
    return f"<div class='tbl'><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>"


def banners(res, ft):
    ms, out = res.get("measurement") or {}, []
    if "too_soon" in ms and ms["too_soon"] is None:
        out.append("Measurement interval unknown: a generated timestamp is missing from one of "
                   "the inventories, so the too-soon check was skipped.")
    elif ms.get("too_soon"):
        out.append(f"Measured {ms.get('days_between')} days after the baseline; wait at least "
                   f"{ms.get('too_soon_days', 14):g} days (2 weeks) for a reliable read.")
    if ms and not ms.get("same_window", True):
        out.append(f"The two runs used different windows (before {ms.get('before_days')} days, "
                   f"after {ms.get('after_days')} days). Figures are normalised to 30 days but "
                   "not strictly comparable.")
    out += res.get("warnings") or []
    if ft and not ft.get("schedule_data"):
        out.append("Neither inventory carries schedule data, so refresh follow-through was "
                   "judged from observed refresh runs rather than from the schedules.")
    if not out:
        return ""
    return "<div class='banner'><ul>" + "".join(f"<li>{esc(x)}</li>" for x in out) + "</ul></div>"


def link(r):
    if r.get("GROUP_ID"):
        n = r.get("MEMBER_COUNT") or len(r.get("MEMBERS") or [])
        lab = f"Duplicate group {r['GROUP_ID']} ({n} members)"
        url = r.get("SAMPLE_URL")
        return (f"<a href='{esc(url)}' target='_blank' rel='noopener'>{esc(lab)}</a>" if url
                else esc(lab))
    el = f"<div class='why'>{esc(r['ELEMENT'])}</div>" if r.get("ELEMENT") else ""
    return obj_link(r.get("OBJECT"), r.get("SAMPLE_URL")) + el


def changes_section(ft, rows):
    if not rows:
        return ("<h2>What changed</h2><p class='note'>No actionable recommendations from the "
                "baseline were tracked.</p>")
    rows = sorted(rows, key=lambda r: (ORDER.get(r["STATUS"], 9), -(r.get("SAVINGS") or 0),
                                       str(r.get("OBJECT") or "")))
    # Everything that changed is shown; untouched items are capped so they don't bury it.
    idle = [r for r in rows if r["STATUS"] in ("not-yet", "not-observed")]
    hidden = idle[MAX_IDLE_ROWS:]
    hidden_ids = {id(r) for r in hidden}
    rows = [r for r in rows if id(r) not in hidden_ids]
    body = "".join(
        f"<tr><td><span class='pill {esc(r['STATUS'])}'>"
        f"{esc(LABEL.get(r['STATUS'], r['STATUS']))}</span></td><td class='obj'>{link(r)}</td>"
        f"<td>{esc(r.get('RECOMMENDED', ''))}</td><td class='wrap-text'>{esc(r.get('DETAIL', ''))}"
        f"<div class='why'>Evidence: {esc(r.get('EVIDENCE', 'none'))}</div></td>"
        f"<td class='num'>{money(r.get('SAVINGS'))}</td><td>{esc(r.get('OWNER_EMAIL') or '')}</td>"
        "</tr>" for r in rows)
    cnt = ", ".join(f"{v} {LABEL.get(k, k).lower()}" for k, v in sorted(
        (ft.get("counts") or {}).items(), key=lambda kv: ORDER.get(kv[0], 9)))
    more = (f"<p class='note'>{esc(len(hidden))} more not-yet or not-observed recommendations "
            "are listed in <code>follow-through.csv</code>.</p>" if hidden else "")
    return ("<h2>What changed</h2><p class='note'>Each row is a recommendation from the baseline "
            f"report and what happened to it. Counts: {esc(cnt)}.</p>"
            + table(["Status", "Object", "Recommended", "What happened", ">$/mo", "Owner"], body)
            + more)


def breakdown_section(res, ft):
    t = res.get("totals") or {}
    rows = [("Read efficiency", t.get("headline_efficiency_savings")),
            ("Refresh cost", t.get("headline_refresh_savings"))]
    out = ("<h2>Savings breakdown</h2><p class='note'>Counted savings by source. Objects whose "
           "traffic shifted are excluded from read efficiency.</p>"
           + table(["Source", ">$/mo"], "".join(
               f"<tr><td>{esc(a)}</td><td class='num'>{money(b, 2)}</td></tr>" for a, b in rows)
               + f"<tr><td><b>Headline</b></td><td class='num'><b>{money(t.get('headline_savings'), 2)}"
               "</b></td></tr>"))
    if ft:
        parts = [("Tied to actions taken (acted on or partly)", ft.get("savings_acted_on")),
                 ("Improved, cause not verified", ft.get("savings_improved")),
                 ("Other changes, not tied to a recommendation", ft.get("savings_other"))]
        out += ("<p class='note'>The same headline split by whether a recommendation explains it."
                "</p>" + table(["Attribution", ">$/mo"], "".join(
                    f"<tr><td>{esc(a)}</td><td class='num'>{money(b, 2)}</td></tr>"
                    for a, b in parts)
                    + f"<tr><td><b>Headline</b></td><td class='num'><b>"
                    f"{money(ft.get('headline_savings'), 2)}</b></td></tr>"))
    n = t.get("traffic_shift_objects") or 0
    out += (f"<p class='note'><b>{esc(n)}</b> objects had a traffic shift and were excluded from "
            f"read efficiency ({esc(money(t.get('excluded_traffic_shift_savings'), 2))}/mo "
            "excluded).</p>")
    reg = [o for o in res.get("objects") or [] if o.get("LATENCY_REGRESSION")]
    if reg:
        body = "".join(
            f"<tr><td class='obj'>{obj_link(o.get('OBJECT'), o.get('SAMPLE_URL'))}"
            f"<div class='why'>{esc(o.get('ELEMENT') or '')}</div></td>"
            f"<td class='num'>{num(o.get('P95_BEFORE'))}</td><td class='num'>{num(o.get('P95_AFTER'))}"
            "</td></tr>" for o in reg)
        out += (f"<div class='banner warn'><b>{len(reg)} latency regression"
                f"{'s' if len(reg) != 1 else ''}:</b> p95 is now above the latency target and "
                "worse than before. Check these before counting the savings as safe.</div>"
                + table(["Object", ">p95 s before", ">p95 s after"], body))
    return out


def owner_section(ft):
    by = ft.get("by_owner") or {}
    if not by:
        return ""
    body = "".join(
        f"<tr><td>{esc(o)}</td><td class='num'>{esc(v['recommendations'])}</td>"
        f"<td class='num'>{esc(v['acted_on'])}</td><td class='num'>{esc(v['not_yet'])}</td>"
        f"<td class='num'>{money(v['savings_acted_on'])}</td>"
        f"<td class='num'>{money(v['savings_total'])}</td></tr>"
        for o, v in sorted(by.items(), key=lambda kv: (-kv[1]["savings_acted_on"], kv[0])))
    return ("<h2>By owner</h2>" + table(
        ["Owner", ">Recommendations", ">Acted on", ">Not yet", ">$/mo from actions", ">$/mo total"],
        body))


def open_section(rem, owner=None):
    top = [r for r in rem.get("top") or []
           if owner is None or owner_of(r.get("OWNER_EMAIL")) == owner_of(owner)]
    if not top:
        return ""
    body = "".join(
        f"<tr><td class='obj'>{link(r)}</td><td>{esc(r.get('ACTION', ''))}"
        f"{' ' + rh.impact_pill(r.get('IMPACT')) if r.get('IMPACT') else ''}</td>"
        f"<td class='num'>{money(r.get('SAVINGS_EXPECTED'))}</td>"
        f"<td>{esc(r.get('OWNER_EMAIL') or '')}</td></tr>" for r in top)
    cons = rem.get("consolidation_savings_expected")
    note = (f" Consolidation (${cons:,.0f}/mo expected) overlaps the per-element figures, so it "
            "is reported separately." if cons else "")
    return ("<h2>Still open</h2><p class='note'>The largest recommendations in the latest "
            f"inventory.{esc(note)}</p>"
            + table(["Object", "Recommendation", ">Expected $/mo", "Owner"], body))


METHOD = ("Savings are attributed to a change because they coincide with it; they are not "
          "proven to be caused by it. Objects whose traffic shifted are excluded from read "
          "efficiency, because their cost moved with usage. An object missing from the later "
          "inventory is not-observed, not removed (inventories list only the top objects). "
          "For a like-for-like read, re-run the advisor over the same scope and window length.")

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sigma Savings Report - @customer@</title><style>@css@</style></head><body>
<header><div class="wrap"><p class="eyebrow">Sigma · Performance per Dollar</p>
<h1>@customer@</h1><div class="sub">Savings since the baseline · scope: @scope@ · @when@</div>
<span class="ro">Read-only analysis. No changes made to Snowflake or Sigma</span></div></header>
<div class="wrap">
<div class="kpis">@kpis@</div>
@banner@
@body@
<footer>@method@ Prepared for @customer@.</footer>
</div></body></html>"""


def fill(**kw):
    return re.sub(r"@(css|customer|scope|when|kpis|banner|body|method)@",
                  lambda m: kw[m.group(1)], PAGE)  # one pass: inserted text is never rescanned


def render(res, customer, owner=None):
    ft, rem, ms = res.get("follow_through"), res.get("remaining") or {}, res.get("measurement") or {}
    t = res.get("totals") or {}
    rows = list((ft or {}).get("rows") or [])
    sub = ""
    if owner is not None:
        label = owner or "unassigned"
        rows = [r for r in rows if owner_of(r.get("OWNER_EMAIL")) == owner_of(owner)]
        done = [r for r in rows if r["STATUS"] in COUNTED]
        kpis = [kpi("Savings from actions taken",
                    money(sum(r.get("SAVINGS") or 0 for r in done)) + "/mo", "owner's changes only"),
                kpi("Recommendations", str(len(rows)), f"assigned to {label}"),
                kpi("Acted on", str(len(done)), "acted on or partly done"),
                kpi("Not yet", str(sum(r["STATUS"] == "not-yet" for r in rows)), "still open")]
        sub = f" · owner: {label}"
    elif ft:
        rate = ft.get("follow_through_rate")
        kpis = [kpi("Realized savings", f"{money(t.get('headline_savings'))}/mo",
                    "coincides with changes; not proven caused"),
                kpi("From actions taken", f"{money(ft.get('savings_acted_on'))}/mo",
                    f"{sum((ft.get('counts') or {}).get(s, 0) for s in COUNTED)} of "
                    f"{ft.get('actionable', 0)} recommendations"),
                kpi("Follow-through rate", "n/a" if rate is None else f"{rate * 100:.0f}%",
                    "acted on or partly done"),
                kpi("Remaining opportunity", f"{money(rem.get('estimated_monthly_savings'))}/mo",
                    "modeled, latest inventory")]
    else:  # compare.json from before follow-through tracking
        kpis = [kpi("Realized savings", f"{money(t.get('headline_savings'))}/mo",
                    "headline, vs the baseline"),
                kpi("Read efficiency", f"{money(t.get('headline_efficiency_savings'))}/mo",
                    "traffic-shifted objects excluded"),
                kpi("Refresh cost", f"{money(t.get('headline_refresh_savings'))}/mo",
                    "materialization builds"),
                kpi("Objects compared", str(t.get("matched_objects", 0)), "seen on both runs")]
    body = ""
    if ft:
        body += changes_section(ft, rows)
    else:
        body += ("<div class='banner'>This comparison was produced before follow-through "
                 "tracking, so recommendations are not tied to savings. Re-run "
                 "<code>compare.py</code> to add it.</div>")
    if owner is None:
        body += breakdown_section(res, ft)
        if ft:
            body += owner_section(ft)
    body += open_section(rem, owner)
    when = (ms.get("after_generated") or "")[:10] or "latest run"
    return fill(css=BASE_CSS + EXTRA_CSS, customer=esc(customer),
                scope=esc(res.get("scope") or "") + esc(sub), when=esc(when),
                kpis="".join(kpis), banner=banners(res, ft), body=body, method=esc(METHOD))


def owner_reports(res, customer, outdir):
    os.makedirs(outdir, exist_ok=True)
    rows = (res.get("follow_through") or {}).get("rows") or []
    owners = sorted({owner_of(r.get("OWNER_EMAIL")) for r in rows if owner_of(r.get("OWNER_EMAIL"))})
    out = []
    for n, o in enumerate(owners + [""], 1):
        mine = [r for r in rows if owner_of(r.get("OWNER_EMAIL")) == o]
        if not mine:
            continue
        fn = f"savings-{n}.html" if o else "savings-unassigned.html"
        with open(os.path.join(outdir, fn), "w") as f:
            f.write(render(res, customer, owner=o))
        out.append([o or "unassigned", fn, sum(r["STATUS"] in COUNTED for r in mine),
                    sum(r["STATUS"] == "not-yet" for r in mine),
                    round(sum(r.get("SAVINGS") or 0 for r in mine if r["STATUS"] in COUNTED), 2)])
    with open(os.path.join(outdir, "owners.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["owner_email", "file", "acted_on", "not_yet", "savings_acted_on"])
        w.writerows(out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", required=True, help="compare.json written by compare.py")
    ap.add_argument("--out", help="output html (required unless --by-owner)")
    ap.add_argument("--customer", default="the organization")
    ap.add_argument("--owner", metavar="EMAIL", help="render only this owner's recommendations")
    ap.add_argument("--by-owner", metavar="DIR",
                    help="write savings-<n>.html per owner plus owners.csv into DIR")
    a = ap.parse_args()
    if not a.out and not a.by_owner:
        ap.error("--out is required unless --by-owner is given")
    if a.owner and a.by_owner:
        ap.error("--owner and --by-owner are mutually exclusive")
    res = json.load(open(a.compare))
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as f:
            f.write(render(res, a.customer, owner=a.owner))
        print("wrote", a.out)
    if a.by_owner:
        rows = owner_reports(res, a.customer, a.by_owner)
        print(f"wrote {len(rows)} owner reports + owners.csv to", a.by_owner)


if __name__ == "__main__":
    main()
