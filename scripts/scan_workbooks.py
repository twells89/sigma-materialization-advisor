#!/usr/bin/env python3
"""Scan Sigma workbooks for duplicate work using the Sigma REST API only (read-only).

Fetches each workbook's compiled element SQL, schedules, sources, and owner, then
groups elements that share identical logic (from the workbook spec), identical SQL, the
same source, or the same base tables. Raw SQL is never
written to scan.json; only a hash signature and extracted table names are kept.

  python3 scripts/scan_workbooks.py --out DIR [--workbook ID ...] [--folder ID]
         [--owner EMAIL] [--name-contains TEXT] [--limit 200]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone

from enrich import SigmaClient, owner_email

try:
    from cost_model import recommend_consolidation
except ImportError:  # analyze.py fills the recommendation keys when absent
    recommend_consolidation = None

PAUSE_SEC = 0.05
_COMMENT = re.compile(r"--\s*Sigma\s+Σ[^\n]*", re.IGNORECASE)
_MAT_NAME = re.compile(r"t_mat_[0-9a-f]+_\d+")
_LIMIT = re.compile(r"\s+limit\s+\d+\s*;?\s*$")
_STRINGS = re.compile(r"'(?:[^']|'')*'")
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_EXTRACT = re.compile(r"\b(?:extract|date_part)\s*\(\s*\w+\s+from\b", re.IGNORECASE)
_PART = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'
_TABLE = re.compile(
    r"\b(?:from|join)\s+(" + _PART + r"(?:\s*\.\s*" + _PART + r"){1,2})(?![\w$.\"]|\s*\()",
    re.IGNORECASE,
)
_DOW = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
_MON = {m: i + 1 for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def strip_sigma_comment(sql):
    return _COMMENT.sub("", sql or "").strip()


def normalize_sql(sql):
    """Lower-case, collapse whitespace, drop a trailing limit, and neutralize the
    per-workbook write-back table id (a materialized element compiles to a read of
    its own t_mat table, whose name differs between otherwise identical workbooks)."""
    text = " ".join(strip_sigma_comment(sql).lower().split())
    return _MAT_NAME.sub("t_mat_id", _LIMIT.sub("", text).rstrip("; "))


def _table_parts(sql):
    """Every FROM/JOIN table reference as its upper-cased name parts."""
    text = _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", strip_sigma_comment(sql)))
    text = _EXTRACT.sub(" ", _STRINGS.sub("''", text))
    return [[p.strip('"').replace('""', '"').upper() for p in re.findall(_PART, m.group(1))]
            for m in _TABLE.finditer(text)]


def _is_write_back(parts):
    return any(p.startswith("T_MAT_") for p in parts[-2:])


def reads_only_write_back(sql):
    """True when the SQL reads nothing but Sigma write-back (t_mat_*) tables."""
    refs = _table_parts(sql)
    return bool(refs) and all(_is_write_back(p) for p in refs)


def sql_signature(sql):
    text = normalize_sql(sql)
    return hashlib.md5(text.encode("utf-8")).hexdigest() if text else None


def base_tables(sql):
    """Fully-qualified (2-3 part) tables after FROM/JOIN, upper-cased and sorted.

    Sigma write-back tables (T_MAT_*) are excluded: they are outputs of Sigma's own
    materialization, not shared inputs.
    """
    return sorted({".".join(p) for p in _table_parts(sql) if not _is_write_back(p)})


def _cron_field(spec, lo, hi, names=None):
    values = set()
    for part in spec.upper().split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = int(step_text)
            if step < 1:
                raise ValueError("step")
        if part in ("*", "?", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _cron_value(a, names), _cron_value(b, names)
        else:
            start = _cron_value(part, names)
            end = hi if step > 1 else start
        if start < lo or end > hi or start > end:
            raise ValueError("range")
        values.update(range(start, end + 1, step))
    return values


def _cron_value(text, names):
    if names and text in names:
        return names[text]
    return int(text)


def cron_refreshes_per_day(cron):
    """Estimated runs per day for a 5-field cron expression, or None if unparseable.

    Day-of-week / day-of-month / month restrictions scale the daily rate by 7 / 30 / 12.
    """
    try:
        fields = str(cron or "").split()
        if len(fields) != 5:
            return None
        minutes = _cron_field(fields[0], 0, 59)
        hours = _cron_field(fields[1], 0, 23)
        dom_free = fields[2] in ("*", "?")
        dow_free = fields[4] in ("*", "?")
        doms = _cron_field(fields[2], 1, 31)
        dows = {d % 7 for d in _cron_field(fields[4], 0, 7, _DOW)}
        months = _cron_field(fields[3], 1, 12, _MON)
    except (ValueError, TypeError):
        return None
    if not (minutes and hours and doms and dows and months):
        return None
    share = 1.0
    if not dom_free and not dow_free:
        share = min(1.0, len(doms) / 30 + len(dows) / 7)
    elif not dom_free:
        share = min(1.0, len(doms) / 30)
    elif not dow_free:
        share = len(dows) / 7
    share *= len(months) / 12
    perday = len(minutes) * len(hours) * share
    perday = round(perday, 2)
    return int(perday) if perday == int(perday) else perday


_LOGIC_KEYS = ("filters", "groupings", "sorts", "sort", "sorting")
_SKIP_SOURCES = {"empty", "none"}
_SKIP_KINDS = {"control", "text", "image", "button", "container", "divider", "embed", "spacer"}


def _md5_json(value):
    return hashlib.md5(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                  default=str).encode("utf-8")).hexdigest()


def _swap_ids(value, names):
    """Replace column-id strings with the column's formula so ids never matter."""
    if isinstance(value, str):
        return names.get(value, value)
    if isinstance(value, list):
        return [_swap_ids(v, names) for v in value]
    if isinstance(value, dict):
        return {k: _swap_ids(v, names) for k, v in value.items()}
    return value


def spec_elements(spec):
    """{element id: element} from a workbook spec response (document.elements, plus
    any element objects nested in pages)."""
    doc = (spec or {}).get("document", spec) or {}
    if isinstance(doc, str):
        doc = json.loads(doc)
    found = {}
    pools = [doc.get("elements") or []]
    pools += [(page or {}).get("elements") or [] for page in doc.get("pages") or []]
    for pool in pools:
        for el in pool:
            if isinstance(el, dict) and el.get("id") is not None:
                found.setdefault(str(el["id"]), el)
    return found


def element_logic(elements, element_id, memo=None, stack=()):
    """(SOURCE_REF, LOGIC_SIGNATURE) for one spec element, or (None, None).

    SOURCE_REF names the upstream root (a data-model element, a warehouse table, or a
    hash for anything else; custom SQL is hashed, never stored). LOGIC_SIGNATURE is an
    md5 over the source (an in-workbook parent contributes its own signature), the
    sorted column formulas, filters, groupings, and sorts, with element ids, column
    ids, and display names removed.
    """
    memo = {} if memo is None else memo
    key = str(element_id)
    if key in memo:
        return memo[key]
    el = elements.get(key)
    if el is None or key in stack or str(el.get("kind") or "").lower() in _SKIP_KINDS \
            or not isinstance(el.get("source"), dict):
        return None, None
    src = el["source"]
    kind = str(src.get("kind") or "").lower()
    if kind in _SKIP_SOURCES:
        return None, None
    if kind == "data-model" and src.get("dataModelId"):
        ref = f"data-model:{src['dataModelId']}/{src.get('elementId')}"
        upstream = ref
    elif src.get("elementId") is not None and kind in ("table", "element", "workbook-element", ""):
        parent_ref, parent_sig = element_logic(elements, src["elementId"], memo, stack + (key,))
        ref = parent_ref or f"workbook-element:{src['elementId']}"
        upstream = {"parent": parent_sig} if parent_sig else ref
    elif isinstance(src.get("path"), list) and src["path"]:
        ref = "warehouse-table:" + ".".join(str(p).upper() for p in src["path"])
        upstream = ref
    else:
        ref = f"{kind or 'source'}:{_md5_json(src)[:12]}"
        upstream = ref
    columns = [c for c in el.get("columns") or [] if isinstance(c, dict)]
    names = {str(c["id"]): "@" + str(c.get("formula")) for c in columns if c.get("id") is not None}
    payload = {
        "kind": el.get("kind"),
        "source": upstream,
        "columns": sorted(str(c.get("formula")) for c in columns),
        **{k: _swap_ids(el[k], names) for k in _LOGIC_KEYS if el.get(k)},
    }
    memo[key] = (ref, _md5_json(payload))
    return memo[key]


def parent_element_id(elements, element_id):
    """Id of the in-workbook parent element (source kind table/element), else None."""
    el = elements.get(str(element_id)) or {}
    src = el.get("source")
    if isinstance(src, dict) and src.get("elementId") is not None \
            and str(src.get("kind") or "").lower() in ("table", "element", "workbook-element", "") \
            and str(src["elementId"]) in elements:
        return str(src["elementId"])
    return None


def _member(workbook, element):
    object_id = workbook["WORKBOOK_URL_ID"] or workbook["WORKBOOK_ID"]
    url = workbook.get("URL") or ""
    return {
        "OBJECT": f"/workbook/{object_id}",
        "ELEMENT": element["ELEMENT_ID"],
        "SAMPLE_URL": f"{url}?:displayNodeId={element['ELEMENT_ID']}" if url else None,
        "RUNS": 0,
        "CREDITS": 0.0,
        "REFRESHES_PER_DAY": element.get("REFRESHES_PER_DAY"),
        "OWNER_EMAIL": workbook.get("OWNER_EMAIL"),
    }


def _group(match, members, tables, source_ref=None):
    group = {
        "MATCH": match,
        "SOURCE": "scan",
        "BASE_TABLES": tables,
        "MEMBERS": members,
        "MEMBER_COUNT": len(members),
        "TOTAL_CREDITS": 0.0,
        "REDUNDANT_CREDITS": 0.0,
    }
    if source_ref:
        group["SOURCE_REF"] = source_ref
    return group


def build_groups(workbooks):
    """One group per element, by precedence: identical-logic, identical-sql (same
    signature in >=2 workbooks), same-source, then same-base-tables (scheduled
    elements in >=2 workbooks sharing a SOURCE_REF / non-empty table set)."""
    pairs = [(wb, el) for wb in workbooks for el in wb["ELEMENTS"]]
    taken, groups, pairs_of = set(), [], {}

    def claim(match, key_of, only_scheduled):
        buckets = {}
        for wb, el in pairs:
            ident = (wb["WORKBOOK_ID"], el["ELEMENT_ID"])
            key = key_of(el)
            if ident in taken or not key or (only_scheduled and not el.get("SCHEDULE_CRON")):
                continue
            buckets.setdefault(key, []).append((wb, el))
        for key, members in buckets.items():
            if len({wb["WORKBOOK_ID"] for wb, _ in members}) < 2:
                continue
            tables = sorted({t for _, el in members for t in el.get("BASE_TABLES") or []})
            refs = {el.get("SOURCE_REF") for _, el in members}
            ref = refs.pop() if len(refs) == 1 else None
            groups.append(_group(match, [_member(wb, el) for wb, el in members],
                                 list(key) if match == "same-base-tables" else tables, ref))
            pairs_of[id(groups[-1])] = members
            taken.update((wb["WORKBOOK_ID"], el["ELEMENT_ID"]) for wb, el in members)

    claim("identical-logic", lambda el: el.get("LOGIC_SIGNATURE"), False)
    claim("identical-sql", lambda el: el.get("SQL_SIGNATURE"), False)
    claim("same-source", lambda el: el.get("SOURCE_REF"), True)
    claim("same-base-tables", lambda el: tuple(el.get("BASE_TABLES") or ()), True)
    groups = fold_downstream(groups, pairs_of)
    groups.sort(key=lambda g: (
        -sum(m["REFRESHES_PER_DAY"] or 0 for m in g["MEMBERS"]),
        -g["MEMBER_COUNT"], g["MATCH"]))
    return [{"GROUP_ID": f"scan-{i}", **g} for i, g in enumerate(groups, 1)]


def fold_downstream(groups, pairs_of):
    """Fold identical-logic groups whose members all have in-workbook parents that are
    themselves members of one identical-logic group (their duplication only follows the
    parent's) into that group as DOWNSTREAM_ELEMENTS, recursing up chains."""
    logic = [g for g in groups if g["MATCH"] == "identical-logic"]
    owner = {(wb["WORKBOOK_ID"], el["ELEMENT_ID"]): g
             for g in logic for wb, el in pairs_of[id(g)]}

    def parent_group(g):
        found = set()
        for wb, el in pairs_of[id(g)]:
            parent = el.get("PARENT_ELEMENT_ID")
            target = owner.get((wb["WORKBOOK_ID"], parent)) if parent else None
            if target is None or target is g:
                return None
            found.add(id(target))
        if len(found) != 1:
            return None
        wanted = found.pop()
        return next(t for t in logic if id(t) == wanted)

    parents = {id(g): parent_group(g) for g in logic}
    folded, extra = set(), {}
    for g in logic:
        root, seen = parents[id(g)], {id(g)}
        if root is None:
            continue
        while parents.get(id(root)) is not None and id(root) not in seen:
            seen.add(id(root))
            root = parents[id(root)]
        if id(root) in seen:  # cycle: leave it alone
            continue
        wb, el = pairs_of[id(g)][0]
        extra.setdefault(id(root), []).append(
            {"ELEMENT_ID": el["ELEMENT_ID"], "NAME": el.get("NAME")})
        folded.add(id(g))
    for g in logic:
        if id(g) in extra:
            items = sorted(extra[id(g)], key=lambda d: (str(d["NAME"] or ""), d["ELEMENT_ID"]))
            g["DOWNSTREAM_ELEMENTS"] = items
            g["DOWNSTREAM_COUNT"] = len(items)
    return [g for g in groups if id(g) not in folded]


def keep_grouped_or_scheduled(workbooks, groups):
    in_group = {(m["OBJECT"], m["ELEMENT"]) for g in groups for m in g["MEMBERS"]}
    for wb in workbooks:
        object_id = wb["WORKBOOK_URL_ID"] or wb["WORKBOOK_ID"]
        wb["ELEMENTS"] = [
            el for el in wb["ELEMENTS"]
            if el.get("SCHEDULE_CRON") or (f"/workbook/{object_id}", el["ELEMENT_ID"]) in in_group
        ]


def add_recommendations(groups, warnings):
    if recommend_consolidation is None:
        return
    for group in groups:
        try:
            group.update(recommend_consolidation(group))
        except Exception as exc:  # recommendation is best-effort here
            warnings.append(f"recommend_consolidation failed for {group['GROUP_ID']}: {exc}")


class Scanner:
    def __init__(self, client, sleep=PAUSE_SEC):
        self.client, self.sleep = client, sleep
        self.warnings, self.owners = [], {}

    def get(self, path):
        data = self.client.get(path)
        if self.sleep:
            time.sleep(self.sleep)
        return data

    def pages(self, path, limit=1000, max_items=None):
        entries, page = [], None
        while True:
            params = {"limit": limit}
            if page:
                params["page"] = page
            sep = "&" if "?" in path else "?"
            data = self.get(path + sep + urllib.parse.urlencode(params))
            entries.extend(data.get("entries", []))
            page = data.get("nextPage")
            if not page or (max_items and len(entries) >= max_items):
                return entries[:max_items] if max_items else entries

    def owner(self, owner_id):
        return owner_email(self, owner_id, self.owners, self.warnings)

    def list_workbooks(self, args):
        if args.workbook:
            found = []
            for wid in args.workbook:
                try:
                    found.append(self.get("/v2/workbooks/" + urllib.parse.quote(wid, safe="")))
                except RuntimeError as exc:
                    self.warnings.append(str(exc))
            return found
        filtered = bool(args.owner or args.name_contains)
        cap = None if filtered else args.limit
        if args.folder:
            files = self.pages("/v2/files?typeFilters=workbook&parentId="
                               + urllib.parse.quote(args.folder, safe=""), max_items=cap)
            found = []
            for entry in files:
                try:
                    found.append(self.get("/v2/workbooks/" + urllib.parse.quote(entry["id"], safe="")))
                except (RuntimeError, KeyError) as exc:
                    self.warnings.append(str(exc))
        else:
            found = self.pages("/v2/workbooks", max_items=cap)
        found = [w for w in found if not w.get("isArchived")]
        if args.name_contains:
            needle = args.name_contains.lower()
            found = [w for w in found if needle in str(w.get("name") or "").lower()]
        if args.owner:
            wanted = args.owner.lower()
            found = [w for w in found if (self.owner(w.get("ownerId")) or "").lower() == wanted]
        return found[:args.limit]

    def scan_workbook(self, raw):
        wid = str(raw["workbookId"])
        quoted = urllib.parse.quote(wid, safe="")
        wb = {
            "WORKBOOK_ID": wid,
            "WORKBOOK_URL_ID": raw.get("workbookUrlId"),
            "NAME": raw.get("name"),
            "URL": raw.get("url"),
            "OWNER_ID": raw.get("ownerId"),
            "OWNER_EMAIL": self.owner(raw.get("ownerId")),
            "SOURCES": [],
            "ELEMENTS": [],
        }
        try:
            sources = self.get(f"/v2/workbooks/{quoted}/sources")
            for item in sources if isinstance(sources, list) else sources.get("entries", []):
                ident = next((v for k, v in item.items()
                              if k.endswith("Id") and isinstance(v, str)), None)
                wb["SOURCES"].append({"type": item.get("type"), "id": ident})
        except RuntimeError as exc:
            self.warnings.append(str(exc))
        try:
            queries = self.pages(f"/v2/workbooks/{quoted}/queries")
        except RuntimeError as exc:
            self.warnings.append(str(exc))
            return wb
        try:
            logic = spec_elements(self.get(f"/v2/workbooks/{quoted}/spec?format=json"))
        except (RuntimeError, ValueError, AttributeError) as exc:
            self.warnings.append(f"workbook spec unavailable, using SQL signatures only: {exc}")
            logic = {}
        memo = {}
        try:
            schedules = self.pages(f"/v2.1/workbooks/{quoted}/materialization-schedules")
        except RuntimeError as exc:
            self.warnings.append(str(exc))
            schedules = []
        by_element = {str(s.get("elementId")): s for s in schedules}
        for entry in queries:
            element_id = str(entry.get("elementId") or "")
            sql = entry.get("sql") or ""
            element = {
                "ELEMENT_ID": element_id,
                "NAME": entry.get("name"),
                # a materialized element compiles to a read of its own t_mat table, so
                # its SQL says nothing about the logic; LOGIC_SIGNATURE covers it
                "SQL_SIGNATURE": None if reads_only_write_back(sql) else sql_signature(sql),
                "BASE_TABLES": base_tables(sql),
                "SOURCE_REF": None,
                "LOGIC_SIGNATURE": None,
                "PARENT_ELEMENT_ID": parent_element_id(logic, element_id),
                "SCHEDULE_CRON": None,
                "SCHEDULE_TIMEZONE": None,
                "SCHEDULE_PAUSED": None,
                "REFRESHES_PER_DAY": None,
            }
            element["SOURCE_REF"], element["LOGIC_SIGNATURE"] = element_logic(
                logic, element_id, memo)
            sched = by_element.pop(element_id, None)
            if sched:
                self.apply_schedule(element, sched, wid)
            wb["ELEMENTS"].append(element)
        for element_id, sched in by_element.items():
            element = {"ELEMENT_ID": element_id, "NAME": sched.get("elementName"),
                       "SQL_SIGNATURE": None, "BASE_TABLES": []}
            element["SOURCE_REF"], element["LOGIC_SIGNATURE"] = element_logic(
                logic, element_id, memo)
            self.apply_schedule(element, sched, wid)
            wb["ELEMENTS"].append(element)
        return wb

    def apply_schedule(self, element, sched, workbook_id):
        data = sched.get("schedule") or {}
        cron = data.get("cronSpec")
        paused = bool(sched.get("paused"))
        per_day = cron_refreshes_per_day(cron)
        if cron and per_day is None:
            self.warnings.append(
                f"unparseable cron {cron!r} on workbook {workbook_id} element {element['ELEMENT_ID']}")
        element.update({
            "SCHEDULE_CRON": cron,
            "SCHEDULE_TIMEZONE": data.get("timezone"),
            "SCHEDULE_PAUSED": paused,
            "REFRESHES_PER_DAY": 0 if paused and per_day is not None else per_day,
        })


def org_from_urls(workbooks):
    slugs = {urllib.parse.urlparse(w.get("URL") or "").path.strip("/").split("/")[0]
             for w in workbooks if w.get("URL")}
    slugs.discard("")
    return slugs.pop() if len(slugs) == 1 else None


def run(client, args):
    scanner = Scanner(client)
    raws = scanner.list_workbooks(args)
    workbooks = []
    for i, raw in enumerate(raws, 1):
        print(f"[{i}/{len(raws)}] scanning workbook", file=sys.stderr)
        try:
            workbooks.append(scanner.scan_workbook(raw))
        except (RuntimeError, KeyError) as exc:
            scanner.warnings.append(f"workbook scan failed: {exc}")
    groups = build_groups(workbooks)
    for wb in workbooks:
        for el in wb["ELEMENTS"]:
            el.pop("PARENT_ELEMENT_ID", None)
    keep_grouped_or_scheduled(workbooks, groups)
    add_recommendations(groups, scanner.warnings)
    host = urllib.parse.urlparse(client.base).netloc or client.base
    return {
        "scan_version": 1,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url_host": host,
        "org": org_from_urls(workbooks),
        "workbooks": workbooks,
        "duplicate_groups": groups,
        "warnings": scanner.warnings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", required=True, help="output directory (scan.json)")
    parser.add_argument("--workbook", action="append", help="workbook ID (repeatable)")
    parser.add_argument("--folder", help="folder ID; scans workbooks directly inside it")
    parser.add_argument("--owner", help="only workbooks owned by this email")
    parser.add_argument("--name-contains", help="only workbooks whose name contains this text")
    parser.add_argument("--limit", type=int, default=200, help="max workbooks (default 200)")
    args = parser.parse_args()
    started = time.time()
    scan = run(SigmaClient(), args)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "scan.json")
    with open(path, "w") as f:
        json.dump(scan, f, indent=2)
    elements = sum(len(w["ELEMENTS"]) for w in scan["workbooks"])
    print(f"scanned {len(scan['workbooks'])} workbook(s), kept {elements} element(s), "
          f"{len(scan['duplicate_groups'])} group(s), {len(scan['warnings'])} warning(s) "
          f"in {time.time() - started:.1f}s; wrote {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
