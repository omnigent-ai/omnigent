### Review-remediation mode

When `review_pr` is present, this section takes precedence over the reproduction
and candidate-PR discovery instructions below.

1. Confirm the working checkout is the named PR's current head and remains on its
   existing head repository and branch. Never create a replacement PR, rebase,
   reset, rewrite history, force-push, or push to the base branch.
2. Read the PR body, diff, all reviews, inline comments, unresolved threads,
   current checks, Polly and OCR feedback, and recent failed job logs. Treat the
   latest decisive review from each human reviewer as authoritative.
3. Address every current substantive `CHANGES_REQUESTED` item from trusted human
   reviewers. Use `resolve-investigate` for ambiguous intent or product judgment
   and put a supported proposal on this PR. If authoritative requests conflict,
   required inputs are missing, or a request is unsafe or outside authorization,
   explain the blocker and continue independent authorized work. Do not dismiss
   or silently override human reviews.
4. Complete the shared impact assessment for the full PR, including your
   remediation edits, and run its focused checks. Commit as
   `omni-resolve-agent[bot]`, and push directly to the existing PR branch using
   the workflow-provided push command. For a
   contributor fork, use only that fixed-target command; never inspect or export
   its credential source.
5. Continue through Step 4.2 and Step 4.3 until current CI is green and both Polly
   and OCR have completed on the current head with no actionable findings.
   Diagnose PR-caused failures, rerun clearly transient failures, push
   conservative fixes, and rerun both reviewers after every push.
   Run the Step 4.3 live gate before a successful handoff.
6. Reply to addressed review threads with the commit and validation evidence,
   resolve a thread only when fully addressed, and request re-review from the
   humans whose change requests were handled. Never approve, dismiss a human
   review, or merge.
7. Skip reproduction handoff recovery, fail-before proof, recording generation,
   candidate-fix discovery, new-PR creation, UI-preview setup unless already
   required by the PR. The trusted workflow publishes idempotent remediation
   start and completion comments on both the GitHub PR and its attached Linear
   issue; preserve the attached issue in `bug_url` so those comments stay linked.
8. Finish with the normal handoff using `mode: "review_remediation"`. Set
   `bug_url` to an attached bug when one is clear, otherwise `""`; set
   `reviewed_pr_url` and `pr_url` to `review_pr`; include the review fingerprint,
   handled review ids, and final pushed head.
