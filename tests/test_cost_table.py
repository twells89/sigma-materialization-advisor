import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from analyze import cost_table_rows, read_cost_table  # noqa: E402

CSV = """workbook_id,data_model_id,element_id,date,credits,query_count,kind,owner_email
wbAAA,,master,2026-01-01,1.5,10,materialization,analyst@example.com
wbAAA,,master,2026-01-10,1.5,10,materialization,analyst@example.com
wbBBB,,,2026-01-05,0.5,40,,
,dmCCC,,2026-01-05,0.2,5,,
"""


def analyze(*args):
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        subprocess.run([sys.executable, str(ROOT / "scripts" / "analyze.py"),
                        "--credit-price", "3", "--out", str(out), *args],
                       check=True, capture_output=True, text=True)
        return (json.loads((out / "inventory.json").read_text()),
                (out / "REPORT.md").read_text())


class CostTableTests(unittest.TestCase):
    def test_aggregates_per_object_and_element(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "costs.csv"
            path.write_text(CSV)
            rows, window, warnings = cost_table_rows(
                read_cost_table(str(path)), 3.0)
        self.assertEqual(window, 10)
        by = {(r["OBJECT"], r["ELEMENT"]): r for r in rows}
        mat = by[("/workbook/wbAAA", "master")]
        self.assertEqual(mat["MATERIALIZATION_CREDITS"], 3.0)
        self.assertEqual(mat["MATERIALIZATION_RUNS"], 20)
        self.assertTrue(mat["HAS_EXISTING_SCHEDULE"])
        self.assertFalse(mat["READ_EVIDENCE_AVAILABLE"])
        self.assertEqual(mat["OWNER_EMAIL"], "analyst@example.com")
        read = by[("/workbook/wbBBB", "(workbook load)")]
        self.assertEqual((read["RUNS"], read["CREDITS"]), (40, 0.5))
        self.assertIn(("/data-model/dmCCC", "(workbook load)"), by)
        self.assertEqual(warnings, [])

    def test_cost_column_divides_by_credit_price(self):
        rows, _, _ = cost_table_rows(
            [{"object": "https://app.example.com/northwind-demo/workbook/Sales-abc?x=1",
              "cost": "30", "query_count": "3"}], 3.0)
        self.assertEqual(rows[0]["OBJECT"], "/workbook/Sales-abc")
        self.assertEqual(rows[0]["CREDITS"], 10.0)
        self.assertTrue(rows[0]["SAMPLE_URL"].startswith("https://"))

    def test_rows_without_id_or_cost_are_skipped_with_warning(self):
        rows, _, warnings = cost_table_rows([{"credits": "1"}, {"workbook_id": "x"}], 3.0)
        self.assertEqual(rows, [])
        self.assertEqual(len(warnings), 1)

    def test_cli_cost_table_scheduled_element_is_investigated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "costs.json"
            path.write_text(json.dumps([
                {"Workbook_ID": "wbAAA", "Element_ID": "master", "Credits": 9,
                 "Query_Count": 30, "Kind": "materialization"},
                {"workbook_id": "wbBBB", "credits": 3, "query_count": 100,
                 "p95_sec": 4},
            ]))
            inv, md = analyze("--from-cost-table", str(path))
        self.assertEqual(inv["schema_version"], 2)
        self.assertEqual(inv["evidence"], "cost-table")
        self.assertTrue(inv["evidence_notes"])
        actions = {r["OBJECT"]: r["ACTION"] for r in inv["candidates"]}
        self.assertEqual(actions["/workbook/wbAAA"], "Investigate materialization")
        self.assertNotIn("Remove", " ".join(actions.values()))
        self.assertIn("Evidence: **cost-table**", md)
        for r in inv["candidates"]:
            self.assertIn(r["IMPACT"], {"High", "Medium", "Low"})
            self.assertTrue(r["FIX_STEPS"])

    def test_limit_trims_candidates_but_totals_use_whole_table(self):
        records = [{"workbook_id": f"wb{i:03d}", "credits": i / 100, "query_count": 5}
                   for i in range(1, 61)]
        records.append({"workbook_id": "wbMAT", "element_id": "master", "credits": 0.001,
                        "query_count": 4, "kind": "materialization"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "costs.json"
            path.write_text(json.dumps(records))
            inv, _ = analyze("--from-cost-table", str(path), "--limit", "10")
        objects = {r["OBJECT"] for r in inv["candidates"]}
        self.assertEqual(len(objects), 11)  # top 10 by credits + the scheduled element
        self.assertIn("/workbook/wb060", objects)
        self.assertIn("/workbook/wbMAT", objects)
        self.assertAlmostEqual(inv["coverage"]["all_sigma_credits"],
                               sum(i / 100 for i in range(1, 61)) + 0.001, places=3)
        self.assertEqual(inv["coverage"]["all_sigma_queries"], 60 * 5 + 4)

    def run_table(self, records, *args):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "costs.json"
            path.write_text(json.dumps(records))
            return analyze("--from-cost-table", str(path), *args)[0]

    def test_table_without_query_count_still_yields_candidates(self):
        inv = self.run_table([
            {"workbook_id": "wbAAA", "credits": 2.0},
            {"workbook_id": "wbBBB", "element_id": "master", "credits": 1.0,
             "kind": "materialization"},
            {"workbook_id": "wbCCC", "credits": 0}])
        self.assertEqual({r["OBJECT"] for r in inv["candidates"]},
                         {"/workbook/wbAAA", "/workbook/wbBBB"})
        self.assertEqual(inv["coverage"]["attributable_credits"], 2.0)
        self.assertEqual(inv["totals"]["credit_coverage_pct"], 100.0)

    def test_numeric_zero_credits_row_is_kept(self):
        rows, _, warnings = cost_table_rows(
            [{"workbook_id": "wbAAA", "credits": 0, "query_count": 5}], 3.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(warnings, [])

    def test_window_single_date_and_explicit_days(self):
        _, window, _ = cost_table_rows(
            [{"workbook_id": "a", "credits": 1, "date": "2026-01-15"}], 3.0)
        self.assertEqual(window, 1)
        rec = [{"workbook_id": "a", "credits": 1, "query_count": 9, "date": d}
               for d in ("2026-01-02", "2026-01-11")]
        self.assertEqual(self.run_table(rec)["days"], 10)
        inv = self.run_table(rec, "--days", "30")
        self.assertEqual(inv["days"], 30)
        self.assertTrue(any("--days" in w for w in inv["warnings"]))

    def test_monthly_looking_dates_warn(self):
        _, window, warnings = cost_table_rows(
            [{"workbook_id": "a", "credits": 1, "date": d}
             for d in ("2026-01-01", "2026-02-01")], 3.0)
        self.assertEqual(window, 32)
        self.assertTrue(any("monthly" in w for w in warnings))
        _, _, ok = cost_table_rows(
            [{"workbook_id": "a", "credits": 1, "date": d}
             for d in ("2026-01-01", "2026-01-02")], 3.0)
        self.assertEqual(ok, [])

    def test_modes_are_mutually_exclusive_and_sigma_only_needs_scan(self):
        for args in (["--sigma-only"],
                     ["--from-rows", "a", "--from-cost-table", "b"]):
            p = subprocess.run([sys.executable, str(ROOT / "scripts" / "analyze.py"), *args],
                               capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
