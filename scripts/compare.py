#!/usr/bin/env python3
"""Before/after impact measurement for two advisor inventories (same scope).
   python3 scripts/compare.py --before A/inventory.json --after B/inventory.json \\
       --out DIR [--traffic-shift-pct 25] [--too-soon-days 14]
Writes compare.json, compare.csv, follow-through.csv and COMPARE.md. Stdlib only.
compare.json also carries `follow_through` (which recommendations were acted on and the
savings tied to them), `remaining` and `measurement`; scripts/render-savings.py renders it.
"""
import argparse, csv, datetime, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan_workbooks import cron_refreshes_per_day  # noqa: E402  (the one cron parser)

TOLERANCE_PCT = 5.0  # cost-per-run moves smaller than this count as "unchanged"
CSV_COLUMNS = [
    "OBJECT", "ELEMENT", "STATUS", "MONTHLY_COST_BEFORE", "MONTHLY_COST_AFTER", "COST_CHANGE",
    "MONTHLY_READ_COST_BEFORE", "MONTHLY_READ_COST_AFTER", "MONTHLY_REFRESH_COST_BEFORE",
    "MONTHLY_REFRESH_COST_AFTER", "MONTHLY_RUNS_BEFORE", "MONTHLY_RUNS_AFTER",
    "MONTHLY_REFRESHES_BEFORE", "MONTHLY_REFRESHES_AFTER", "RUNS_CHANGE_PCT",
    "COST_PER_RUN_BEFORE", "COST_PER_RUN_AFTER", "EFFICIENCY_SAVINGS", "REFRESH_SAVINGS",
    "COUNTED_SAVINGS", "TRAFFIC_SHIFT", "P95_BEFORE", "P95_AFTER", "LATENCY_REGRESSION",
    "ACTION_BEFORE", "ACTION_AFTER", "SAMPLE_URL",
]
FT_COLUMNS = ["OBJECT", "ELEMENT", "GROUP_ID", "RECOMMENDED", "STATUS", "EVIDENCE", "DETAIL",
              "SAVINGS", "OWNER_EMAIL", "SAMPLE_URL"]
TOO_SOON_DAYS = 14.0  # a shorter gap between the two runs is not a reliable read
REFRESH_DROP_PCT = 30.0  # metrics evidence: refresh runs fell at least this much
CPR_DROP_PCT = 20.0  # read cost per run fell at least this much -> improved
FIX_ACTIONS = ("Retune materialization", "Remove materialization candidate",
               "Add materialization candidate", "Consolidate into data model",
               "Optimize query/model", "Optimize then reassess")
COUNTED = ("acted-on", "partly")


def _price(inv):
    return float((inv.get("config") or {}).get("credit_price") or 0)


def _days(inv):
    return max(int(inv.get("days") or 30), 1)


def _evidence(inv):
    return inv.get("evidence") or "snowflake"  # inventories before evidence modes


def _n(c, k):
    return float(c.get(k) or 0)


def _monthly(c, inv):
    """Read cost and read runs, refresh cost and refresh runs, each normalised to 30 days
    with the inventory's own window and price. Reads are the served workload (CREDITS +
    QAS_CREDITS over RUNS); refresh is the materialization build cost."""
    f = 30.0 / _days(inv)
    refresh = _n(c, "MATERIALIZATION_CREDITS") + _n(c, "MATERIALIZATION_QAS_CREDITS")
    if "CREDITS" in c or "QAS_CREDITS" in c:
        read = _n(c, "CREDITS") + _n(c, "QAS_CREDITS")
    else:  # older inventories carry only the total
        total = c.get("TOTAL_CREDITS")
        read = max(float(total if total is not None else 0) - refresh, 0.0)
    p = _price(inv) * f
    return {"read_cost": read * p, "refresh_cost": refresh * p,
            "read_runs": _n(c, "RUNS") * f, "refreshes": _n(c, "MATERIALIZATION_RUNS") * f}


def _r(x, n=4):
    return None if x is None else round(x, n)


def _pct(after, before):
    return (after - before) / before * 100.0 if before else (100.0 if after else 0.0)


def key(c):
    return (c.get("OBJECT") or "", c.get("ELEMENT") or "")


def compare(before, after, traffic_shift_pct=25.0, too_soon_days=TOO_SOON_DAYS):
    cfg = after.get("config") or before.get("config") or {}
    slo = float(cfg.get("latency_slo_sec") or 0)
    slow = float(cfg.get("acceptable_slowdown_sec") or 0)
    b = {key(c): c for c in before["candidates"]}
    a = {key(c): c for c in after["candidates"]}
    rows = []
    for k in sorted(set(b) | set(a)):
        cb, ca = b.get(k), a.get(k)
        row = {"OBJECT": k[0], "ELEMENT": k[1],
               "ACTION_BEFORE": (cb or {}).get("ACTION"),
               "ACTION_AFTER": (ca or {}).get("ACTION"),
               "SAMPLE_URL": (ca or cb or {}).get("SAMPLE_URL")}
        mb = _monthly(cb, before) if cb else None
        ma = _monthly(ca, after) if ca else None

        def side(m, name):
            tot = m["read_cost"] + m["refresh_cost"] if m else None
            return {f"MONTHLY_COST_{name}": _r(tot, 2),
                    f"MONTHLY_READ_COST_{name}": _r(m and m["read_cost"], 2),
                    f"MONTHLY_REFRESH_COST_{name}": _r(m and m["refresh_cost"], 2),
                    f"MONTHLY_RUNS_{name}": _r(m and m["read_runs"], 1),
                    f"MONTHLY_REFRESHES_{name}": _r(m and m["refreshes"], 1)}

        row.update(side(mb, "BEFORE"), **side(ma, "AFTER"))
        row.update(
            COST_CHANGE=None, RUNS_CHANGE_PCT=None, COST_PER_RUN_BEFORE=None,
            COST_PER_RUN_AFTER=None, EFFICIENCY_SAVINGS=0.0, REFRESH_SAVINGS=0.0,
            COUNTED_SAVINGS=0.0, TRAFFIC_SHIFT=False, P95_BEFORE=(cb or {}).get("P95_SEC"),
            P95_AFTER=(ca or {}).get("P95_SEC"), LATENCY_REGRESSION=False)
        if not cb:
            row["STATUS"] = "new"
        elif not ca:
            row["STATUS"] = "not-observed"
        else:
            tot = lambda m: m["read_cost"] + m["refresh_cost"]
            row["COST_CHANGE"] = round(tot(ma) - tot(mb), 2)
            cost_b, runs_b = mb["read_cost"], mb["read_runs"]
            cost_a, runs_a = ma["read_cost"], ma["read_runs"]
            cpr_b = cost_b / runs_b if runs_b else None
            cpr_a = cost_a / runs_a if runs_a else None
            row["COST_PER_RUN_BEFORE"], row["COST_PER_RUN_AFTER"] = _r(cpr_b, 6), _r(cpr_a, 6)
            if runs_b:
                row["RUNS_CHANGE_PCT"] = round((runs_a - runs_b) / runs_b * 100.0, 1)
                row["TRAFFIC_SHIFT"] = abs(row["RUNS_CHANGE_PCT"]) > traffic_shift_pct
            else:
                row["TRAFFIC_SHIFT"] = runs_a > 0
            if cpr_b is not None and cpr_a is not None:
                row["EFFICIENCY_SAVINGS"] = round((cpr_b - cpr_a) * runs_a, 2)
            row["REFRESH_SAVINGS"] = round(mb["refresh_cost"] - ma["refresh_cost"], 2)
            row["COUNTED_SAVINGS"] = round(
                (0.0 if row["TRAFFIC_SHIFT"] else row["EFFICIENCY_SAVINGS"])
                + row["REFRESH_SAVINGS"], 2)
            pb, pa = row["P95_BEFORE"], row["P95_AFTER"]
            row["LATENCY_REGRESSION"] = bool(pb is not None and pa is not None
                                             and pa > slo + slow and pa > pb)
            if cpr_b is not None and cpr_a is not None and cpr_b > 0:
                chg = (cpr_a - cpr_b) / cpr_b * 100.0
            else:
                chg = _pct(cost_a, cost_b)
            rchg = _pct(ma["refresh_cost"], mb["refresh_cost"])
            row["STATUS"] = ("regressed" if row["LATENCY_REGRESSION"]
                             or max(chg, rchg) > TOLERANCE_PCT else
                             "improved" if min(chg, rchg) < -TOLERANCE_PCT else "unchanged")
        rows.append(row)
    rows.sort(key=lambda r: (-((r["EFFICIENCY_SAVINGS"] or 0) + (r["REFRESH_SAVINGS"] or 0)),
                             r["OBJECT"], r["ELEMENT"]))
    matched = [r for r in rows if r["STATUS"] not in ("new", "not-observed")]
    shifted = [r for r in matched if r["TRAFFIC_SHIFT"]]
    eff = round(sum(r["EFFICIENCY_SAVINGS"] for r in matched if not r["TRAFFIC_SHIFT"]), 2)
    raw_eff = round(sum(r["EFFICIENCY_SAVINGS"] for r in matched), 2)
    refresh = round(sum(r["REFRESH_SAVINGS"] for r in matched), 2)

    def scope_cost(inv):
        credits = float((inv.get("totals") or {}).get("credits", 0) or 0)
        return round(credits * _price(inv) * 30.0 / _days(inv), 2)

    counts = {}
    for r in rows:
        counts[r["STATUS"]] = counts.get(r["STATUS"], 0) + 1
    warnings = []
    for f, get in (("evidence", _evidence), ("days", lambda i: i.get("days"))):
        if get(before) != get(after):
            warnings.append(f"{f} differs: before={get(before)!r} after={get(after)!r}. "
                            "Figures are normalised to 30 days but not strictly comparable.")
    if _price(before) != _price(after):
        warnings.append(f"credit_price differs: before={_price(before)} "
                        f"after={_price(after)}; each side uses its own price.")
    gap = measurement(before, after, too_soon_days)["days_between"]
    if gap is None:
        warnings.append("A generated timestamp is missing or unreadable, so the measurement "
                        "interval is unknown and the too-soon check was skipped.")
    elif gap < 0:
        warnings.append(f"The after inventory is {-gap:g} days older than the before one; "
                        "the two may be swapped.")
    totals = {
        "monthly_cost_before": scope_cost(before), "monthly_cost_after": scope_cost(after),
        "headline_savings": round(eff + refresh, 2),
        "headline_efficiency_savings": eff, "headline_refresh_savings": refresh,
        "raw_savings": round(raw_eff + refresh, 2), "raw_efficiency_savings": raw_eff,
        "raw_refresh_savings": refresh,
        "excluded_traffic_shift_savings": round(raw_eff - eff, 2),
        "traffic_shift_objects": len(shifted),
        "latency_regressions": sum(1 for r in matched if r["LATENCY_REGRESSION"]),
        "matched_objects": len(matched), "counts": counts,
    }
    totals["scope_cost_change"] = round(
        totals["monthly_cost_after"] - totals["monthly_cost_before"], 2)
    side = lambda inv: {"generated": inv.get("generated"), "days": inv.get("days"),
                        "evidence": _evidence(inv), "credit_price": _price(inv)}
    return {"scope": after.get("scope"), "traffic_shift_pct": traffic_shift_pct,
            "before": side(before), "after": side(after),
            "warnings": warnings, "totals": totals, "objects": rows,
            "follow_through": follow_through(before, after, rows, totals["headline_savings"]),
            "remaining": remaining(after),
            "measurement": measurement(before, after, too_soon_days)}


def _sched(c, member=False):
    """Schedule state of a candidate or group member, or None when its inventory carries no
    usable schedule data for it. `rate` is refreshes/day: 0 when paused or unscheduled, None
    when scheduled but the cron could not be parsed. `has` comes from the cron and paused
    fields only (HAS_EXISTING_SCHEDULE also fires for any refresh run in the window, so a
    schedule deleted mid-window would still read as present). A group member counts only
    when its rate came from a cron (REFRESHES_SOURCE == "cron"; a missing source is an
    older inventory and reads as observed): a window average is not a schedule."""
    if not c:
        return None
    if member:
        if c.get("REFRESHES_SOURCE") != "cron" or c.get("REFRESHES_PER_DAY") is None:
            return None
        rpd = c["REFRESHES_PER_DAY"]
        return {"has": bool(rpd), "paused": False, "rate": rpd}
    if not (c.get("SIGMA_ENRICHMENT") == "matched" or any(
            k in c for k in ("SCHEDULE_CRON", "SCHEDULE_PAUSED", "REFRESHES_PER_DAY"))):
        return None
    cron, rpd = c.get("SCHEDULE_CRON"), c.get("REFRESHES_PER_DAY")
    if rpd is None and cron:
        rpd = cron_refreshes_per_day(cron)
    has = bool(cron or rpd)
    paused = bool(c.get("SCHEDULE_PAUSED"))
    return {"has": has, "paused": paused, "rate": 0 if paused or not has else rpd}


def _sched_text(s):
    if s["paused"]:
        return "paused"
    if not s["has"]:
        return "no schedule"
    r = s["rate"]
    if r is None:
        return "a schedule with an unreadable cron"
    if r <= 0:
        return "paused or no schedule"
    mins = 1440.0 / r if r > 24 else 0
    label = ("hourly" if r == 24 else "daily" if r == 1 else
             f"every {mins:.0f} minutes" if mins and abs(mins - round(mins)) < 0.01 else
             "less than daily" if r < 1 else f"{r:g} times a day")
    return f"{label} ({r:g}/day)"


def _unreadable(*scheds):
    """A schedule exists but its cron could not be read."""
    return any(s and s["has"] and not s["paused"] and s["rate"] is None for s in scheds)


def _index(inv):
    cands = {key(c): c for c in inv["candidates"]}
    mem = {}
    for g in inv.get("duplicate_groups") or []:
        for m in g.get("MEMBERS") or []:
            k = key(m)
            if k not in mem or (mem[k].get("REFRESHES_PER_DAY") is None
                                and m.get("REFRESHES_PER_DAY") is not None):
                mem[k] = m
    return cands, mem


def _sched_of(idx, k):
    return _sched(idx[0].get(k)) or _sched(idx[1].get(k), member=True)


def _seen(idx, k):
    return k in idx[0] or k in idx[1]


def _drop(b, a):
    return b > 0 and a <= b * (1 - REFRESH_DROP_PCT / 100.0)


def _metrics_refresh(mb, ma, sb=None, sa=None):
    b, a = mb["refreshes"], ma["refreshes"]
    why = ("Schedule present but its cron could not be read; used refresh counts instead"
           if _unreadable(sb, sa) else "No schedule data in the inventories")
    return b, a, ("metrics" if b or a else "none"), (
        f"{why}; monthly refreshes went from {b:.0f} to {a:.0f}")


def _judge(act, sb, sa, mb, ma, row):
    """(status, evidence, detail) for one candidate seen on both sides."""
    if act.startswith("Optimize"):
        cb, ca = row["COST_PER_RUN_BEFORE"], row["COST_PER_RUN_AFTER"]
        if cb and ca is not None:
            chg = (ca - cb) / cb * 100.0
            d = f"Read cost per run went from ${cb:.4f} to ${ca:.4f} ({chg:+.0f}%)"
            if chg <= -CPR_DROP_PCT:
                return "improved", "metrics", d + "; the cause is not verified"
            return "not-yet", "metrics", d
        return "not-yet", "none", "No read cost per run to compare"
    both = sb is not None and sa is not None
    if act == "Retune materialization":
        if both and sa["paused"] and not sb["paused"]:
            return "acted-on", "schedule", f"Schedule paused (was {_sched_text(sb)})"
        if both and sb["rate"] is not None and sa["rate"] is not None:
            t = f"from {_sched_text(sb)} to {_sched_text(sa)}"
            if sa["rate"] < sb["rate"]:
                return "acted-on", "schedule", f"Schedule changed {t}"
            if sa["rate"] > sb["rate"]:
                return "reverted", "schedule", f"Schedule went back up {t}"
            return "not-yet", "schedule", f"Schedule unchanged: {_sched_text(sa)}"
        b, a, ev, d = _metrics_refresh(mb, ma, sb, sa)
        return ("acted-on" if _drop(b, a) else "not-yet"), ev, d
    if act == "Remove materialization candidate":
        if both and sb["has"]:
            if sa["paused"] or not sa["has"]:
                return "acted-on", "schedule", ("Schedule paused" if sa["has"] else
                                                f"Schedule removed (was {_sched_text(sb)})")
            return "not-yet", "schedule", f"Schedule still {_sched_text(sa)}"
        b, a, ev, d = _metrics_refresh(mb, ma, sb, sa)
        return ("acted-on" if b > 0 and a == 0 else "not-yet"), ev, d
    if sa is not None:  # Add materialization candidate
        if sa["has"] and not sa["paused"]:
            return "acted-on", "schedule", f"A schedule now exists: {_sched_text(sa)}"
        return "not-yet", "schedule", "No active schedule yet"
    b, a, ev, d = _metrics_refresh(mb, ma, sb, sa)
    return ("acted-on" if a > 0 else "not-yet"), ev, d


def _judge_group(g, bi, ai, mon_b, mon_a):
    ks = [key(m) for m in g.get("MEMBERS") or []]
    if not any(_seen(ai, k) for k in ks):
        return "not-observed", "none", "No member is in the after inventory (not removed)"
    n, pairs, removed, unread = len(ks), 0, 0, False
    for k in ks:
        sb, sa = _sched_of(bi, k), _sched_of(ai, k)
        unread = unread or _unreadable(sb, sa)
        # schedule evidence needs a readable rate on both sides: an after side that is
        # missing or unreadable is "unknown", never "removed"
        if (sb and sb["rate"] is not None and sb["rate"] != 0
                and sa is not None and sa["rate"] is not None):
            pairs += 1
            removed += bool(sa["paused"] or not sa["has"])
    if pairs:
        if removed:
            return ("acted-on" if removed >= n - 1 else "partly"), "schedule", (
                f"{removed} of {n} member schedules removed or paused")
        return "not-yet", "schedule", "No member schedule has been removed or paused yet"
    both = [k for k in ks if k in mon_b and k in mon_a]
    b, a = sum(mon_b[k]["refreshes"] for k in both), sum(mon_a[k]["refreshes"] for k in both)
    why = ("Schedule present but its cron could not be read; used refresh counts instead"
           if unread else "No schedule data")
    return ("acted-on" if _drop(b, a) else "not-yet"), ("metrics" if b or a else "none"), (
        f"{why}; member refreshes went from {b:.0f} to {a:.0f} a month")


def follow_through(before, after, rows, headline):
    """Tie the counted savings to the recommendations that were acted on. Every object's
    COUNTED_SAVINGS goes to at most one row (a counted group claims its members first), so
    acted-on + improved + other add up to the headline exactly."""
    rowmap = {key(r): r for r in rows}
    bi, ai = _index(before), _index(after)
    mon = lambda inv: {key(c): _monthly(c, inv) for c in inv["candidates"]}
    mon_b, mon_a = mon(before), mon(after)
    out, na, claimed = [], 0, set()

    def base(rec, other, act):
        return {"OBJECT": rec.get("OBJECT"), "ELEMENT": rec.get("ELEMENT") or "",
                "SAMPLE_URL": other.get("SAMPLE_URL") or rec.get("SAMPLE_URL"),
                "OWNER_EMAIL": other.get("OWNER_EMAIL") or rec.get("OWNER_EMAIL"),
                "RECOMMENDED": act}

    for g in before.get("duplicate_groups") or []:
        members = g.get("MEMBERS") or []
        if g.get("LOW_VALUE") or not members or g.get("ACTION") != "Consolidate into data model":
            na += 1
            continue
        st, ev, d = _judge_group(g, bi, ai, mon_b, mon_a)
        mine = [ai[1].get(key(m)) or ai[0].get(key(m)) or {} for m in members]
        e = base(members[0], next((m for m in mine if m.get("OWNER_EMAIL")), {}), g["ACTION"])
        e.update(GROUP_ID=g.get("GROUP_ID"), ELEMENT="",
                 MEMBERS=[{"OBJECT": m.get("OBJECT"), "ELEMENT": m.get("ELEMENT") or ""}
                          for m in members],
                 STATUS=st, EVIDENCE=ev, DETAIL=d, SAVINGS=0.0)
        if st in COUNTED:
            ks = [k for k in map(key, members) if k in rowmap and k not in claimed]
            claimed.update(ks)
            e["SAVINGS"] = round(sum(rowmap[k]["COUNTED_SAVINGS"] for k in ks), 2)
        out.append(e)
    for c in before["candidates"]:
        act, k = c.get("ACTION") or "", key(c)
        if act not in FIX_ACTIONS or act == "Consolidate into data model":
            na += 1
            continue
        ca = ai[0].get(k)
        e = base(c, ca or {}, act)  # the after owner and link win, as for groups
        e["SAVINGS"] = 0.0
        if not ca:
            e.update(STATUS="not-observed", EVIDENCE="none",
                     DETAIL="Not in the after inventory (not treated as removed)")
        else:
            st, ev, d = _judge(act, _sched(c), _sched(ca), mon_b[k], mon_a[k], rowmap[k])
            e.update(STATUS=st, EVIDENCE=ev, DETAIL=d)
            if k not in claimed:
                e["SAVINGS"] = rowmap[k]["COUNTED_SAVINGS"]
                claimed.add(k)  # a duplicated key must not be credited twice
        out.append(e)
    out.sort(key=lambda e: (-e["SAVINGS"], e["OBJECT"] or "", e["ELEMENT"]))
    tot = lambda *sts: round(sum((e["SAVINGS"] for e in out if e["STATUS"] in sts), 0.0), 2)
    acted, improved = tot(*COUNTED), tot("improved")
    counts = {"n/a": na} if na else {}
    for e in out:
        counts[e["STATUS"]] = counts.get(e["STATUS"], 0) + 1
    done = sum(counts.get(s, 0) for s in COUNTED)
    owners = {}
    for e in out:
        o = owners.setdefault((e["OWNER_EMAIL"] or "").strip().lower() or "unassigned", {
            "recommendations": 0, "acted_on": 0, "not_yet": 0,
            "savings_acted_on": 0.0, "savings_total": 0.0})
        o["recommendations"] += 1
        o["acted_on"] += e["STATUS"] in COUNTED
        o["not_yet"] += e["STATUS"] == "not-yet"
        o["savings_acted_on"] = round(
            o["savings_acted_on"] + (e["SAVINGS"] if e["STATUS"] in COUNTED else 0), 2)
        o["savings_total"] = round(o["savings_total"] + e["SAVINGS"], 2)
    return {"rows": out, "counts": counts, "actionable": len(out),
            "follow_through_rate": round(done / len(out), 4) if out else None,
            "savings_acted_on": acted, "savings_improved": improved,
            "savings_other": round(headline - acted - improved, 2),
            "headline_savings": headline,
            "schedule_data": any(_sched(c) for inv in (before, after) for c in inv["candidates"]),
            "by_owner": owners}


def _when(text):
    try:
        d = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def measurement(before, after, too_soon_days=TOO_SOON_DAYS):
    tb, ta = _when(before.get("generated")), _when(after.get("generated"))
    gap = round((ta - tb).total_seconds() / 86400.0, 1) if tb and ta else None
    return {"before_generated": before.get("generated"), "after_generated": after.get("generated"),
            "days_between": gap, "too_soon_days": too_soon_days,
            "too_soon": None if gap is None else gap < too_soon_days,  # None: interval unknown
            "same_window": before.get("days") == after.get("days"),
            "before_days": before.get("days"), "after_days": after.get("days"),
            "scope": after.get("scope")}


def remaining(after, limit=10):
    """What is still open in the after inventory (consolidation is reported apart)."""
    top = []
    for c in after["candidates"]:
        if c.get("ACTION") in FIX_ACTIONS and c.get("ACTION") != "Consolidate into data model":
            top.append((c, {"OBJECT": c.get("OBJECT"), "ELEMENT": c.get("ELEMENT") or ""}))
    for g in after.get("duplicate_groups") or []:
        m = (g.get("MEMBERS") or [{}])[0]
        if g.get("ACTION") == "Consolidate into data model" and not g.get("LOW_VALUE"):
            top.append((g, {"OBJECT": m.get("OBJECT"), "ELEMENT": "", "GROUP_ID": g.get("GROUP_ID"),
                            "MEMBER_COUNT": g.get("MEMBER_COUNT") or len(g.get("MEMBERS") or [])}))
    top.sort(key=lambda t: (-float(t[0].get("SAVINGS_EXPECTED") or 0),
                            -float(t[0].get("SCORE") or 0)))
    t = after.get("totals") or {}

    def first(r, k):
        return r.get(k) or next((m[k] for m in r.get("MEMBERS") or [] if m.get(k)), None)

    return {"estimated_monthly_savings": t.get("estimated_monthly_savings"),
            "consolidation_savings_expected": t.get("consolidation_savings_expected"),
            "open_recommendations": len(top),
            "top": [dict(d, ACTION=r.get("ACTION"), IMPACT=r.get("IMPACT"),
                         SAVINGS_EXPECTED=r.get("SAVINGS_EXPECTED"),
                         OWNER_EMAIL=first(r, "OWNER_EMAIL"), SAMPLE_URL=first(r, "SAMPLE_URL"))
                    for r, d in top[:limit]]}


def money(x):
    return "n/a" if x is None else f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def markdown(res):
    t, w = res["totals"], res["warnings"]
    L = ["# Before / after impact", "",
         f"_Scope: **{res['scope']}** · before: {res['before']['generated'] or '?'} "
         f"({res['before']['days']}d) · after: {res['after']['generated'] or '?'} "
         f"({res['after']['days']}d). All figures normalised to 30 days._", ""]
    if w:
        L += ["## Warnings", ""] + [f"- {x}" for x in w] + [""]
    L += ["## Headline", "",
          f"- **Savings: {money(t['headline_savings'])}/month** = "
          f"{money(t['headline_efficiency_savings'])} read efficiency + "
          f"{money(t['headline_refresh_savings'])} refresh, across {t['matched_objects']} "
          "objects seen on both sides. Read efficiency excludes objects whose traffic "
          "shifted; refresh savings are never excluded.",
          f"- Raw total including traffic-shifted objects: {money(t['raw_savings'])}/month "
          f"({t['traffic_shift_objects']} objects excluded from read efficiency, "
          f"{money(t['excluded_traffic_shift_savings'])}/month).",
          f"- Scope compute cost: {money(t['monthly_cost_before'])}/month before, "
          f"{money(t['monthly_cost_after'])}/month after "
          f"({money(t['scope_cost_change'])} change). This total moves with traffic, so use "
          "it for context, not as proof of savings.",
          f"- Latency regressions: {t['latency_regressions']}.", ""]
    L += ["## How to read this", "",
          "- **Not-observed is not removed.** The inventory lists only the top objects by "
          "cost. An object missing from the after run may have dropped below the cutoff, "
          "lost traffic, or been removed; this report does not tell which, and "
          "not-observed objects contribute nothing to the savings above.",
          f"- **Traffic-shifted objects are excluded from read efficiency.** If an object's "
          f"monthly read runs moved by more than {res['traffic_shift_pct']:g}%, its read "
          "cost change mostly reflects usage, not efficiency, so it is reported but not "
          "counted. Refresh savings do not depend on traffic and are always counted.",
          "- Read efficiency savings = (read cost per run before - after) x monthly read "
          "runs after. Refresh savings = monthly refresh cost before - after. Positive "
          "means money saved per month at today's traffic.",
          f"- Status is `unchanged` when read cost per run and refresh cost each moved less "
          f"than {TOLERANCE_PCT:g}%; `regressed` also covers any latency regression (after "
          "p95 above the latency target plus allowed slowdown, and worse than before).",
          ""]
    ft, ms = res.get("follow_through"), res.get("measurement") or {}
    if ft:
        pct = ft["follow_through_rate"]
        L += ["## Follow-through", "",
              (f"- Measured {ms['days_between']} days after the baseline"
               + (" (too soon: wait at least 2 weeks for a reliable read)."
                  if ms.get("too_soon") else ".")
               if ms.get("days_between") is not None else
               "- Measurement interval unknown (a generated timestamp is missing)."),
              f"- {ft['actionable']} actionable recommendations; follow-through "
              f"{'n/a' if pct is None else f'{pct * 100:.0f}%'} (acted-on or partly). "
              f"Statuses: {', '.join(f'{v} {k}' for k, v in sorted(ft['counts'].items()))}.",
              f"- Savings tied to actions taken: {money(ft['savings_acted_on'])}/month; "
              f"improved, cause not verified: {money(ft['savings_improved'])}; other changes "
              f"not tied to a recommendation: {money(ft['savings_other'])}. These add up to "
              f"the headline {money(ft['headline_savings'])}.",
              "- Savings are attributed to a change because they coincide with it, not "
              "proven caused by it.", ""]
    cnt = ", ".join(f"{v} {k}" for k, v in sorted(t["counts"].items())) or "none"
    L += ["## Objects", "", f"Status counts: {cnt}.", "",
          "| Object | Element | Status | Cost/mo before | Cost/mo after | Read runs change | "
          "Efficiency savings | Refresh savings | p95 before | p95 after | Flags |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in res["objects"][:50]:
        flags = ", ".join(f for f, on in (
            ("TRAFFIC_SHIFT", r["TRAFFIC_SHIFT"]),
            ("LATENCY_REGRESSION", r["LATENCY_REGRESSION"])) if on)
        pct = "" if r["RUNS_CHANGE_PCT"] is None else f"{r['RUNS_CHANGE_PCT']:+.1f}%"
        p = lambda x: "" if x is None else f"{x:.2f}"
        L.append(f"| {r['OBJECT']} | {r['ELEMENT']} | {r['STATUS']} | "
                 f"{money(r['MONTHLY_COST_BEFORE'])} | {money(r['MONTHLY_COST_AFTER'])} | "
                 f"{pct} | {money(r['EFFICIENCY_SAVINGS'])} | {money(r['REFRESH_SAVINGS'])} | "
                 f"{p(r['P95_BEFORE'])} | {p(r['P95_AFTER'])} | {flags} |")
    if len(res["objects"]) > 50:
        L.append(f"\n_Showing 50 of {len(res['objects'])} objects; see compare.csv for all._")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--traffic-shift-pct", type=float, default=25.0,
                    help="flag objects whose monthly read runs changed by more than this "
                    "percent (default 25)")
    ap.add_argument("--too-soon-days", type=float, default=TOO_SOON_DAYS,
                    help="flag the read as too soon when the two runs are closer than this "
                    "many days apart (default 14)")
    ap.add_argument("--force", action="store_true",
                    help="compare even if the scope differs or after is older than before")
    a = ap.parse_args()
    before, after = json.load(open(a.before)), json.load(open(a.after))
    if before.get("scope") != after.get("scope") and not a.force:
        sys.exit(f"refusing: scope differs (before={before.get('scope')!r}, "
                 f"after={after.get('scope')!r}); the comparison would not be like for like. "
                 "Re-run the advisor on the same scope or pass --force.")
    gap = measurement(before, after)["days_between"]
    if gap is not None and gap < 0 and not a.force:
        sys.exit(f"refusing: the after inventory ({after.get('generated')}) is older than the "
                 f"before one ({before.get('generated')}); the arguments look swapped. "
                 "Swap them or pass --force.")
    res = compare(before, after, a.traffic_shift_pct, a.too_soon_days)
    if before.get("scope") != after.get("scope"):
        res["warnings"].insert(0, f"scope differs (before={before.get('scope')!r}, "
                                  f"after={after.get('scope')!r}); forced.")
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "compare.json"), "w") as f:
        json.dump(res, f, indent=2)
    with open(os.path.join(a.out, "compare.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(res["objects"])
    with open(os.path.join(a.out, "follow-through.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(res["follow_through"]["rows"])
    with open(os.path.join(a.out, "COMPARE.md"), "w") as f:
        f.write(markdown(res))
    for x in res["warnings"]:
        print("warning:", x, file=sys.stderr)
    t = res["totals"]
    print(f"headline savings ${t['headline_savings']:,.2f}/mo "
          f"(read efficiency ${t['headline_efficiency_savings']:,.2f} + refresh "
          f"${t['headline_refresh_savings']:,.2f}; raw ${t['raw_savings']:,.2f}); "
          f"wrote {a.out}")
    ft = res["follow_through"]
    print(f"follow-through {ft['counts']}; ${ft['savings_acted_on']:,.2f} from actions taken, "
          f"${ft['savings_improved']:,.2f} improved (unverified), "
          f"${ft['savings_other']:,.2f} other")
    if res["measurement"]["too_soon"] is None:
        print("warning: a generated timestamp is missing; measurement interval unknown",
              file=sys.stderr)
    elif res["measurement"]["too_soon"]:
        print(f"warning: only {res['measurement']['days_between']} days between the runs; "
              f"wait at least {a.too_soon_days:g} for a reliable read", file=sys.stderr)


if __name__ == "__main__":
    main()
