import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, fn):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / fn)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


compare = load("compare", "compare.py")
rs = load("render_savings", "render-savings.py")

ORDERS = "/workbook/Orders-Overview-abc123"
RETURNS = "/workbook/Returns-Review-777"
MART = "/data-model/Sales-Mart-def456"
A, B = "analyst@example.com", "lead@example.com"


def cand(obj, action, mat_runs, mat_cr, owner=None, cron=None, **kw):
    c = {"OBJECT": obj, "ELEMENT": "(workbook load)", "RUNS": 100, "CREDITS": 1.0,
         "MATERIALIZATION_RUNS": mat_runs, "MATERIALIZATION_CREDITS": mat_cr,
         "TOTAL_CREDITS": 1.0 + mat_cr, "P95_SEC": 2.0, "ACTION": action,
         "SIGMA_ENRICHMENT": "matched", "SAVINGS_EXPECTED": 10.0, "IMPACT": "Medium",
         "SAMPLE_URL": f"https://app.example.com/northwind-demo{obj}"}
    if cron:
        c.update(SCHEDULE_CRON=cron, SCHEDULE_PAUSED=False, HAS_EXISTING_SCHEDULE=True)
    if owner:
        c["OWNER_EMAIL"] = owner
    c.update(kw)
    return c


def inv(cands, gen="2026-10-01T00:00:00+00:00", days=30):
    return {"schema_version": 2, "generated": gen, "days": days, "scope": "northwind-demo",
            "evidence": "snowflake",
            "config": {"credit_price": 3.0, "latency_slo_sec": 5.0, "acceptable_slowdown_sec": 1.0},
            "totals": {"credits": 10.0, "estimated_monthly_savings": 25.0,
                       "consolidation_savings_expected": 9.0},
            "candidates": cands}


def result(gen_after="2026-11-05T00:00:00+00:00", days_after=30):
    before = inv([cand(MART, "Retune materialization", 2880, 72.0, A, "*/15 * * * *"),
                  cand(ORDERS, "Retune materialization", 720, 72.0, B, "0 * * * *"),
                  cand(RETURNS, "Optimize query/model", 0, 0.0, B)])
    after = inv([cand(MART, "Retune materialization", 30, 3.0, A, "0 7 * * *"),
                 cand(ORDERS, "Retune materialization", 720, 72.0, B, "0 * * * *"),
                 cand(RETURNS, "Optimize query/model", 0, 0.0, B)], gen_after, days_after)
    after["duplicate_groups"] = [{
        "GROUP_ID": "dup-1", "ACTION": "Consolidate into data model", "SAVINGS_EXPECTED": 9.0,
        "IMPACT": "Low", "MEMBERS": [{"OBJECT": ORDERS, "ELEMENT": "(workbook load)",
                                      "OWNER_EMAIL": B, "SAMPLE_URL": "https://app.example.com/x"}]}]
    return compare.compare(before, after)


class RenderSavingsTests(unittest.TestCase):
    def test_sections_kpis_and_status_order(self):
        h = rs.render(result(), "Northwind")
        for text in ("Realized savings", "From actions taken", "Follow-through rate",
                     "Remaining opportunity", "<h2>What changed</h2>", "<h2>Savings breakdown</h2>",
                     "<h2>By owner</h2>", "<h2>Still open</h2>", "not proven to be caused",
                     "$207/mo", "Northwind"):
            self.assertIn(text, h)
        self.assertLess(h.index(">Acted on<"), h.index(">Not yet<"))
        self.assertIn("every 15 minutes (96/day)", h)
        self.assertIn("lead@example.com", h)
        self.assertIn("Duplicate group dup-1 (1 members)", h)
        self.assertNotIn("reliable read", h)  # 35 days apart

    def test_breakdown_buckets_equal_headline(self):
        res = result()
        ft = res["follow_through"]
        self.assertAlmostEqual(ft["savings_acted_on"] + ft["savings_improved"]
                               + ft["savings_other"], res["totals"]["headline_savings"], places=2)
        h = rs.render(res, "Northwind")
        self.assertIn("Tied to actions taken", h)
        self.assertIn("Other changes, not tied to a recommendation", h)

    def test_untouched_recommendations_are_capped(self):
        idle = [cand(f"/workbook/Northwind-Report-{i:03d}", "Optimize query/model", 0, 0.0)
                for i in range(30)]
        before = inv([cand(MART, "Retune materialization", 2880, 72.0, A, "*/15 * * * *")] + idle)
        after = inv([cand(MART, "Retune materialization", 30, 3.0, A, "0 7 * * *")] + idle,
                    "2026-11-05T00:00:00+00:00")
        h = rs.render(compare.compare(before, after), "Northwind")
        self.assertEqual(h.count("pill not-yet'>"), rs.MAX_IDLE_ROWS)
        self.assertIn(">Acted on<", h)
        self.assertIn("15 more not-yet or not-observed recommendations", h)

    def test_banners(self):
        h = rs.render(result("2026-10-05T00:00:00+00:00", days_after=14), "Northwind")
        self.assertIn("Measured 4.0 days after the baseline", h)
        self.assertIn("wait at least 14 days (2 weeks) for a reliable read", h)
        self.assertIn("different windows", h)
        res = result()
        res["warnings"] = ["scope differs (before='a', after='b'); forced."]
        self.assertIn("scope differs", rs.render(res, "Northwind"))

    def test_no_schedule_data_banner(self):
        res = result()
        res["follow_through"]["schedule_data"] = False
        self.assertIn("Neither inventory carries schedule data", rs.render(res, "x"))

    def test_everything_is_escaped(self):
        res = result()
        res["follow_through"]["rows"][0].update(
            DETAIL="<img src=x onerror=alert(1)>", OWNER_EMAIL="<b>evil</b>@example.com",
            SAMPLE_URL="https://app.example.com/x'onclick='alert(1)", OBJECT="/workbook/<i>x</i>")
        res["measurement"]["after_generated"] = "<u>t</u>"
        res["scope"] = "<scope>"
        res["warnings"] = ["<script>alert(2)</script>"]
        res["remaining"]["top"][0]["ACTION"] = "<s>act</s>"
        h = rs.render(res, "<script>alert(3)</script>", owner=None)
        for bad in ("<img", "<b>evil", "<script", "<i>x", "<u>t", "<s>act", "'onclick='"):
            self.assertNotIn(bad, h)
        self.assertIn("&lt;script&gt;alert(3)", h)
        # the customer name cannot inject a template token into the page
        self.assertIn("Report - @kpis@</title>", rs.render(res, "@kpis@"))

    def test_responsive_css_shared_with_the_main_report(self):
        h = rs.render(result(), "Northwind")
        self.assertIn("@media (max-width:860px)", h)
        self.assertIn("overflow-x:auto", h)
        self.assertIn("width=device-width", h)
        self.assertIn("max-width:980px", h)  # render-html.py's own rule, not a forked copy
        self.assertIn(rs.rh.PAGE.split("<style>")[1].split("\n")[0].replace("{{", "{")
                      .replace("}}", "}"), h)

    def test_kpi_grid_uses_minmax_and_no_duplicate_rule(self):
        h = rs.render(result(), "Northwind")
        self.assertIn("repeat(2,minmax(0,1fr))", h)
        self.assertIn("overflow-wrap:anywhere", h)
        self.assertNotIn("k4", h)
        self.assertNotIn(",1fr)", h.replace("minmax(0,1fr)", ""))

    def test_realized_label_and_unknown_interval_banner(self):
        self.assertIn("coincides with changes; not proven caused", rs.render(result(), "x"))
        res = result()
        res["measurement"]["too_soon"] = None
        res["measurement"]["days_between"] = None
        h = rs.render(res, "x")
        self.assertIn("Measurement interval unknown", h)
        self.assertNotIn("Measured None", h)

    def test_old_compare_json_without_follow_through(self):
        res = result()
        for k in ("follow_through", "remaining", "measurement"):
            del res[k]
        h = rs.render(res, "Northwind")
        self.assertIn("before follow-through tracking", h)
        self.assertIn("Realized savings", h)
        self.assertNotIn("<h2>What changed</h2>", h)
        self.assertIn("<h2>Savings breakdown</h2>", h)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(rs.owner_reports(res, "Northwind", tmp), [])
            self.assertTrue((Path(tmp) / "owners.csv").exists())

    def test_latency_regressions_listed_as_warnings(self):
        res = result()
        res["objects"][0]["LATENCY_REGRESSION"] = True
        res["objects"][0].update(P95_BEFORE=2.0, P95_AFTER=9.0)
        h = rs.render(res, "Northwind")
        self.assertIn("1 latency regression:", h)
        self.assertIn("9.00", h)

    def test_owner_view_filters_rows(self):
        h = rs.render(result(), "Northwind", owner=A)
        self.assertIn("owner: analyst@example.com", h)
        self.assertIn("Sales-Mart", h)
        self.assertNotIn("Returns-Review", h)
        self.assertNotIn("<h2>By owner</h2>", h)
        self.assertNotIn("<h2>Savings breakdown</h2>", h)

    def test_by_owner_files_and_csv(self):
        res = result()
        with tempfile.TemporaryDirectory() as tmp:
            rows = rs.owner_reports(res, "Northwind", tmp)
            self.assertEqual([r[0] for r in rows], [A, B])
            self.assertTrue((Path(tmp) / "savings-1.html").exists())
            self.assertTrue((Path(tmp) / "savings-2.html").exists())
            got = list(csv.DictReader((Path(tmp) / "owners.csv").read_text().splitlines()))
            self.assertEqual(list(got[0]), ["owner_email", "file", "acted_on", "not_yet",
                                            "savings_acted_on"])
            self.assertEqual((got[0]["owner_email"], got[0]["acted_on"], got[0]["savings_acted_on"]),
                             (A, "1", "207.0"))
            self.assertEqual((got[1]["acted_on"], got[1]["not_yet"]), ("0", "2"))

    def test_unassigned_owner_gets_its_own_file(self):
        res = result()
        for r in res["follow_through"]["rows"]:
            r["OWNER_EMAIL"] = None
        with tempfile.TemporaryDirectory() as tmp:
            rows = rs.owner_reports(res, "Northwind", tmp)
            self.assertEqual([r[:2] for r in rows], [["unassigned", "savings-unassigned.html"]])

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "compare.json"
            src.write_text(json.dumps(result()))
            out = Path(tmp) / "sub" / "savings.html"
            proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "render-savings.py"),
                                   "--compare", str(src), "--out", str(out), "--customer", "Northwind",
                                   "--by-owner", str(Path(tmp) / "owners")],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Northwind", out.read_text())
            self.assertTrue((Path(tmp) / "owners" / "owners.csv").exists())
            bad = subprocess.run([sys.executable, str(ROOT / "scripts" / "render-savings.py"),
                                  "--compare", str(src)], capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)


if __name__ == "__main__":
    unittest.main()
