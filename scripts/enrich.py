#!/usr/bin/env python3
"""Enrich a v2 inventory with Sigma schedules, controls, and lineage (read-only)."""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from cost_model import recommend, score


class SigmaClient:
    def __init__(self):
        self.base = os.environ.get("SIGMA_BASE_URL", "").rstrip("/")
        self.token = os.environ.get("SIGMA_API_TOKEN")
        if not self.base:
            raise SystemExit("Set SIGMA_BASE_URL.")
        if not self.token:
            client_id = os.environ.get("SIGMA_CLIENT_ID")
            client_secret = os.environ.get("SIGMA_CLIENT_SECRET")
            if not client_id or not client_secret:
                raise SystemExit(
                    "Set SIGMA_API_TOKEN or SIGMA_CLIENT_ID + SIGMA_CLIENT_SECRET."
                )
            form = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }).encode()
            req = urllib.request.Request(
                self.base + "/v2/auth/token", data=form, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=60) as response:
                self.token = json.load(response)["access_token"]

    def get(self, path, retries=4):
        req = urllib.request.Request(
            self.base + path, method="GET",
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/json"},
        )
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=90) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < retries:
                    try:
                        wait = float(exc.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError, AttributeError):
                        wait = 0.0
                    time.sleep(min(max(wait, 2 ** attempt), 30))
                    continue
                detail = exc.read().decode(errors="replace")[:300]
                raise RuntimeError(
                    f"GET {path} -> HTTP {exc.code}: {detail}") from exc

    def paged(self, path, limit=1000):
        entries, page = [], None
        while True:
            params = {"limit": limit}
            if page:
                params["page"] = page
            data = self.get(path + "?" + urllib.parse.urlencode(params))
            entries.extend(data.get("entries", []))
            page = data.get("nextPage")
            if not page:
                return entries


def descendants(lineage, element_id):
    reverse = {}
    for entry in lineage:
        child = entry.get("elementId")
        for source in entry.get("sourceIds") or []:
            reverse.setdefault(str(source), set()).add(str(child))
    seen, stack = set(), list(reverse.get(str(element_id), set()))
    while stack:
        item = stack.pop()
        if item in seen:
            continue
        seen.add(item)
        stack.extend(reverse.get(item, set()))
    return seen


def match_workbook(candidate, workbooks):
    obj = str(candidate.get("OBJECT") or "")
    sample = str(candidate.get("SAMPLE_URL") or "")
    for workbook in workbooks:
        values = (
            str(workbook.get("workbookId") or ""),
            str(workbook.get("workbookUrlId") or ""),
            str(workbook.get("url") or ""),
        )
        if any(value and (value in obj or value in sample) for value in values):
            return workbook
        url_path = urllib.parse.urlparse(values[2]).path if values[2] else ""
        if obj and url_path.endswith(obj):
            return workbook
    return None


def owner_email(client, owner_id, cache, warnings):
    """Member email for an ownerId; lookups are cached and failures only warn."""
    owner_id = str(owner_id or "")
    if not owner_id:
        return None
    if owner_id not in cache:
        try:
            member = client.get("/v2/members/" + urllib.parse.quote(owner_id, safe=""))
            cache[owner_id] = member.get("email")
        except RuntimeError as exc:
            warnings.append(f"owner lookup failed: {exc}")
            cache[owner_id] = None
    return cache[owner_id]


def set_owner(row, workbook, client, cache, warnings):
    owner_id = workbook.get("ownerId")
    if not owner_id:
        return
    row["OWNER_ID"] = owner_id
    email = owner_email(client, owner_id, cache, warnings)
    if email:
        row["OWNER_EMAIL"] = email


def fill_group_owners(groups, workbooks, client, cache, warnings):
    for group in groups or []:
        for member in group.get("MEMBERS") or []:
            if member.get("OWNER_EMAIL") or "/workbook/" not in str(member.get("OBJECT") or ""):
                continue
            workbook = match_workbook(member, workbooks)
            if workbook:
                set_owner(member, workbook, client, cache, warnings)


def node_from_candidate(candidate):
    sample = str(candidate.get("SAMPLE_URL") or "")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(sample).query)
    return (query.get(":displayNodeId") or [candidate.get("ELEMENT") or ""])[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inv", required=True, help="v2 inventory.json")
    parser.add_argument("--out", help="output path (default: overwrite --inv)")
    args = parser.parse_args()
    with open(args.inv) as f:
        inventory = json.load(f)
    if inventory.get("schema_version") != 2:
        raise SystemExit("enrich.py requires schema_version 2 inventory.")

    client = SigmaClient()
    workbooks = client.paged("/v2/workbooks")
    cache, warnings = {}, list(inventory.get("warnings", []))
    owners = {}
    matched = 0
    for candidate in inventory.get("candidates", []):
        if "/workbook/" not in str(candidate.get("OBJECT") or ""):
            continue
        workbook = match_workbook(candidate, workbooks)
        if not workbook:
            candidate.update({
                "SIGMA_ENRICHMENT": "workbook_not_accessible",
                "HAS_CONTROLS": None,
                "CONTROL_TARGETS_RESOLVED": False,
                "LINEAGE_COMPLETE": False,
            })
            candidate.update(recommend(
                candidate, inventory.get("config"), inventory.get("days", 30),
                float(inventory.get("config", {}).get("credit_price", 3.0)),
            ))
            candidate["SCORE"] = round(score(candidate, inventory.get("config")), 2)
            continue
        matched += 1
        workbook_id = str(workbook["workbookId"])
        set_owner(candidate, workbook, client, owners, warnings)
        if workbook_id not in cache:
            quoted = urllib.parse.quote(workbook_id, safe="")
            try:
                elements = client.paged(f"/v2/workbooks/{quoted}/elements")
                lineage = client.paged(f"/v2/workbooks/{quoted}/lineage")
                schedules = client.paged(
                    f"/v2.1/workbooks/{quoted}/materialization-schedules"
                )
                cache[workbook_id] = (elements, lineage, schedules, None)
            except RuntimeError as exc:
                warnings.append(str(exc))
                cache[workbook_id] = (None, None, None, str(exc))
        elements, lineage, schedules, enrichment_error = cache[workbook_id]
        if enrichment_error:
            candidate.update({
                "WORKBOOK_ID": workbook_id,
                "HAS_CONTROLS": None,
                "CONTROL_TARGETS_RESOLVED": False,
                "LINEAGE_COMPLETE": False,
                "SIGMA_ENRICHMENT": "error",
            })
            candidate.update(recommend(
                candidate, inventory.get("config"), inventory.get("days", 30),
                float(inventory.get("config", {}).get("credit_price", 3.0)),
            ))
            candidate["SCORE"] = round(score(candidate, inventory.get("config")), 2)
            continue
        controls = [
            element for element in elements
            if str(element.get("type") or "").lower() == "control"
        ]
        candidate.update({
            "WORKBOOK_ID": workbook_id,
            "HAS_CONTROLS": bool(controls),
            "CONTROL_COUNT": len(controls),
            # The list-elements and lineage APIs expose controls but not target bindings.
            "CONTROL_TARGETS_RESOLVED": False if controls else True,
            "LINEAGE_COMPLETE": bool(lineage),
            "SIGMA_ENRICHMENT": "matched",
        })
        node = str(node_from_candidate(candidate))
        schedule = next((
            item for item in schedules
            if node in {str(item.get("elementId") or ""),
                        str(item.get("sheetId") or "")}
        ), None)
        if schedule:
            element_id = str(schedule.get("elementId") or node)
            schedule_data = schedule.get("schedule") or {}
            candidate.update({
                "HAS_EXISTING_SCHEDULE": True,
                "ELEMENT_ID": element_id,
                "SHEET_ID": schedule.get("sheetId"),
                "SCHEDULE_CRON": schedule_data.get("cronSpec"),
                "SCHEDULE_TIMEZONE": schedule_data.get("timezone"),
                "SCHEDULE_PAUSED": bool(schedule.get("paused")),
                "DOWNSTREAM_ELEMENTS": len(descendants(lineage, element_id)),
            })
        elif node:
            candidate["ELEMENT_ID"] = node

        candidate.update(recommend(
            candidate, inventory.get("config"), inventory.get("days", 30),
            float(inventory.get("config", {}).get("credit_price", 3.0)),
        ))
        candidate["SCORE"] = round(score(candidate, inventory.get("config")), 2)

    fill_group_owners(inventory.get("duplicate_groups"), workbooks, client, owners, warnings)
    inventory["candidates"].sort(key=lambda row: -row.get("SCORE", 0))
    actions = {}
    total_savings = 0.0
    for candidate in inventory["candidates"]:
        action = candidate.get("ACTION", "Monitor")
        actions[action] = actions.get(action, 0) + 1
        total_savings += float(candidate.get("SAVINGS_EXPECTED", 0) or 0)
        if "CURRENT_MONTHLY_COST" in candidate:
            candidate["PROJECTED_MONTHLY_COST"] = round(max(
                float(candidate["CURRENT_MONTHLY_COST"])
                - float(candidate.get("SAVINGS_EXPECTED", 0) or 0), 0
            ), 2)
    inventory.setdefault("totals", {})["actions"] = actions
    inventory["totals"]["estimated_monthly_savings"] = round(total_savings, 2)
    inventory["warnings"] = warnings
    inventory["enrichment"] = {
        "accessible_workbooks": len(workbooks),
        "matched_candidates": matched,
        "owners_resolved": sum(1 for email in owners.values() if email),
        "control_target_bindings_available": False,
    }
    output = args.out or args.inv
    with open(output, "w") as f:
        json.dump(inventory, f, indent=2)
    if os.path.basename(output) == "inventory.json":
        from analyze import write_md
        write_md(os.path.dirname(os.path.abspath(output)), inventory)
    print(f"enriched {matched} candidate(s); wrote {output}", file=sys.stderr)


if __name__ == "__main__":
    main()
