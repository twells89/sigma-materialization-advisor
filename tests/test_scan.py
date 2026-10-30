import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import scan_workbooks as scan  # noqa: E402

COMMENT = '\n\n-- Sigma Σ {"request-id":"r1","user-id":"u1","email":"analyst@example.com"}'
ORDERS = 'select "ID" from NORTHWIND.SALES.ORDERS limit 1000'


class FakeClient:
    """Routes GETs by path; no network."""
    base = "https://api.example.invalid"

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, path):
        route = urlparse(path).path
        self.calls.append(route)
        value = self.routes.get(route)
        if value is None:
            raise RuntimeError(f"GET {route} -> HTTP 404")
        return value


def workbook(wid, owner="m1", name="Sales"):
    return {"workbookId": wid, "workbookUrlId": "u-" + wid, "name": name,
            "url": f"https://app.example.invalid/northwind-demo/workbook/u-{wid}",
            "ownerId": owner}


def routes_for(wid, queries, schedules=()):
    # per-workbook formulas keep spec-based logic unique, so SQL grouping is exercised
    elements = [spec_el(q["elementId"], DM, [f"[{wid}/{q['elementId']}]"]) for q in queries]
    return {
        f"/v2/workbooks/{wid}/spec": spec(*elements),
        f"/v2/workbooks/{wid}": workbook(wid),
        f"/v2/workbooks/{wid}/queries": {"entries": queries, "nextPage": None},
        f"/v2.1/workbooks/{wid}/materialization-schedules":
            {"entries": list(schedules), "nextPage": None},
        f"/v2/workbooks/{wid}/sources":
            [{"type": "data-model", "dataModelId": "dm1", "elementIds": ["e"]}],
    }


def query(element, sql):
    return {"elementId": element, "name": element.title(), "sql": sql + COMMENT}


def schedule(element, cron, paused=False):
    return {"elementId": element, "elementName": element, "sheetId": "s-" + element,
            "paused": paused, "schedule": {"cronSpec": cron, "timezone": "UTC"}}


def args(**kw):
    base = dict(workbook=None, folder=None, owner=None, name_contains=None, limit=200)
    base.update(kw)
    return SimpleNamespace(**base)


class ParsingTests(unittest.TestCase):
    def test_strip_comment_removes_identity(self):
        out = scan.strip_sigma_comment("select 1" + COMMENT)
        self.assertEqual(out, "select 1")
        self.assertNotIn("example.com", out)

    def test_signature_ignores_comment_case_whitespace_and_limit(self):
        a = scan.sql_signature("SELECT  a\nFROM t LIMIT 1000" + COMMENT)
        b = scan.sql_signature("select a from t" + COMMENT.replace("r1", "r2"))
        self.assertEqual(a, b)
        self.assertNotEqual(a, scan.sql_signature("select b from t"))
        self.assertIsNone(scan.sql_signature(COMMENT))

    def test_signature_neutralizes_write_back_table_id(self):
        a = scan.sql_signature('select x from W.S."t_mat_' + "a" * 32 + '_0"')
        b = scan.sql_signature('select x from W.S."t_mat_' + "b" * 32 + '_0"')
        self.assertEqual(a, b)

    def test_base_tables(self):
        sql = (
            'select * from NORTHWIND.SALES.ORDERS o left join '
            '(select k from "northwind"."sales"."customers" group by k) Q2 on 1=1 '
            'join CUSTOMERS c on 1=1 join WRITEBACK_DB.WRITEBACK."t_mat_ab12_0" m on 1=1 '
            "where extract(year from o.order_date) = 2024 and note = 'from a.b.c'"
            + COMMENT
        )
        self.assertEqual(scan.base_tables(sql),
                         ["NORTHWIND.SALES.CUSTOMERS", "NORTHWIND.SALES.ORDERS"])

    def test_base_tables_ignores_table_functions_and_subquery_aliases(self):
        sql = "select * from (select a from NORTHWIND.SALES.ORDERS) Q1, table(flatten(Q1.a))"
        self.assertEqual(scan.base_tables(sql), ["NORTHWIND.SALES.ORDERS"])

    def test_cron_refreshes_per_day(self):
        f = scan.cron_refreshes_per_day
        self.assertEqual(f("0 * * * *"), 24)
        self.assertEqual(f("*/15 * * * *"), 96)
        self.assertEqual(f("0 6,18 * * *"), 2)
        self.assertEqual(f("0 9-17 * * *"), 9)
        self.assertEqual(f("*/30 8-17/2 * * *"), 10)
        self.assertAlmostEqual(f("0 9 * * 1-5"), 5 / 7, places=2)
        self.assertAlmostEqual(f("0 0 1 * *"), 1 / 30, places=2)
        self.assertAlmostEqual(f("0 0 * * MON"), 1 / 7, places=2)

    def test_cron_unparseable_is_none(self):
        for bad in ("", None, "not a cron", "61 * * * *", "* * *", "*/0 * * * *"):
            self.assertIsNone(scan.cron_refreshes_per_day(bad), bad)


class GroupingTests(unittest.TestCase):
    def wb(self, wid, elements):
        return {"WORKBOOK_ID": wid, "WORKBOOK_URL_ID": "u-" + wid, "URL": None,
                "OWNER_EMAIL": "analyst@example.com", "ELEMENTS": elements}

    def el(self, eid, sig, tables=(), cron=None, per_day=None):
        return {"ELEMENT_ID": eid, "SQL_SIGNATURE": sig, "BASE_TABLES": list(tables),
                "SCHEDULE_CRON": cron, "REFRESHES_PER_DAY": per_day}

    def test_identical_sql_needs_two_workbooks(self):
        wbs = [self.wb("a", [self.el("e1", "s1"), self.el("e2", "s1")]),
               self.wb("b", [self.el("e1", "s2")])]
        self.assertEqual(scan.build_groups(wbs), [])
        wbs[1]["ELEMENTS"].append(self.el("e3", "s1"))
        groups = scan.build_groups(wbs)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["MATCH"], "identical-sql")
        self.assertEqual(groups[0]["MEMBER_COUNT"], 3)
        self.assertEqual(groups[0]["SOURCE"], "scan")
        self.assertEqual(groups[0]["GROUP_ID"], "scan-1")

    def test_same_base_tables_only_scheduled_and_not_already_identical(self):
        tables = ["NORTHWIND.SALES.ORDERS"]
        wbs = [
            self.wb("a", [self.el("m", "s1", tables, "0 * * * *", 24)]),
            self.wb("b", [self.el("m", "s2", tables, "0 * * * *", 24)]),
            self.wb("c", [self.el("m", "s3", tables)]),  # unscheduled: ignored
        ]
        groups = scan.build_groups(wbs)
        self.assertEqual([g["MATCH"] for g in groups], ["same-base-tables"])
        self.assertEqual(groups[0]["BASE_TABLES"], tables)
        self.assertEqual(groups[0]["MEMBER_COUNT"], 2)
        wbs[1]["ELEMENTS"][0]["SQL_SIGNATURE"] = "s1"
        self.assertEqual([g["MATCH"] for g in scan.build_groups(wbs)], ["identical-sql"])

    def test_empty_base_tables_never_group(self):
        wbs = [self.wb("a", [self.el("m", "s1", (), "0 * * * *", 24)]),
               self.wb("b", [self.el("m", "s2", (), "0 * * * *", 24)])]
        self.assertEqual(scan.build_groups(wbs), [])

    def test_member_shape_and_ranking(self):
        wbs = [self.wb("a", [self.el("hot", "h", (), "*/15 * * * *", 96),
                             self.el("cold", "c")]),
               self.wb("b", [self.el("hot", "h", (), "*/15 * * * *", 96),
                             self.el("cold", "c")])]
        groups = scan.build_groups(wbs)
        self.assertEqual(groups[0]["MEMBERS"][0]["ELEMENT"], "hot")
        member = groups[0]["MEMBERS"][0]
        self.assertEqual(member["OBJECT"], "/workbook/u-a")
        self.assertEqual(set(member), {"OBJECT", "ELEMENT", "SAMPLE_URL", "RUNS", "CREDITS",
                                       "REFRESHES_PER_DAY", "OWNER_EMAIL"})
        self.assertEqual(groups[1]["MEMBERS"][0]["ELEMENT"], "cold")


class RunTests(unittest.TestCase):
    def client(self):
        routes = {"/v2/members/m1": {"email": "analyst@example.com"}}
        for wid, cron in (("a", "0 * * * *"), ("b", "*/15 * * * *")):
            routes.update(routes_for(
                wid,
                [query("master", ORDERS), query("chart", "select 1 from NORTHWIND.SALES.REGIONS"),
                 query("solo", "select " + wid + " from NORTHWIND.SALES.OTHER")],
                [schedule("master", cron)],
            ))
        routes["/v2/workbooks"] = {"entries": [workbook("a"), workbook("b")], "nextPage": None}
        return FakeClient(routes)

    def test_scan_output_shape_and_privacy(self):
        client = self.client()
        out = scan.run(client, args(workbook=["a", "b"]))
        self.assertEqual(out["scan_version"], 1)
        self.assertEqual(out["base_url_host"], "api.example.invalid")
        self.assertEqual(out["org"], "northwind-demo")
        self.assertEqual(out["warnings"], [])
        self.assertEqual(len(out["workbooks"]), 2)
        first = out["workbooks"][0]
        self.assertEqual(first["OWNER_EMAIL"], "analyst@example.com")
        self.assertEqual(first["SOURCES"], [{"type": "data-model", "id": "dm1"}])
        self.assertEqual({e["ELEMENT_ID"] for e in first["ELEMENTS"]}, {"master", "chart"})
        master = next(e for e in first["ELEMENTS"] if e["ELEMENT_ID"] == "master")
        self.assertEqual(master["REFRESHES_PER_DAY"], 24)
        self.assertEqual(master["BASE_TABLES"], ["NORTHWIND.SALES.ORDERS"])
        self.assertEqual(
            {g["MATCH"] for g in out["duplicate_groups"]}, {"identical-sql"})
        text = json.dumps(out)
        self.assertNotIn("request-id", text)
        self.assertNotIn("select ", text.lower())
        self.assertEqual(client.calls.count("/v2/members/m1"), 1)

    def test_paused_schedule_counts_zero_refreshes(self):
        routes = routes_for("a", [query("m", ORDERS)], [schedule("m", "0 * * * *", True)])
        routes["/v2/members/m1"] = {"email": "analyst@example.com"}
        out = scan.run(FakeClient(routes), args(workbook=["a"]))
        element = out["workbooks"][0]["ELEMENTS"][0]
        self.assertTrue(element["SCHEDULE_PAUSED"])
        self.assertEqual(element["REFRESHES_PER_DAY"], 0)

    def test_failures_are_warnings(self):
        routes = routes_for("a", [query("m", ORDERS)], [schedule("m", "weekly")])
        out = scan.run(FakeClient(routes), args(workbook=["a", "missing"]))
        self.assertEqual(len(out["workbooks"]), 1)
        text = " ".join(out["warnings"])
        self.assertIn("missing", text)
        self.assertIn("unparseable cron", text)
        self.assertIn("/v2/members/m1", text)
        self.assertIsNone(out["workbooks"][0]["OWNER_EMAIL"])
        self.assertEqual(out["workbooks"][0]["ELEMENTS"][0]["REFRESHES_PER_DAY"], None)

    def test_name_owner_and_limit_filters(self):
        routes = {"/v2/members/m1": {"email": "analyst@example.com"},
                  "/v2/members/m2": {"email": "other@example.com"},
                  "/v2/workbooks": {"entries": [
                      workbook("a", "m1", "Sales Overview"),
                      workbook("b", "m2", "Sales Detail"),
                      workbook("c", "m1", "Finance"),
                      dict(workbook("d", "m1", "Sales Old"), isArchived=True)],
                      "nextPage": None}}
        scanner = scan.Scanner(FakeClient(routes), sleep=0)
        ids = lambda found: [w["workbookId"] for w in found]  # noqa: E731
        self.assertEqual(ids(scanner.list_workbooks(args(name_contains="sales"))), ["a", "b"])
        self.assertEqual(
            ids(scanner.list_workbooks(args(name_contains="sales", owner="ANALYST@example.com"))),
            ["a"])
        self.assertEqual(ids(scanner.list_workbooks(args(limit=2))), ["a", "b"])

    def test_folder_scope_lists_files_then_fetches_workbooks(self):
        routes = {"/v2/files": {"entries": [{"id": "a", "type": "workbook"}], "nextPage": None},
                  "/v2/workbooks/a": workbook("a")}
        scanner = scan.Scanner(FakeClient(routes), sleep=0)
        found = scanner.list_workbooks(args(folder="f1"))
        self.assertEqual([w["workbookId"] for w in found], ["a"])

    def test_paging_follows_next_page(self):
        class Paged(FakeClient):
            def get(self, path):
                page = dict(p.split("=") for p in urlparse(path).query.split("&")).get("page")
                return ({"entries": [1], "nextPage": "2"} if not page
                        else {"entries": [2], "nextPage": None})
        scanner = scan.Scanner(Paged({}), sleep=0)
        self.assertEqual(scanner.pages("/v2/x"), [1, 2])


def spec_el(eid, source, formulas, name=None, kind="table", **extra):
    cols = [{"id": f"{eid}-c{i}", "formula": f, "name": f"Col {i}"} for i, f in enumerate(formulas)]
    return {"id": eid, "kind": kind, "name": name or eid, "source": source, "columns": cols, **extra}


DM = {"kind": "data-model", "dataModelId": "dm-1", "elementId": "dm-el"}


def spec(*elements):
    return {"document": {"elements": list(elements), "pages": []}}


class LogicTests(unittest.TestCase):
    def logic(self, elements, eid):
        return scan.element_logic(scan.spec_elements(spec(*elements)), eid)

    def test_data_model_source_ref(self):
        ref, sig = self.logic([spec_el("m", DM, ["[T/A]"])], "m")
        self.assertEqual(ref, "data-model:dm-1/dm-el")
        self.assertEqual(len(sig), 32)

    def test_signature_ignores_ids_names_and_column_order(self):
        a = spec_el("m1", DM, ["[T/A]", "[T/B]"], name="One")
        b = spec_el("zz", DM, ["[T/B]", "[T/A]"], name="Two")
        self.assertEqual(self.logic([a], "m1")[1], self.logic([b], "zz")[1])

    def test_signature_changes_with_formula_filter_or_source(self):
        base = self.logic([spec_el("m", DM, ["[T/A]"])], "m")[1]
        self.assertNotEqual(base, self.logic([spec_el("m", DM, ["[T/B]"])], "m")[1])
        other = dict(DM, elementId="dm-other")
        self.assertNotEqual(base, self.logic([spec_el("m", other, ["[T/A]"])], "m")[1])
        filtered = spec_el("m", DM, ["[T/A]"], filters=[{"columnId": "m-c0", "kind": "list"}])
        self.assertNotEqual(base, self.logic([filtered], "m")[1])

    def test_filter_column_ids_do_not_matter(self):
        f1 = spec_el("m", DM, ["[T/A]"], filters=[{"columnId": "m-c0", "values": [1]}])
        f2 = spec_el("x", DM, ["[T/A]"], filters=[{"columnId": "x-c0", "values": [1]}])
        self.assertEqual(self.logic([f1], "m")[1], self.logic([f2], "x")[1])

    def test_child_inherits_chain_root_and_parent_logic(self):
        child = spec_el("c", {"kind": "table", "elementId": "m"}, ["Sum([M/A])"], kind="bar-chart")
        ref1, sig1 = self.logic([spec_el("m", DM, ["[T/A]"]), child], "c")
        self.assertEqual(ref1, "data-model:dm-1/dm-el")
        _, sig2 = self.logic([spec_el("m", DM, ["[T/B]"]), child], "c")
        self.assertNotEqual(sig1, sig2)

    def test_missing_parent_falls_back_to_workbook_element(self):
        child = spec_el("c", {"kind": "table", "elementId": "gone"}, ["[X/A]"])
        self.assertEqual(self.logic([child], "c")[0], "workbook-element:gone")

    def test_cycle_and_non_data_elements_are_none(self):
        a = spec_el("a", {"kind": "table", "elementId": "b"}, ["[B/X]"])
        b = spec_el("b", {"kind": "table", "elementId": "a"}, ["[A/X]"])
        ref, _ = self.logic([a, b], "a")
        self.assertTrue(ref.startswith("workbook-element:"))
        ctl = {"id": "ctl", "kind": "control", "controlId": "ctl"}
        self.assertEqual(self.logic([ctl], "ctl"), (None, None))
        self.assertEqual(self.logic([spec_el("e", {"kind": "empty"}, [])], "e"), (None, None))
        self.assertEqual(self.logic([], "nope"), (None, None))

    def test_warehouse_and_custom_sql_sources_never_store_raw_text(self):
        table = spec_el("t", {"kind": "warehouse-table", "path": ["northwind", "sales", "orders"]}, ["[O/A]"])
        self.assertEqual(self.logic([table], "t")[0], "warehouse-table:NORTHWIND.SALES.ORDERS")
        sql = spec_el("q", {"kind": "sql", "statement": "select secret from NORTHWIND.SALES.ORDERS"}, ["[Q/A]"])
        ref = self.logic([sql], "q")[0]
        self.assertTrue(ref.startswith("sql:"))
        self.assertNotIn("secret", ref)

    def test_spec_elements_reads_nested_page_elements(self):
        doc = {"document": {"elements": [spec_el("a", DM, [])],
                            "pages": [{"elements": [spec_el("b", DM, [])]}]}}
        self.assertEqual(set(scan.spec_elements(doc)), {"a", "b"})


class PrecedenceTests(unittest.TestCase):
    def wb(self, wid, elements):
        return {"WORKBOOK_ID": wid, "WORKBOOK_URL_ID": "u-" + wid, "URL": None,
                "OWNER_EMAIL": None, "ELEMENTS": elements}

    def el(self, eid, logic=None, sql=None, ref=None, tables=(), cron=None):
        return {"ELEMENT_ID": eid, "LOGIC_SIGNATURE": logic, "SQL_SIGNATURE": sql,
                "SOURCE_REF": ref, "BASE_TABLES": list(tables), "SCHEDULE_CRON": cron,
                "REFRESHES_PER_DAY": 24 if cron else None}

    def test_identical_logic_beats_identical_sql_and_one_group_per_element(self):
        wbs = [self.wb("a", [self.el("m", "L", "S", "ref", cron="0 * * * *")]),
               self.wb("b", [self.el("m", "L", "S", "ref", cron="0 * * * *")])]
        groups = scan.build_groups(wbs)
        self.assertEqual([g["MATCH"] for g in groups], ["identical-logic"])
        self.assertEqual(groups[0]["SOURCE_REF"], "ref")

    def test_leftovers_fall_through_to_lower_precedence(self):
        wbs = [self.wb("a", [self.el("m", "L1", "S", "ref")]),
               self.wb("b", [self.el("m", "L2", "S", "ref")])]
        self.assertEqual([g["MATCH"] for g in scan.build_groups(wbs)], ["identical-sql"])

    def test_same_source_needs_schedules_and_two_workbooks(self):
        wbs = [self.wb("a", [self.el("m", "L1", "S1", "ref", cron="0 * * * *")]),
               self.wb("b", [self.el("m", "L2", "S2", "ref", cron="*/15 * * * *")]),
               self.wb("c", [self.el("m", "L3", "S3", "ref")])]
        groups = scan.build_groups(wbs)
        self.assertEqual([g["MATCH"] for g in groups], ["same-source"])
        self.assertEqual(groups[0]["MEMBER_COUNT"], 2)

    def test_same_source_beats_same_base_tables(self):
        t = ["NORTHWIND.SALES.ORDERS"]
        wbs = [self.wb("a", [self.el("m", "L1", "S1", "ref", t, "0 * * * *")]),
               self.wb("b", [self.el("m", "L2", "S2", "ref", t, "0 * * * *")])]
        self.assertEqual([g["MATCH"] for g in scan.build_groups(wbs)], ["same-source"])

    def test_no_spec_falls_back_to_sql_behaviour(self):
        wbs = [self.wb("a", [self.el("m", sql="S")]), self.wb("b", [self.el("m", sql="S")])]
        groups = scan.build_groups(wbs)
        self.assertEqual([g["MATCH"] for g in groups], ["identical-sql"])
        self.assertNotIn("SOURCE_REF", groups[0])


class SpecScanTests(unittest.TestCase):
    def routes(self, wid, cron, sql=None):
        routes = routes_for(
            wid, [query("m", sql or "select 1 from W.S." + '"t_mat_' + wid * 4 + '_0"')],
            [schedule("m", cron)])
        routes[f"/v2/workbooks/{wid}/spec"] = spec(spec_el("m", DM, ["[T/A]"]))
        routes["/v2/members/m1"] = {"email": "analyst@example.com"}
        return routes

    def test_materialized_elements_group_by_logic(self):
        routes = {**self.routes("aaaa", "0 * * * *"), **self.routes("bbbb", "*/15 * * * *")}
        out = scan.run(FakeClient(routes), args(workbook=["aaaa", "bbbb"]))
        self.assertEqual([g["MATCH"] for g in out["duplicate_groups"]], ["identical-logic"])
        self.assertEqual(out["duplicate_groups"][0]["SOURCE_REF"], "data-model:dm-1/dm-el")
        element = out["workbooks"][0]["ELEMENTS"][0]
        self.assertEqual(element["SOURCE_REF"], "data-model:dm-1/dm-el")
        self.assertEqual(len(element["LOGIC_SIGNATURE"]), 32)
        self.assertEqual(out["warnings"], [])

    def test_spec_failure_warns_and_keeps_sql_grouping(self):
        sql = "select 1 from NORTHWIND.SALES.ORDERS"
        routes = {**self.routes("aaaa", "0 * * * *", sql), **self.routes("bbbb", "0 * * * *", sql)}
        del routes["/v2/workbooks/aaaa/spec"]
        out = scan.run(FakeClient(routes), args(workbook=["aaaa", "bbbb"]))
        self.assertTrue(any("spec unavailable" in w for w in out["warnings"]))
        self.assertEqual([g["MATCH"] for g in out["duplicate_groups"]], ["identical-sql"])
        self.assertIsNone(out["workbooks"][0]["ELEMENTS"][0]["LOGIC_SIGNATURE"])

    def test_write_back_only_sql_has_no_sql_signature(self):
        # without a spec, two materialized elements reading only their own t_mat tables
        # must not be called identical just because those reads look alike
        routes = {**self.routes("aaaa", "0 * * * *"), **self.routes("bbbb", "0 * * * *")}
        del routes["/v2/workbooks/aaaa/spec"]
        del routes["/v2/workbooks/bbbb/spec"]
        out = scan.run(FakeClient(routes), args(workbook=["aaaa", "bbbb"]))
        self.assertIsNone(out["workbooks"][0]["ELEMENTS"][0]["SQL_SIGNATURE"])
        self.assertNotIn("identical-sql", [g["MATCH"] for g in out["duplicate_groups"]])
        self.assertTrue(scan.reads_only_write_back('select 1 from W.S."t_mat_ab_0"'))
        self.assertFalse(scan.reads_only_write_back(
            'select 1 from W.S."t_mat_ab_0" join NORTHWIND.SALES.ORDERS o on 1=1'))
        self.assertFalse(scan.reads_only_write_back("select 1"))


class FoldTests(unittest.TestCase):
    def wb(self, wid, elements):
        return {"WORKBOOK_ID": wid, "WORKBOOK_URL_ID": "u-" + wid, "URL": None,
                "OWNER_EMAIL": None, "ELEMENTS": elements}

    def el(self, eid, logic, parent=None, name=None):
        return {"ELEMENT_ID": eid, "NAME": name or eid.title(), "LOGIC_SIGNATURE": logic,
                "SQL_SIGNATURE": None, "SOURCE_REF": "ref", "BASE_TABLES": [],
                "PARENT_ELEMENT_ID": parent, "SCHEDULE_CRON": None, "REFRESHES_PER_DAY": None}

    def test_children_fold_into_parent_group(self):
        wbs = [self.wb(w, [self.el("m", "LM"), self.el("c1", "L1", "m"),
                           self.el("c2", "L2", "m")]) for w in ("a", "b")]
        groups = scan.build_groups(wbs)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["MEMBERS"][0]["ELEMENT"], "m")
        self.assertEqual(groups[0]["DOWNSTREAM_COUNT"], 2)
        self.assertEqual([d["ELEMENT_ID"] for d in groups[0]["DOWNSTREAM_ELEMENTS"]], ["c1", "c2"])
        self.assertEqual(set(groups[0]["DOWNSTREAM_ELEMENTS"][0]), {"ELEMENT_ID", "NAME"})

    def test_deeper_chain_folds_to_the_root(self):
        wbs = [self.wb(w, [self.el("m", "LM"), self.el("t", "LT", "m"),
                           self.el("c", "LC", "t")]) for w in ("a", "b")]
        groups = scan.build_groups(wbs)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["DOWNSTREAM_COUNT"], 2)

    def test_child_group_kept_when_parent_not_grouped(self):
        wbs = [self.wb("a", [self.el("m", "LA"), self.el("c", "LC", "m")]),
               self.wb("b", [self.el("m", "LB"), self.el("c", "LC", "m")])]
        groups = scan.build_groups(wbs)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["MEMBERS"][0]["ELEMENT"], "c")
        self.assertNotIn("DOWNSTREAM_COUNT", groups[0])

    def test_child_group_kept_when_a_member_has_no_grouped_parent(self):
        wbs = [self.wb("a", [self.el("m", "LM"), self.el("c", "LC", "m")]),
               self.wb("b", [self.el("m", "LM"), self.el("c", "LC", "m")]),
               self.wb("z", [self.el("c", "LC")])]
        groups = scan.build_groups(wbs)
        self.assertEqual(sorted(g["MEMBERS"][0]["ELEMENT"] for g in groups), ["c", "m"])

    def test_parent_element_id(self):
        elements = scan.spec_elements(spec(
            spec_el("m", DM, ["[T/A]"]),
            spec_el("c", {"kind": "table", "elementId": "m"}, ["[M/A]"]),
            spec_el("o", {"kind": "table", "elementId": "gone"}, ["[X/A]"])))
        self.assertEqual(scan.parent_element_id(elements, "c"), "m")
        self.assertIsNone(scan.parent_element_id(elements, "m"))
        self.assertIsNone(scan.parent_element_id(elements, "o"))

    def test_run_folds_charts_and_hides_internal_key(self):
        def routes(wid):
            r = routes_for(wid, [query("m", "select 1 from W.S.T1"), query("c", "select 2 from W.S.T2")],
                           [schedule("m", "0 * * * *")])
            r[f"/v2/workbooks/{wid}/spec"] = spec(
                spec_el("m", DM, ["[T/A]"]),
                spec_el("c", {"kind": "table", "elementId": "m"}, ["Sum([M/A])"], kind="bar-chart"))
            r["/v2/members/m1"] = {"email": "analyst@example.com"}
            return r
        out = scan.run(FakeClient({**routes("aaaa"), **routes("bbbb")}), args(workbook=["aaaa", "bbbb"]))
        self.assertEqual(len(out["duplicate_groups"]), 1)
        group = out["duplicate_groups"][0]
        self.assertEqual((group["MATCH"], group["DOWNSTREAM_COUNT"]), ("identical-logic", 1))
        self.assertNotIn("PARENT_ELEMENT_ID", json.dumps(out))


if __name__ == "__main__":
    unittest.main()
