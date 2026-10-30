#!/usr/bin/env python3
"""Sigma performance-and-cost advisor.

Read Snowflake ACCOUNT_USAGE via the `snow` CLI, report complete Sigma-tagged cost
coverage, attribute actionable workload to Sigma objects, measure materialization
refresh cost and observed reads, and emit keep/retune/investigate/remove/add/query-
optimization decisions.

Read-only: SELECTs against SNOWFLAKE.ACCOUNT_USAGE only.

    python3 scripts/analyze.py --conn default --days 30 --out out [--org <org-slug>]
"""
import argparse, csv, json, os, re, subprocess, sys
from datetime import datetime, timezone

from cost_model import (DEFAULT_CONFIG, coerce_row, group_credits, merge_config, recommend,
                        recommend_consolidation, score)

# Aggregates in-warehouse (cheap) at workbook+element grain, joining real per-query
# credits from QUERY_ATTRIBUTION_HISTORY. {DAYS} and {ORG_FILTER} are injected.
SQL = r"""
with qh as (
  select query_id, query_parameterized_hash, total_elapsed_time, bytes_scanned,
         bytes_spilled_to_local_storage, bytes_spilled_to_remote_storage,
         queued_overload_time, partitions_scanned, partitions_total,
         iff(position('{' in query_tag) > 0,
             try_parse_json(substr(query_tag, position('{' in query_tag))), null) as j
  from snowflake.account_usage.query_history
  where start_time > dateadd('day', -{DAYS}, {END}) and start_time <= {END}
    and query_tag ilike 'Sigma %'
    and execution_status = 'SUCCESS'
),
s as (
  select query_id, query_parameterized_hash, total_elapsed_time, bytes_scanned,
         bytes_spilled_to_local_storage, bytes_spilled_to_remote_storage,
         queued_overload_time, partitions_scanned, partitions_total,
         j:"sourceUrl"::string  as source_url,
         j:"kind"::string       as kind,
         split_part(j:"sourceUrl"::string, '/', 4) as org,
         regexp_substr(j:"sourceUrl"::string, '/(workbook|report|data-model)/[^?]+') as object,
         regexp_substr(j:"sourceUrl"::string, '[?&]:displayNodeId=([^&]+)', 1, 1, 'e', 1) as element
  from qh
  where j:"sourceUrl" is not null
    and coalesce(j:"kind"::string, '') <> 'materialization'
    {ORG_FILTER}
),
att as (
  select query_id, sum(credits_attributed_compute) as credits,
         sum(credits_used_query_acceleration) as qas_credits
  from snowflake.account_usage.query_attribution_history
  where start_time > dateadd('day', -{DAYS}, {END}) and start_time <= {END}
  group by 1
)
select
  s.org,
  s.object,
  coalesce(s.element, '(workbook load)')          as element,
  count(*)                                        as runs,
  round(sum(coalesce(att.credits, 0)), 4)         as credits,
  round(sum(coalesce(att.qas_credits, 0)), 4)     as qas_credits,
  round(avg(s.total_elapsed_time)/1000.0, 2)      as avg_sec,
  round(approx_percentile(s.total_elapsed_time/1000.0, .5), 2) as p50_sec,
  round(approx_percentile(s.total_elapsed_time/1000.0, .95), 2) as p95_sec,
  round(max(s.total_elapsed_time)/1000.0, 2)      as max_sec,
  round(sum(s.total_elapsed_time)/1000.0, 1)      as total_sec,
  sum(s.bytes_scanned)                             as bytes_scanned,
  max(s.bytes_scanned)                            as max_bytes,
  sum(coalesce(s.bytes_spilled_to_local_storage,0) +
      coalesce(s.bytes_spilled_to_remote_storage,0)) as bytes_spilled,
  round(sum(coalesce(s.queued_overload_time,0))/1000.0, 1) as queued_overload_sec,
  round(sum(s.partitions_scanned)/nullif(sum(s.partitions_total),0)*100, 2)
                                                   as partition_scan_pct,
  count(distinct s.query_parameterized_hash)       as query_patterns,
  max(s.source_url)                               as sample_url
from s
left join att using (query_id)
group by 1, 2, 3
having count(*) >= {MIN_RUNS}
order by credits desc, total_sec desc
limit {LIMIT};
"""


COVERAGE_SQL = r"""
with parsed as (
  select query_id,
         iff(position('{' in query_tag) > 0,
             try_parse_json(substr(query_tag, position('{' in query_tag))), null) as j
  from snowflake.account_usage.query_history
  where start_time > dateadd('day', -{DAYS}, {END}) and start_time <= {END}
    and query_tag ilike 'Sigma %'
    and execution_status = 'SUCCESS'
),
qh as (
  select * from parsed where 1=1
    {ORG_FILTER}
),
att as (
  select query_id, sum(credits_attributed_compute) credits,
         sum(credits_used_query_acceleration) qas_credits
  from snowflake.account_usage.query_attribution_history
  where start_time > dateadd('day', -{DAYS}, {END}) and start_time <= {END}
  group by 1
)
select count(*) all_sigma_queries,
       round(sum(coalesce(att.credits,0)),4) all_sigma_credits,
       round(sum(coalesce(att.qas_credits,0)),4) all_qas_credits,
       count_if(qh.j:"sourceUrl"::string is not null
                and coalesce(qh.j:"kind"::string,'') <> 'materialization')
         attributable_queries,
       round(sum(iff(qh.j:"sourceUrl"::string is not null
                     and coalesce(qh.j:"kind"::string,'') <> 'materialization',
                     coalesce(att.credits,0),0)),4) attributable_credits,
       round(sum(iff(qh.j:"sourceUrl"::string is not null
                     and coalesce(qh.j:"kind"::string,'') <> 'materialization',
                     coalesce(att.qas_credits,0),0)),4) attributable_qas_credits,
       count_if(qh.j:"kind"::string = 'materialization') materialization_queries,
       round(sum(iff(qh.j:"kind"::string = 'materialization',
                     coalesce(att.credits,0),0)),4) materialization_credits,
       round(sum(iff(qh.j:"kind"::string = 'materialization',
                     coalesce(att.qas_credits,0),0)),4) materialization_qas_credits,
       count_if(qh.j:"sourceUrl"::string is null
                and coalesce(qh.j:"kind"::string,'') <> 'materialization')
         unattributed_queries,
       round(sum(iff(qh.j:"sourceUrl"::string is null
                     and coalesce(qh.j:"kind"::string,'') <> 'materialization',
                     coalesce(att.credits,0),0)),4) unattributed_credits,
       round(sum(iff(qh.j:"sourceUrl"::string is null
                     and coalesce(qh.j:"kind"::string,'') <> 'materialization',
                     coalesce(att.qas_credits,0),0)),4) unattributed_qas_credits
from qh left join att using(query_id);
"""


MATERIALIZATION_SQL = r"""
with tagged as (
  select query_id, total_elapsed_time/1000.0 sec,
         regexp_substr(j:"sourceUrl"::string, '/workbook/[^?]+') wb_path,
         j:"sourceUrl"::string source_url, j:"kind"::string kind,
         split_part(j:"sourceUrl"::string, '/', 4) org,
         regexp_substr(j:"sourceUrl"::string, '/(workbook|report|data-model)/[^?]+') object,
         regexp_substr(j:"sourceUrl"::string, '[?&]:displayNodeId=([^&]+)',
                       1, 1, 'e', 1) element
  from (
    select query_id, total_elapsed_time,
           iff(position('{' in query_tag) > 0,
               try_parse_json(substr(query_tag, position('{' in query_tag))), null) j
    from snowflake.account_usage.query_history
    where start_time > dateadd('day', -{DAYS}, {END}) and start_time <= {END}
      and query_tag ilike 'Sigma %' and execution_status='SUCCESS'
  )
  where j:"sourceUrl"::string is not null
    {ORG_FILTER}
),
att as (
  select query_id, sum(credits_attributed_compute) credits,
         sum(credits_used_query_acceleration) qas_credits
  from snowflake.account_usage.query_attribution_history
  where start_time > dateadd('day', -{DAYS}, {END}) and start_time <= {END}
  group by 1
),
mat_runs as (
  select source_url, wb_path, org, object, coalesce(element,'(workbook load)') element,
         count(*) materialization_runs,
         round(sum(coalesce(att.credits,0)),4) materialization_credits,
         round(sum(coalesce(att.qas_credits,0)),4) materialization_qas_credits,
         round(approx_percentile(sec,.95),2) materialization_p95_sec
  from tagged left join att using(query_id)
  where kind='materialization' and wb_path is not null
  group by 1,2,3,4,5
),
mat_objects as (
  select distinct t.source_url, t.wb_path,
         coalesce(f.value:"objectId"::string, f.value:"objectName"::string) obj
  from tagged t
  join snowflake.account_usage.access_history ah using(query_id),
       lateral flatten(input=>ah.objects_modified) f
  where t.kind='materialization' and t.wb_path is not null
    and coalesce(f.value:"objectId"::string, f.value:"objectName"::string) is not null
),
read_objects as (
  select t.query_id, t.wb_path,
         coalesce(f.value:"objectId"::string, f.value:"objectName"::string) obj
  from tagged t
  join snowflake.account_usage.access_history ah using(query_id),
       lateral flatten(input=>ah.direct_objects_accessed) f
  where coalesce(t.kind,'') <> 'materialization'
),
matched_queries as (
  select distinct m.source_url, t.query_id, t.sec, coalesce(att.credits,0) credits,
         coalesce(att.qas_credits,0) qas_credits
  from read_objects r
  join tagged t using(query_id, wb_path)
  join mat_objects m on m.wb_path=r.wb_path and m.obj=r.obj
  left join att using(query_id)
),
matched as (
  select source_url, count(*) matched_materialized_reads,
         round(sum(credits),4) matched_read_credits,
         round(sum(qas_credits),4) matched_read_qas_credits,
         round(approx_percentile(sec,.95),2) matched_read_p95_sec
  from matched_queries group by 1
),
workbook_reads as (
  select wb_path, count(*) workbook_reads,
         round(sum(coalesce(att.credits,0)),4) workbook_read_credits,
         round(sum(coalesce(att.qas_credits,0)),4) workbook_read_qas_credits,
         round(approx_percentile(sec,.95),2) workbook_read_p95_sec
  from tagged left join att using(query_id)
  where coalesce(kind,'') <> 'materialization' group by 1
)
select m.org, m.object, m.element, m.source_url sample_url,
       m.materialization_runs, m.materialization_credits,
       m.materialization_qas_credits, m.materialization_p95_sec,
       coalesce(x.matched_materialized_reads,0) matched_materialized_reads,
       coalesce(x.matched_read_credits,0) matched_read_credits,
       coalesce(x.matched_read_qas_credits,0) matched_read_qas_credits,
       x.matched_read_p95_sec,
       greatest(coalesce(w.workbook_reads,0) -
                coalesce(x.matched_materialized_reads,0),0) workbook_unmatched_reads,
       greatest(coalesce(w.workbook_read_credits,0) -
                coalesce(x.matched_read_credits,0),0) workbook_unmatched_read_credits,
       greatest(coalesce(w.workbook_read_qas_credits,0) -
                coalesce(x.matched_read_qas_credits,0),0)
         workbook_unmatched_read_qas_credits,
       w.workbook_read_p95_sec workbook_unmatched_read_p95_sec
from mat_runs m
left join matched x using(source_url)
left join workbook_reads w using(wb_path)
order by m.materialization_credits desc;
"""


# Duplicate work: identical materialization builds, materializations over the same
# source tables, and identical live queries issued from several workbooks. Mirrors
# sql/sigma_duplicates.sql; analyze.py assembles the per-member rows into groups.
DUPLICATE_SQL = r"""
with tagged as (
  select query_id, query_parameterized_hash qph,
         iff(j:"kind"::string = 'materialization', query_text, null) qtext,
         j:"sourceUrl"::string source_url, j:"kind"::string kind,
         split_part(j:"sourceUrl"::string, '/', 4) org,
         regexp_substr(j:"sourceUrl"::string,
                       '/(workbook|report|data-model)/[^?]+') object,
         coalesce(regexp_substr(j:"sourceUrl"::string,
                                '[?&]:displayNodeId=([^&]+)', 1, 1, 'e', 1),
                  '(workbook load)') element
  from (
    select query_id, query_parameterized_hash, query_text,
           iff(position('{' in query_tag) > 0,
               try_parse_json(substr(query_tag, position('{' in query_tag))), null) j
    from snowflake.account_usage.query_history
    where start_time > dateadd('day', -{DAYS}, {END}) and start_time <= {END}
      and query_tag ilike 'Sigma %' and execution_status = 'SUCCESS'
  )
  where j:"sourceUrl"::string is not null
    {ORG_FILTER}
),
att as (
  select query_id, sum(coalesce(credits_attributed_compute,0)
                       + coalesce(credits_used_query_acceleration,0)) credits
  from snowflake.account_usage.query_attribution_history
  where start_time > dateadd('day', -{DAYS}, {END}) and start_time <= {END}
  group by 1
),
-- Materialization CTAS statements, reduced to the text after "as select".
-- Sigma's trailing "-- Sigma ... {json}" comment (user/email) is stripped. The CTAS
-- target (a per-workbook t_mat table) sits before "as select", so it is not hashed;
-- t_mat reads inside the SELECT body are kept: they are different tables.
mat as (
  select query_id, source_url, org, object, element,
         md5(regexp_substr(norm, ' as \\(?select .*')) sig
  from (
    select query_id, source_url, org, object, element,
           regexp_replace(
             regexp_replace(lower(qtext), '\\s+', ' '),
             '\\s*--\\s*sigma.*$', '') norm
    from tagged where kind = 'materialization' and object is not null
  )
  where norm like 'create %' and regexp_substr(norm, ' as \\(?select .*') is not null
),
-- Source tables each materialization read, excluding Sigma write-back tables and
-- anything the same job created.
mat_bases as (
  select m.object, m.element, f.value:"objectName"::string obj
  from mat m
  join snowflake.account_usage.access_history ah on ah.query_id = m.query_id,
       lateral flatten(input => ah.base_objects_accessed) f
  where f.value:"objectName"::string is not null
    and not regexp_like(upper(f.value:"objectName"::string), '(.*\\.)?"?T_MAT_.*')
    and not array_contains(f.value:"objectName"::variant,
                           transform(ah.objects_modified, o -> o:"objectName"))
),
member_bases as (
  select object, element,
         array_to_string(array_sort(array_agg(distinct obj)), ',') base_tables
  from mat_bases group by 1, 2
),
build_members as (
  select m.sig, m.org, m.object, m.element, max(m.source_url) sample_url,
         count(*) runs, round(sum(coalesce(att.credits, 0)), 4) credits
  from mat m left join att using (query_id)
  group by 1, 2, 3, 4
),
build_groups as (
  select sig from build_members
  group by 1 having count(distinct object || '|' || element) >= 2
),
identical_build as (
  select 'identical-build' match, b.sig signature, b.org, b.object, b.element,
         b.sample_url, b.runs, b.credits, mb.base_tables
  from build_members b
  join build_groups g using (sig)
  left join member_bases mb on mb.object = b.object and mb.element = b.element
),
element_build as (
  select object, element, max(sig) sig from build_members group by 1, 2
),
base_members as (
  select md5(mb.base_tables) sig, b.org, b.object, b.element,
         max(b.sample_url) sample_url, sum(b.runs) runs, sum(b.credits) credits,
         max(mb.base_tables) base_tables, max(eb.sig) build_sig
  from member_bases mb
  join build_members b on b.object = mb.object and b.element = mb.element
  join element_build eb on eb.object = mb.object and eb.element = mb.element
  where mb.base_tables <> ''
  group by 1, 2, 3, 4
),
base_groups as (
  select sig from base_members
  group by 1
  having count(distinct object) >= 2
     -- skip groups already explained by a single identical build
     and not (count(build_sig) = count(*) and min(build_sig) = max(build_sig)
              and min(build_sig) in (select sig from build_groups))
),
same_base as (
  select 'same-base-tables' match, m.sig signature, m.org, m.object, m.element,
         m.sample_url, m.runs, round(m.credits, 4) credits, m.base_tables
  from base_members m join base_groups using (sig)
),
live as (
  select t.qph, t.org, t.object, max(t.element) element, max(t.source_url) sample_url,
         count(*) runs, round(sum(coalesce(att.credits, 0)), 4) credits
  from tagged t left join att using (query_id)
  where coalesce(t.kind, '') <> 'materialization' and t.qph is not null
    and t.object like '/workbook/%'
  group by 1, 2, 3
),
live_groups as (
  select qph from live
  group by 1
  having count(distinct object) >= 2
     and (sum(credits) >= {MAT_MIN_CREDITS} / 10 or sum(runs) >= {MAT_MIN_RUNS})
),
identical_live as (
  select 'identical-live-query' match, l.qph signature, l.org, l.object, l.element,
         l.sample_url, l.runs, l.credits, null base_tables
  from live l join live_groups using (qph)
)
select * from identical_build
union all select * from same_base
union all select * from identical_live
order by match, signature, credits desc;
"""


WAREHOUSE_SQL = r"""
select round(sum(credits_used_compute),4) warehouse_compute_credits,
       round(sum(credits_attributed_compute_queries),4) attributed_query_credits,
       round(sum(credits_used_compute)-sum(credits_attributed_compute_queries),4)
         estimated_idle_credits,
       round((sum(credits_used_compute)-sum(credits_attributed_compute_queries)) /
             nullif(sum(credits_used_compute),0)*100,2) estimated_idle_pct,
       count(distinct warehouse_name) warehouses
from snowflake.account_usage.warehouse_metering_history
where start_time > dateadd('day', -{DAYS}, {END}) and start_time <= {END};
"""


# Credit attribution lags QUERY_HISTORY by hours. Every window ends at the newest
# attributed query, so recent runs are not counted at zero cost.
WATERMARK_SQL = r"""
select date_part(epoch_millisecond, max(start_time)) end_ms,
       date_part(epoch_millisecond, current_timestamp()) now_ms
from snowflake.account_usage.query_attribution_history
where start_time > dateadd('day', -{DAYS}, current_timestamp());
"""


def attribution_end(conn, days):
    """(SQL literal, ISO UTC end, lag hours) for the window end. Falls back to
    current_timestamp() when attribution is empty or unreadable."""
    try:
        r = {str(k).lower(): v for k, v in run_sql(
            conn, WATERMARK_SQL.replace("{DAYS}", str(days)))[0].items()}
        end_ms, now_ms = int(float(r["end_ms"])), int(float(r["now_ms"]))
    except Exception:
        return "current_timestamp()", None, None
    iso = datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat()
    return (f"to_timestamp_ltz({end_ms}, 3)", iso,
            round(max(now_ms - end_ms, 0) / 3_600_000, 1))


def detect_credit_price(conn):
    """Best-effort: read the account's effective $/credit from ORGANIZATION_USAGE.
    Returns float or None (no access / internal account with no billing data)."""
    queries = [
        "select round(sum(usage_in_currency)/nullif(sum(usage),0),4) as rate "
        "from snowflake.organization_usage.usage_in_currency_daily "
        "where usage_type='compute' and usage_date > dateadd('day',-30,current_date())",
        "select round(avg(effective_rate),4) as rate "
        "from snowflake.organization_usage.rate_sheet_daily "
        "where usage_type ilike '%compute%' and date > dateadd('day',-90,current_date())",
    ]
    for q in queries:
        try:
            r = run_sql(conn, q)
            v = r[0].get("RATE") if r else None
            if v not in (None, "", 0):
                return float(v)
        except Exception:
            pass
    return None


def run_sql(conn, sql):
    p = subprocess.run(["snow", "sql", "-c", conn, "--format", "json", "--stdin"],
                       input=sql, capture_output=True, text=True)
    out = p.stdout.strip()
    # SSO/info lines go to stderr; stdout should be JSON. Be defensive anyway.
    i = out.find("[")
    if i < 0:
        raise RuntimeError(
            f"snow returned no JSON.\nstdout:\n{out[:500]}\nstderr:\n{p.stderr[-800:]}"
        )
    return json.loads(out[i:])


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def canon(obj):
    """Stable workbook/data-model id: Snowflake tags carry `<name-slug>-<id>`, Sigma's
    API carries the bare id, and a rename changes the slug."""
    return str(obj or "").rstrip("/").rsplit("/", 1)[-1].lower().rsplit("-", 1)[-1]


def _mkey(member_or_row, aliases=None):
    c = canon(member_or_row.get("OBJECT"))
    return ((aliases or {}).get(c, c), member_or_row.get("ELEMENT"))


def _wb_keys(w):
    """Both ids a workbook may be known by (Sigma workbookId, URL slug id)."""
    return {canon(w[k]) for k in ("WORKBOOK_ID", "WORKBOOK_URL_ID") if w.get(k)}


def scan_aliases(scan):
    """canon(workbookId) -> canon(URL id), so a row keyed by either id lines up."""
    out = {}
    for w in scan.get("workbooks", []):
        if w.get("WORKBOOK_ID") and w.get("WORKBOOK_URL_ID"):
            out[canon(w["WORKBOOK_ID"])] = canon(w["WORKBOOK_URL_ID"])
    return out


def _split_tables(value):
    if isinstance(value, (list, tuple)):
        parts = value
    else:
        parts = str(value or "").split(",")
    return sorted({str(p).strip().strip("[]\"'").upper() for p in parts if str(p).strip()})


# ---- cost-table input -------------------------------------------------------

def read_cost_table(path):
    """CSV with header, or a JSON array of objects. Column names are case-insensitive."""
    if str(path).lower().endswith(".json"):
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("rows", data.get("data", []))
    else:
        with open(path, newline="") as f:
            data = list(csv.DictReader(f))
    return [{str(k).strip().lower(): v for k, v in rec.items() if k is not None}
            for rec in data]


def _cost_object(rec):
    raw = str(rec.get("object") or "").strip()
    if raw:
        m = re.search(r"/(workbook|report|data-model)/[^?#\s]+", raw)
        if m:
            return m.group(0), (raw if raw.startswith("http") else None)
        return f"/workbook/{raw.strip('/')}", None
    if str(rec.get("data_model_id") or "").strip():
        return f"/data-model/{str(rec['data_model_id']).strip()}", None
    if str(rec.get("workbook_id") or "").strip():
        return f"/workbook/{str(rec['workbook_id']).strip()}", None
    return None, None


def _has(v):
    return v is not None and str(v).strip() != ""


def _query_count_keys(records):
    keys = set()
    for rec in records:
        obj, _ = _cost_object(rec)
        if obj and _has(rec.get("query_count")):
            element = str(rec.get("element_id") or "").strip() or "(workbook load)"
            keys.add((obj, element))
    return keys


def cost_table_rows(records, credit_price, org=None):
    """Aggregate cost-table records to one candidate per (OBJECT, ELEMENT).
    Returns (rows, window_days, warnings); window_days is the span of the dates
    (None without dates). p95 is the max reported value (percentiles cannot be
    combined exactly)."""
    agg, dates, warnings, skipped = {}, set(), [], 0
    for rec in records:
        obj, url = _cost_object(rec)
        credits = (_num(rec.get("credits")) if _has(rec.get("credits"))
                   else _num(rec.get("cost")) / credit_price if credit_price else 0.0)
        if not obj or not (_has(rec.get("credits")) or _has(rec.get("cost"))):
            skipped += 1
            continue
        element = str(rec.get("element_id") or "").strip() or "(workbook load)"
        d = str(rec.get("date") or "").strip()[:10]
        if d:
            try:
                dates.add(datetime.strptime(d, "%Y-%m-%d").date())
            except ValueError:
                pass
        n = int(_num(rec.get("query_count")))
        mat = str(rec.get("kind") or "").strip().lower() == "materialization"
        row = agg.setdefault((obj, element), {
            "ORG": org, "OBJECT": obj, "ELEMENT": element, "SAMPLE_URL": url,
            "RUNS": 0, "CREDITS": 0.0, "AVG_SEC": 0.0, "P95_SEC": 0.0,
            "_AVG_W": 0.0, "MATERIALIZATION_RUNS": 0, "MATERIALIZATION_CREDITS": 0.0,
            "MATERIALIZATION_P95_SEC": 0.0, "READ_EVIDENCE_AVAILABLE": False,
        })
        row["SAMPLE_URL"] = row["SAMPLE_URL"] or url
        if str(rec.get("owner_email") or "").strip():
            row["OWNER_EMAIL"] = str(rec["owner_email"]).strip()
        if mat:
            row["MATERIALIZATION_RUNS"] += n
            row["MATERIALIZATION_CREDITS"] += credits
            row["MATERIALIZATION_P95_SEC"] = max(row["MATERIALIZATION_P95_SEC"],
                                                 _num(rec.get("p95_sec")))
            row["HAS_EXISTING_SCHEDULE"] = True
        else:
            row["RUNS"] += n
            row["CREDITS"] += credits
            row["AVG_SEC"] += _num(rec.get("avg_sec")) * max(n, 1)
            row["_AVG_W"] += max(n, 1)
            row["P95_SEC"] = max(row["P95_SEC"], _num(rec.get("p95_sec")))
    if skipped:
        warnings.append(f"Cost table: skipped {skipped} row(s) with no object id or no "
                        "credits/cost value.")
    window = ((max(dates) - min(dates)).days + 1) if dates else None
    if dates and all(d.day == 1 for d in dates):
        warnings.append(
            f"Cost table: every date is the 1st of a month, so it looks monthly and the "
            f"{window}-day window may be understated (monthly figures overstated); "
            "pass --days to set the window.")
    rows = []
    for row in agg.values():
        w = row.pop("_AVG_W")
        row["AVG_SEC"] = round(row["AVG_SEC"] / w, 2) if w else 0.0
        row["CREDITS"] = round(row["CREDITS"], 4)
        row["MATERIALIZATION_CREDITS"] = round(row["MATERIALIZATION_CREDITS"], 4)
        rows.append(row)
    return rows, window, warnings


# ---- Sigma metadata scan (scripts/scan_workbooks.py) -------------------------

def load_scan(path):
    with open(path) as f:
        scan = json.load(f)
    if not isinstance(scan, dict) or "workbooks" not in scan:
        raise SystemExit(f"{path} is not a scan.json (no 'workbooks' key); "
                         "produce it with scripts/scan_workbooks.py")
    return scan


def scan_candidates(scan, org=None):
    """One candidate per active scheduled element in the scan (sigma-only mode)."""
    rows = []
    for w in scan.get("workbooks", []):
        obj = "/workbook/" + str(w.get("WORKBOOK_URL_ID") or w.get("WORKBOOK_ID"))
        for e in w.get("ELEMENTS", []):
            if not e.get("SCHEDULE_CRON") or e.get("SCHEDULE_PAUSED"):
                continue
            rows.append({
                "ORG": scan.get("org") or org, "OBJECT": obj,
                "ELEMENT": e.get("ELEMENT_ID") or "(workbook load)",
                "SAMPLE_URL": w.get("URL"), "HAS_EXISTING_SCHEDULE": True,
                "READ_EVIDENCE_AVAILABLE": False,
                "REFRESHES_PER_DAY": e.get("REFRESHES_PER_DAY"),
                "SCHEDULE_CRON": e.get("SCHEDULE_CRON"), "SCHEDULE_PAUSED": False,
                "OWNER_EMAIL": w.get("OWNER_EMAIL"),
                "WORKBOOK_NAME": w.get("NAME"), "ELEMENT_NAME": e.get("NAME"),
            })
    return rows


def apply_scan(rows, scan):
    """Fill owner and refresh cadence on candidates from scan metadata."""
    owners, elements = {}, {}
    for w in scan.get("workbooks", []):
        for k in _wb_keys(w):
            owners[k] = w.get("OWNER_EMAIL")
            for e in w.get("ELEMENTS", []):
                elements[(k, e.get("ELEMENT_ID"))] = e
    for r in rows:
        k = _mkey(r)
        if not r.get("OWNER_EMAIL") and owners.get(k[0]):
            r["OWNER_EMAIL"] = owners[k[0]]
        e = elements.get(k)
        if e and r.get("REFRESHES_PER_DAY") is None:
            r["REFRESHES_PER_DAY"] = e.get("REFRESHES_PER_DAY")
        if e and e.get("SCHEDULE_PAUSED") is not None and "SCHEDULE_PAUSED" not in r:
            r["SCHEDULE_PAUSED"] = bool(e["SCHEDULE_PAUSED"])
        if e and e.get("SCHEDULE_CRON") and not r.get("SCHEDULE_CRON"):
            r["SCHEDULE_CRON"] = e["SCHEDULE_CRON"]


# ---- duplicate groups --------------------------------------------------------

def _member(row, days, match):
    runs = int(_num(row.get("RUNS")))
    return {
        "OBJECT": row.get("OBJECT"), "ELEMENT": row.get("ELEMENT") or "(workbook load)",
        "SAMPLE_URL": row.get("SAMPLE_URL"), "RUNS": runs,
        "CREDITS": round(_num(row.get("CREDITS")), 4),
        "REFRESHES_PER_DAY": row.get("REFRESHES_PER_DAY") if "REFRESHES_PER_DAY" in row
        else (round(runs / max(days, 1), 2) if match != "identical-live-query" else None),
        # "cron": a schedule's cadence (scan/enrich); "observed": runs over the window
        "REFRESHES_SOURCE": "cron" if "REFRESHES_PER_DAY" in row else "observed",
        "OWNER_EMAIL": row.get("OWNER_EMAIL"),
    }


def _add_member(members, m):
    key = _mkey(m)
    cur = members.get(key)
    if cur is None:
        members[key] = m
        return
    for f in ("RUNS", "CREDITS"):
        cur[f] = round(cur[f] + m[f], 4)
    if cur.get("REFRESHES_PER_DAY") is not None and m.get("REFRESHES_PER_DAY") is not None:
        cur["REFRESHES_PER_DAY"] = round(cur["REFRESHES_PER_DAY"] + m["REFRESHES_PER_DAY"], 2)
        if cur.get("REFRESHES_SOURCE") != m.get("REFRESHES_SOURCE"):
            cur["REFRESHES_SOURCE"] = "observed"
    for f in ("SAMPLE_URL", "OWNER_EMAIL"):
        cur[f] = cur.get(f) or m.get(f)


def assemble_duplicates(rows, days):
    """Per-(group, member) Snowflake rows -> duplicate groups (no recommendation yet)."""
    by_sig = {}
    for raw in rows:
        r = {str(k).upper(): v for k, v in raw.items()}
        match = r.get("MATCH")
        if not match or not r.get("OBJECT"):
            continue
        g = by_sig.setdefault((match, r.get("SIGNATURE")),
                              {"MATCH": match, "SOURCE": "snowflake", "BASE_TABLES": [],
                               "_members": {}})
        g["BASE_TABLES"] = sorted(set(g["BASE_TABLES"]) | set(_split_tables(r.get("BASE_TABLES"))))
        _add_member(g["_members"], _member(r, days, match))
    groups = [g for g in by_sig.values()]
    # identical live queries shared by the same workbooks are one finding, not N
    live, out = {}, []
    for g in groups:
        g["MEMBERS"] = list(g.pop("_members").values())
        if len({canon(m["OBJECT"]) for m in g["MEMBERS"]}) < 2 and g["MATCH"] != "identical-build":
            continue
        if g["MATCH"] == "identical-live-query":
            key = frozenset(canon(m["OBJECT"]) for m in g["MEMBERS"])
            tgt = live.get(key)
            if tgt is None:
                live[key] = g
                out.append(g)
            else:
                mm = {_mkey(m): m for m in tgt["MEMBERS"]}
                for m in g["MEMBERS"]:
                    _add_member(mm, m)
                tgt["MEMBERS"] = list(mm.values())
        else:
            out.append(g)
    # an element belongs to one finding: same-base-tables groups give up members an
    # identical build already claimed, and vanish when fewer than two workbooks remain
    claimed = {_mkey(m) for g in out if g["MATCH"] == "identical-build" for m in g["MEMBERS"]}
    keep = []
    for g in out:
        if g["MATCH"] == "same-base-tables":
            g["MEMBERS"] = [m for m in g["MEMBERS"] if _mkey(m) not in claimed]
            if len({canon(m["OBJECT"]) for m in g["MEMBERS"]}) < 2:
                continue
        keep.append(g)
    return keep


def _normalise_scan_groups(scan):
    out = []
    for g in scan.get("duplicate_groups", []):
        members = []
        for m in g.get("MEMBERS", []):
            members.append({
                "OBJECT": m.get("OBJECT"), "ELEMENT": m.get("ELEMENT") or "(workbook load)",
                "SAMPLE_URL": m.get("SAMPLE_URL"), "RUNS": int(_num(m.get("RUNS"))),
                "CREDITS": round(_num(m.get("CREDITS")), 4),
                "REFRESHES_PER_DAY": m.get("REFRESHES_PER_DAY"),
                "REFRESHES_SOURCE": "cron",
                "OWNER_EMAIL": m.get("OWNER_EMAIL"),
            })
        out.append({"MATCH": g.get("MATCH"), "SOURCE": "scan",
                    "BASE_TABLES": _split_tables(g.get("BASE_TABLES")), "MEMBERS": members,
                    **{k: g[k] for k in ("SOURCE_REF", "DOWNSTREAM_ELEMENTS",
                                         "DOWNSTREAM_COUNT") if g.get(k)}})
    return out


def _same_members(a, b):
    """Same (object, element) set, or the same workbooks (element ids differ between
    Snowflake tags and the Sigma API). A Snowflake materialization group is also
    absorbed by a scan group that covers its workbooks plus unscheduled copies."""
    if a["MATCH"] == "identical-live-query":
        return False
    ka = {_mkey(m) for m in a["MEMBERS"]}
    kb = {_mkey(m) for m in b["MEMBERS"]}
    if ka == kb:
        return True
    oa = [canon(m["OBJECT"]) for m in a["MEMBERS"]]
    ob = [canon(m["OBJECT"]) for m in b["MEMBERS"]]
    if len(set(oa)) != len(oa) or len(set(ob)) != len(ob):
        return False
    # element ids differ across sources, so only scheduled scan members (the
    # materialized ones Snowflake can see) may stand in for a Snowflake member
    common = set(oa) & set(ob)
    if not common or not all(m.get("REFRESHES_PER_DAY") for m in b["MEMBERS"]
                             if canon(m["OBJECT"]) in common):
        return False
    return set(oa) == set(ob) or (len(oa) >= 2 and set(oa) <= set(ob))


def merge_groups(snowflake_groups, scan_groups):
    """Matching member sets collapse to one group: Snowflake wins, scan fills gaps."""
    out = list(snowflake_groups)
    for s in scan_groups:
        twin = next((g for g in snowflake_groups if _same_members(g, s)), None)
        if twin is None:
            out.append(s)
            continue
        if twin["MATCH"] == "same-base-tables" and s["MATCH"] in (
                "identical-logic", "identical-sql"):
            twin["MATCH"] = s["MATCH"]
        if not twin["BASE_TABLES"]:
            twin["BASE_TABLES"] = s["BASE_TABLES"]
        for f in ("SOURCE_REF", "DOWNSTREAM_ELEMENTS", "DOWNSTREAM_COUNT"):
            if s.get(f) and not twin.get(f):
                twin[f] = s[f]
        by_obj = {canon(m["OBJECT"]): m for m in s["MEMBERS"]}
        have = {canon(m["OBJECT"]) for m in twin["MEMBERS"]}
        for m in twin["MEMBERS"]:
            sm = by_obj.get(canon(m["OBJECT"]))
            if sm:
                for f in ("OWNER_EMAIL", "SAMPLE_URL"):
                    if m.get(f) is None:
                        m[f] = sm.get(f)
                # the schedule's cron beats a window average (schedules may be newer
                # than the analysis window)
                if sm.get("REFRESHES_PER_DAY") is not None:
                    m["REFRESHES_PER_DAY"] = sm["REFRESHES_PER_DAY"]
                    m["REFRESHES_SOURCE"] = "cron"
        twin["MEMBERS"] += [m for k, m in by_obj.items() if k not in have]
    return out


def finalize_groups(groups, candidates, cfg, days, credit_price, aliases=None):
    """Fill missing member credits from candidates, recommend, rank, number, tag."""
    by_key = {}
    for r in candidates:
        by_key.setdefault(_mkey(r, aliases), []).append(r)
    def candidates_of(m):
        c = by_key.get(_mkey(m, aliases), [])
        if not c and m.get("REFRESHES_PER_DAY"):
            # element ids can differ between sources: for a scheduled member fall back
            # to the workbook's only materializing candidate
            mats = [x for x in candidates if _mkey(x, aliases)[0] == _mkey(m, aliases)[0]
                    and _num(x.get("MATERIALIZATION_RUNS")) > 0]
            c = mats if len(mats) == 1 else []
        return c

    for g in groups:
        for m in g["MEMBERS"]:
            if m["CREDITS"] or g["MATCH"] == "identical-live-query":
                continue
            c = candidates_of(m)
            m["CREDITS"] = round(sum(_num(x.get("MATERIALIZATION_CREDITS"))
                                     + _num(x.get("MATERIALIZATION_QAS_CREDITS"))
                                     for x in c), 4)
            m["RUNS"] = m["RUNS"] or sum(int(_num(x.get("MATERIALIZATION_RUNS"))) for x in c)
            if not m.get("SAMPLE_URL") and c:
                m["SAMPLE_URL"] = c[0].get("SAMPLE_URL")
            if not m.get("OWNER_EMAIL") and c:
                m["OWNER_EMAIL"] = c[0].get("OWNER_EMAIL")
        g["MEMBER_COUNT"] = len(g["MEMBERS"])
        total, redundant = group_credits(g)
        g["TOTAL_CREDITS"] = round(total, 4)
        g["REDUNDANT_CREDITS"] = round(redundant, 4)
        g.update(recommend_consolidation(g, cfg, days, credit_price))
        g["SCORE"] = round(score(g, cfg), 2)
        # Measured, weak-match groups with almost no repeat cost (often throwaway copies)
        # stay in the inventory but are summarized rather than detailed in reports.
        g["LOW_VALUE"] = bool(
            g["MATCH"] not in ("identical-build", "identical-logic")
            and any(_num(m.get("CREDITS")) > 0 for m in g["MEMBERS"])
            and g.get("SAVINGS_EXPECTED", 0) < 0.1 * float(cfg["min_monthly_savings"])
        )
    groups.sort(key=lambda g: -g["SCORE"])
    counts = {}
    for g in groups:
        prefix = "scan" if g["SOURCE"] == "scan" else "sf"
        counts[prefix] = counts.get(prefix, 0) + 1
        g["GROUP_ID"] = f"{prefix}-{counts[prefix]}"
        for m in g["MEMBERS"]:
            for r in candidates_of(m):
                r.setdefault("CONSOLIDATION_GROUP", g["GROUP_ID"])
    return [{"GROUP_ID": g["GROUP_ID"], **{k: v for k, v in g.items() if k != "GROUP_ID"}}
            for g in groups]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conn", default="default", help="snow CLI connection name")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--org", default=None, help="Sigma org slug to scope to (else account-wide)")
    ap.add_argument("--min-runs", type=int, default=2, help="min runs to appear at all")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--out", default="cost-optimization-out")
    ap.add_argument("--mat-min-runs", type=int, default=DEFAULT_CONFIG["mat_min_runs"])
    ap.add_argument("--mat-min-avg-sec", type=float,
                    default=DEFAULT_CONFIG["mat_min_avg_sec"])
    ap.add_argument("--mat-min-credits", type=float,
                    default=DEFAULT_CONFIG["mat_min_credits"])
    ap.add_argument("--latency-slo-sec", type=float,
                    default=DEFAULT_CONFIG["latency_slo_sec"])
    ap.add_argument("--freshness", default=DEFAULT_CONFIG["freshness"],
                    help="real-time / <=1h / <=1day / varies")
    ap.add_argument("--acceptable-slowdown-sec", type=float,
                    default=DEFAULT_CONFIG["acceptable_slowdown_sec"])
    ap.add_argument("--credit-price", type=float, default=None,
                    help="$ per Snowflake credit (default: auto-detect from ORGANIZATION_USAGE, else 3.0)")
    ap.add_argument("--goal", choices=["cost", "latency", "performance_per_dollar", "both"],
                    default=DEFAULT_CONFIG["goal"])
    ap.add_argument("--from-rows", help="JSON array of workload rows, or a v2 bundle with "
                    "workload/coverage/materializations/warehouses")
    ap.add_argument("--from-cost-table", help="CSV or JSON cost table (workbook_id|"
                    "data_model_id|object, credits|cost, optional element_id/date/"
                    "query_count/avg_sec/p95_sec/kind/owner_email); no Snowflake needed")
    ap.add_argument("--sigma-only", action="store_true",
                    help="no cost data: schedules and duplicate work from --scan only")
    ap.add_argument("--scan", help="scan.json from scripts/scan_workbooks.py (Sigma "
                    "metadata: schedules, owners, duplicate SQL); optional in every mode")
    ap.add_argument("--config", help="JSON file with any of the above (intake answers); flags win")
    a = ap.parse_args()
    if sum(map(bool, (a.from_rows, a.from_cost_table, a.sigma_only))) > 1:
        ap.error("--from-rows, --from-cost-table and --sigma-only are mutually exclusive")
    if a.sigma_only and not a.scan:
        ap.error("--sigma-only requires --scan scan.json (see scripts/scan_workbooks.py)")
    offline = bool(a.from_rows or a.from_cost_table or a.sigma_only)
    file_config = {}
    if a.config and os.path.exists(a.config):
        with open(a.config) as f:
            file_config = json.load(f)
        def explicitly_supplied(key):
            flag = "--" + key.replace("_", "-")
            return any(arg == flag or arg.startswith(flag + "=")
                       for arg in sys.argv[1:])
        for key in (
            "mat_min_runs", "mat_min_avg_sec", "mat_min_credits", "credit_price",
            "days", "goal", "latency_slo_sec", "freshness",
            "acceptable_slowdown_sec", "org", "min_runs", "limit",
        ):
            if key in file_config and not explicitly_supplied(key):
                setattr(a, key, file_config[key])
    model_config = merge_config({
        **file_config,
        "mat_min_runs": a.mat_min_runs,
        "mat_min_avg_sec": a.mat_min_avg_sec,
        "mat_min_credits": a.mat_min_credits,
        "latency_slo_sec": a.latency_slo_sec,
        "freshness": a.freshness,
        "acceptable_slowdown_sec": a.acceptable_slowdown_sec,
        "goal": "performance_per_dollar" if a.goal == "both" else a.goal,
    })

    price_source = "provided"
    if a.credit_price is None:
        a.credit_price = None if offline else detect_credit_price(a.conn)
        price_source = "auto-detected" if a.credit_price else "assumed"
        if a.credit_price is None:
            a.credit_price = 3.0
    os.makedirs(a.out, exist_ok=True)

    org_filter = ""
    if a.org:
        safe_org = str(a.org).replace("'", "''")
        org_filter = f"and j:\"sourceUrl\"::string ilike '%/{safe_org}/%'"
    sql = (SQL.replace("{DAYS}", str(a.days)).replace("{ORG_FILTER}", org_filter)
              .replace("{MIN_RUNS}", str(a.min_runs)).replace("{LIMIT}", str(a.limit)))

    coverage_rows, materialization_rows, warehouse_rows, warnings = [], [], [], []
    duplicate_rows, evidence_notes, window_end = [], [], None
    evidence = "snowflake"
    scan = load_scan(a.scan) if a.scan else None
    if a.org:
        warnings.append(
            "Org-scoped totals exclude Sigma-tagged queries without a sourceUrl "
            "because those queries cannot be assigned safely to an org."
        )
    if a.sigma_only:
        evidence = "sigma-only"
        rows = scan_candidates(scan, a.org)
        evidence_notes += [
            "No cost data: credits, runs, and load times were not measured, so "
            "savings are not estimated.",
            "Read utilization not measured: schedules and duplicate work come from "
            "Sigma metadata only.",
        ]
    elif a.from_cost_table:
        evidence = "cost-table"
        records = read_cost_table(a.from_cost_table)
        rows, window, ct_warnings = cost_table_rows(records, a.credit_price, a.org)
        warnings += ct_warnings
        if window and window != a.days:
            if any(arg == "--days" or arg.startswith("--days=") for arg in sys.argv[1:]):
                warnings.append(f"Cost table dates span {window} day(s) but --days "
                                f"{a.days} was supplied and is used for the window.")
            else:
                a.days = window
        # Totals come from the whole table; candidates are cut like the Snowflake SQL
        # (min runs, top N by credits) so both paths produce comparable inventories.
        coverage_rows = [{
            "all_sigma_queries": sum(r["RUNS"] + r["MATERIALIZATION_RUNS"] for r in rows),
            "all_sigma_credits": sum(r["CREDITS"] + r["MATERIALIZATION_CREDITS"] for r in rows),
            "attributable_queries": sum(r["RUNS"] for r in rows),
            "attributable_credits": sum(r["CREDITS"] for r in rows),
            "materialization_queries": sum(r["MATERIALIZATION_RUNS"] for r in rows),
            "materialization_credits": sum(r["MATERIALIZATION_CREDITS"] for r in rows),
        }]
        scheduled = lambda r: r["MATERIALIZATION_RUNS"] or r["MATERIALIZATION_CREDITS"] > 0
        counted = _query_count_keys(records)
        kept = [
            r for r in rows
            if scheduled(r)
            or ((r["OBJECT"], r["ELEMENT"]) in counted and r["RUNS"] >= a.min_runs)
            or ((r["OBJECT"], r["ELEMENT"]) not in counted
                and r["CREDITS"] + r["MATERIALIZATION_CREDITS"] > 0)
        ]
        kept.sort(key=lambda r: -(r["CREDITS"] + r["MATERIALIZATION_CREDITS"]))
        rows = kept[:a.limit] + [r for r in kept[a.limit:] if scheduled(r)]
        evidence_notes += [
            "Read utilization not measured: cost-table input has no served-read "
            "history, so scheduled elements are flagged for investigation, never "
            "for removal.",
            "Credits are as supplied in the cost table; coverage is relative to "
            "that table, not all Sigma spend.",
            "No scan, spill, or pruning evidence: expensive elements show as Monitor "
            "or Add rather than Optimize query/model.",
        ]
    elif a.from_rows:
        evidence = "rows"
        with open(a.from_rows) as f:
            supplied = json.load(f)
        if isinstance(supplied, dict) and "workload" in supplied:
            rows = supplied.get("workload", [])
            coverage_rows = supplied.get("coverage", [])
            materialization_rows = supplied.get("materializations", [])
            warehouse_rows = supplied.get("warehouses", [])
            duplicate_rows = supplied.get("duplicates", [])
            if not materialization_rows:
                evidence_notes.append("The bundle has no materialization rows, so "
                                      "refresh cost and served reads are unknown.")
            if "duplicates" not in supplied and not scan:
                evidence_notes.append("The bundle has no duplicate-detection rows "
                                      "(run sql/sigma_duplicates.sql or pass --scan).")
        else:
            rows = supplied.get("rows", supplied) if isinstance(supplied, dict) else supplied
            evidence_notes.append("A bare workload array was supplied: no coverage, "
                                  "materialization, or duplicate evidence.")
    else:
        print(f"querying Snowflake ACCOUNT_USAGE ({a.days}d, "
              f"{'org='+a.org if a.org else 'account-wide'}) ...", file=sys.stderr)
        end_sql, window_end, lag_h = attribution_end(a.conn, a.days)
        if window_end is None:
            warnings.append("Credit attribution watermark unavailable; the window ends "
                            "now, so the newest runs may show zero credits.")
        elif lag_h >= 1:
            evidence_notes.append(
                f"Cost data ends at {window_end[:16].replace('T', ' ')} UTC "
                f"({lag_h}h ago, Snowflake attribution lag); the {a.days}-day window "
                "ends there so recent runs are not counted at zero cost.")
        sql = sql.replace("{END}", end_sql)
        rows = run_sql(a.conn, sql)
        coverage_rows = run_sql(
            a.conn, COVERAGE_SQL.replace("{DAYS}", str(a.days)).replace("{END}", end_sql)
            .replace("{ORG_FILTER}", org_filter)
        )
        mat_sql = (MATERIALIZATION_SQL.replace("{DAYS}", str(a.days)).replace("{END}", end_sql)
                   .replace("{ORG_FILTER}", org_filter))
        try:
            materialization_rows = run_sql(a.conn, mat_sql)
        except Exception as e:
            warnings.append("Materialization utilization unavailable: " + str(e)[:240])
        dup_sql = (DUPLICATE_SQL.replace("{DAYS}", str(a.days)).replace("{END}", end_sql)
                   .replace("{ORG_FILTER}", org_filter)
                   .replace("{MAT_MIN_CREDITS}", str(a.mat_min_credits))
                   .replace("{MAT_MIN_RUNS}", str(a.mat_min_runs)))
        try:
            duplicate_rows = run_sql(a.conn, dup_sql)
        except Exception as e:
            warnings.append("Duplicate-work detection unavailable: " + str(e)[:240])
        if a.org:
            warnings.append(
                "Warehouse idle context is omitted for org scope because warehouse "
                "metering cannot be safely assigned to one Sigma org."
            )
        else:
            try:
                warehouse_rows = run_sql(
                    a.conn, WAREHOUSE_SQL.replace("{DAYS}", str(a.days)).replace("{END}", end_sql)
                )
            except Exception as e:
                warnings.append("Warehouse idle analysis unavailable: " + str(e)[:240])

    rows = [coerce_row(r) for r in rows]
    materialization_rows = [coerce_row(r) for r in materialization_rows]
    by_key = {(r.get("OBJECT"), r.get("ELEMENT")): r for r in rows}
    for mat in materialization_rows:
        key = (mat.get("OBJECT"), mat.get("ELEMENT"))
        target = by_key.get(key)
        if target is None:
            target = {
                "ORG": mat.get("ORG"),
                "OBJECT": mat.get("OBJECT"),
                "ELEMENT": mat.get("ELEMENT"),
                "SAMPLE_URL": mat.get("SAMPLE_URL"),
                "RUNS": mat.get("MATCHED_MATERIALIZED_READS", 0),
                "CREDITS": mat.get("MATCHED_READ_CREDITS", 0),
                "QAS_CREDITS": mat.get("MATCHED_READ_QAS_CREDITS", 0),
                "AVG_SEC": 0.0,
                "P50_SEC": 0.0,
                "P95_SEC": mat.get("MATCHED_READ_P95_SEC", 0),
                "MAX_SEC": 0.0,
                "TOTAL_SEC": 0.0,
                "MAX_BYTES": 0.0,
            }
            rows.append(target)
            by_key[key] = target
        target.update({
            k: v for k, v in mat.items()
            if k.startswith("MATERIALIZATION_") or k.startswith("MATCHED_")
            or k.startswith("UNMATCHED_") or k.startswith("WORKBOOK_UNMATCHED_")
            or k in {"HAS_CONTROLS", "CONTROL_TARGETS_RESOLVED",
                     "LINEAGE_COMPLETE"}
        })
        target["HAS_EXISTING_SCHEDULE"] = True

    if scan:
        apply_scan(rows, scan)

    for r in rows:
        rec = recommend(r, model_config, a.days, a.credit_price)
        r.update(rec)
        r["SCORE"] = round(score(r, model_config), 2)
        r["DOLLARS"] = round(
            (r.get("CREDITS", 0) + r.get("QAS_CREDITS", 0)
             + r.get("MATERIALIZATION_CREDITS", 0)
             + r.get("MATERIALIZATION_QAS_CREDITS", 0))
            * a.credit_price, 2
        )
        r["TOTAL_CREDITS"] = round(
            r.get("CREDITS", 0) + r.get("QAS_CREDITS", 0)
            + r.get("MATERIALIZATION_CREDITS", 0)
            + r.get("MATERIALIZATION_QAS_CREDITS", 0), 4
        )
        r["CURRENT_MONTHLY_COST"] = round(
            r["DOLLARS"] * 30.0 / max(a.days, 1), 2
        )
        r["PROJECTED_MONTHLY_COST"] = round(
            max(r["CURRENT_MONTHLY_COST"] - r["SAVINGS_EXPECTED"], 0), 2
        )
    rows.sort(key=lambda r: -r["SCORE"])

    groups = assemble_duplicates(duplicate_rows, a.days)
    if scan:
        groups = merge_groups(groups, _normalise_scan_groups(scan))
    duplicate_groups = finalize_groups(groups, rows, model_config, a.days, a.credit_price,
                                       scan_aliases(scan) if scan else None)
    if duplicate_groups and not any(_num(g["TOTAL_CREDITS"]) for g in duplicate_groups):
        evidence_notes.append("Duplicate groups carry no credit data, so consolidation "
                              "savings are not estimated; groups are ranked by refresh "
                              "frequency and member count.")

    coverage = {str(k).lower(): v for k, v in
                (coverage_rows[0].items() if coverage_rows else [])}
    candidate_credits = round(sum(
        r.get("CREDITS", 0) + r.get("QAS_CREDITS", 0)
        + r.get("MATERIALIZATION_CREDITS", 0)
        + r.get("MATERIALIZATION_QAS_CREDITS", 0)
        for r in rows
    ), 4)
    all_credits = (
        float(coverage.get("all_sigma_credits", candidate_credits) or 0)
        + float(coverage.get("all_qas_credits", 0) or 0)
    )
    all_queries = int(float(coverage.get(
        "all_sigma_queries", sum(r.get("RUNS", 0) for r in rows)
    ) or 0))
    attributable_credits = (
        float(coverage.get("attributable_credits", candidate_credits) or 0)
        + float(coverage.get("attributable_qas_credits", 0) or 0)
    )
    materialization_credits = (
        float(coverage.get("materialization_credits", 0) or 0)
        + float(coverage.get("materialization_qas_credits", 0) or 0)
    )
    mapped_credits = attributable_credits + materialization_credits
    warehouse = ({str(k).lower(): v for k, v in warehouse_rows[0].items()}
                 if warehouse_rows else {})
    action_counts = {}
    for r in rows:
        action_counts[r["ACTION"]] = action_counts.get(r["ACTION"], 0) + 1
    for r in rows:
        r.pop("_IGNORE", None)
    totals = {
        "queries": all_queries,
        "credits": round(all_credits, 4),
        "dollars": round(all_credits * a.credit_price, 2),
        "candidate_credits": candidate_credits,
        "attributable_credits": round(attributable_credits, 4),
        "credit_coverage_pct": round(
            mapped_credits / all_credits * 100, 2
        ) if all_credits else None,
        "objects": len({r.get("OBJECT") for r in rows if r.get("OBJECT")}),
        "orgs": len({r.get("ORG") for r in rows if r.get("ORG")}),
        "actions": action_counts,
        "estimated_monthly_savings": round(
            sum(r.get("SAVINGS_EXPECTED", 0) for r in rows), 2
        ),
        # overlaps the per-element estimates above, so it is never added into them
        "consolidation_savings_expected": round(
            sum(g.get("SAVINGS_EXPECTED", 0) for g in duplicate_groups), 2
        ),
    }
    inv = {
        "schema_version": 2,
        "generated": datetime.now(timezone.utc).isoformat(),
        "evidence": evidence,
        "evidence_notes": evidence_notes,
        "days": a.days,
        "window_end": window_end,
        "scope": a.org or "account-wide",
        "totals": totals,
        "coverage": {
            "all_sigma_queries": all_queries,
            "all_sigma_credits": round(all_credits, 4),
            "attributable_queries": int(float(
                coverage.get("attributable_queries", sum(r["RUNS"] for r in rows))
            ) or 0),
            "attributable_credits": round(attributable_credits, 4),
            "materialization_queries": int(float(
                coverage.get("materialization_queries", 0) or 0
            )),
            "materialization_credits": round(materialization_credits, 4),
            "unattributed_queries": int(float(
                coverage.get("unattributed_queries", 0) or 0
            )),
            "unattributed_credits": round(
                float(coverage.get("unattributed_credits", 0) or 0)
                + float(coverage.get("unattributed_qas_credits", 0) or 0), 4
            ),
        },
        "warehouse": warehouse,
        "warnings": warnings,
        "candidates": rows,
        "duplicate_groups": duplicate_groups,
        "config": {
            **model_config,
            "credit_price": a.credit_price,
            "credit_price_source": price_source,
        },
    }
    with open(os.path.join(a.out, "inventory.json"), "w") as f:
        json.dump(inv, f, indent=2)
    write_md(a.out, inv)
    print(f"\nReport: {os.path.join(a.out, 'REPORT.md')}", file=sys.stderr)


def write_md(outdir, inv):
    L, w = [], lambda s="": L.append(s)
    t = inv["totals"]
    cfg = inv.get("config", {})
    coverage = inv.get("coverage", {})
    warehouse = inv.get("warehouse", {})
    w("# Sigma Performance & Cost Opportunities\n")
    w(f"_Scope: **{inv['scope']}** · last {inv['days']} days · "
      f"{t['queries']:,} Sigma queries · {t['credits']:.2f} credits "
      f"(~${t.get('dollars',0):,.0f}) · generated {inv['generated'][:10]} · "
      f"**analysis is read-only**_\n")
    pct = t.get("credit_coverage_pct")
    w(f"Attribution coverage: **{pct if pct is not None else 'unknown'}%** of Sigma "
      f"query credits map to a source URL. Unattributed/system activity remains a separate "
      f"cost pool and is not silently dropped.\n")
    w(f"Evidence: **{inv.get('evidence', 'snowflake')}**")
    for note in inv.get("evidence_notes", []):
        w(f"- {note}")
    w("")
    w(f"Estimated monthly savings (per element): **${t.get('estimated_monthly_savings', 0):,.0f}**"
      + (f" · Consolidation savings (separate; overlaps the per-element figure): "
         f"**${t.get('consolidation_savings_expected', 0):,.0f}**"
         if inv.get("duplicate_groups") else "") + "\n")
    w("## Recommended decisions\n")
    w("| # | Object | Element | Action | Impact | Runs | Credits | p95 s | Refreshes | "
      "Matched reads | Confidence |")
    w("|---:|---|---|---|---|---:|---:|---:|---:|---:|---|")
    for i, r in enumerate(inv["candidates"][:25], 1):
        obj = (r.get("OBJECT") or "").replace("/workbook/", "wb: ").replace(
            "/data-model/", "dm: ").replace("/report/", "rpt: ")
        url = r.get("SAMPLE_URL")
        obj_cell = f"[{obj}]({url})" if url else obj
        w(f"| {i} | {obj_cell} | {r.get('ELEMENT','')} | {r['ACTION']} | "
          f"{r.get('IMPACT','')} | {r.get('RUNS',0)} | "
          f"{r.get('TOTAL_CREDITS',r.get('CREDITS',0)):.4f} | "
          f"{r.get('P95_SEC',0):.2f} | {r.get('MATERIALIZATION_RUNS',0)} | "
          f"{r.get('MATCHED_MATERIALIZED_READS',0)} | {r['CONFIDENCE']} |")
    w("")
    for r in inv["candidates"][:10]:
        w(f"### {r['ACTION']}: {r.get('OBJECT','')} / {r.get('ELEMENT','')}\n")
        w(f"- Impact: {r.get('IMPACT', '')}")
        w(f"- Why: {r['WHY']}")
        w(f"- Performance: {r['PERFORMANCE_IMPACT']}")
        w(f"- Validate: {r['VALIDATION']}")
        for n, step in enumerate(r.get("FIX_STEPS", []), 1):
            w(f"  {n}. {step}")
        w("")
    groups = inv.get("duplicate_groups", [])
    if groups:
        w("## Duplicate work\n")
        w("Elements that do the same work more than once. Consolidation savings overlap "
          "the per-element estimates above and are reported separately.\n")
        w("| Group | Match | Members | Redundant credits | Expected savings $/mo | "
          "Impact | Confidence |")
        w("|---|---|---:|---:|---:|---|---|")
        for g in groups[:25]:
            w(f"| {g['GROUP_ID']} | {g['MATCH']} | {g['MEMBER_COUNT']} | "
              f"{g.get('REDUNDANT_CREDITS', 0):.4f} | {g.get('SAVINGS_EXPECTED', 0):,.0f} | "
              f"{g.get('IMPACT', '')} | {g['CONFIDENCE']} |")
        w("")
        low = [g for g in groups if g.get("LOW_VALUE")]
        if low:
            w(f"_{len(low)} group(s) have no measurable repeat cost and are summarized only._\n")
        for g in [g for g in groups if not g.get("LOW_VALUE")][:10]:
            w(f"### {g['GROUP_ID']}: {g['MATCH']} ({g['MEMBER_COUNT']} elements)\n")
            for m in g["MEMBERS"]:
                label = f"{m.get('OBJECT','')} / {m.get('ELEMENT','')}"
                label = f"[{label}]({m['SAMPLE_URL']})" if m.get("SAMPLE_URL") else label
                w(f"- {label} — {m.get('CREDITS', 0):.4f} credits")
            if g.get("BASE_TABLES"):
                w(f"- Base tables: {', '.join(g['BASE_TABLES'])}")
            w(f"- Why: {g['WHY']}")
            w(f"- Validate: {g['VALIDATION']}")
            for n, step in enumerate(g.get("FIX_STEPS", []), 1):
                w(f"  {n}. {step}")
            w("")
    if warehouse:
        w("## Warehouse context\n")
        w(f"- Compute: {warehouse.get('warehouse_compute_credits','?')} credits")
        w(f"- Estimated idle: {warehouse.get('estimated_idle_credits','?')} credits "
          f"({warehouse.get('estimated_idle_pct','?')}%)")
        w("- Idle is a warehouse-level upper bound; it is not automatically attributed "
          "to Sigma or to any one workbook.\n")
    w("## Method & caveats\n")
    w("- Query cost comes from `QUERY_ATTRIBUTION_HISTORY`; materialization utilization "
      "matches `ACCESS_HISTORY.objects_modified` to `direct_objects_accessed`.")
    w("- A removal recommendation requires a measured no-materialization baseline. "
      "Refresh cost alone is never treated as proof that removal is safe.")
    w("- Served reads count every non-materialization query kind (adhoc, export, "
      "screenshot, ...) that reads a materialized table.")
    w("- Duplicate work is matched by identical materialization builds, identical "
      "source-table sets, identical live queries, and (with `--scan`) Sigma metadata; "
      "consolidation savings are estimates and are never added to per-element savings.")
    w("- Controls can bypass materialization. Unresolved target bindings force manual review.")
    w("- `ACCOUNT_USAGE` is delayed and query attribution excludes idle, storage, transfer, "
      "cloud services, and most serverless costs.")
    if inv.get("warnings"):
        w("\n## Warnings\n")
        for warning in inv["warnings"]:
            w(f"- {warning}")
    with open(os.path.join(outdir, "REPORT.md"), "w") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    main()
