# Otto closure feedback

When a human closes an unmerged Otto PR, the existing closure/reopen notice
asks the closing maintainer for a one-line reason if the PR discussion does
not already contain an explanation. Categories are optional text, not GitHub
labels: wrong root cause, design conflict, wrong mechanism, missed surface,
not needed, or other. The request tags the actor on the latest matching GitHub
closure event, never the person rerunning the workflow.

The handler recognizes the same Resolve app author logins as the replacement
PR automation. It paginates PR comments, submitted reviews and inline review
comments. Free-form feedback or a link from the closer or another maintainer
counts as an explanation; empty comments, simple acknowledgements/approvals,
commands, quoted text and hidden metadata do not. Explicit automated closure
explanations also suppress a request. The rule intentionally errs toward
accepting existing maintainer discussion: it does not use a model to decide
whether a prior substantive comment is the final closure reason. An explanation
stored only in another system is not fetched; link it from the PR.

The request is appended to the existing `<!-- reopen-notice -->` comment.
The workflow serializes notices per PR and reads that marker on redelivery,
including after a reopen/reclose. Existing notices are not edited or backfilled.
Comment POSTs disable automatic retries: if GitHub accepts a comment but its
response fails, a workflow rerun finds the marker instead of posting again.
Read errors abort without posting. The PR must still be closed, unmerged, and
on the same closure event after the discussion reads.

This change does not collect or classify responses, write to Linear or Slack,
or create another Health ingestion path. It does not change rollout settings.
Deployment is the usual merge to the default branch; `pull_request_target`
checks out that trusted branch's `.github` directory, never PR code. Bot closes,
merged PRs and community PR notices retain their existing behavior.

## Announcement draft — not posted

> When closing an Otto PR, please leave a one-line reason so we can learn from
> it. Free text is welcome; optional categories are wrong root cause, design
> conflict, wrong mechanism, missed surface, not needed, or other. A link to an
> existing explanation is enough. Otto's closure notice will ask once if it
> cannot find feedback in the PR discussion. No need to revisit old PRs.

## Verification

Fixture tests need Node.js 24 and no credentials:

```sh
node --test .github/workflows/reopen-notice.test.js .github/workflows/reopen-pr.test.js
```

The process tests run the production `actions/github-script` bundle, including
its Octokit pagination and retry handling, in a child process with a serialized
webhook fixture. All API calls use a loopback HTTP server and a fake token; the
child receives no credentials from the parent environment. Download the pinned
bundle once, then run the tests:

```sh
mkdir -p /tmp/otto-github-script
curl --fail --location \
  https://raw.githubusercontent.com/actions/github-script/3a2844b7e9c422d3c10d287c895573f7108da1b3/dist/index.js \
  -o /tmp/otto-github-script/index.js
GITHUB_SCRIPT_BUNDLE=/tmp/otto-github-script/index.js \
  node --test .github/workflows/reopen-notice.process.test.js
```

CI checks out the same pinned action and runs both suites on Ubuntu/Node 24.
The process suite covers evidence and markers on later pages, existing reasons
on all three discussion surfaces, repeated deliveries, failed reads, ambiguous
POST responses and a PR reopened while its reviews are being read. These are
real action-process tests with a mocked GitHub service, not a live webhook,
GitHub permission check, model canary or OS sandbox test. No model, Omnigent
runtime, Docker or bwrap is required by this Actions handler.

For a future authorized live check, use a disposable Otto PR: close it without
feedback and inspect the single closer-tagged request; rerun the notice and
confirm no duplicate. On a second disposable PR, leave a reason before closing
and confirm the reopen notice contains no request. This check sends real GitHub
notifications and is separate from the offline verification above.
