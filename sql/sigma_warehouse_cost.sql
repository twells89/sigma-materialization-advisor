-- Warehouse-level context. Idle credits are an upper bound and must not be attributed
-- to Sigma or to a workbook without additional workload evidence.
set days = 30;
-- The window ends where credit attribution ends (QUERY_ATTRIBUTION_HISTORY lags
-- QUERY_HISTORY by hours), so the newest runs are not counted at zero cost.
set end_ts = (select coalesce(max(start_time), current_timestamp())
              from snowflake.account_usage.query_attribution_history
              where start_time > dateadd('day', -$days, current_timestamp()));

select round(sum(credits_used_compute),4) warehouse_compute_credits,
       round(sum(credits_attributed_compute_queries),4) attributed_query_credits,
       round(sum(credits_used_compute)-sum(credits_attributed_compute_queries),4)
         estimated_idle_credits,
       round((sum(credits_used_compute)-sum(credits_attributed_compute_queries)) /
             nullif(sum(credits_used_compute),0)*100,2) estimated_idle_pct,
       count(distinct warehouse_name) warehouses
from snowflake.account_usage.warehouse_metering_history
where start_time > dateadd('day', -$days, $end_ts) and start_time <= $end_ts;
