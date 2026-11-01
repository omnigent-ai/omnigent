CREATE OR REPLACE TEMPORARY VIEW fixture_gateway AS
SELECT request_id, concat('inv-', request_id) AS invocation_id, cast(at AS TIMESTAMP) AS event_time,
  'fixture' AS workspace_id, 'sp' AS requester, endpoint AS destination_name,
  named_struct('destination_metadata', named_struct('model_serving_endpoint', endpoint)) AS model_metadata,
  map('pipeline', pipeline, 'repository', 'example/repo', 'run_id', request_id, 'run_attempt', '1') AS request_tags,
  tokens AS input_tokens, 0 AS output_tokens,
  named_struct('cache_read_input_tokens', 0) AS token_details, 200 AS status_code
FROM VALUES
  ('p1', '2026-10-29T00:10:00Z', 'priced', 'polly', 1),
  ('d1', '2026-10-29T00:20:00Z', 'priced', 'docs', 3),
  ('p2', '2026-10-29T01:10:00Z', 'priced', 'polly', 1),
  ('p3', '2026-10-29T02:10:00Z', 'partial-price', 'polly', 1),
  ('p4', '2026-10-29T03:10:00Z', 'correction', 'polly', 1),
  ('p5', '2026-10-29T04:10:00Z', 'mixed', 'polly', 1),
  ('p6', '2026-10-29T05:10:00Z', 'zero', 'polly', 0),
  ('p7', '2026-10-29T23:50:00Z', 'last-hour', 'polly', 1),
  ('p8', '2026-08-31T00:00:00Z', 'first-hour', 'polly', 1),
  ('before', '2026-08-30T23:59:59Z', 'excluded', 'polly', 1),
  ('after', '2026-10-30T00:00:00Z', 'excluded', 'polly', 1)
AS fixture(request_id, at, endpoint, pipeline, tokens);

CREATE OR REPLACE TEMPORARY VIEW fixture_entities AS
SELECT 'fixture' AS workspace_id, endpoint AS served_entity_id, endpoint AS endpoint_name
FROM VALUES ('priced'), ('mixed'), ('legacy') AS fixture(endpoint);

CREATE OR REPLACE TEMPORARY VIEW fixture_legacy AS
SELECT 'fixture' AS workspace_id, request_id AS databricks_request_id,
  cast(at AS TIMESTAMP) AS request_time, 'sp' AS requester, endpoint AS served_entity_id,
  1 AS input_token_count, 0 AS output_token_count, 200 AS status_code
FROM VALUES
  ('p1', '2026-10-29T00:10:00Z', 'priced'),
  ('inv-d1', '2026-10-29T00:20:00Z', 'priced'),
  ('legacy-mixed', '2026-10-29T04:20:00Z', 'mixed'),
  ('legacy-only', '2026-10-29T06:20:00Z', 'legacy')
AS fixture(request_id, at, endpoint);

CREATE OR REPLACE TEMPORARY VIEW fixture_billing AS
SELECT 'fixture' AS workspace_id, cast(at AS TIMESTAMP) AS usage_start_time,
  cast(substring(at, 1, 10) AS DATE) AS usage_date,
  named_struct('run_as', principal) AS identity_metadata,
  named_struct('endpoint_name', endpoint) AS usage_metadata,
  quantity AS usage_quantity, sku AS sku_name, 'AWS' AS cloud, 'TOKEN' AS usage_unit,
  'MODEL_SERVING' AS billing_origin_product, 'TOKEN' AS usage_type
FROM VALUES
  ('2026-10-29T00:00:00Z', 'priced', 'sp', 'priced', 12),
  ('2026-10-29T00:00:00Z', 'priced', 'sp', 'priced', -4),
  ('2026-10-29T02:00:00Z', 'partial-price', 'sp', 'priced', 4),
  ('2026-10-29T02:00:00Z', 'partial-price', 'sp', 'unknown', 3),
  ('2026-10-29T03:00:00Z', 'correction', 'sp', 'priced', -2),
  ('2026-10-29T04:00:00Z', 'mixed', 'sp', 'priced', 5),
  ('2026-10-29T04:00:00Z', 'mixed', NULL, 'priced', 3),
  ('2026-10-29T05:00:00Z', 'zero', 'sp', 'priced', 7),
  ('2026-10-29T06:00:00Z', 'legacy', NULL, 'priced', 4),
  ('2026-10-29T07:00:00Z', 'billing-only', 'sp', 'priced', 6),
  ('2026-10-29T08:00:00Z', 'unpriced-only', 'sp', 'unknown', 9),
  ('2026-10-29T23:00:00Z', 'last-hour', 'sp', 'priced', 3),
  ('2026-08-31T00:00:00Z', 'first-hour', 'sp', 'priced', 2),
  ('2026-08-30T23:00:00Z', 'excluded', 'sp', 'priced', 99),
  ('2026-10-30T00:00:00Z', 'excluded', 'sp', 'priced', 99)
AS fixture(at, endpoint, principal, sku, quantity);

CREATE OR REPLACE TEMPORARY VIEW fixture_prices AS
SELECT 'priced' AS sku_name, 'AWS' AS cloud, 'TOKEN' AS usage_unit, 'USD' AS currency_code,
  TIMESTAMP '2026-01-01T00:00:00Z' AS price_start_time, cast(NULL AS TIMESTAMP) AS price_end_time,
  named_struct('effective_list', named_struct('default', 1.0)) AS pricing;
