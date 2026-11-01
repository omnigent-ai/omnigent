# CI model usage attribution

Model-using CI workflows attach a stable pipeline name and GitHub run identity
to model requests. The AI Gateway records them in
`system.ai_gateway.usage.request_tags`, allowing usage to be grouped by workflow
without assigning a separate service principal to every workflow.

## Workflow coverage

[The maintained inventory](../.github/model-usage.json) classifies all 94 workflow
definitions: 13 model consumers, five mock test lanes, and 76 workflows without
model calls. Tests require new workflow files to be classified and check that
shared actions and reusable workflows keep their caller's pipeline identity.

`merge-ready-sweep` reads GitHub status metadata and dispatches `merge-ready` for
expiry or reevaluation. Both workflows perform deterministic GitHub checks
without model calls.

| Pipeline | Model calls |
|---|---|
| `polly-review` | Coordinator, Claude/Codex reviewers, and nested workers |
| `open-code-review` | OCR action |
| `auto-assign-reviewer` | Direct chat ranking request |
| `doc-sync` | Classifier and documentation drafter |
| `draft-release-notes` | Shared agent action |
| `feature-blog` | Scout, drafter, and image-generation chat request |
| `issue-triage` | Native agent or reusable triage classifier |
| `needs-info-response` | Reusable triage classifier |
| `publish-changelog` | Shared agent action |
| `security-triage` | Native agent |
| `vscode-release-pr` | Direct chat drafting request |
| `flake-stress-e2e` | Live test clients and policy calls |

`issue-prioritization-v2.yml` requires the calling workflow's pipeline as an input;
its callers remain distinct. The bounded probe uses `usage-attribution-probe`.
The mock workflows `e2e`, `e2e-ui`, `integration`, `server-compat`, and
`flake-stress-ui` also retain separate labels, but their mock calls generate no
Databricks usage. Dispatch-only workflows attribute inference to the workflow
they start. Deployed applications and scheduled Databricks jobs are separate
consumers and are not labeled with the workflow that deployed them.

This is source-level coverage, not evidence that every workflow has run against
the gateway. Workflows that select an older checkout or install Omnigent from
PyPI need a revision/release containing the runtime support. Historical flake
reproductions remain runnable; revisions predating attribution emit a warning
and their usage remains untagged. The corresponding
internal-repository rollout has its own inventory and release dependencies.

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

File-configured clients also need CI provider configuration: Qwen uses
`generationConfig.customHeaders`, Goose uses a declarative provider's `headers`,
Hermes uses provider `extra_headers`, and Kimi uses provider `custom_headers`.
Their `with_*_ci_request_tags` helpers preserve existing configuration and add
tags only to gateway entries. CI must call these helpers when preparing the
vendor files and select those files for both native and headless launches;
exporting context alone does not configure these standalone clients. Vendor
schema and local configuration tests do not replace live client validation.

### Runtime scope and failure behavior

The OpenAI SDK merges `default_headers` into its existing headers. Attribution
adds only `Databricks-Ai-Gateway-Request-Tags` to opted-in Databricks AI Gateway
clients; it does not replace authentication, content headers, retries, or the
HTTP transport. Disabled attribution and other destinations receive no new
headers. A transport test exercises the real SDK and checks the request URL,
JSON body, authentication, and default headers against an untagged client.

Malformed context, missing pipeline identity, or tags exceeding the 4 KiB CI
header budget produce a warning and skip attribution instead of blocking model
calls or child launches. CI setup should call `ci_request_tags(strict=True)` to
catch configuration errors early. Existing request tags that cannot be safely
merged are preserved unchanged. CI fields take precedence over same-named tags.

Attribution is explicit process configuration, including outside GitHub Actions.
Client headers capture it at construction: do not reuse one configured client
or daemon across unrelated pipeline contexts. Gateway-side policy rejection or
future SDK changes remain possible; check both workflow health and untagged usage
when rolling this out.

Native Anthropic clients use the AI Gateway Anthropic route instead of the legacy
serving route; chat clients use `mlflow/v1` and Responses clients use `codex/v1`.
OCR receives the same tags through its `llm_extra_headers` action input,
quoted to preserve JSON commas. Automatic reviews continue using trusted base
code; OCR's default-branch dispatch requirement remains in effect.

## Verify before merging

Polly's workflow already exists on the default branch and supports dispatching
the selected branch. After pushing the candidate branch, run:

```sh
gh workflow run polly-review.yml --ref YOUR_BRANCH -f pr=YOUR_PR -F usage_probe=true
```

This job sends three short, non-agent requests over Anthropic Messages, OpenAI
Responses, and MLflow Chat Completions using the configured model variables.
It has read-only repository permissions, does not post a
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
reads a rolling 60 complete UTC days, includes legacy traffic and unmatched billing,
and returns daily pipeline/model requests, tokens, workflow runs, attempts, and
estimated USD list cost. It is a dataset query to add to an existing dashboard;
the PR does not publish or schedule dashboard changes. Bounds and buckets use
epoch arithmetic, so the result is independent of the SQL session timezone.

Plot `usage_date` against `estimated_list_usd`, colored by `pipeline`, with filters
for `service_principal`, repository, model endpoint, and date. Add requests and
workflow-attempt charts alongside cost. Runs counted per day/model are not
additive across models or dates; a run spanning midnight appears on both days.

The cost allocation divides each billed model/hour/principal amount by request
token weight (`input + 5 × output`), matching the existing dashboard's output
weight convention. It includes signed billing corrections and effective USD list
prices. This remains an estimate: cache reads/writes and model-specific rates
can change relative request costs. Cache tokens are already part of total input
and must not be added again. `estimated_list_usd` is NULL for a daily group if any
hour lacks an allocation or contains unpriced billing. `known_list_usd` exposes
the priced subtotal, which may be incomplete; `requests_without_allocated_cost`
and `incomplete_prices` expose the gaps. Reconcile known subtotals against priced
billing, and inspect missing prices separately. Zero-token or unmatched billing
is preserved as `Unallocated billing`.

Legacy requesters are not assumed to equal billing `run_as`: legacy bills may
have no principal. Until route-to-billing provenance is established, an endpoint
hour containing both legacy and gateway requests has `mixed_routes = true` and
its bills remain `Unallocated billing`. This deliberately withholds ambiguous
pipeline estimates instead of charging gateway pipelines for potentially shared
legacy usage. Keep `Unallocated billing` and `Untagged` visible when assessing
attribution coverage. Null-principal legacy bills can be allocated among legacy
requesters in hours containing only legacy traffic.

Request tags do not backfill historical usage or become invoice line items.
Cache-aware model pricing and reconciliation are needed for more accurate
per-run costs; this query must not be presented as exact pipeline charges.

The warehouse regression runs the actual dataset against temporary session views
with a fixed clock. It checks UTC/Los Angeles/Kathmandu sessions, boundary dates,
deduplication, mixed routes, partial billing, missing prices, and signed corrections.
It reads no real usage data and creates no persistent objects. Run it explicitly:

```sh
CI_MODEL_USAGE_SQL_PROFILE=<profile> CI_MODEL_USAGE_SQL_WAREHOUSE=<warehouse-id> \
  uv run --no-project --python 3.12 --with pytest --with databricks-sdk \
  --with databricks-sql-connector python -m pytest \
  --confcutdir=tests/scripts -o addopts= tests/scripts/test_ci_model_usage_sql.py
```
