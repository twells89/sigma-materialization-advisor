import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("render_html", ROOT / "scripts" / "render-html.py")
render_html = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_html)

ORDERS = "/workbook/Orders-Overview-abc123"
RETURNS = "/workbook/Returns-Review-777"
MART = "/data-model/Sales-Mart-def456"


def cand(obj, owner=None, action="Retune materialization", savings=10.0, **kw):
    c = {"OBJECT": obj, "ELEMENT": "(workbook load)", "RUNS": 50, "TOTAL_CREDITS": 2.0, "P95_SEC": 3.0,
         "MATERIALIZATION_RUNS": 4, "MATCHED_MATERIALIZED_READS": 10, "ACTION": action,
         "WHY": "Because it is expensive.", "VALIDATION": "Compare p95 before and after.",
         "SAVINGS_EXPECTED": savings, "SAMPLE_URL": f"https://app.example.com/northwind-demo{obj}"}
    if owner:
        c["OWNER_EMAIL"] = owner
    c.update(kw)
    return c


def member(obj, owner=None):
    return {"OBJECT": obj, "ELEMENT": "Orders table", "SAMPLE_URL": f"https://app.example.com/northwind-demo{obj}",
            "RUNS": 20, "CREDITS": 1.5, "REFRESHES_PER_DAY": 4.0, "OWNER_EMAIL": owner}


def group(owner_a="analyst@example.com", owner_b="analyst@example.com"):
    return {"GROUP_ID": "dup-1", "MATCH": "identical-build", "SOURCE": "snowflake",
            "BASE_TABLES": ["NORTHWIND.SALES.ORDERS"], "MEMBER_COUNT": 2, "TOTAL_CREDITS": 3.0,
            "REDUNDANT_CREDITS": 1.5, "ACTION": "Consolidate into data model", "CONFIDENCE": "medium",
            "WHY": "Two workbooks build the same table.", "SAVINGS_LOW": 5.0, "SAVINGS_EXPECTED": 8.0,
            "SAVINGS_HIGH": 12.0, "IMPACT": "Medium",
            "FIX_STEPS": ["Build the shared logic once in a data model.", "Repoint both workbooks."],
            "VALIDATION": "Keep old schedules until reads move.",
            "MEMBERS": [member(ORDERS, owner_a), member(RETURNS, owner_b)]}


def base_inv(**extra):
    inv = {"schema_version": 2, "generated": "2026-10-01T00:00:00+00:00", "days": 30, "scope": "northwind-demo",
           "totals": {"queries": 100, "credits": 6.0, "dollars": 18.0, "credit_coverage_pct": 80.0,
                      "actions": {"Retune materialization": 2}, "estimated_monthly_savings": 20.0},
           "config": {"credit_price": 3.0}, "candidates": [cand(ORDERS), cand(RETURNS)]}
    inv.update(extra)
    return inv


class RenderTests(unittest.TestCase):
    def test_backward_compatible_with_sample_inventory(self):
        # Strip every key added after schema v2 shipped to emulate an older inventory.
        inv = json.loads((ROOT / "example" / "sample-inventory.json").read_text())
        for key in ("evidence", "evidence_notes", "duplicate_groups"):
            inv.pop(key, None)
        inv["totals"].pop("consolidation_savings_expected", None)
        for c in inv["candidates"]:
            for key in ("IMPACT", "FIX_STEPS", "CONSOLIDATION_GROUP", "OWNER_EMAIL",
                        "REFRESHES_PER_DAY", "READ_EVIDENCE_AVAILABLE"):
                c.pop(key, None)
        html = render_html.render(inv, "Northwind Trading Co.")
        self.assertIn("Performance per Dollar", html)
        self.assertIn("QUERY_ATTRIBUTION_HISTORY", html)
        self.assertNotIn("Duplicate work", html)
        self.assertNotIn("class='banner'", html)
        self.assertNotIn("class='imp ", html)
        self.assertNotIn("<ol class='steps'>", html)
        self.assertNotIn("Consolidation (separate)", html)
        self.assertIn("kpis\">", html)

    def test_kpi_grid_cannot_overflow_a_narrow_screen(self):
        css = render_html.PAGE
        self.assertIn("repeat(2,minmax(0,1fr))", css)
        self.assertIn("repeat(4,minmax(0,1fr))", css)
        self.assertIn("repeat(5,minmax(0,1fr))", css)
        self.assertIn("repeat(3,minmax(0,1fr))", css)
        self.assertIn(".kpi-v{{font-size:24px;font-weight:700;color:var(--accent);"
                      "overflow-wrap:anywhere}}", css)
        self.assertNotIn(",1fr)", css.replace("minmax(0,1fr)", ""))

    def test_impact_pill_and_fix_steps(self):
        inv = base_inv()
        inv["candidates"][0].update(IMPACT="High", FIX_STEPS=["Lower the refresh frequency.", "Re-check p95."])
        html = render_html.render(inv, "Northwind")
        self.assertIn("High impact", html)
        self.assertIn("<ol class='steps'><li>Lower the refresh frequency.</li><li>Re-check p95.</li></ol>", html)

    def test_duplicate_section_and_separate_kpi(self):
        inv = base_inv(duplicate_groups=[group()])
        inv["totals"]["consolidation_savings_expected"] = 8.0
        inv["candidates"][0]["CONSOLIDATION_GROUP"] = "dup-1"
        html = render_html.render(inv, "Northwind")
        self.assertIn("Duplicate work", html)
        self.assertIn("identical-build", html)
        self.assertIn("NORTHWIND.SALES.ORDERS", html)
        self.assertIn("Repoint both workbooks.", html)
        self.assertIn("$5 to $12/mo (expected $8)", html)
        self.assertIn("Consolidation (separate)", html)
        self.assertIn("kpis k5", html)
        self.assertIn("$20/mo", html)  # headline modeled savings unchanged, consolidation not added
        self.assertNotIn("$28/mo", html)
        self.assertIn("Part of duplicate group dup-1", html)
        self.assertIn(f"href='https://app.example.com/northwind-demo{RETURNS}'", html)

    def test_downstream_line(self):
        g = group()
        g.update(DOWNSTREAM_COUNT=2, DOWNSTREAM_ELEMENTS=[
            {"ELEMENT_ID": "e1", "NAME": "Revenue <by> Region"}, {"ELEMENT_ID": "e2", "NAME": "Orders"}])
        html = render_html.render(base_inv(duplicate_groups=[g]), "Northwind")
        self.assertIn("2 downstream elements follow this logic: Revenue &lt;by&gt; Region, Orders", html)
        self.assertNotIn("downstream elements", render_html.render(base_inv(duplicate_groups=[group()]), "Northwind"))

    def test_sigma_only_group_shows_no_cost_data(self):
        g = group()
        g.update(TOTAL_CREDITS=0, SAVINGS_LOW=0, SAVINGS_EXPECTED=0, SAVINGS_HIGH=0, MATCH="identical-sql")
        for m in g["MEMBERS"]:
            m["CREDITS"] = 0
        inv = base_inv(duplicate_groups=[g], evidence="sigma-only",
                       evidence_notes=["No cost data: workbooks scanned through the Sigma API only."])
        html = render_html.render(inv, "Northwind")
        self.assertIn("No cost data", html)
        self.assertNotIn("$0 to $0/mo", html)

    def test_evidence_banner_and_footer(self):
        inv = base_inv(evidence="cost-table",
                       evidence_notes=["Read utilization not measured: cost-table input."])
        html = render_html.render(inv, "Northwind")
        self.assertIn("class='banner'", html)
        self.assertIn("Supplied cost table", html)
        self.assertIn("Read utilization not measured: cost-table input.", html)
        self.assertNotIn("QUERY_ATTRIBUTION_HISTORY", html)
        self.assertIn("supplied cost table", html)
        snow = render_html.render(base_inv(evidence="snowflake"), "Northwind")
        self.assertIn("QUERY_ATTRIBUTION_HISTORY", snow)

    def test_escaping(self):
        inv = base_inv(evidence="cost-table", evidence_notes=["<script>alert(1)</script>"])
        inv["candidates"][0]["WHY"] = "<img src=x onerror=alert(1)>"
        inv["candidates"][0]["FIX_STEPS"] = ["<b>bold</b>"]
        inv["duplicate_groups"] = [dict(group(), WHY="<i>x</i>")]
        html = render_html.render(inv, "<Co>")
        for raw in ("<script>alert", "<img src=x", "<b>bold</b>", "<i>x</i>", "<Co>"):
            self.assertNotIn(raw, html)

    def test_owner_filter(self):
        inv = base_inv(duplicate_groups=[group(owner_b="other@example.com")])
        inv["candidates"] = [cand(ORDERS, "analyst@example.com"), cand(RETURNS, "other@example.com"),
                             cand(MART)]
        mine = render_html.render(inv, "Northwind", owner="Analyst@Example.com")
        self.assertIn("Orders-Overview-abc123", mine)
        self.assertNotIn("Sales-Mart-def456", mine)
        self.assertIn("owner: Analyst@Example.com", mine)
        self.assertIn("Duplicate work", mine)
        unassigned = render_html.render(inv, "Northwind", owner="")
        self.assertIn("Sales-Mart-def456", unassigned)
        self.assertNotIn("Duplicate work", unassigned)

    def test_by_owner_cli(self):
        inv = base_inv(duplicate_groups=[group(owner_b="other@example.com")])
        inv["candidates"] = [cand(ORDERS, "analyst@example.com", savings=7.0),
                             cand(RETURNS, "other@example.com", savings=3.0), cand(MART)]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "inventory.json"
            p.write_text(json.dumps(inv))
            outdir = Path(tmp) / "owners"
            subprocess.run([sys.executable, str(ROOT / "scripts" / "render-html.py"), "--inv", str(p),
                            "--by-owner", str(outdir)], check=True, capture_output=True, text=True)
            rows = list(csv.DictReader((outdir / "owners.csv").read_text().splitlines()))
            self.assertEqual([r["owner_email"] for r in rows], ["analyst@example.com", "other@example.com", "unassigned"])
            by = {r["owner_email"]: r for r in rows}
            self.assertEqual(by["analyst@example.com"]["candidates"], "1")
            self.assertEqual(by["analyst@example.com"]["groups"], "1")
            self.assertEqual(float(by["analyst@example.com"]["expected_savings"]), 7.0)
            self.assertEqual(by["unassigned"]["groups"], "0")
            for r in rows:
                self.assertTrue((outdir / r["file"]).exists())
            self.assertNotIn("example.com", rows[0]["file"])
            self.assertIn("Sales-Mart-def456", (outdir / "report-unassigned.html").read_text())

    def test_cli_requires_out_or_by_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "inventory.json"
            p.write_text(json.dumps(base_inv()))
            proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "render-html.py"), "--inv", str(p)],
                                  capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
