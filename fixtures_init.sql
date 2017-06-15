CREATE TABLE hive.default.error_aggregates (
  window row(start timestamp, "end" timestamp),
  channel varchar,
  version varchar,
  build_id varchar,
  application varchar,
  os_name varchar,
  os_version varchar,
  architecture varchar,
  country varchar,
  experiment_id varchar,
  experiment_branch varchar,
  e10s_enabled boolean,
  e10s_cohort varchar,
  gfx_compositor varchar,
  usage_hours double,
  "count" bigint,
  main_crashes bigint,
  content_crashes bigint,
  gpu_crashes bigint,
  plugin_crashes bigint,
  gmplugin_crashes bigint,
  content_shutdown_crashes bigint,
  browser_shim_usage_blocked bigint,
  permissions_sql_corrupted bigint,
  defective_permissions_sql_removed bigint,
  slow_script_notice_count bigint,
  slow_script_page_count bigint,
  submission_date varchar
)
WITH (format = 'parquet');
