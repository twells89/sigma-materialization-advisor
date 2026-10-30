import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("compare", ROOT / "scripts" / "compare.py")
compare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare)

ORDERS = "/workbook/Orders-Overview-abc123"
MART = "/data-model/Sales-Mart-def456"


def cand(obj, element="(workbook load)", runs=100, credits=1.0, p95=2.0, mat_runs=0, action="Monitor"):
    return {"OBJECT": obj, "ELEMENT": element, "RUNS": runs, "MATERIALIZATION_RUNS": mat_runs,
            "TOTAL_CREDITS": credits, "P95_SEC": p95, "ACTION": action,
            "SAMPLE_URL": f"https://app.example.com/northwind-demo{obj}"}


def inv(cands, days=30, price=3.0, scope="northwind-demo", evidence="snowflake", credits=None):
    return {"schema_version": 2, "generated": "2026-10-01T00:00:00+00:00", "days": days, "scope": scope,
            "evidence": evidence,
            "config": {"credit_price": price, "latency_slo_sec": 5.0, "acceptable_slowdown_sec": 1.0},
            "totals": {"credits": credits if credits is not None else sum(c["TOTAL_CREDITS"] for c in cands)},
            "candidates": cands}


def by_obj(res):
    return {(r["OBJECT"], r["ELEMENT"]): r for r in res["objects"]}


class CompareTests(unittest.TestCase):
    def test_efficiency_savings_and_status(self):
        before = inv([cand(ORDERS, runs=100, credits=2.0)])
        after = inv([cand(ORDERS, runs=100, credits=1.0)])
        res = compare.compare(before, after)
        r = by_obj(res)[(ORDERS, "(workbook load)")]
        self.assertEqual(r["STATUS"], "improved")
        self.assertAlmostEqual(r["MONTHLY_COST_BEFORE"], 6.0)
        self.assertAlmostEqual(r["MONTHLY_COST_AFTER"], 3.0)
        self.assertAlmostEqual(r["COST_CHANGE"], -3.0)
        self.assertAlmostEqual(r["EFFICIENCY_SAVINGS"], 3.0)
        self.assertFalse(r["TRAFFIC_SHIFT"])
        self.assertAlmostEqual(res["totals"]["headline_efficiency_savings"], 3.0)

    def test_normalises_different_windows_and_prices(self):
        # same real behaviour: 100 runs/30d at 2 credits vs 200 runs/60d at 4 credits, price differs
        before = inv([cand(ORDERS, runs=100, credits=2.0)], days=30, price=3.0)
        after = inv([cand(ORDERS, runs=200, credits=4.0)], days=60, price=4.5)
        res = compare.compare(before, after)
        r = by_obj(res)[(ORDERS, "(workbook load)")]
        self.assertAlmostEqual(r["MONTHLY_RUNS_BEFORE"], 100.0)
        self.assertAlmostEqual(r["MONTHLY_RUNS_AFTER"], 100.0)
        self.assertAlmostEqual(r["MONTHLY_COST_BEFORE"], 6.0)
        self.assertAlmostEqual(r["MONTHLY_COST_AFTER"], 9.0)  # 4 cr * 4.5 * 30/60
        self.assertAlmostEqual(r["RUNS_CHANGE_PCT"], 0.0)
        self.assertEqual(r["STATUS"], "regressed")  # cost per run rose 50% because of the price change

    def test_materialization_runs_counted(self):
        before = inv([cand(MART, runs=50, mat_runs=50, credits=2.0)])
        after = inv([cand(MART, runs=50, mat_runs=50, credits=2.0)])
        r = by_obj(compare.compare(before, after))[(MART, "(workbook load)")]
        self.assertAlmostEqual(r["MONTHLY_RUNS_AFTER"], 50.0)  # reads only
        self.assertAlmostEqual(r["MONTHLY_REFRESHES_AFTER"], 50.0)
        self.assertEqual(r["STATUS"], "unchanged")
        self.assertEqual(r["EFFICIENCY_SAVINGS"], 0.0)

    def test_traffic_shift_excluded_from_headline(self):
        before = inv([cand(ORDERS, runs=100, credits=2.0), cand(MART, runs=100, credits=2.0)])
        # ORDERS: runs halve and cost drops proportionally-ish; MART: genuinely cheaper per run, same traffic
        after = inv([cand(ORDERS, runs=40, credits=0.4), cand(MART, runs=100, credits=1.0)])
        res = compare.compare(before, after, traffic_shift_pct=25)
        rows = by_obj(res)
        o = rows[(ORDERS, "(workbook load)")]
        self.assertTrue(o["TRAFFIC_SHIFT"])
        self.assertAlmostEqual(o["RUNS_CHANGE_PCT"], -60.0)
        self.assertGreater(o["EFFICIENCY_SAVINGS"], 0)
        self.assertFalse(rows[(MART, "(workbook load)")]["TRAFFIC_SHIFT"])
        t = res["totals"]
        self.assertAlmostEqual(t["headline_efficiency_savings"], 3.0)
        self.assertGreater(t["raw_efficiency_savings"], t["headline_efficiency_savings"])
        self.assertEqual(t["traffic_shift_objects"], 1)
        # a looser threshold stops flagging it
        loose = compare.compare(before, after, traffic_shift_pct=80)
        self.assertEqual(loose["totals"]["traffic_shift_objects"], 0)
        self.assertAlmostEqual(loose["totals"]["headline_efficiency_savings"], t["raw_efficiency_savings"])

    def test_latency_regression(self):
        before = inv([cand(ORDERS, credits=2.0, p95=3.0)])
        after = inv([cand(ORDERS, credits=1.0, p95=7.5)])
        res = compare.compare(before, after)
        r = by_obj(res)[(ORDERS, "(workbook load)")]
        self.assertTrue(r["LATENCY_REGRESSION"])
        self.assertEqual(r["STATUS"], "regressed")
        self.assertEqual(res["totals"]["latency_regressions"], 1)
        # slower but inside slo + slowdown allowance is not a regression
        ok = by_obj(compare.compare(before, inv([cand(ORDERS, credits=1.0, p95=5.5)])))[(ORDERS, "(workbook load)")]
        self.assertFalse(ok["LATENCY_REGRESSION"])
        # already slow before and faster after is not a regression
        better = by_obj(compare.compare(inv([cand(ORDERS, p95=9.0)]), inv([cand(ORDERS, p95=8.0)])))
        self.assertFalse(better[(ORDERS, "(workbook load)")]["LATENCY_REGRESSION"])

    def test_not_observed_and_new(self):
        before = inv([cand(ORDERS, credits=2.0), cand(MART, credits=2.0)])
        after = inv([cand(ORDERS, credits=2.0), cand("/workbook/Returns-Review-777", credits=1.0)])
        res = compare.compare(before, after)
        rows = by_obj(res)
        gone = rows[(MART, "(workbook load)")]
        self.assertEqual(gone["STATUS"], "not-observed")
        self.assertEqual(gone["EFFICIENCY_SAVINGS"], 0.0)
        self.assertIsNone(gone["MONTHLY_COST_AFTER"])
        self.assertEqual(rows[("/workbook/Returns-Review-777", "(workbook load)")]["STATUS"], "new")
        self.assertEqual(res["totals"]["counts"]["not-observed"], 1)
        self.assertEqual(res["totals"]["headline_efficiency_savings"], 0.0)

    def test_refresh_retune_with_steady_reads_is_savings_not_traffic_shift(self):
        def refresh_cand(refreshes, refresh_credits):
            c = cand(MART, runs=100, credits=1.0, mat_runs=refreshes)
            c.update(CREDITS=1.0, QAS_CREDITS=0.0, MATERIALIZATION_CREDITS=refresh_credits,
                     MATERIALIZATION_QAS_CREDITS=0.0, TOTAL_CREDITS=1.0 + refresh_credits)
            return c
        # hourly -> daily: ~$219/mo -> ~$12/mo at $3/credit, 100 reads either way
        res = compare.compare(inv([refresh_cand(720, 72.0)]), inv([refresh_cand(30, 3.0)]))
        r = by_obj(res)[(MART, "(workbook load)")]
        self.assertFalse(r["TRAFFIC_SHIFT"])
        self.assertAlmostEqual(r["RUNS_CHANGE_PCT"], 0.0)
        self.assertAlmostEqual(r["MONTHLY_COST_BEFORE"], 219.0)
        self.assertAlmostEqual(r["MONTHLY_COST_AFTER"], 12.0)
        self.assertAlmostEqual(r["MONTHLY_READ_COST_AFTER"], 3.0)
        self.assertAlmostEqual(r["MONTHLY_REFRESH_COST_BEFORE"], 216.0)
        self.assertAlmostEqual(r["EFFICIENCY_SAVINGS"], 0.0)
        self.assertAlmostEqual(r["REFRESH_SAVINGS"], 207.0)
        self.assertEqual(r["STATUS"], "improved")
        t = res["totals"]
        self.assertAlmostEqual(t["headline_savings"], 207.0)
        self.assertAlmostEqual(t["headline_refresh_savings"], 207.0)
        self.assertAlmostEqual(t["headline_efficiency_savings"], 0.0)
        self.assertEqual(t["traffic_shift_objects"], 0)

    def test_refresh_savings_survive_a_traffic_shift(self):
        def c(reads, read_cr, refreshes, refresh_cr):
            x = cand(MART, runs=reads, credits=read_cr + refresh_cr, mat_runs=refreshes)
            x.update(CREDITS=read_cr, MATERIALIZATION_CREDITS=refresh_cr)
            return x
        res = compare.compare(inv([c(100, 1.0, 720, 72.0)]), inv([c(10, 0.05, 30, 3.0)]))
        r = by_obj(res)[(MART, "(workbook load)")]
        self.assertTrue(r["TRAFFIC_SHIFT"])
        t = res["totals"]
        self.assertAlmostEqual(t["headline_efficiency_savings"], 0.0)  # shifted: excluded
        self.assertAlmostEqual(t["headline_refresh_savings"], 207.0)
        self.assertAlmostEqual(t["headline_savings"], 207.0)
        self.assertGreater(t["raw_savings"], t["headline_savings"])
        self.assertEqual(r["COUNTED_SAVINGS"], 207.0)

    def test_missing_evidence_means_snowflake(self):
        old = inv([cand(ORDERS)])
        del old["evidence"]
        res = compare.compare(old, inv([cand(ORDERS)], evidence="snowflake"))
        self.assertEqual(res["warnings"], [])
        self.assertEqual(res["before"]["evidence"], "snowflake")
        res = compare.compare(old, inv([cand(ORDERS)], evidence="cost-table"))
        self.assertEqual(len(res["warnings"]), 1)

    def test_csv_and_markdown_carry_both_components(self):
        for col in ("REFRESH_SAVINGS", "COUNTED_SAVINGS", "MONTHLY_REFRESH_COST_AFTER"):
            self.assertIn(col, compare.CSV_COLUMNS)
        md = compare.markdown(compare.compare(inv([cand(ORDERS)]), inv([cand(ORDERS)])))
        self.assertIn("read efficiency", md)
        self.assertIn("Refresh savings", md)

    def test_warnings_for_evidence_and_days(self):
        res = compare.compare(inv([cand(ORDERS)], evidence="snowflake"),
                              inv([cand(ORDERS)], days=14, evidence="cost-table"))
        self.assertEqual(len(res["warnings"]), 2)

    def run_cli(self, tmp, before, after, *extra):
        paths = []
        for name, d in (("a.json", before), ("b.json", after)):
            p = Path(tmp) / name
            p.write_text(json.dumps(d))
            paths.append(str(p))
        out = Path(tmp) / "out"
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "compare.py"), "--before", paths[0],
                               "--after", paths[1], "--out", str(out), *extra],
                              capture_output=True, text=True)
        return proc, out

    def test_cli_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, out = self.run_cli(tmp, inv([cand(ORDERS, credits=2.0), cand(MART)]),
                                     inv([cand(ORDERS, credits=1.0)]))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            res = json.loads((out / "compare.json").read_text())
            self.assertAlmostEqual(res["totals"]["headline_efficiency_savings"], 3.0)
            rows = list(csv.DictReader((out / "compare.csv").read_text().splitlines()))
            self.assertEqual(len(rows), 2)
            self.assertEqual({r["STATUS"] for r in rows}, {"improved", "not-observed"})
            md = (out / "COMPARE.md").read_text()
            self.assertIn("Not-observed is not removed", md)
            self.assertIn("excluded from read efficiency", md)

    def test_scope_mismatch_refused_unless_forced(self):
        a, b = inv([cand(ORDERS)], scope="northwind-demo"), inv([cand(ORDERS)], scope="account-wide")
        with tempfile.TemporaryDirectory() as tmp:
            proc, out = self.run_cli(tmp, a, b)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("scope differs", proc.stderr)
            self.assertFalse((out / "compare.json").exists())
        with tempfile.TemporaryDirectory() as tmp:
            proc, out = self.run_cli(tmp, a, b, "--force")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            res = json.loads((out / "compare.json").read_text())
            self.assertTrue(any("scope differs" in w for w in res["warnings"]))

    def test_identical_inventories_are_all_unchanged(self):
        i = inv([cand(ORDERS), cand(MART, credits=3.0)])
        res = compare.compare(i, i)
        self.assertEqual({r["STATUS"] for r in res["objects"]}, {"unchanged"})
        self.assertEqual(res["totals"]["raw_efficiency_savings"], 0.0)


def rc(obj, action, refreshes=0, refresh_cr=0.0, reads=100, read_cr=1.0, element="(workbook load)", **kw):
    """A candidate with separated read and refresh credits (refresh is what retuning moves)."""
    c = cand(obj, element=element, runs=reads, credits=read_cr + refresh_cr, mat_runs=refreshes,
             action=action)
    c.update(CREDITS=read_cr, QAS_CREDITS=0.0, MATERIALIZATION_CREDITS=refresh_cr, **kw)
    return c


def sched(cron=None, paused=False, matched=True):
    c = {"SCHEDULE_PAUSED": paused if cron else None}
    if cron:
        c["SCHEDULE_CRON"] = cron
        c["HAS_EXISTING_SCHEDULE"] = True
    if matched:
        c["SIGMA_ENRICHMENT"] = "matched"
    return c


def one(res, obj=MART, element="(workbook load)"):
    return next(e for e in res["follow_through"]["rows"]
                if e["OBJECT"] == obj and e["ELEMENT"] == element and not e.get("GROUP_ID"))


def dup(gid, objs, action="Consolidate into data model", owner="analyst@example.com"):
    return {"GROUP_ID": gid, "ACTION": action, "MATCH": "identical-build",
            "MEMBERS": [{"OBJECT": o, "ELEMENT": "(workbook load)", "OWNER_EMAIL": owner,
                         "SAMPLE_URL": f"https://app.example.com/northwind-demo{o}"} for o in objs]}


def add_groups(i, *groups):
    i["duplicate_groups"] = list(groups)
    return i


def assert_adds_up(tc, res):
    ft = res["follow_through"]
    tc.assertAlmostEqual(ft["savings_acted_on"] + ft["savings_improved"] + ft["savings_other"],
                         res["totals"]["headline_savings"], places=2)
    tc.assertAlmostEqual(ft["headline_savings"], res["totals"]["headline_savings"])


class FollowThroughTests(unittest.TestCase):
    def test_retune_schedule_acted_on_with_plain_detail_and_savings(self):
        before = inv([rc(MART, "Retune materialization", 2880, 72.0, **sched("*/15 * * * *"))])
        after = inv([rc(MART, "Retune materialization", 30, 3.0, **sched("0 7 * * *"))])
        res = compare.compare(before, after)
        e = one(res)
        self.assertEqual((e["STATUS"], e["EVIDENCE"]), ("acted-on", "schedule"))
        self.assertIn("every 15 minutes (96/day)", e["DETAIL"])
        self.assertIn("daily (1/day)", e["DETAIL"])
        self.assertAlmostEqual(e["SAVINGS"], 207.0)
        self.assertEqual(e["OWNER_EMAIL"], None)
        ft = res["follow_through"]
        self.assertAlmostEqual(ft["savings_acted_on"], 207.0)
        self.assertEqual(ft["follow_through_rate"], 1.0)
        assert_adds_up(self, res)

    def test_retune_paused_reverted_and_unchanged(self):
        before = inv([rc(MART, "Retune materialization", 720, 72.0, **sched("0 * * * *"))])
        paused = compare.compare(before, inv([rc(MART, "Retune materialization", 0, 0.0,
                                                 **sched("0 * * * *", paused=True))]))
        self.assertEqual(one(paused)["STATUS"], "acted-on")
        self.assertIn("paused", one(paused)["DETAIL"])
        up = compare.compare(before, inv([rc(MART, "Retune materialization", 720, 72.0,
                                             **sched("*/30 * * * *"))]))
        self.assertEqual((one(up)["STATUS"], one(up)["EVIDENCE"]), ("reverted", "schedule"))
        same = compare.compare(before, before)
        self.assertEqual(one(same)["STATUS"], "not-yet")
        self.assertEqual(same["follow_through"]["follow_through_rate"], 0.0)

    def test_metrics_fallback_when_no_schedule_data(self):
        before = inv([rc(MART, "Retune materialization", 720, 72.0)])
        res = compare.compare(before, inv([rc(MART, "Retune materialization", 30, 3.0)]))
        e = one(res)
        self.assertEqual((e["STATUS"], e["EVIDENCE"]), ("acted-on", "metrics"))
        self.assertIn("No schedule data", e["DETAIL"])
        self.assertFalse(res["follow_through"]["schedule_data"])
        # a drop under 30% is not enough
        mild = compare.compare(before, inv([rc(MART, "Retune materialization", 600, 60.0)]))
        self.assertEqual(one(mild)["STATUS"], "not-yet")

    def test_remove_and_add_materialization(self):
        before = inv([rc(MART, "Remove materialization candidate", 30, 3.0, **sched("0 7 * * *")),
                      rc(ORDERS, "Add materialization candidate", 0, 0.0, **sched())])
        gone = inv([rc(MART, "Remove materialization candidate", 0, 0.0, **sched()),
                    rc(ORDERS, "Add materialization candidate", 30, 3.0, **sched("0 7 * * *"))])
        res = compare.compare(before, gone)
        self.assertEqual((one(res)["STATUS"], one(res)["EVIDENCE"]), ("acted-on", "schedule"))
        self.assertIn("removed", one(res)["DETAIL"])
        self.assertEqual(one(res, ORDERS)["STATUS"], "acted-on")
        self.assertIn("now exists", one(res, ORDERS)["DETAIL"])
        still = compare.compare(before, before)
        self.assertEqual({e["STATUS"] for e in still["follow_through"]["rows"]}, {"not-yet"})
        # metrics fallback: no schedule fields anywhere
        b2 = inv([rc(MART, "Remove materialization candidate", 30, 3.0),
                  rc(ORDERS, "Add materialization candidate", 0, 0.0)])
        a2 = inv([rc(MART, "Remove materialization candidate", 0, 0.0),
                  rc(ORDERS, "Add materialization candidate", 30, 3.0)])
        m = compare.compare(b2, a2)
        self.assertEqual([(e["STATUS"], e["EVIDENCE"]) for e in m["follow_through"]["rows"]],
                         [("acted-on", "metrics")] * 2)

    def test_optimize_improved_needs_a_20_percent_drop(self):
        before = inv([cand(ORDERS, runs=100, credits=2.0, action="Optimize query/model"),
                      cand(MART, runs=100, credits=2.0, action="Optimize then reassess")])
        after = inv([cand(ORDERS, runs=100, credits=1.5), cand(MART, runs=100, credits=1.7)])
        res = compare.compare(before, after)
        self.assertEqual(one(res, ORDERS)["STATUS"], "improved")
        self.assertIn("not verified", one(res, ORDERS)["DETAIL"])
        self.assertEqual(one(res, MART)["STATUS"], "not-yet")  # -15%
        ft = res["follow_through"]
        self.assertAlmostEqual(ft["savings_improved"], 1.5)  # (0.02-0.015)*100 runs*$3 / 30d window
        self.assertEqual(ft["savings_acted_on"], 0.0)
        assert_adds_up(self, res)

    def test_group_consolidation_attributes_without_double_counting(self):
        objs = [ORDERS, MART, "/workbook/Returns-Review-777"]
        before = inv([rc(o, "Retune materialization", 720, 72.0, **sched("0 * * * *")) for o in objs])
        after = inv([rc(objs[0], "Retune materialization", 0, 0.0, **sched()),
                     rc(objs[1], "Retune materialization", 0, 0.0, **sched()),
                     rc(objs[2], "Retune materialization", 720, 72.0, **sched("0 * * * *"))])
        add_groups(before, dup("dup-1", objs))
        add_groups(after, dup("dup-1", objs))
        res = compare.compare(before, after)
        g = next(e for e in res["follow_through"]["rows"] if e.get("GROUP_ID"))
        self.assertEqual((g["STATUS"], g["EVIDENCE"]), ("acted-on", "schedule"))  # 2 of 3 removed
        self.assertIn("2 of 3", g["DETAIL"])
        self.assertEqual(len(g["MEMBERS"]), 3)
        self.assertAlmostEqual(g["SAVINGS"], 2 * 216.0)
        # members keep their own status but their savings are attributed to the group only
        for o in objs[:2]:
            self.assertEqual(one(res, o)["STATUS"], "acted-on")
            self.assertEqual(one(res, o)["SAVINGS"], 0.0)
        ft = res["follow_through"]
        self.assertAlmostEqual(sum(e["SAVINGS"] for e in ft["rows"]), 432.0)
        self.assertAlmostEqual(ft["savings_acted_on"], 432.0)
        self.assertAlmostEqual(ft["savings_other"], 0.0)
        assert_adds_up(self, res)

    def test_group_partly_and_not_yet(self):
        objs = [ORDERS, MART, "/workbook/Returns-Review-777"]
        mk = lambda removed: inv([
            rc(o, "Monitor", 0, 0.0, **sched()) if o in removed else
            rc(o, "Monitor", 720, 72.0, **sched("0 * * * *")) for o in objs])
        before = add_groups(mk([]), dup("dup-1", objs))
        part = compare.compare(before, add_groups(mk([ORDERS]), dup("dup-1", objs)))
        g = part["follow_through"]["rows"][0]
        self.assertEqual(g["STATUS"], "partly")
        self.assertAlmostEqual(g["SAVINGS"], 216.0)
        self.assertEqual(part["follow_through"]["follow_through_rate"], 1.0)
        none = compare.compare(before, add_groups(mk([]), dup("dup-1", objs)))
        self.assertEqual(none["follow_through"]["rows"][0]["STATUS"], "not-yet")
        assert_adds_up(self, part)

    def test_group_metrics_fallback(self):
        objs = [ORDERS, MART]
        mk = lambda n: inv([rc(o, "Monitor", n, 1.0) for o in objs])
        res = compare.compare(add_groups(mk(720), dup("dup-1", objs)),
                              add_groups(mk(100), dup("dup-1", objs)))
        g = res["follow_through"]["rows"][0]
        self.assertEqual((g["STATUS"], g["EVIDENCE"]), ("acted-on", "metrics"))

    def test_not_observed_is_never_acted_on(self):
        objs = [ORDERS, MART]
        before = add_groups(inv([rc(o, "Retune materialization", 720, 72.0, **sched("0 * * * *"))
                                 for o in objs]), dup("dup-1", objs))
        after = inv([cand("/workbook/Returns-Review-777")])
        res = compare.compare(before, after)
        ft = res["follow_through"]
        self.assertEqual({e["STATUS"] for e in ft["rows"]}, {"not-observed"})
        self.assertEqual(ft["savings_acted_on"], 0.0)
        self.assertEqual(ft["follow_through_rate"], 0.0)
        assert_adds_up(self, res)

    def test_non_actionable_counted_but_not_listed(self):
        before = inv([cand(ORDERS, action="Keep materialization"), cand(MART, action="Monitor"),
                      cand("/workbook/Returns-Review-777", action="Investigate materialization")])
        ft = compare.compare(before, before)["follow_through"]
        self.assertEqual(ft["rows"], [])
        self.assertEqual(ft["counts"], {"n/a": 3})
        self.assertEqual(ft["actionable"], 0)
        self.assertIsNone(ft["follow_through_rate"])

    def test_untied_savings_land_in_other_and_everything_adds_up(self):
        before = inv([rc(MART, "Retune materialization", 2880, 72.0, **sched("*/15 * * * *")),
                      rc(ORDERS, "Optimize query/model", 0, 0.0, reads=100, read_cr=2.0),
                      rc("/workbook/Returns-Review-777", "Monitor", 0, 0.0, reads=100, read_cr=2.0),
                      rc("/workbook/Budget-Plan-555", "Retune materialization", 720, 72.0,
                         **sched("0 * * * *"))])
        after = inv([rc(MART, "Retune materialization", 30, 3.0, **sched("0 7 * * *")),
                     rc(ORDERS, "Optimize query/model", 0, 0.0, reads=100, read_cr=1.0),
                     rc("/workbook/Returns-Review-777", "Monitor", 0, 0.0, reads=100, read_cr=1.0),
                     rc("/workbook/Budget-Plan-555", "Retune materialization", 100, 10.0,
                        **sched("0 * * * *"))])  # more refresh savings but the schedule is the same
        res = compare.compare(before, after)
        ft = res["follow_through"]
        self.assertAlmostEqual(ft["savings_acted_on"], 207.0)
        self.assertAlmostEqual(ft["savings_improved"], 3.0)
        self.assertEqual(one(res, "/workbook/Budget-Plan-555")["STATUS"], "not-yet")
        self.assertGreater(ft["savings_other"], 186.0)  # Monitor row + the not-yet refresh savings
        assert_adds_up(self, res)
        self.assertEqual(ft["counts"]["n/a"], 1)

    def test_by_owner_and_remaining(self):
        a, b = "analyst@example.com", "lead@example.com"
        before = inv([rc(MART, "Retune materialization", 2880, 72.0, **sched("*/15 * * * *"),
                         OWNER_EMAIL=a),
                      rc(ORDERS, "Retune materialization", 720, 72.0, **sched("0 * * * *"),
                         OWNER_EMAIL=b)])
        after_c = [rc(MART, "Retune materialization", 30, 3.0, **sched("0 7 * * *"), OWNER_EMAIL=a),
                   rc(ORDERS, "Retune materialization", 720, 72.0, **sched("0 * * * *"),
                      OWNER_EMAIL=b, SAVINGS_EXPECTED=40.0, IMPACT="High"),
                   rc("/workbook/Returns-Review-777", "Optimize query/model", SAVINGS_EXPECTED=5.0)]
        after = inv(after_c)
        after["totals"]["estimated_monthly_savings"] = 45.0
        after["totals"]["consolidation_savings_expected"] = 9.0
        res = compare.compare(before, after)
        by = res["follow_through"]["by_owner"]
        self.assertEqual(by[a]["acted_on"], 1)
        self.assertAlmostEqual(by[a]["savings_acted_on"], 207.0)
        self.assertEqual((by[b]["acted_on"], by[b]["not_yet"]), (0, 1))
        self.assertEqual(by[a]["recommendations"], 1)
        rem = res["remaining"]
        self.assertEqual(rem["open_recommendations"], 3)
        self.assertEqual(rem["estimated_monthly_savings"], 45.0)
        self.assertEqual(rem["consolidation_savings_expected"], 9.0)
        self.assertEqual(rem["top"][0]["OBJECT"], ORDERS)
        self.assertEqual(set(rem["top"][0]),
                         {"OBJECT", "ELEMENT", "ACTION", "IMPACT", "SAVINGS_EXPECTED",
                          "OWNER_EMAIL", "SAMPLE_URL"})

    def test_unassigned_owner_bucket(self):
        before = inv([cand(MART, action="Optimize query/model")])
        by = compare.compare(before, before)["follow_through"]["by_owner"]
        self.assertEqual(list(by), ["unassigned"])

    def test_measurement_too_soon_and_window(self):
        i = inv([cand(ORDERS)])
        j = inv([cand(ORDERS)], days=14)
        j["generated"] = "2026-10-08T12:00:00+00:00"  # 7.5 days later
        ms = compare.compare(i, j)["measurement"]
        self.assertEqual(ms["days_between"], 7.5)
        self.assertTrue(ms["too_soon"])
        self.assertFalse(ms["same_window"])
        self.assertEqual(ms["scope"], "northwind-demo")
        j["generated"] = "2026-10-31T00:00:00Z"
        ms = compare.compare(i, j)["measurement"]
        self.assertEqual(ms["days_between"], 30.0)
        self.assertFalse(ms["too_soon"])
        self.assertTrue(compare.compare(i, i)["measurement"]["too_soon"])
        # a lower threshold accepts a short gap; an unreadable timestamp is not "too soon"
        j["generated"] = "2026-10-08T00:00:00+00:00"
        self.assertFalse(compare.compare(i, j, too_soon_days=5)["measurement"]["too_soon"])
        j["generated"] = "yesterday"
        ms = compare.compare(i, j)["measurement"]
        self.assertIsNone(ms["days_between"])
        self.assertIsNone(ms["too_soon"])  # unknown, not "fine"

    def test_group_schedule_evidence_needs_a_cron_rate_on_both_sides(self):
        objs = [ORDERS, MART]

        def gm(o, rpd, src="cron"):
            m = {"OBJECT": o, "ELEMENT": "(workbook load)", "REFRESHES_PER_DAY": rpd}
            if src:
                m["REFRESHES_SOURCE"] = src
            return m

        def run(bm, am, refreshes_after=720):
            before = inv([rc(o, "Monitor", 720, 72.0) for o in objs])
            after = inv([rc(o, "Monitor", refreshes_after, 72.0) for o in objs])
            before["duplicate_groups"] = [{"GROUP_ID": "g", "ACTION": "Consolidate into data model",
                                           "MEMBERS": bm}]
            after["duplicate_groups"] = [{"GROUP_ID": "g", "ACTION": "Consolidate into data model",
                                          "MEMBERS": am}]
            return compare.compare(before, after)["follow_through"]["rows"][0]

        before = [gm(o, 24) for o in objs]
        # an unreadable after cron (None) is unknown, never "schedule removed"
        g = run(before, [gm(ORDERS, None), gm(MART, None)])
        self.assertEqual((g["STATUS"], g["EVIDENCE"]), ("not-yet", "metrics"))
        g = run(before, [gm(ORDERS, None), gm(MART, 24)])  # only the readable member counts
        self.assertEqual((g["STATUS"], g["DETAIL"][:2]), ("not-yet", "No"))
        # a Snowflake window average (observed) is not a schedule, even when it drops to 0
        g = run(before, [gm(ORDERS, 0.0, "observed"), gm(MART, 24, "observed")])
        self.assertEqual(g["EVIDENCE"], "metrics")
        g = run(before, [gm(ORDERS, 0.0, None), gm(MART, 24, None)])  # older inventory
        self.assertEqual(g["EVIDENCE"], "metrics")
        g = run([gm(o, 24, "observed") for o in objs], [gm(o, 0, "cron") for o in objs])
        self.assertEqual(g["EVIDENCE"], "metrics")
        # both sides cron-sourced: a member at 0/day is a removed or paused schedule
        g = run(before, [gm(ORDERS, 0), gm(MART, 24)])
        self.assertEqual((g["STATUS"], g["EVIDENCE"]), ("acted-on", "schedule"))

    def test_has_schedule_comes_from_cron_fields_not_refresh_runs(self):
        # Refresh runs in the window set HAS_EXISTING_SCHEDULE even if the schedule was
        # deleted mid-window; an enriched side must be judged on its cron alone.
        before = inv([rc(MART, "Remove materialization candidate", 2880, 72.0,
                         **sched("*/15 * * * *"))])
        after = inv([rc(MART, "Remove materialization candidate", 300, 10.0,
                        HAS_EXISTING_SCHEDULE=True, SIGMA_ENRICHMENT="matched")])
        e = one(compare.compare(before, after), MART)
        self.assertEqual((e["STATUS"], e["EVIDENCE"]), ("acted-on", "schedule"))
        self.assertIn("Schedule removed", e["DETAIL"])
        # not enriched or scanned on the after side: runs say nothing about the schedule
        after = inv([rc(MART, "Remove materialization candidate", 300, 10.0,
                        HAS_EXISTING_SCHEDULE=True)])
        e = one(compare.compare(before, after), MART)
        self.assertEqual((e["STATUS"], e["EVIDENCE"]), ("not-yet", "metrics"))

    def test_unreadable_cron_and_zero_rate_wording(self):
        before = inv([rc(MART, "Retune materialization", 900, 72.0,
                         **dict(sched("not a cron"), HAS_EXISTING_SCHEDULE=True))])
        after = inv([rc(MART, "Retune materialization", 30, 3.0, **sched("0 6 * * *"))])
        e = one(compare.compare(before, after), MART)
        self.assertEqual(e["EVIDENCE"], "metrics")
        self.assertIn("Schedule present but its cron could not be read; used refresh counts "
                      "instead", e["DETAIL"])
        self.assertNotIn("No schedule data in the inventories", e["DETAIL"])
        s = compare._sched_text({"has": True, "paused": False, "rate": 0})
        self.assertNotIn("less than daily", s)
        self.assertIn("paused", s)
        # a scan-style after side (rate 0, no cron) reads as paused or no schedule
        before = inv([rc(MART, "Retune materialization", 720, 72.0, REFRESHES_PER_DAY=24)])
        after = inv([rc(MART, "Retune materialization", 0, 0.0, REFRESHES_PER_DAY=0)])
        e = one(compare.compare(before, after), MART)
        self.assertEqual(e["STATUS"], "acted-on")
        self.assertNotIn("less than daily", e["DETAIL"])

    def test_after_owner_wins_so_reassignment_credits_the_new_owner(self):
        before = inv([rc(MART, "Retune materialization", 720, 72.0, OWNER_EMAIL="old@example.com",
                         **sched("0 * * * *"))])
        after = inv([rc(MART, "Retune materialization", 0, 0.0, OWNER_EMAIL="new@example.com",
                        **sched())])
        res = compare.compare(before, after)
        self.assertEqual(one(res, MART)["OWNER_EMAIL"], "new@example.com")
        by = res["follow_through"]["by_owner"]
        self.assertEqual(list(by), ["new@example.com"])
        self.assertGreater(by["new@example.com"]["savings_acted_on"], 0)

    def test_swapped_inventories_refused_unless_forced(self):
        older = inv([cand(ORDERS, credits=2.0)])
        newer = inv([cand(ORDERS, credits=1.0)])
        newer["generated"] = "2026-10-20T00:00:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            proc, out = CompareTests("test_cli_outputs").run_cli(tmp, newer, older)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("swapped", proc.stderr)
            self.assertFalse((out / "compare.json").exists())
        with tempfile.TemporaryDirectory() as tmp:
            proc, out = CompareTests("test_cli_outputs").run_cli(tmp, newer, older, "--force")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            res = json.loads((out / "compare.json").read_text())
            self.assertTrue(any("swapped" in w for w in res["warnings"]))

    def test_missing_generated_means_interval_unknown_not_too_soon(self):
        i, j = inv([cand(ORDERS)]), inv([cand(ORDERS)])
        del j["generated"]
        res = compare.compare(i, j)
        self.assertIsNone(res["measurement"]["too_soon"])
        self.assertTrue(any("interval is unknown" in w for w in res["warnings"]))
        self.assertIn("interval unknown", compare.markdown(res))

    def test_duplicated_candidate_key_is_credited_once(self):
        c = rc(MART, "Retune materialization", 720, 72.0, **sched("0 * * * *"))
        before = inv([c, dict(c)])
        after = inv([rc(MART, "Retune materialization", 0, 0.0, **sched())])
        res = compare.compare(before, after)
        rows = [e for e in res["follow_through"]["rows"] if e["OBJECT"] == MART]
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(sum(e["SAVINGS"] for e in rows), 216.0)
        assert_adds_up(self, res)

    def test_cli_writes_follow_through_outputs(self):
        before = inv([rc(MART, "Retune materialization", 2880, 72.0, **sched("*/15 * * * *"))])
        after = inv([rc(MART, "Retune materialization", 30, 3.0, **sched("0 7 * * *"))])
        after["generated"] = "2026-10-05T00:00:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            proc, out = CompareTests("test_cli_outputs").run_cli(tmp, before, after)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            res = json.loads((out / "compare.json").read_text())
            for k in ("follow_through", "remaining", "measurement"):
                self.assertIn(k, res)
            rows = list(csv.DictReader((out / "follow-through.csv").read_text().splitlines()))
            self.assertEqual([r["STATUS"] for r in rows], ["acted-on"])
            self.assertIn("## Follow-through", (out / "COMPARE.md").read_text())
            self.assertIn("too soon", (out / "COMPARE.md").read_text())
            self.assertIn("days between", proc.stderr)
            proc, _ = CompareTests("test_cli_outputs").run_cli(tmp, before, after,
                                                               "--too-soon-days", "3")
            self.assertNotIn("days between", proc.stderr)


if __name__ == "__main__":
    unittest.main()
