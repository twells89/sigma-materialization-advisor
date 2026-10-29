-- Duplicate Sigma work: elements that build the same table, read the same source
-- tables, or run the same live query from several workbooks.
-- Requires SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY (Enterprise Edition) for
-- same-base-tables; the other two matches only need QUERY_HISTORY.
--
-- Edit the SET vars, run, then add the result to a --from-rows bundle under the key
-- "duplicates" (or run analyze.py directly against Snowflake).
--
-- Returns one row per (group, member). MATCH is one of:
--   identical-build       materialization CTAS with the same SELECT text
--   same-base-tables      materialized elements reading the same set of source tables
--                         (analyze.py drops a group whose members all share one build)
--   identical-live-query  same parameterized live query issued from 2+ workbooks
set days = 30;
set org = '';                -- '' = account-wide; else a Sigma org slug
set mat_min_credits = 0.5;   -- live-query groups need >= this/10 credits ...
set mat_min_runs = 12;       -- ... or at least this many runs
-- The window ends where credit attribution ends (QUERY_ATTRIBUTION_HISTORY lags
-- QUERY_HISTORY by hours), so the newest runs are not counted at zero cost.
set end_ts = (select coalesce(max(start_time), current_timestamp())
              from snowflake.account_usage.query_attribution_history
              where start_time > dateadd('day', -$days, current_timestamp()));

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
    where start_time > dateadd('day', -$days, $end_ts) and start_time <= $end_ts
      and query_tag ilike 'Sigma %' and execution_status = 'SUCCESS'
  )
  where j:"sourceUrl"::string is not null
    and ($org = '' or j:"sourceUrl"::string ilike '%/' || $org || '/%')
),
att as (
  select query_id, sum(coalesce(credits_attributed_compute,0)
                       + coalesce(credits_used_query_acceleration,0)) credits
  from snowflake.account_usage.query_attribution_history
  where start_time > dateadd('day', -$days, $end_ts) and start_time <= $end_ts
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
     and (sum(credits) >= $mat_min_credits / 10 or sum(runs) >= $mat_min_runs)
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
