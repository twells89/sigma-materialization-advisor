-- Sigma → Snowflake cost attribution (standalone / Cortex Code friendly).
-- Runs natively in a Snowflake worksheet or Cortex Code — no external CLI needed.
-- Attributes attributed compute credits to the Sigma object that issued each query.
--
-- Edit the three SET vars, run, then either read the result table or export it as JSON
-- and render with:  analyze.py --from-rows result.json --goal both
set days     = 30;     -- analysis window (days)
set org      = '';     -- '' = account-wide; else a Sigma org slug, e.g. 'acme'
set min_runs = 2;      -- ignore objects run fewer than this many times
-- The window ends where credit attribution ends (QUERY_ATTRIBUTION_HISTORY lags
-- QUERY_HISTORY by hours), so the newest runs are not counted at zero cost.
set end_ts = (select coalesce(max(start_time), current_timestamp())
              from snowflake.account_usage.query_attribution_history
              where start_time > dateadd('day', -$days, current_timestamp()));

with qh as (
  select query_id, query_parameterized_hash, total_elapsed_time, bytes_scanned,
         bytes_spilled_to_local_storage, bytes_spilled_to_remote_storage,
         queued_overload_time, partitions_scanned, partitions_total,
         iff(position('{' in query_tag) > 0,
             try_parse_json(substr(query_tag, position('{' in query_tag))), null) as j
  from snowflake.account_usage.query_history
  where start_time > dateadd('day', -$days, $end_ts) and start_time <= $end_ts
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
    and ($org = '' or j:"sourceUrl"::string ilike '%/' || $org || '/%')
),
att as (
  select query_id, sum(credits_attributed_compute) as credits,
         sum(credits_used_query_acceleration) as qas_credits
  from snowflake.account_usage.query_attribution_history
  where start_time > dateadd('day', -$days, $end_ts) and start_time <= $end_ts
  group by 1
)
select
  s.org,
  s.object,
  coalesce(s.element, '(workbook load)')      as element,
  count(*)                                    as runs,
  round(sum(coalesce(att.credits, 0)), 4)     as credits,
  round(sum(coalesce(att.qas_credits, 0)), 4) as qas_credits,
  round(avg(s.total_elapsed_time)/1000.0, 2)  as avg_sec,
  round(approx_percentile(s.total_elapsed_time/1000.0, .5), 2) as p50_sec,
  round(approx_percentile(s.total_elapsed_time/1000.0, .95), 2) as p95_sec,
  round(max(s.total_elapsed_time)/1000.0, 2)  as max_sec,
  round(sum(s.total_elapsed_time)/1000.0, 1)  as total_sec,
  sum(s.bytes_scanned)                        as bytes_scanned,
  max(s.bytes_scanned)                        as max_bytes,
  sum(coalesce(s.bytes_spilled_to_local_storage,0) +
      coalesce(s.bytes_spilled_to_remote_storage,0)) as bytes_spilled,
  round(sum(coalesce(s.queued_overload_time,0))/1000.0, 1) as queued_overload_sec,
  round(sum(s.partitions_scanned)/nullif(sum(s.partitions_total),0)*100, 2)
                                              as partition_scan_pct,
  count(distinct s.query_parameterized_hash)  as query_patterns,
  max(s.source_url)                           as sample_url
from s
left join att using (query_id)
group by 1, 2, 3
having count(*) >= $min_runs
order by credits desc, total_sec desc
limit 100;
