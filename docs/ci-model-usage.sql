-- Rolling 60 complete UTC days. Bind workspace_id to the workspace being billed.
-- Costs are list-price allocations, not per-request charges. Cache mix can bias
-- the input + 5 * output weighting; keep the untagged and unmatched rows visible.
WITH reporting_window AS (
  SELECT cast(floor(unix_timestamp(current_timestamp()) / 86400) AS INT) AS end_day
), gateway AS (
  SELECT request_id, invocation_id, event_time, requester,
    coalesce(model_metadata.destination_metadata.model_serving_endpoint,
      regexp_replace(destination_name, '^system[.]ai[.]', '')) AS endpoint_name,
    request_tags, input_tokens, output_tokens, token_details, status_code
  FROM system.ai_gateway.usage
  WHERE workspace_id = :workspace_id
    AND event_time >= timestamp_seconds(((SELECT end_day FROM reporting_window) - 60) * 86400L)
    AND event_time < timestamp_seconds((SELECT end_day FROM reporting_window) * 86400L)
), gateway_ids AS (
  SELECT explode(array(request_id, invocation_id)) AS request_id FROM gateway
), entities AS (
  SELECT served_entity_id, max(endpoint_name) AS endpoint_name
  FROM system.serving.served_entities
  WHERE workspace_id = :workspace_id
  GROUP BY served_entity_id
), request_rows AS (
  SELECT floor(unix_timestamp(event_time) / 3600) AS usage_hour,
    requester, requester AS billing_principal, endpoint_name,
    coalesce(nullif(request_tags['pipeline'], ''), 'Untagged') AS pipeline,
    request_tags['repository'] AS repository,
    request_tags['run_id'] AS run_id, request_tags['run_attempt'] AS run_attempt,
    coalesce(input_tokens, 0) AS input_tokens,
    coalesce(output_tokens, 0) AS output_tokens,
    coalesce(token_details.cache_read_input_tokens, 0) AS cache_read_tokens,
    status_code >= 400 AS failed, 'AI Gateway' AS route
  FROM gateway
  UNION ALL
  SELECT floor(unix_timestamp(u.request_time) / 3600), u.requester, NULL, e.endpoint_name,
    'Untagged', NULL, NULL, NULL,
    coalesce(u.input_token_count, 0), coalesce(u.output_token_count, 0), NULL,
    u.status_code >= 400, 'Legacy serving'
  FROM system.serving.endpoint_usage u
  JOIN entities e USING (served_entity_id)
  WHERE u.workspace_id = :workspace_id
    AND u.request_time >= timestamp_seconds(((SELECT end_day FROM reporting_window) - 60) * 86400L)
    AND u.request_time < timestamp_seconds((SELECT end_day FROM reporting_window) * 86400L)
    AND NOT EXISTS (
      SELECT 1 FROM gateway_ids g
      WHERE u.databricks_request_id = g.request_id
    )
), requests AS (
  SELECT usage_hour, requester, billing_principal, endpoint_name, pipeline,
    repository, run_id, run_attempt, route,
    count(*) AS requests, count_if(failed) AS failed_requests,
    sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
    sum(cache_read_tokens) AS cache_read_tokens,
    sum(input_tokens + 5 * output_tokens) AS weight
  FROM request_rows
  GROUP BY ALL
), weighted AS (
  SELECT *, sum(weight) OVER (
    PARTITION BY usage_hour, billing_principal, endpoint_name
  ) AS total_weight,
    min(route) OVER (PARTITION BY usage_hour, endpoint_name)
      <> max(route) OVER (PARTITION BY usage_hour, endpoint_name) AS mixed_routes
  FROM requests
), billed AS (
  SELECT floor(unix_timestamp(u.usage_start_time) / 3600) AS usage_hour,
    u.identity_metadata.run_as AS billing_principal,
    u.usage_metadata.endpoint_name AS endpoint_name,
    sum(u.usage_quantity * p.pricing.effective_list.default) AS known_list_usd,
    count_if(p.pricing.effective_list.default IS NULL) AS unpriced_records
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND p.currency_code = 'USD'
    AND u.usage_start_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_start_time < p.price_end_time)
  WHERE u.workspace_id = :workspace_id
    AND u.usage_date >= date_add(DATE '1970-01-01', (SELECT end_day FROM reporting_window) - 60)
    AND u.usage_date < date_add(DATE '1970-01-01', (SELECT end_day FROM reporting_window))
    AND u.billing_origin_product = 'MODEL_SERVING' AND u.usage_type = 'TOKEN'
  GROUP BY ALL
), allocated AS (
  SELECT w.*,
    b.known_list_usd * w.weight / nullif(w.total_weight, 0) AS known_list_usd,
    b.unpriced_records > 0 AS incomplete_prices,
    IF(b.unpriced_records = 0,
      b.known_list_usd * w.weight / nullif(w.total_weight, 0), NULL) AS estimated_list_usd
  FROM weighted w
  LEFT JOIN billed b ON w.usage_hour = b.usage_hour
    AND w.billing_principal <=> b.billing_principal
    AND w.endpoint_name <=> b.endpoint_name
    AND NOT w.mixed_routes
  UNION ALL
  SELECT b.usage_hour, b.billing_principal, b.billing_principal, b.endpoint_name,
    'Unallocated billing', NULL, NULL, NULL, 'Billing only',
    0, 0, 0, 0, NULL, 0, 0, false, b.known_list_usd, b.unpriced_records > 0,
    IF(b.unpriced_records = 0, b.known_list_usd, NULL)
  FROM billed b
  WHERE NOT EXISTS (
    SELECT 1 FROM weighted w WHERE w.usage_hour = b.usage_hour
      AND w.billing_principal <=> b.billing_principal
      AND w.endpoint_name <=> b.endpoint_name AND w.total_weight > 0 AND NOT w.mixed_routes
  )
)
SELECT date_add(DATE '1970-01-01', cast(floor(usage_hour / 24) AS INT)) AS usage_date,
  requester AS service_principal,
  pipeline, repository, endpoint_name, route,
  sum(requests) AS requests, sum(failed_requests) AS failed_requests,
  count(DISTINCT IF(run_id IS NOT NULL, named_struct('repo', repository, 'run', run_id), NULL)) AS workflow_runs,
  count(DISTINCT IF(run_id IS NOT NULL, named_struct('repo', repository, 'run', run_id, 'attempt', run_attempt), NULL)) AS workflow_attempts,
  sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
  sum(cache_read_tokens) AS cache_read_tokens,
  sum(IF(estimated_list_usd IS NULL, requests, 0)) AS requests_without_allocated_cost,
  bool_or(mixed_routes) AS mixed_routes,
  bool_or(incomplete_prices) AS incomplete_prices,
  sum(known_list_usd) AS known_list_usd,
  IF(count_if(estimated_list_usd IS NULL) = 0,
    sum(estimated_list_usd), NULL) AS estimated_list_usd
FROM allocated
GROUP BY ALL
ORDER BY usage_date, service_principal, pipeline
