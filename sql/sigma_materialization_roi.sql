-- Observed materialization cost and utilization.
-- Requires access to SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY (Enterprise Edition).
-- A low match rate is evidence to investigate; it is not proof that removal is safe.
-- Served reads count every non-materialization query kind (adhoc, export, screenshot, ...).
set days = 30;
set org = '';
-- The window ends where credit attribution ends (QUERY_ATTRIBUTION_HISTORY lags
-- QUERY_HISTORY by hours), so the newest runs are not counted at zero cost.
set end_ts = (select coalesce(max(start_time), current_timestamp())
              from snowflake.account_usage.query_attribution_history
              where start_time > dateadd('day', -$days, current_timestamp()));

with tagged as (
  select query_id, total_elapsed_time/1000.0 sec,
         regexp_substr(j:"sourceUrl"::string, '/workbook/[^?]+') wb_path,
         j:"sourceUrl"::string source_url, j:"kind"::string kind,
         split_part(j:"sourceUrl"::string, '/', 4) org,
         regexp_substr(j:"sourceUrl"::string,
                       '/(workbook|report|data-model)/[^?]+') object,
         regexp_substr(j:"sourceUrl"::string, '[?&]:displayNodeId=([^&]+)',
                       1, 1, 'e', 1) element
  from (
    select query_id, total_elapsed_time,
           iff(position('{' in query_tag) > 0,
               try_parse_json(substr(query_tag, position('{' in query_tag))), null) j
    from snowflake.account_usage.query_history
    where start_time > dateadd('day', -$days, $end_ts) and start_time <= $end_ts
      and query_tag ilike 'Sigma %' and execution_status='SUCCESS'
  )
  where j:"sourceUrl"::string is not null
    and ($org = '' or j:"sourceUrl"::string ilike '%/' || $org || '/%')
),
att as (
  select query_id, sum(credits_attributed_compute) credits,
         sum(credits_used_query_acceleration) qas_credits
  from snowflake.account_usage.query_attribution_history
  where start_time > dateadd('day', -$days, $end_ts) and start_time <= $end_ts
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
