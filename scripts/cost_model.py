#!/usr/bin/env python3
"""Pure recommendation logic for the Sigma performance-and-cost advisor.

The model deliberately separates observations from counterfactuals. In particular, an
expensive materialization is never marked safe to remove unless a measured no-
materialization baseline is supplied.
"""
from copy import deepcopy


DEFAULT_CONFIG = {
    "goal": "performance_per_dollar",
    "latency_slo_sec": 5.0,
    "freshness": "varies",
    "acceptable_slowdown_sec": 1.0,
    "min_monthly_savings": 10.0,
    "mat_min_runs": 12,
    "mat_min_avg_sec": 3.0,
    "mat_min_credits": 0.5,
    "min_served_reads": 5,
    "min_served_reads_per_refresh": 1.0,
    "retune_refresh_reduction_pct": 0.5,
    "big_bytes": 1_000_000_000,
    "impact_high_monthly": 100.0,
    "impact_medium_monthly": 20.0,
}


ACTION_STEPS = {
    "Keep materialization": [
        "No change needed: the saved copy is being used and loads fast.",
        "Look again next month. If fewer people open the workbook, refresh it less often.",
    ],
    "Retune materialization": [
        "Open the workbook, find the element named in the report, and open its "
        "materialization schedule.",
        "Ask the data owner how often the underlying data really changes; refresh "
        "no more often than that (daily is often enough).",
        "Lower the schedule (for example from hourly to every 4 hours or daily) and "
        "write down the old setting first.",
        "Check again in a week or two: the workbook should open just as fast and "
        "refresh credits should drop.",
    ],
    "Remove materialization candidate": [
        "Confirm with the workbook owner that nobody relies on the saved copy.",
        "Write down the current schedule (cron and timezone) so it can be restored.",
        "Turn the schedule off and watch load time and credits for one to two weeks.",
        "Restore the schedule if the workbook becomes slower than your target.",
    ],
    "Add materialization candidate": [
        "Open the workbook and find the element named in the report: it is opened "
        "often and costs a lot each time.",
        "Turn on materialization for that element with a daily schedule at a quiet hour.",
        "After a week, compare load time and credits; keep it only if both improve.",
    ],
    "Optimize query/model": [
        "Open the element and remove columns it does not show; add a date or "
        "category filter if people only look at part of the data.",
        "If several elements repeat the same joins or calculations, move them into "
        "one shared data model so the work happens once.",
        "Ask your data team whether the source table is organized (clustered) by "
        "the column you filter on most.",
        "Re-run this report next week to see whether cost and load time dropped.",
    ],
    "Optimize then reassess": [
        "Open the element and look for large joins, heavy calculations, or "
        "missing filters.",
        "Fix the slowest part first, then re-run this report before adding "
        "more materialization.",
    ],
    "Investigate materialization": [
        "Open the workbook and check whether the schedule is active, failing, or "
        "recently changed.",
        "Check in Sigma who opens the workbook and how often.",
        "If page controls (filters) feed this element, review them: they can "
        "bypass the saved copy.",
        "Change nothing until the owner has decided to keep, lower, or remove it.",
    ],
    "Consolidate into data model": [
        "Create one data model that holds the shared logic (same source tables, "
        "joins, and calculations).",
        "Turn on materialization on that data model at the cadence the most "
        "demanding workbook needs.",
        "Repoint each listed workbook element to the data model instead of its "
        "own copy.",
        "Keep the old schedules until readers have moved over, then turn them "
        "off with the owner's approval.",
    ],
    "Monitor": [
        "Nothing to do now.",
        "Re-run this report after a month to see whether usage or cost changed.",
    ],
}


NUMERIC_FIELDS = {
    "RUNS", "CREDITS", "QAS_CREDITS", "AVG_SEC", "P50_SEC", "P95_SEC",
    "MAX_SEC", "TOTAL_SEC", "MAX_BYTES", "BYTES_SCANNED", "BYTES_SPILLED",
    "QUEUED_OVERLOAD_SEC", "PARTITION_SCAN_PCT", "QUERY_PATTERNS",
    "MATERIALIZATION_RUNS", "MATERIALIZATION_CREDITS",
    "MATERIALIZATION_QAS_CREDITS",
    "MATERIALIZATION_P95_SEC", "MATCHED_MATERIALIZED_READS",
    "MATCHED_READ_CREDITS", "MATCHED_READ_QAS_CREDITS",
    "MATCHED_READ_P95_SEC", "UNMATCHED_READS",
    "UNMATCHED_READ_CREDITS", "UNMATCHED_READ_P95_SEC",
    "WORKBOOK_UNMATCHED_READS", "WORKBOOK_UNMATCHED_READ_CREDITS",
    "WORKBOOK_UNMATCHED_READ_QAS_CREDITS", "WORKBOOK_UNMATCHED_READ_P95_SEC",
    "COUNTERFACTUAL_P95_SEC", "COUNTERFACTUAL_CREDITS",
}


def merge_config(overrides=None):
    config = deepcopy(DEFAULT_CONFIG)
    if overrides:
        config.update({k: v for k, v in overrides.items() if v is not None})
    return config


def coerce_row(row):
    result = {str(k).upper(): v for k, v in row.items()}
    for key in NUMERIC_FIELDS:
        value = result.get(key)
        try:
            result[key] = float(value) if value not in (None, "") else 0.0
        except (TypeError, ValueError):
            result[key] = 0.0
    result["RUNS"] = int(result.get("RUNS", 0))
    result["MATERIALIZATION_RUNS"] = int(result.get("MATERIALIZATION_RUNS", 0))
    result["MATCHED_MATERIALIZED_READS"] = int(
        result.get("MATCHED_MATERIALIZED_READS", 0)
    )
    result["UNMATCHED_READS"] = int(result.get("UNMATCHED_READS", 0))
    return result


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _monthly(value, days):
    return float(value or 0) * 30.0 / max(float(days or 30), 1.0)


def _result(action, why, confidence, risk, performance, validation,
            savings_low=0.0, savings_expected=0.0, savings_high=0.0):
    return {
        "ACTION": action,
        "RECOMMENDATION": action,
        "WHY": why,
        "CONFIDENCE": confidence,
        "RISK": risk,
        "PERFORMANCE_IMPACT": performance,
        "VALIDATION": validation,
        "SAVINGS_LOW": round(max(savings_low, 0.0), 2),
        "SAVINGS_EXPECTED": round(max(savings_expected, 0.0), 2),
        "SAVINGS_HIGH": round(max(savings_high, 0.0), 2),
    }


def _recommend_core(row, config=None, days=30, credit_price=3.0):
    cfg = merge_config(config)
    r = coerce_row(row)
    query_credits = r.get("CREDITS", 0) + r.get("QAS_CREDITS", 0)
    live_credits = _monthly(query_credits, days)
    mat_credits = _monthly(
        r.get("MATERIALIZATION_CREDITS", 0)
        + r.get("MATERIALIZATION_QAS_CREDITS", 0), days
    )
    live_cost = live_credits * credit_price
    mat_cost = mat_credits * credit_price
    current_cost = live_cost + mat_cost
    p95 = r.get("P95_SEC") or r.get("AVG_SEC")
    mat_runs = r.get("MATERIALIZATION_RUNS", 0)
    served = r.get("MATCHED_MATERIALIZED_READS", 0)
    served_per_refresh = served / mat_runs if mat_runs else 0.0
    has_schedule = bool(r.get("HAS_EXISTING_SCHEDULE")) or mat_runs > 0
    has_controls = bool(r.get("HAS_CONTROLS"))
    controls_resolved = bool(r.get("CONTROL_TARGETS_RESOLVED"))
    lineage_complete = r.get("LINEAGE_COMPLETE", True) not in (False, 0, "false")
    freshness = str(cfg.get("freshness", "varies")).lower()

    if has_schedule:
        unresolved_controls = has_controls and not controls_resolved
        if unresolved_controls or not lineage_complete:
            reason = (
                "Control targets are not resolved"
                if unresolved_controls else "lineage is incomplete"
            )
            return _result(
                "Investigate materialization",
                f"{reason}; observed utilization is {served} matched read(s) across "
                f"{mat_runs} refresh(es). Removal safety cannot be inferred.",
                "medium", "high", "Unknown until control/lineage behavior is verified.",
                "Review controls and lineage, then benchmark the element with and "
                "without materialization before changing the schedule.",
            )
        if r.get("READ_EVIDENCE_AVAILABLE", True) in (False, 0, "false", "False"):
            facts = []
            rpd = _num(r.get("REFRESHES_PER_DAY"))
            if rpd:
                facts.append(f"It refreshes about {rpd:g} time(s) a day.")
            if mat_credits:
                facts.append(f"Refresh cost is about {mat_credits:.2f} credits/month "
                             f"(${mat_cost:,.0f}).")
            return _result(
                "Investigate materialization",
                ("Read utilization cannot be measured from this evidence (no "
                 "Snowflake query history), so it is unknown whether anyone uses "
                 "the saved copy. " + " ".join(facts)).strip(),
                "low", "medium",
                "Unknown: served reads and load time were not measured.",
                "Check who opens this workbook (usage in Sigma) or re-run the "
                "analysis against Snowflake query history before changing the "
                "schedule.",
            )
        if mat_runs == 0:
            return _result(
                "Investigate materialization",
                "A Sigma schedule exists, but no refresh history was observed in the "
                "analysis window.",
                "low", "medium", "Utilization and refresh TCO are unknown.",
                "Check whether the schedule is new, paused, failing, or outside the "
                "ACCOUNT_USAGE latency window before changing it.",
            )
        counterfactual_p95 = r.get("COUNTERFACTUAL_P95_SEC", 0)
        counterfactual_credits = r.get("COUNTERFACTUAL_CREDITS", 0)
        counterfactual_safe = (
            counterfactual_p95 > 0
            and counterfactual_p95 <= float(cfg["latency_slo_sec"])
            + float(cfg["acceptable_slowdown_sec"])
        )
        if counterfactual_safe and counterfactual_credits > 0:
            projected_cost = _monthly(counterfactual_credits, days) * credit_price
            savings = current_cost - projected_cost
            if savings >= float(cfg["min_monthly_savings"]):
                return _result(
                    "Remove materialization candidate",
                    f"Measured no-materialization p95 is {counterfactual_p95:.2f}s "
                    f"and remains within the latency allowance; projected monthly "
                    f"savings are about ${savings:.0f}.",
                    "high", "medium",
                    f"Measured p95 without materialization: {counterfactual_p95:.2f}s.",
                    "Remove only with owner approval, preserve the cron/timezone, and "
                    "monitor latency and credits with a rollback window.",
                    savings * 0.8, savings, savings,
                )

        poor_utilization = (
            served < int(cfg["min_served_reads"])
            or served_per_refresh < float(cfg["min_served_reads_per_refresh"])
        )
        if poor_utilization:
            reducible = mat_cost * float(cfg["retune_refresh_reduction_pct"])
            return _result(
                "Retune materialization",
                f"Only {served} matched read(s) were observed across {mat_runs} "
                f"refresh(es) ({served_per_refresh:.2f} reads/refresh).",
                "medium", "medium",
                "Expected to preserve cached performance while reducing refresh work; "
                "the exact impact requires a cadence experiment.",
                "Align cadence to source updates and user access. Measure one full "
                "business cycle before considering removal.",
                0.0, reducible * 0.5, reducible,
            )
        if (r.get("MATCHED_READ_P95_SEC") or p95) <= float(cfg["latency_slo_sec"]):
            return _result(
                "Keep materialization",
                f"It serves {served} observed read(s) across {mat_runs} refresh(es), "
                f"and matched-read p95 is "
                f"{(r.get('MATCHED_READ_P95_SEC') or p95):.2f}s.",
                "medium", "low", "Current cached performance meets the configured SLO.",
                "Continue measuring refresh cost and matched reads; retune if "
                "utilization falls.",
            )
        return _result(
            "Optimize then reassess",
            f"The schedule is used, but observed p95 remains {p95:.2f}s—above the "
            f"{float(cfg['latency_slo_sec']):.2f}s target.",
            "medium", "medium", "Query/model work is needed to improve the slow path.",
            "Inspect pruning, spill, joins, calculations, and control bypass before "
            "adding more materialization.",
        )

    heavy = (
        p95 >= float(cfg["mat_min_avg_sec"])
        or query_credits >= float(cfg["mat_min_credits"])
    )
    repetitive = r.get("RUNS", 0) >= int(cfg["mat_min_runs"])
    inefficient = (
        r.get("BYTES_SPILLED", 0) > 0
        or r.get("MAX_BYTES", 0) >= float(cfg["big_bytes"])
        or r.get("PARTITION_SCAN_PCT", 0) >= 80
    )
    materialization_allowed = freshness not in {"real-time", "realtime", "live"}
    data_model_unresolved = (
        "/data-model/" in str(r.get("OBJECT") or "")
        and not r.get("DATA_MODEL_EVIDENCE_COMPLETE")
    )

    if inefficient:
        return _result(
            "Optimize query/model",
            f"The workload is expensive per run (p95 {p95:.2f}s) and shows scan, "
            "pruning, or spill pressure. Lower the cost floor before caching it.",
            "medium", "low", "Expected to reduce both live-query cost and latency.",
            "Inspect query insights/profile, filters, joins, projected columns, and "
            "pre-aggregation; remeasure before materializing.",
        )
    if repetitive and heavy and not lineage_complete:
        return _result(
            "Investigate materialization",
            "The workload is repetitive and expensive, but Sigma lineage or schedule "
            "evidence is incomplete.",
            "low", "medium", "Placement and downstream impact are unknown.",
            "Resolve the Sigma asset and lineage before creating a schedule.",
        )
    if repetitive and heavy and data_model_unresolved:
        return _result(
            "Investigate materialization",
            "The data-model workload is repetitive and expensive, but schedule and "
            "downstream-consumer utilization are not resolved by the workbook-only "
            "materialization evidence pass.",
            "low", "medium", "Potentially high fan-out; performance impact is unknown.",
            "Inspect the data-model schedule and consumer lineage before adding, "
            "retuning, or removing materialization.",
        )
    if repetitive and heavy and materialization_allowed and not has_controls:
        potential = live_cost * 0.5
        return _result(
            "Add materialization candidate",
            f"The element is repetitive ({r.get('RUNS')} runs) and non-trivial "
            f"({p95:.2f}s p95, {query_credits:.2f} query + QAS credits).",
            "low", "medium", "Likely faster repeat reads; freshness and refresh cost "
            "must be measured.",
            "Create only after a capped benchmark. Compare avoided live-query credits "
            "with refresh compute and storage.",
            0.0, potential * 0.5, potential,
        )
    if repetitive and heavy and has_controls:
        return _result(
            "Investigate materialization",
            "The workload is repetitive and expensive, but the workbook contains "
            "controls whose target bindings are unresolved.",
            "low", "high", "A direct control target can bypass materialization.",
            "Review control targets and prefer an upstream parent whose children are "
            "controlled; benchmark before creating a schedule.",
        )
    return _result(
        "Monitor",
        f"Observed cost and latency do not justify a change ({query_credits:.3f} "
        f"credits, {p95:.2f}s p95).",
        "high", "low", "No material performance change expected.",
        "Revisit if usage, cost, or the latency target changes.",
    )


def impact_tier(action, savings_expected, p95, cfg):
    """High/Medium/Low by monthly savings or how far p95 exceeds the latency target."""
    if action in ("Monitor", "Keep materialization"):
        return "Low"
    slo = float(cfg["latency_slo_sec"])
    if (savings_expected >= float(cfg["impact_high_monthly"])
            or (p95 and p95 >= 3 * slo)):
        return "High"
    if (savings_expected >= float(cfg["impact_medium_monthly"])
            or (p95 and p95 > slo)):
        return "Medium"
    return "Low"


def _finish(result, p95, cfg):
    result["IMPACT"] = impact_tier(
        result["ACTION"], result["SAVINGS_EXPECTED"], p95, cfg)
    result["FIX_STEPS"] = list(ACTION_STEPS.get(result["ACTION"], []))
    return result


def recommend(row, config=None, days=30, credit_price=3.0):
    """Return an evidence-based action for one query or materialization opportunity."""
    cfg = merge_config(config)
    r = coerce_row(row)
    p95 = r.get("P95_SEC") or r.get("AVG_SEC")
    return _finish(_recommend_core(row, cfg, days, credit_price), p95, cfg)


MATCH_LABELS = {
    "identical-build": "build the identical table",
    "identical-sql": "use identical SQL",
    "identical-logic": "use identical logic",
    "same-source": "read the same source",
    "same-base-tables": "read the same source tables",
    "identical-live-query": "run the same live query",
}


def group_credits(group):
    """(total, redundant) credits across a group's members (redundant = all but the largest)."""
    credits = [_num(m.get("CREDITS")) for m in group.get("MEMBERS", [])]
    total = sum(credits)
    return total, total - max(credits, default=0.0)


def recommend_consolidation(group, config=None, days=30, credit_price=3.0):
    """Recommend building shared logic once in a data model for a duplicate group."""
    cfg = merge_config(config)
    match = group.get("MATCH")
    members = group.get("MEMBERS", [])
    n = int(group.get("MEMBER_COUNT") or len(members))
    total, redundant = group_credits(group)
    if group.get("REDUNDANT_CREDITS") is not None:
        redundant = _num(group.get("REDUNDANT_CREDITS"))
    if group.get("TOTAL_CREDITS") is not None:
        total = _num(group.get("TOTAL_CREDITS"))
    monthly = _monthly(redundant, days) * credit_price
    per_day = sum(_num(m.get("REFRESHES_PER_DAY")) for m in members)
    low = expected = high = 0.0
    if match in ("identical-build", "identical-sql", "identical-logic"):
        low, expected, high = monthly * .5, monthly * .8, monthly
    elif match in ("same-base-tables", "same-source"):
        expected, high = monthly * .3, monthly * .6
    elif match == "identical-live-query":
        expected, high = monthly * .3, monthly * .5
    confidence = "medium" if match in ("identical-build", "identical-logic") else "low"
    risk = "high" if match in ("same-base-tables", "same-source") else "medium"
    what = MATCH_LABELS.get(match, "overlap")
    if total > 0:
        why = (f"{n} elements {what}, so the same work is done {n} times. "
               f"Roughly {redundant:.2f} of {total:.2f} credits in this window are "
               f"repeat work (about ${monthly:,.0f}/month at list price).")
    else:
        why = (f"{n} elements {what}, so the same work is done {n} times"
               + (f" (about {per_day:g} scheduled refreshes a day in total)"
                  if per_day else "")
               + ". No cost data in this run, so savings are not estimated.")
    if match in ("same-base-tables", "same-source"):
        why += (" Their SQL differs, so check the logic really overlaps before "
                "combining.")
    if match == "identical-live-query":
        why += " Materialize it once in a data model instead of querying live each time."
    ref = str(group.get("SOURCE_REF") or "")
    if ref.startswith("data-model:"):
        why += (f" They all start from the data-model element {ref[len('data-model:'):]}: "
                "materialize that data-model element (or a new data-model element "
                "with these columns) once instead of in each workbook.")
    if n >= 3:
        why += (" A table in your warehouse (for example a Snowflake dynamic table "
                "or dbt model) may fit better if other tools also need this data.")
    validation = (
        "Build the shared logic once in a data model, materialize it there, "
        "repoint the listed elements, keep the old schedules until reads move, "
        "then retire them under the removal safeguards.")
    result = _result(
        "Consolidate into data model", why, confidence, risk,
        "Repointed elements read one shared table: expect similar or better load "
        "time; the shared refresh now serves all of them.",
        validation, low, expected, high,
    )
    _finish(result, 0, cfg)
    if ref.startswith("data-model:"):
        result["FIX_STEPS"] = [
            f"Open the data model and turn on materialization for the element "
            f"{ref[len('data-model:'):]}, or add a new data-model element with "
            "the columns these workbooks use and materialize that.",
            "Repoint each listed workbook element to the data-model element "
            "instead of materializing its own copy.",
            "Keep the old schedules until readers have moved over, then turn them "
            "off with the owner's approval.",
        ]
    return result


def score(row, config=None):
    cfg = merge_config(config)
    r = coerce_row(row)
    credits = (r.get("CREDITS", 0) + r.get("QAS_CREDITS", 0)
               + r.get("MATERIALIZATION_CREDITS", 0)
               + r.get("MATERIALIZATION_QAS_CREDITS", 0))
    p95 = r.get("P95_SEC") or r.get("AVG_SEC")
    runs = r.get("RUNS", 0) + r.get("MATERIALIZATION_RUNS", 0)
    if r.get("MEMBERS"):
        credits = group_credits(r)[1]
        runs = sum(_num(m.get("REFRESHES_PER_DAY")) for m in r["MEMBERS"]) * len(r["MEMBERS"])
    action = str(r.get("ACTION") or r.get("RECOMMENDATION") or "")
    action_weight = {
        "Remove materialization candidate": 500,
        "Retune materialization": 400,
        "Consolidate into data model": 450,
        "Investigate materialization": 350,
        "Optimize query/model": 300,
        "Optimize then reassess": 300,
        "Add materialization candidate": 250,
        "Keep materialization": 100,
        "Monitor": 0,
    }.get(action, 0)
    goal = cfg.get("goal")
    if goal == "cost":
        return credits * 1000 + runs * 0.05 + action_weight
    if goal == "latency":
        return p95 * 100 + credits * 100 + action_weight
    return credits * 1000 + p95 * 25 + runs * 0.05 + action_weight
