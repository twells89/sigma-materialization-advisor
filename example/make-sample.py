#!/usr/bin/env python3
"""Regenerate the fully synthetic Northwind v2 bundle and committed reports."""
import json, os, subprocess, sys
HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
U = "https://app.sigmacomputing.com/northwind"


def row(obj, el, runs, credits, avg, p95, mx, mb, url, **extra):
    return {"ORG": "northwind", "OBJECT": obj, "ELEMENT": el, "RUNS": runs,
            "CREDITS": credits, "AVG_SEC": avg, "P50_SEC": avg * .7, "P95_SEC": p95,
            "MAX_SEC": mx, "TOTAL_SEC": round(avg * runs, 1), "MAX_BYTES": mb,
            "BYTES_SCANNED": mb * runs, "BYTES_SPILLED": 0,
            "PARTITION_SCAN_PCT": 25, "SAMPLE_URL": url, **extra}


workload = [
    # Add: repeated and costly, with no existing schedule.
    row("/data-model/Sales-Mart-DM-aaa111", "(workbook load)", 240, 18.4, 2.1, 4.0, 6.0,
        400_000_000, f"{U}/data-model/Sales-Mart-DM-aaa111", QAS_CREDITS=2.0),
    row("/workbook/Customer-Overview-jjj000", "orders", 180, 4.2, 2.2, 4.5, 6.5,
        350_000_000,
        f"{U}/workbook/Customer-Overview-jjj000?:displayNodeId=orders"),
    # Keep: high utilization and fast matched reads.
    row("/workbook/Exec-Revenue-Daily-bbb222", "master", 510, 1.2, .7, 1.3, 2.1,
        2_500_000_000, f"{U}/workbook/Exec-Revenue-Daily-bbb222?:displayNodeId=master"),
    # Retune: many refreshes, almost no matched reads.
    row("/workbook/Ops-Live-Tiles-ccc333", "kpi-row", 80, .3, .7, 2.0, 3.1,
        300_000_000, f"{U}/workbook/Ops-Live-Tiles-ccc333?:displayNodeId=kpi-row"),
    # Investigate: controls exist and target bindings are unresolved.
    row("/workbook/Finance-Snapshot-hhh888", "master", 60, .6, 1.1, 3.0, 4.0,
        80_000_000, f"{U}/workbook/Finance-Snapshot-hhh888?:displayNodeId=master",
        HAS_CONTROLS=True, CONTROL_TARGETS_RESOLVED=False),
    # Remove candidate: only because a measured no-materialization baseline is present.
    row("/workbook/Legacy-Snapshot-iii999", "table-main", 40, .5, .8, 1.5, 2.0,
        100_000_000, f"{U}/workbook/Legacy-Snapshot-iii999?:displayNodeId=table-main",
        COUNTERFACTUAL_P95_SEC=3.2, COUNTERFACTUAL_CREDITS=.4),
    # Optimize: slow, high scan, poor pruning.
    row("/workbook/Adhoc-Cohort-Explorer-ddd444", "cohort-tbl", 5, 1.1, 14.7, 18.0,
        22.0, 9_500_000_000,
        f"{U}/workbook/Adhoc-Cohort-Explorer-ddd444?:displayNodeId=cohort-tbl",
        PARTITION_SCAN_PCT=94),
    # Three copies of one scheduled workbook: duplicate work (see `duplicates` below).
    row("/workbook/Orders-Daily-Copy-A-mmm111", "master", 120, .6, .8, 1.4, 2.0,
        90_000_000, f"{U}/workbook/Orders-Daily-Copy-A-mmm111?:displayNodeId=master"),
    row("/workbook/Orders-Daily-Copy-B-nnn222", "master", 90, .5, .8, 1.4, 2.0,
        90_000_000, f"{U}/workbook/Orders-Daily-Copy-B-nnn222?:displayNodeId=master"),
    row("/workbook/Orders-Daily-Copy-C-ooo333", "master", 40, .2, .8, 1.4, 2.0,
        90_000_000, f"{U}/workbook/Orders-Daily-Copy-C-ooo333?:displayNodeId=master"),
    # Monitor: cheap and fast.
    row("/workbook/Marketing-Overview-fff666", "chart-1", 64, 0.18, 0.4, .7, 0.9,
        40_000_000, f"{U}/workbook/Marketing-Overview-fff666?:displayNodeId=chart-1"),
    row("/workbook/HR-Headcount-ggg777", "table-main", 22, 0.09, 0.3, .5, 0.7,
        12_000_000, f"{U}/workbook/HR-Headcount-ggg777?:displayNodeId=table-main"),
]


def mat(obj, el, url, runs, credits, served, matched_p95, unmatched, unmatched_p95):
    return {
        "ORG": "northwind", "OBJECT": obj, "ELEMENT": el, "SAMPLE_URL": url,
        "MATERIALIZATION_RUNS": runs, "MATERIALIZATION_CREDITS": credits,
        "MATERIALIZATION_QAS_CREDITS": 0,
        "MATERIALIZATION_P95_SEC": 12,
        "MATCHED_MATERIALIZED_READS": served, "MATCHED_READ_CREDITS": .05,
        "MATCHED_READ_QAS_CREDITS": 0, "MATCHED_READ_P95_SEC": matched_p95,
        "WORKBOOK_UNMATCHED_READS": unmatched,
        "WORKBOOK_UNMATCHED_READ_CREDITS": .2,
        "WORKBOOK_UNMATCHED_READ_QAS_CREDITS": 0,
        "WORKBOOK_UNMATCHED_READ_P95_SEC": unmatched_p95,
    }


materializations = [
    mat("/workbook/Exec-Revenue-Daily-bbb222", "master",
        f"{U}/workbook/Exec-Revenue-Daily-bbb222?:displayNodeId=master",
        30, 1.2, 500, 1.3, 10, 8.0),
    mat("/workbook/Ops-Live-Tiles-ccc333", "kpi-row",
        f"{U}/workbook/Ops-Live-Tiles-ccc333?:displayNodeId=kpi-row",
        90, 2.5, 4, 1.0, 76, 9.0),
    mat("/workbook/Finance-Snapshot-hhh888", "master",
        f"{U}/workbook/Finance-Snapshot-hhh888?:displayNodeId=master",
        30, 1.0, 20, 1.2, 40, 7.0),
    mat("/workbook/Legacy-Snapshot-iii999", "table-main",
        f"{U}/workbook/Legacy-Snapshot-iii999?:displayNodeId=table-main",
        30, 8.0, 40, 1.5, 0, 0),
] + [
    mat(f"/workbook/Orders-Daily-Copy-{c}-{i}", "master",
        f"{U}/workbook/Orders-Daily-Copy-{c}-{i}?:displayNodeId=master",
        30, 2.0, served, 1.1, 5, 6.0)
    for c, i, served in (("A", "mmm111", 90), ("B", "nnn222", 60), ("C", "ooo333", 25))
]


def dup(match, signature, obj, el, runs, credits, tables=None):
    return {"MATCH": match, "SIGNATURE": signature, "ORG": "northwind",
            "OBJECT": obj, "ELEMENT": el,
            "SAMPLE_URL": f"{U}{obj}?:displayNodeId={el}", "RUNS": runs,
            "CREDITS": credits, "BASE_TABLES": tables}


ORDERS = "NORTHWIND.SALES.CUSTOMERS,NORTHWIND.SALES.ORDERS"
duplicates = [
    # Identical CTAS text in three copies of one workbook.
    dup("identical-build", "sig-orders-daily", f"/workbook/Orders-Daily-Copy-{c}-{i}",
        "master", 30, 2.0, ORDERS)
    for c, i in (("A", "mmm111"), ("B", "nnn222"), ("C", "ooo333"))
] + [
    # Different SQL, same source table.
    dup("same-base-tables", "sig-ledger", "/workbook/Finance-Snapshot-hhh888",
        "master", 30, 1.0, "NORTHWIND.FINANCE.LEDGER"),
    dup("same-base-tables", "sig-ledger", "/workbook/Legacy-Snapshot-iii999",
        "table-main", 30, 8.0, "NORTHWIND.FINANCE.LEDGER"),
    # The same live query from two dashboards.
    dup("identical-live-query", "sig-live-orders", "/workbook/Customer-Overview-jjj000",
        "orders", 180, 4.2),
    dup("identical-live-query", "sig-live-orders", "/workbook/Marketing-Overview-fff666",
        "chart-1", 64, .18),
]

bundle = {
    "workload": workload,
    "coverage": [{
        "ALL_SIGMA_QUERIES": 2500, "ALL_SIGMA_CREDITS": 45.0,
        "ALL_QAS_CREDITS": 5.0,
        "ATTRIBUTABLE_QUERIES": 981, "ATTRIBUTABLE_CREDITS": 23.37,
        "ATTRIBUTABLE_QAS_CREDITS": 2.0,
        "MATERIALIZATION_QUERIES": 120, "MATERIALIZATION_CREDITS": 4.0,
        "MATERIALIZATION_QAS_CREDITS": 1.0,
        "UNATTRIBUTED_QUERIES": 1399, "UNATTRIBUTED_CREDITS": 17.63,
        "UNATTRIBUTED_QAS_CREDITS": 2.0,
    }],
    "materializations": materializations,
    "duplicates": duplicates,
    "warehouses": [{
        "WAREHOUSE_COMPUTE_CREDITS": 70.0,
        "ATTRIBUTED_QUERY_CREDITS": 45.0,
        "ESTIMATED_IDLE_CREDITS": 25.0,
        "ESTIMATED_IDLE_PCT": 35.71,
        "WAREHOUSES": 2,
    }],
}
out = os.path.join(HERE, "sample-rows.json")
json.dump(bundle, open(out, "w"), indent=2)
print("wrote", out, f"({len(workload)} workload rows)")
subprocess.run([
    sys.executable, os.path.join(ROOT, "scripts", "analyze.py"),
    "--from-rows", out, "--credit-price", "3",
    "--goal", "performance_per_dollar", "--out", HERE,
], check=True)
os.replace(os.path.join(HERE, "inventory.json"),
           os.path.join(HERE, "sample-inventory.json"))
os.replace(os.path.join(HERE, "REPORT.md"),
           os.path.join(HERE, "sample-REPORT.md"))
subprocess.run([
    sys.executable, os.path.join(ROOT, "scripts", "render-html.py"),
    "--inv", os.path.join(HERE, "sample-inventory.json"),
    "--out", os.path.join(HERE, "sample-report.html"),
    "--customer", "Northwind Trading Co. (sample)",
], check=True)
