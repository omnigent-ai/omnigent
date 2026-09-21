# Open Code Review

The `Open Code Review` workflow runs alongside Polly. It posts inline findings
and a fresh summary for each review; low-severity findings go in the summary.
Repeated runs preserve existing threads and avoid posting overlapping inline
comments. It does not approve PRs or request changes.

## Triggers

- Comment `/ocr` on its own line. The commenter needs repository write access
  or an entry in the existing `REVIEW_ALLOWLIST` JSON-array repository variable.
  This also enables review of external/fork PRs.
- Comment `/ocr force` to rerun a completed review of the same commit.
- Actions → Open Code Review → Run workflow, using the default branch and a PR
  number. The optional `force` checkbox reruns an already-reviewed commit.
  Closed and draft PRs are skipped.

Reviews are opt-in only; PR events and pushes do not start OCR.
Use `/ocr` to review a new revision.
Only eligible requests enter the per-PR queue, so unrelated comments cannot
cancel or replace an active review.

Before calling the model, OCR checks for a completed review of the current head
SHA. A duplicate request posts at most one skip notice per SHA. Completion
receipts are uploaded only after a complete review of that exact commit, no
finding-filter failures, and successful publication. Only receipts from
successful runs of this workflow on the default branch count; generated
comment text cannot suppress a review. Receipts expire after 90 days (or the
repository's shorter retention limit). Deleting or expiring a receipt makes
the commit eligible for review again.
Failed, partial, skipped, or filter-failed reviews remain retryable with `/ocr`.
A force run still avoids duplicating overlapping inline findings, but always
produces a new summary.

## Configuration

The workflow reuses the `LLM_API_KEY` and `GATEWAY_BASE_URL` repository secrets.
The gateway URL uses the same Anthropic surface as Polly: append `/anthropic`
unless it already ends in `/anthropic`. Authentication uses a bearer token.
The model comes from `OMNIGENT_CI_REVIEW_ANTHROPIC_MODEL`, falling back to
`OMNIGENT_CI_ANTHROPIC_MODEL`. No model identifier or credential is committed.

The upstream action is pinned to a commit, the CLI to `1.12.0`, and automatic
CLI updates are disabled. It uses medium effort, concurrency two, a 15-minute
per-task timeout, and a 600,000-token stopping threshold. Final requests can
exceed that threshold. The job has a 30-minute timeout.

`rules.json` includes our Python and frontend tests, which OCR otherwise
excludes by default, while retaining its built-in language rules. OCR's other
file filters and size limits still apply; inspect the coverage artifact.

The workflow runs from trusted base/default-branch context. The upstream
action reads the PR head through Git objects and does not run PR-authored code
or install the PR's dependencies. External authors cannot trigger a
secret-bearing review without an authorized `/ocr` request.

Upstream artifact uploads are disabled. The workflow uploads separate copies
of the result JSON and stderr with gateway credentials, URL, and origin
redacted, including their JSON-escaped forms. Diagnostics expire after seven
days. Detecting the gateway key fails the job and prevents recording completion;
only sanitized copies are uploaded. This check protects diagnostic artifacts;
the upstream action publishes PR comments before it runs.

## Verify after merging

1. Run `gh workflow run open-code-review.yml --repo omnigent-ai/omnigent -f pr=7878`
   for an open, non-draft PR, or comment `/ocr` on one.
2. Use a PR with changed Python or frontend tests. Open the Actions run and
   confirm the action checks out the trusted default branch and loads
   `.github/open-code-review/rules.json` without an unreadable-rule error.
   Download `ocr-review-result-<run-id>-<attempt>` and check that the manifest's
   `input.resolved_head` matches the requested SHA and coverage includes those
   test files.
3. Confirm the PR receives a summary and any inline findings. Verify the action
   produces `comments_failed=0` and a nonempty `summary_comment_url`, the result
   status is `complete`, and `ocr-completed-<pr>-<sha>` contains the matching
   PR, head, and summary URL. This receipt step is gated on those outputs.
   Rerun `/ocr`: the model step should be skipped and one skip notice should
   link to the prior successful run. Repeat `/ocr` to confirm it does not post
   another skip notice.
4. Comment `/ocr force`: confirm a new review and summary, without duplicating
   overlapping inline comments. Push a commit and confirm plain `/ocr` reviews it.
5. Inspect the downloaded diagnostic copies for redaction. Never put real
   secrets in PR content to test this; the local tests use synthetic credentials.
6. For a run with a finding-filter error (`Review filter: failed` or
   `Review filter failed` in stderr), confirm a warning and a note appear in
   the Actions summary. There must be no completion receipt for that run,
   and plain `/ocr` must retry it. Validate any retained findings manually.
   Incomplete results must fail the completeness step; publication failures
   must also prevent a receipt. If these failures do not occur during the
   smoke test, record those live scenarios as unverified; local fixture tests
   cover them without deliberately disrupting the shared gateway.

Adding this workflow in a branch does not activate the default-branch triggers.
No GitHub run or comment is needed to validate it locally:

```bash
actionlint .github/workflows/open-code-review.yml
python3 -m unittest discover -s tests/scripts -p test_open_code_review_workflow.py
```

With OCR CLI `1.12.0` installed, verify rule loading without calling the model:

```bash
OCR_NO_UPDATE=1 ocr review --from <base-sha> --to <head-sha> \
  --rule .github/open-code-review/rules.json --preview
```

Choose a diff containing Python or frontend tests and confirm they appear in
the preview's review list.
