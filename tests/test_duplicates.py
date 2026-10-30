import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from analyze import (_mkey, _normalise_scan_groups, apply_scan, assemble_duplicates,  # noqa: E402
                     canon, merge_groups, scan_candidates)

U = "https://app.example.com/northwind-demo"


def drow(match, sig, wb, el="master", runs=30, credits=3.0, tables="NORTHWIND.SALES.ORDERS"):
    return {"MATCH": match, "SIGNATURE": sig, "ORG": "northwind-demo",
            "OBJECT": f"/workbook/{wb}", "ELEMENT": el,
            "SAMPLE_URL": f"{U}/workbook/{wb}?:displayNodeId={el}",
            "RUNS": runs, "CREDITS": credits, "BASE_TABLES": tables}


def workload(wb, el="master"):
    return {"ORG": "northwind-demo", "OBJECT": f"/workbook/{wb}", "ELEMENT": el,
            "RUNS": 30, "CREDITS": 0.1, "AVG_SEC": 1, "P95_SEC": 2, "MAX_BYTES": 1,
            "SAMPLE_URL": f"{U}/workbook/{wb}?:displayNodeId={el}"}


def mat(wb, el="master", credits=3.0, runs=30):
    return {"ORG": "northwind-demo", "OBJECT": f"/workbook/{wb}", "ELEMENT": el,
            "SAMPLE_URL": f"{U}/workbook/{wb}?:displayNodeId={el}",
            "MATERIALIZATION_RUNS": runs, "MATERIALIZATION_CREDITS": credits,
            "MATCHED_MATERIALIZED_READS": 100, "MATCHED_READ_P95_SEC": 1.0}


def scan_fixture():
    """Synthetic scan.json: two copies of a scheduled Northwind workbook."""
    def wb(url_id, owner):
        return {"WORKBOOK_ID": f"id-{url_id}", "WORKBOOK_URL_ID": url_id,
                "NAME": f"Northwind Orders {url_id}", "URL": f"{U}/workbook/{url_id}",
                "OWNER_ID": "m1", "OWNER_EMAIL": owner, "SOURCES": [],
                "ELEMENTS": [{"ELEMENT_ID": "master", "NAME": "Orders",
                              "SQL_SIGNATURE": "abc", "BASE_TABLES": ["NORTHWIND.SALES.ORDERS"],
                              "SCHEDULE_CRON": "0 * * * *", "SCHEDULE_TIMEZONE": "UTC",
                              "SCHEDULE_PAUSED": False, "REFRESHES_PER_DAY": 24},
                             {"ELEMENT_ID": "paused", "NAME": "Old",
                              "SCHEDULE_CRON": "0 0 * * *", "SCHEDULE_PAUSED": True,
                              "REFRESHES_PER_DAY": 1}]}
    members = [{"OBJECT": f"/workbook/{u}", "ELEMENT": "master", "SAMPLE_URL": f"{U}/workbook/{u}",
                "RUNS": 0, "CREDITS": 0.0, "REFRESHES_PER_DAY": 24, "OWNER_EMAIL": o}
               for u, o in (("wbAAA", "analyst@example.com"), ("wbBBB", "other@example.com"))]
    return {"scan_version": 1, "generated": "2026-01-01T00:00:00+00:00",
            "base_url_host": "api.example.com", "org": "northwind-demo",
            "workbooks": [wb("wbAAA", "analyst@example.com"), wb("wbBBB", "other@example.com")],
            "duplicate_groups": [{
                "GROUP_ID": "scan-1", "MATCH": "identical-sql", "SOURCE": "scan",
                "BASE_TABLES": ["NORTHWIND.SALES.ORDERS"], "MEMBERS": members,
                "MEMBER_COUNT": 2}],
            "warnings": []}


def analyze(tmp, *args):
    out = Path(tmp) / "out"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "analyze.py"),
                    "--credit-price", "3", "--out", str(out), *args],
                   check=True, capture_output=True, text=True)
    return (json.loads((out / "inventory.json").read_text()),
            (out / "REPORT.md").read_text())


class AssembleTests(unittest.TestCase):
    def test_canon_ignores_name_slug(self):
        self.assertEqual(canon("/workbook/Sales-Copy-AbC123"), canon("/workbook/abc123"))

    def test_groups_by_signature_and_redundant_credits(self):
        groups = assemble_duplicates([
            drow("identical-build", "s1", "wbA"), drow("identical-build", "s1", "wbB"),
            drow("identical-build", "s1", "wbC", credits=1.0),
        ], 30)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["MEMBERS"]), 3)
        self.assertEqual(groups[0]["BASE_TABLES"], ["NORTHWIND.SALES.ORDERS"])

    def test_same_base_tables_dropped_when_explained_by_one_build(self):
        rows = [drow("identical-build", "s1", "wbA"), drow("identical-build", "s1", "wbB"),
                drow("same-base-tables", "t1", "wbA"), drow("same-base-tables", "t1", "wbB")]
        self.assertEqual([g["MATCH"] for g in assemble_duplicates(rows, 30)],
                         ["identical-build"])
        rows.append(drow("same-base-tables", "t1", "wbC", el="other"))
        # build {A,B} + base {A,B,C}: C alone is not a duplicate, nothing overlaps
        self.assertEqual([g["MATCH"] for g in assemble_duplicates(rows, 30)],
                         ["identical-build"])

    def test_same_base_tables_drops_members_claimed_by_a_build(self):
        rows = [drow("identical-build", "s1", "wbA"), drow("identical-build", "s1", "wbB"),
                *[drow("same-base-tables", "t1", wb) for wb in ("wbA", "wbB", "wbC", "wbD")]]
        groups = assemble_duplicates(rows, 30)
        by = {g["MATCH"]: g for g in groups}
        self.assertEqual(sorted(canon(m["OBJECT"]) for m in by["same-base-tables"]["MEMBERS"]),
                         ["wbc", "wbd"])
        claimed = [_mkey(m) for g in groups for m in g["MEMBERS"]]
        self.assertEqual(len(claimed), len(set(claimed)))

    def test_consolidation_savings_do_not_double_count_overlapping_groups(self):
        bundle = {
            "workload": [workload(wb) for wb in ("wbAAA", "wbBBB", "wbCCC")],
            "materializations": [mat(wb) for wb in ("wbAAA", "wbBBB", "wbCCC")],
            "duplicates": [drow("identical-build", "s1", wb) for wb in ("wbAAA", "wbBBB")]
            + [drow("same-base-tables", "t1", wb) for wb in ("wbAAA", "wbBBB", "wbCCC")],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.json"
            path.write_text(json.dumps(bundle))
            inv, _ = analyze(tmp, "--from-rows", str(path))
        (g,) = inv["duplicate_groups"]
        self.assertEqual(g["MATCH"], "identical-build")
        self.assertEqual(inv["totals"]["consolidation_savings_expected"], g["SAVINGS_EXPECTED"])

    def test_live_query_groups_with_same_workbooks_merge_and_slug_variants_collapse(self):
        rows = [drow("identical-live-query", "q1", "wbA", credits=1),
                drow("identical-live-query", "q1", "wbB", credits=1),
                drow("identical-live-query", "q2", "wbA", credits=2),
                drow("identical-live-query", "q2", "wbB", credits=2),
                # one workbook seen under two name slugs is not a duplicate
                drow("identical-live-query", "q3", "Old-Name-wbC"),
                drow("identical-live-query", "q3", "New-Name-wbC")]
        groups = assemble_duplicates(rows, 30)
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(m["CREDITS"] for m in groups[0]["MEMBERS"]), [3.0, 3.0])

    def test_merge_snowflake_wins_and_fills_from_scan(self):
        sf = assemble_duplicates([drow("identical-build", "s1", "Orders-Copy-wbAAA"),
                                  drow("identical-build", "s1", "Orders-Copy-wbBBB")], 30)
        sf[0]["BASE_TABLES"] = []
        scan = scan_fixture()
        from analyze import _normalise_scan_groups
        merged = merge_groups(sf, _normalise_scan_groups(scan))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["SOURCE"], "snowflake")
        self.assertEqual(merged[0]["MATCH"], "identical-build")
        self.assertEqual(merged[0]["BASE_TABLES"], ["NORTHWIND.SALES.ORDERS"])
        self.assertIn("analyst@example.com", {m["OWNER_EMAIL"] for m in merged[0]["MEMBERS"]})

    def test_merge_upgrades_same_tables_to_scan_match(self):
        sf = assemble_duplicates([drow("same-base-tables", "s1", "Orders-Copy-wbAAA"),
                                  drow("same-base-tables", "s1", "Orders-Copy-wbBBB")], 30)
        merged = merge_groups(sf, _normalise_scan_groups(scan_fixture()))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["MATCH"], "identical-sql")

    def test_member_refresh_source_marks_cron_vs_observed(self):
        sf = assemble_duplicates([drow("identical-build", "s1", "Orders-Copy-wbAAA"),
                                  drow("identical-build", "s1", "Orders-Copy-wbBBB")], 30)
        self.assertEqual({m["REFRESHES_SOURCE"] for m in sf[0]["MEMBERS"]}, {"observed"})
        scan = scan_fixture()
        sg = _normalise_scan_groups(scan)
        self.assertEqual({m["REFRESHES_SOURCE"] for m in sg[0]["MEMBERS"]}, {"cron"})
        merged = merge_groups(sf, sg)
        for m in merged[0]["MEMBERS"]:  # the scan's cron overwrote the window average
            self.assertEqual((m["REFRESHES_PER_DAY"], m["REFRESHES_SOURCE"]), (24, "cron"))

    def test_apply_scan_copies_paused_and_scan_candidates_carry_the_cron(self):
        scan = scan_fixture()
        rows = [{"OBJECT": "/workbook/" + scan["workbooks"][0]["WORKBOOK_URL_ID"],
                 "ELEMENT": "paused"}]
        apply_scan(rows, scan)
        self.assertIs(rows[0]["SCHEDULE_PAUSED"], True)
        self.assertEqual(rows[0]["REFRESHES_PER_DAY"], 1)
        (c, *_) = scan_candidates(scan)
        self.assertEqual((c["SCHEDULE_CRON"], c["SCHEDULE_PAUSED"]), ("0 * * * *", False))

    def test_merge_keeps_scan_downstream_elements(self):
        sf = assemble_duplicates([drow("identical-build", "s1", "Orders-Copy-wbAAA"),
                                  drow("identical-build", "s1", "Orders-Copy-wbBBB")], 30)
        scan = scan_fixture()
        scan["duplicate_groups"][0].update({
            "DOWNSTREAM_COUNT": 2,
            "DOWNSTREAM_ELEMENTS": [{"ELEMENT_ID": "kpi", "NAME": "Revenue"},
                                    {"ELEMENT_ID": "bar", "NAME": "By Region"}]})
        from analyze import _normalise_scan_groups
        merged = merge_groups(sf, _normalise_scan_groups(scan))
        self.assertEqual(merged[0]["DOWNSTREAM_COUNT"], 2)
        self.assertEqual(len(merged[0]["DOWNSTREAM_ELEMENTS"]), 2)
        # cron-derived refreshes/day from the scan beat the Snowflake window average
        self.assertEqual({m["REFRESHES_PER_DAY"] for m in merged[0]["MEMBERS"]}, {24})

    def test_cheap_weak_groups_are_low_value_but_builds_are_not(self):
        from analyze import finalize_groups
        from cost_model import merge_config
        rows = [drow("identical-live-query", "q1", "Scratch-wbA", runs=1, credits=0.001),
                drow("identical-live-query", "q1", "Scratch-wbB", runs=1, credits=0.001),
                drow("identical-build", "b1", "Orders-wbC", credits=0.001),
                drow("identical-build", "b1", "Orders-wbD", credits=0.001)]
        groups = finalize_groups(assemble_duplicates(rows, 30), [], merge_config(), 30, 3.0)
        flags = {g["MATCH"]: g["LOW_VALUE"] for g in groups}
        self.assertTrue(flags["identical-live-query"])
        self.assertFalse(flags["identical-build"])

    def test_candidate_keeps_highest_scoring_consolidation_group(self):
        from analyze import finalize_groups
        from cost_model import merge_config
        rows = [drow(match, match, wb, credits=3.0)
                for match in ("identical-build", "identical-live-query")
                for wb in ("wbA", "wbB")]
        candidates = [workload(wb) for wb in ("wbA", "wbB")]
        groups = finalize_groups(
            assemble_duplicates(rows, 30), candidates, merge_config(), 30, 3.0)
        best = groups[0]
        self.assertEqual(best["MATCH"], "identical-build")
        self.assertEqual({r["CONSOLIDATION_GROUP"] for r in candidates},
                         {best["GROUP_ID"]})

    def test_unscheduled_scan_group_is_not_absorbed_and_superset_is(self):
        from analyze import _normalise_scan_groups
        sf = assemble_duplicates([drow("identical-build", "s1", "Orders-wbAAA"),
                                  drow("identical-build", "s1", "Orders-wbBBB")], 30)
        scan = scan_fixture()
        extra = {"OBJECT": "/workbook/wbCCC", "ELEMENT": "master", "RUNS": 0, "CREDITS": 0.0,
                 "REFRESHES_PER_DAY": None}
        scan["duplicate_groups"][0]["MEMBERS"].append(extra)
        other = json.loads(json.dumps(scan["duplicate_groups"][0]))
        for m in other["MEMBERS"]:
            m["ELEMENT"], m["REFRESHES_PER_DAY"] = "el-chart", None
        scan["duplicate_groups"].append(other)
        merged = merge_groups(sf, _normalise_scan_groups(scan))
        self.assertEqual([g["SOURCE"] for g in merged], ["snowflake", "scan"])
        self.assertEqual(len(merged[0]["MEMBERS"]), 3)


    def test_live_query_group_never_absorbs_a_scan_group(self):
        from analyze import _normalise_scan_groups
        live = assemble_duplicates([drow("identical-live-query", "q1", "Orders-wbAAA"),
                                    drow("identical-live-query", "q1", "Orders-wbBBB")], 30)
        merged = merge_groups(live, _normalise_scan_groups(scan_fixture()))
        self.assertEqual([g["SOURCE"] for g in merged], ["snowflake", "scan"])
        self.assertEqual(merged[0]["MATCH"], "identical-live-query")


class SqlTests(unittest.TestCase):
    def test_build_signature_keeps_t_mat_reads_and_files_stay_in_sync(self):
        from analyze import DUPLICATE_SQL
        text = (ROOT / "sql" / "sigma_duplicates.sql").read_text()
        region = lambda t: t[t.index("mat as ("):t.index("member_bases as")]
        for t in (DUPLICATE_SQL, text):
            self.assertNotIn("'t_mat'", t)
            self.assertIn("T_MAT_", region(t))  # write-back exclusion on schema or table
            self.assertIn("regexp_like(upper(f.value:\"objectName\"::string)", t)
        self.assertEqual(region(DUPLICATE_SQL), region(text))

    def test_every_window_ends_at_the_attribution_watermark(self):
        import analyze
        for name in ("SQL", "COVERAGE_SQL", "MATERIALIZATION_SQL", "DUPLICATE_SQL",
                     "WAREHOUSE_SQL"):
            t = getattr(analyze, name)
            self.assertNotIn("current_timestamp", t, name)
            self.assertEqual(t.count("-{DAYS}, {END})"), t.count("start_time <= {END}"), name)
            self.assertGreater(t.count("start_time <= {END}"), 0, name)
        for f in sorted((ROOT / "sql").glob("*.sql")):
            t = f.read_text()
            body = t[t.index("set end_ts"):].split(";", 1)[1]
            self.assertNotIn("current_timestamp", body, f.name)
            self.assertEqual(body.count("-$days, $end_ts)"), body.count("start_time <= $end_ts"),
                             f.name)

    def test_attribution_end_literal_lag_and_fallback(self):
        import analyze
        from unittest import mock
        end_ms = 1_800_000_000_000
        with mock.patch.object(analyze, "run_sql",
                               return_value=[{"END_MS": end_ms, "NOW_MS": end_ms + 5_400_000}]):
            lit, iso, lag = analyze.attribution_end("c", 30)
        self.assertEqual(lit, f"to_timestamp_ltz({end_ms}, 3)")
        self.assertTrue(iso.startswith("2027-01-15T08:00:00"))
        self.assertEqual(lag, 1.5)
        for rows in ([{"END_MS": None, "NOW_MS": end_ms}], RuntimeError("no access")):
            kw = {"side_effect": rows} if isinstance(rows, Exception) else {"return_value": rows}
            with mock.patch.object(analyze, "run_sql", **kw):
                self.assertEqual(analyze.attribution_end("c", 30),
                                 ("current_timestamp()", None, None))


class PipelineTests(unittest.TestCase):
    def test_cost_table_keyed_by_workbook_id_picks_up_scan_metadata(self):
        scan = scan_fixture()
        for w, wid in zip(scan["workbooks"], ("wbidAAA1", "wbidBBB2")):
            w["WORKBOOK_ID"] = wid
            w["WORKBOOK_URL_ID"] = "Orders-" + w["WORKBOOK_URL_ID"]
        for m in scan["duplicate_groups"][0]["MEMBERS"]:
            m["OBJECT"] = "/workbook/Orders-" + m["OBJECT"].rsplit("/", 1)[-1]
        table = [{"workbook_id": wid, "element_id": "master", "credits": 4, "query_count": 30,
                  "kind": "materialization"} for wid in ("wbidAAA1", "wbidBBB2")]
        with tempfile.TemporaryDirectory() as tmp:
            sp, tp = Path(tmp) / "scan.json", Path(tmp) / "costs.json"
            sp.write_text(json.dumps(scan))
            tp.write_text(json.dumps(table))
            inv, _ = analyze(tmp, "--from-cost-table", str(tp), "--scan", str(sp))
        for r in inv["candidates"]:
            self.assertIn("example.com", r["OWNER_EMAIL"])
            self.assertEqual(r["REFRESHES_PER_DAY"], 24)
            self.assertEqual(r["CONSOLIDATION_GROUP"], "scan-1")
        (g,) = inv["duplicate_groups"]
        self.assertEqual(g["TOTAL_CREDITS"], 8.0)
        self.assertEqual(g["REDUNDANT_CREDITS"], 4.0)


    def test_rows_bundle_with_duplicates(self):
        bundle = {
            "workload": [workload("wbAAA"), workload("wbBBB"), workload("wbCCC")],
            "materializations": [mat("wbAAA"), mat("wbBBB"), mat("wbCCC")],
            "duplicates": [drow("identical-build", "s1", wb, credits=3.0)
                           for wb in ("wbAAA", "wbBBB", "wbCCC")],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.json"
            path.write_text(json.dumps(bundle))
            inv, md = analyze(tmp, "--from-rows", str(path))
        self.assertEqual(inv["evidence"], "rows")
        (g,) = inv["duplicate_groups"]
        self.assertEqual(g["GROUP_ID"], "sf-1")
        self.assertEqual(g["MATCH"], "identical-build")
        self.assertEqual(g["MEMBER_COUNT"], 3)
        self.assertEqual(g["TOTAL_CREDITS"], 9.0)
        self.assertEqual(g["REDUNDANT_CREDITS"], 6.0)
        self.assertEqual(g["ACTION"], "Consolidate into data model")
        self.assertEqual(g["CONFIDENCE"], "medium")
        self.assertAlmostEqual(g["SAVINGS_EXPECTED"], 6.0 * 3 * 0.8 * 30 / 30, places=2)
        self.assertEqual(inv["totals"]["consolidation_savings_expected"], g["SAVINGS_EXPECTED"])
        per_element = sum(r["SAVINGS_EXPECTED"] for r in inv["candidates"])
        self.assertEqual(inv["totals"]["estimated_monthly_savings"], round(per_element, 2))
        self.assertTrue(all(r.get("CONSOLIDATION_GROUP") == "sf-1" for r in inv["candidates"]))
        self.assertIn("## Duplicate work", md)
        self.assertIn("sf-1", md)
        self.assertIn("Impact", md)

    def test_sigma_only_from_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan_path = Path(tmp) / "scan.json"
            scan_path.write_text(json.dumps(scan_fixture()))
            inv, md = analyze(tmp, "--sigma-only", "--scan", str(scan_path))
        self.assertEqual(inv["evidence"], "sigma-only")
        self.assertEqual(inv["totals"]["credits"], 0)
        # paused schedules are not candidates
        self.assertEqual(sorted(r["OBJECT"] for r in inv["candidates"]),
                         ["/workbook/wbAAA", "/workbook/wbBBB"])
        for r in inv["candidates"]:
            self.assertEqual(r["ACTION"], "Investigate materialization")
            self.assertFalse(r["READ_EVIDENCE_AVAILABLE"])
            self.assertTrue(r["SAMPLE_URL"].endswith(r["OBJECT"]))
            self.assertEqual(r["HAS_EXISTING_SCHEDULE"], True)
            self.assertEqual(r["REFRESHES_PER_DAY"], 24)
            self.assertEqual(r["CONSOLIDATION_GROUP"], "scan-1")
        (g,) = inv["duplicate_groups"]
        self.assertEqual(g["GROUP_ID"], "scan-1")
        self.assertEqual(g["SOURCE"], "scan")
        self.assertEqual(g["SAVINGS_EXPECTED"], 0)
        self.assertIn("No cost data", g["WHY"])
        self.assertEqual(inv["totals"]["consolidation_savings_expected"], 0)
        self.assertTrue(any("No cost data" in n for n in inv["evidence_notes"]))

    def test_scan_groups_get_credits_from_snowflake_candidates(self):
        bundle = {"workload": [workload("Orders-wbAAA"), workload("Orders-wbBBB")],
                  "materializations": [mat("Orders-wbAAA", credits=4.0),
                                       mat("Orders-wbBBB", credits=4.0)]}
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = Path(tmp) / "bundle.json"
            bundle_path.write_text(json.dumps(bundle))
            scan_path = Path(tmp) / "scan.json"
            scan_path.write_text(json.dumps(scan_fixture()))
            inv, _ = analyze(tmp, "--from-rows", str(bundle_path), "--scan", str(scan_path))
        (g,) = inv["duplicate_groups"]
        self.assertEqual(g["GROUP_ID"], "scan-1")
        self.assertEqual(g["TOTAL_CREDITS"], 8.0)
        self.assertEqual(g["REDUNDANT_CREDITS"], 4.0)
        self.assertAlmostEqual(g["SAVINGS_EXPECTED"], 4.0 * 3 * 0.8, places=2)
        self.assertEqual(g["CONFIDENCE"], "low")
        owners = {r["OBJECT"]: r.get("OWNER_EMAIL") for r in inv["candidates"]}
        self.assertEqual(owners["/workbook/Orders-wbAAA"], "analyst@example.com")

    def test_sample_bundle_has_duplicate_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv, _ = analyze(tmp, "--from-rows", str(ROOT / "example" / "sample-rows.json"))
        self.assertTrue(inv["duplicate_groups"])


if __name__ == "__main__":
    unittest.main()
