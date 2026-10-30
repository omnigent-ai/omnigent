# CI model usage attribution

Polly and Open Code Review attach a stable pipeline name and GitHub run identity
to model requests. The AI Gateway records them in
`system.ai_gateway.usage.request_tags`, allowing usage to be grouped by workflow
without assigning a separate service principal to every workflow.

## Enable attribution in another CI job

Set `OMNIGENT_CI_PIPELINE` to a stable identifier such as `resolve` in the job that
runs Omnigent. Repository, workflow file, run ID, attempt, and job come from the
GitHub environment. Optional context is supplied through:

- `OMNIGENT_CI_PR_NUMBER` and `OMNIGENT_CI_REVIEWED_SHA`.
- `OMNIGENT_CI_SOURCE_SESSION_ID`, when the originating session is known. Do not
  substitute an individual reviewer or worker's session ID.

The shared gateway helper merges these tags with existing request tags. Native
Codex, Claude, and Pi configuration and the OpenAI Agents SDK use this helper.
Filtered agent subprocesses inherit only the known attribution fields through
`OMNIGENT_CI_CONTEXT`; credentials are not part of that context. In remote
deployments, pass the context to the process that actually makes model calls.
Other repositories need a version of Omnigent containing this support and must
opt their jobs in. Raw HTTP clients need the request header explicitly.

Polly and OCR use the AI Gateway Anthropic route instead of the legacy serving
route. OCR receives the same tags through its `llm_extra_headers` action input,
quoted to preserve JSON commas. Automatic reviews continue using trusted base
code; OCR's default-branch dispatch requirement remains in effect.

## Verify before merging

Polly's workflow already exists on the default branch and supports dispatching
the selected branch. After pushing the candidate branch, run:

```sh
gh workflow run polly-review.yml --ref YOUR_BRANCH -f pr=YOUR_PR -F usage_probe=true
```

This job sends two short, non-agent requests using the configured Anthropic and
OpenAI model variables. It has read-only repository permissions, does not post a
review, and prints response/request IDs, tags, and token usage without credentials.
It validates the gateway header and route, including OCR's header encoding; it
does not replace running the OCR action or testing Polly's child agents.

To exercise Polly's real parent and workers from the selected branch:

```sh
gh workflow run polly-review.yml --ref YOUR_BRANCH -f pr=YOUR_PR -F force=true
```

That command runs a full review and may post findings. Automatic reviews of this
PR still execute base-branch code and cannot validate this branch's implementation.
Run OCR from the default branch after merge to verify its full action.

Query the OSS workspace using its explicitly selected CLI profile or SQL editor:

```sql
SELECT event_time, request_id, requester, destination_name, user_agent,
       request_tags, input_tokens, output_tokens, token_details
FROM system.ai_gateway.usage
WHERE workspace_id = :workspace_id
  AND event_time >= current_timestamp() - INTERVAL 2 DAYS
  AND request_tags['repository'] = :repository
  AND request_tags['run_id'] = :run_id
ORDER BY event_time;
```

Check that parent and child clients retain the same pipeline, repository, run ID,
and attempt. Rerunning the GitHub run should preserve its ID and increment the
attempt. Cross-check against the workflow's response IDs and diagnostics; tagged
rows alone do not prove that no untagged calls escaped. System-table ingestion is
asynchronous, so successful HTTP delivery precedes visibility here. Billing may
arrive later still; absence immediately after a run is inconclusive.

## Daily dashboard dataset

Use [ci-model-usage.sql](ci-model-usage.sql) with a `workspace_id` parameter. It
reads a rolling 60 complete days, includes legacy traffic and unmatched billing,
and returns daily pipeline/model requests, tokens, workflow runs, attempts, and
estimated USD list cost. It is a dataset query to add to an existing dashboard;
the PR does not publish or schedule dashboard changes.

Plot `usage_date` against `estimated_list_usd`, colored by `pipeline`, with filters
for `service_principal`, repository, model endpoint, and date. Add requests and
workflow-attempt charts alongside cost. Runs counted per day/model are not
additive across models or dates; a run spanning midnight appears on both days.

The cost allocation divides each billed model/hour/principal amount by request
token weight (`input + 5 × output`), matching the existing dashboard's output
weight convention. It includes signed billing corrections and effective USD list
prices. This remains an estimate: cache reads/writes and model-specific rates
can change relative request costs. Cache tokens are already part of total input
and must not be added again. Missing billing remains NULL; zero-token or unmatched
billing is preserved as `Unallocated billing`. Keep that category and `Untagged`
visible when assessing attribution coverage.

Request tags do not backfill historical usage or become invoice line items.
Cache-aware model pricing and reconciliation are needed for more accurate
per-run costs; this query must not be presented as exact pipeline charges.
