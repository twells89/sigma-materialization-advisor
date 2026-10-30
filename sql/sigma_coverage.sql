-- Full Sigma-tagged query-cost coverage. Run alongside sigma_cost.sql so the report
-- distinguishes all Sigma cost from the subset attributable to a workbook/data model.
set days = 30;
set org = '';
-- The window ends where credit attribution ends (QUERY_ATTRIBUTION_HISTORY lags
-- QUERY_HISTORY by hours), so the newest runs are not counted at zero cost.
set end_ts = (select coalesce(max(start_time), current_timestamp())
              from snowflake.account_usage.query_attribution_history
              where start_time > dateadd('day', -$days, current_timestamp()));

with parsed as (
  select query_id,
         iff(position('{' in query_tag) > 0,
             try_parse_json(substr(query_tag, position('{' in query_tag))), null) as j
  from snowflake.account_usage.query_history
  where start_time > dateadd('day', -$days, $end_ts) and start_time <= $end_ts
    and query_tag ilike 'Sigma %'
    and execution_status = 'SUCCESS'
),
qh as (
  select * from parsed
  where $org = '' or j:"sourceUrl"::string ilike '%/' || $org || '/%'
),
att as (
  select query_id, sum(credits_attributed_compute) credits,
         sum(credits_used_query_acceleration) qas_credits
  from snowflake.account_usage.query_attribution_history
  where start_time > dateadd('day', -$days, $end_ts) and start_time <= $end_ts
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
