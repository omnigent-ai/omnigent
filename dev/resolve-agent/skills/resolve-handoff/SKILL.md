---
name: resolve-handoff
description: Emit the complete Resolve handoff with exact modes, outcomes, test evidence, and publication state.
---

## Output — the resolution handoff

The **last thing in your final message** must be exactly one fenced ```json code
block — the machine-readable handoff, parsed by taking the last ```json fence in
the message. Same discipline as repro-agent:

- Write whatever prose summary you like above it, but the ```json block is the
  **last chunk** of the message, with nothing after its closing fence. Do not
  split the handoff across multiple sections or emit a second data block.
- **One exception (author path):** you also emit an *interim* handoff right after
  opening the PR (Step 3.5) so the workflow can post the PR link to Linear before
  Step 4 finishes. That is fine — the caller reads the **last** valid handoff in
  the session, so this final one supersedes the interim block. The interim block
  carries `pr_url` + a provisional `outcome`; this final block is authoritative.
- Emit it as **JSON**, never YAML. Include **every** key below, always, even when
  a value is empty (`""`, `[]`).
- `mode` must be exactly `"reviewed_existing_pr"`, `"authored_fix"`, or
  `"review_remediation"` — which entry path you took.
- `outcome` must be **exactly one** of the string literals `"fixed"`,
  `"partially_fixed"`, `"not_fixed"`, `"nothing_to_fix"`, `"needs_more_info"` —
  lowercase, no other wording. This is the field the caller reads, so it must
  match verbatim.

```json
{
  "bug_url": "https://github.com/omnigent-ai/omnigent/issues/1234",
  "mode": "authored_fix",
  "outcome": "fixed",
  "problem_summary": "People see internal catalog IDs in the model picker instead of readable model names.",
  "solution_summary": "The model picker now shows a friendly name for every model.",
  "root_cause": "picker rendered raw catalog IDs because format_label() was never called on the option list",
  "fix_summary": "call format_label() when building picker options in web/src/model/picker.tsx",
  "review_body": "",
  "files_changed": ["web/src/model/picker.tsx"],
  "facets": [
    {"symptom": "picker display", "outcome": "fixed", "test_transition": "test_1234 failed: raw IDs shown → passes: friendly labels"},
    {"symptom": "catalog default", "outcome": "nothing_to_fix", "test_transition": "already_fixed in #3448; skipped"}
  ],
  "tests": {
    "e2e": "",
    "added": ["tests/web/model/test_picker_label.py::test_display_label"]
  },
  "recordings": [
    {"surface": "web", "kind": "before", "path": "recordings/1234/before-picker.webm", "format": "webm",
     "capture_mode": "playwright_ui",
     "caption": "open the model picker → select the catalog → picker shows raw IDs"},
    {"surface": "web", "kind": "after", "path": "recordings/1234/after-picker.webm", "format": "webm",
     "capture_mode": "playwright_ui",
     "caption": "open the model picker → select the catalog → picker now shows friendly names"}
  ],
  "recording_unavailable_reason": "",
  "test_audit": "Reused the display-label regression; the same assertions fail on base and pass on head. Browser reproduction evidence is staged in .omnigent/repro-evidence/; workflow upload is pending at <workflow run URL>, artifact resolve-bundle-<run-id>, path repro-evidence/. No additional browser boundary was found.",
  "impact_assessment": {
    "base_sha": "<full target-branch tip SHA>",
    "head_sha": "<full candidate HEAD SHA>",
    "worktree_state": "clean",
    "risks": [
      {
        "files": ["web/src/model/picker.tsx"],
        "behavior": "Changing labels can alter model selection or restoration",
        "consumers": ["model picker", "restored sessions"],
        "invariant": "Selections and restored sessions retain the same model IDs",
        "check": "<exact command exercising selection and session restoration>",
        "result": "passed",
        "evidence": "<retained output reference, tested build and environment>"
      }
    ],
    "uncovered_boundaries": [],
    "not_applicable_reason": ""
  },
  "remaining_work": [],
  "hermetic_check": "test_picker_label re-run with ambient env vars set — still passes",
  "pr_url": "https://github.com/omnigent-ai/omnigent/pull/4200",
  "reviewed_pr_url": "",
  "pushed_branch": "",
  "ci_status": "green (all required checks pass)",
  "polly_review": "clean on <full candidate HEAD SHA>: fixed null-deref; fresh review has no findings",
  "ocr_review": "clean on <full candidate HEAD SHA>: no findings",
  "review_cycle": {
    "head_sha": "<full candidate HEAD SHA>",
    "fingerprint": "<fingerprint from the final review snapshot>",
    "dispositions": [
      {"key": "comment:<id>", "status": "addressed", "reason": "Finding 1: null-deref fixed in <commit>; <test> passes. Finding 2: suggested fallback already exists at <path:line> and is covered by <test>, so not needed."}
    ]
  },
  "ui_preview": "labeled ui-preview on every PR; preview at https://…; posted connect instructions",
  "validation_surface": "server",
  "validation_prompt": "Reproduce and validate a bug fix. Steps: open the model picker in the catalog view… Before this fix, raw catalog IDs were shown. Confirm the fix by checking that friendly labels appear. Report whether each step now behaves correctly.",
  "maintainer_review": "requested review from @PattaraS (issue assignee)",
  "review_fingerprint": "",
  "handled_review_ids": [],
  "last_pushed_sha": "",
  "session_id": "dc59e331-..."
}
```

Field meanings:

For modes that drive an open PR, `fixed` also requires the Step 4.3 live gate
to pass. Interim and workflow-owned implementation handoffs do not certify PR
readiness; name pending publication/review steps in `remaining_work`.

- `bug_url` — the bug link, carried through from the recovered handoff.
- `mode` — `reviewed_existing_pr` (Step 2A: a candidate PR existed, you reviewed
  it and kept it as the fix) or `authored_fix` (Step 2B: you wrote the fix). Use
  `authored_fix` when you started from an existing PR but opened your own — for
  **either** reason: its approach wasn't viable (2A.5), or its approach was fine
  but it was an unpushable **fork PR that needed a fix** so you took it over (Step
  4 preamble). In both cases name the reviewed/forked PR in your prose and
  `reviewed_pr_url` so the two stay linked.
  Use `review_remediation` only when the input supplied `review_pr`; in that mode
  preserve the existing PR and branch and address its requested changes directly.
- `review_fingerprint` / `handled_review_ids` / `last_pushed_sha` — populated in
  review-remediation mode for scanner deduplication and workflow retry recovery;
  otherwise `""`, `[]`, and `""`.
- `outcome` — overall: `fixed` (every live facet resolved and proven — by your fix
  or by the reviewed PR — and the shared impact assessment has no unresolved
  required checks), `partially_fixed`, `not_fixed` (couldn't resolve, or the
  reviewed PR doesn't fix it), `nothing_to_fix` (recovered verdict was
  `already_fixed`/`not_reproduced`, or the 2B.1 audit showed `main` has since
  fixed it — name the fixing commit and recommend closing the ticket), or
  `needs_more_info` (couldn't recover a reliable reproduction, evidence is unsafe,
  required inputs/authorization are missing, or setup/environment blocks verification).
  When intended behavior is ambiguous, follow `resolve-investigate`: a supported
  proposal with an unresolved design choice is `partially_fixed`, with the choice
  in `remaining_work`. Do not describe it as a proven fix.
- `problem_summary` / `solution_summary` — the two user-facing paragraphs shown
  prominently in the Linear update under **What's the problem?** and **How is it
  fixed?** Write plain, natural English for someone who uses the product but has
  not read the code. `problem_summary` describes what the person experiences and
  why it matters. `solution_summary` describes the corrected behavior and result.
  Keep implementation symbols, filenames, commit/merge bookkeeping, test lists,
  and CI details out of both fields; those belong in the technical fields below.
  Include both fields even for review mode and no-change outcomes.
- `root_cause` / `fix_summary` / `files_changed` — the cause and the change.
  These are the technical details shown under **Additional notes** and used by
  publication/review fallbacks, so concrete symbols and filenames are welcome.
  In review mode, describe the reviewed PR's approach and leave `files_changed`
  empty (you changed nothing).
  Distinguish the observed symptom from the supported cause; include sources for
  historical intent, competing explanations checked, and any proposed policy
  change. State missing evidence plainly; never include credentials.
- `review_body` — the PR-facing review text from Step 2A. Fill it in for
  `reviewed_existing_pr`, including workflow-owned publication; use `""` in
  other modes. State the verdict and reason first, then separate the proof and
  any remaining action into short bullets. Do not paste `root_cause`,
  `fix_summary`, `ci_status`, or `polly_review` wholesale. The workflow publisher
  posts this field verbatim before adding the tested commit and marker, so it
  must stand alone as a useful review. Encode paragraph and bullet breaks as
  `\n` within the JSON string.
- `facets` — per-facet, mirroring the recovered breakdown: each with its own
  `outcome` and a `test_transition` (the fail→pass proof, or why it was skipped).
- `tests` — selected permanent regression coverage. `e2e` is the retained e2e
  path, or `""` when none is needed. `added` keeps its legacy name but lists the
  other selected checks, including reused unchanged tests or extensions to an
  existing module. Each entry must be a bare repository-relative path or test
  node ID that resolves on the committed candidate. Do not append labels such
  as `(reused unchanged)`, shell commands, or result summaries; put those in
  `test_audit`. Before handing off, check the file portion of every reference
  against the committed tree and use the same node IDs as the executed checks.
  Do not list artifact-only reproduction paths here. In comment-only review,
  keep the existing review procedure and do not commit temporary test source.
  Use `test_audit` for brief selection reasoning: reused/retained tests, why any
  new permanent e2e is necessary, and gaps. For reproduction-only evidence, give
  the run URL/artifact/path or verified persistent local path from 2B.4, plus
  its retention status; identify pending uploads and unresolved retention.
- `recordings` — your after-fix clips (`kind: "after"`) and any recovered
  before-clips, using `{surface, kind, path, format, capture_mode, caption}`.
  Follow the recording rules in Step 2B.5 on both author and review runs; in
  review mode, record the reviewed PR head. Keep recovered before-clips and
  captions unchanged. Each after-clip's caption lists the actions shown, ending
  with the corrected behavior. A missing before-clip is not a reason to skip
  the after-clip. Use `[]` only for internal/API-only results with no visible
  user interaction, or when recording is blocked as described above.
- `recording_unavailable_reason` — leave empty when every expected clip is
  present. Otherwise explain each missing clip:

  - For internal/API-only results, say there is no visible user interaction
    and put the written before/after evidence in the PR Demo section.
  - For a recording failure, name the missing tool or the environment problem.
    Text-only CLI output is not a reason to skip recording.
  - Do not substitute a video of test output or a made-up demonstration.
    Missing or rejected footage must not block the fix or PR.
- `test_audit` — required in both author and review modes for reproduction-driven
  runs. Record the shared repro audit: patch-scope concerns, whether the original
  test was accepted/repaired/rejected and why, exact before/after revisions and
  commands, relevant environment/feature gates, behavioral fail→pass evidence,
  and any blockers. Preserve original evidence when repairing a test. A restored
  artifact or a green candidate run alone is not an audit.
- `impact_assessment` — required in every mode. Use the shared impact assessment
  above. `base_sha` is the target branch tip used for the comparison; the full
  diff starts at its merge-base with `head_sha`. `worktree_state` records tested
  dirt and content hashes, not just the final clean status. Each `risks` entry
  maps changed files and affected consumers to a preserved invariant, check,
  result, and evidence. Include required checks that failed or could not run;
  explain their gaps in `uncovered_boundaries`. Use empty SHAs/state only when
  stopping before a candidate can be identified, with `not_applicable_reason`.
  Otherwise leave that reason empty unless the inspected diff has no behavioral
  impact. This narrative does not certify execution or replace `test_audit`.
- `remaining_work` — a list of specific unresolved behavior, required checks, or
  delivery steps for `partially_fixed`; empty when none remain. Resolved original
  facets do not hide a regression or an uncovered required boundary.
- `hermetic_check` — the result of the Step 2B.5 hostile-env re-run when the diff
  touched env-derived defaults: which added/edited tests you re-ran with ambient
  vars set and that they still passed. Empty string when not applicable (no such
  test in the diff).
- `pr_url` — the PR you **opened** (author mode), including a draft proposal
  identified as such in `fix_summary`. Empty in review
  mode, when `skip_push` was set, or if you stopped before opening one.
- `reviewed_pr_url` — the existing PR you **reviewed** (review mode), or the fork
  PR you **took over** into your own (fork takeover — `pr_url` is then yours).
  Empty when you authored from scratch with no upstream PR.
- `pushed_branch` — the local branch holding the committed fix that you did
  **not** push because `skip_push` was set (author mode). Empty otherwise. A human
  pushes and opens the PR from it.
- `ci_status` — the result of the Step 4.2 CI loop (run on **both** paths now):
  `green` when the checks you're responsible for pass, otherwise the failing checks
  and whether each was diff-caused vs pre-existing/flaky/infra. If a fork PR needed
  a fix and you took over into your own PR, this reflects **your** PR's checks.
  Empty when `skip_push` was set or you stopped before there was a PR to land.
- `polly_review` / `ocr_review` — each reviewer's current-head result, rounds,
  run/comment links, fixes, and individually justified invalid/not-needed findings
  (including non-blocking notes). Missing, stale, skipped, failed, or undispatched
  reviews are explicitly incomplete and block readiness/approval. Empty when the
  publication mode skips Step 4; that does not mean the PR is ready to merge.
- `review_cycle` — the Step 4.3 snapshot receipt: `head_sha`, `fingerprint`, and
  `dispositions`. Include one record per feedback `key` from the snapshot, with
  `status` (`addressed`, `invalid`, or `not_needed`) and a nonempty `reason`
  explaining every finding in that document with commit/test/code evidence.
  Mixed dispositions in one summary belong individually in its `reason`; use
  `addressed` for the document when all its findings are settled. Revalidate
  after new feedback or a push. The live checker verifies coverage and freshness,
  not whether the reasoning is correct. Use `{}` when Step 4 has not run (interim,
  local-only, or workflow-owned author publication); preserve partial receipts
  and name missing reviews/findings in `remaining_work` if the loop is blocked.
- `ui_preview` — the result of Step 4.1 (run on every PR, not just frontend fixes):
  the **preview URL** (verbatim, so the ticket write-back can surface it and a
  reviewer can `omnigent claude -p '<prompt>' --server <url>`), or why it failed to
  deploy (e.g. workspace secrets not configured, or the label couldn't be
  applied under your identity). Empty when no PR was opened.
- `validation_surface` — which side the fix runs on, from Step 4.1: `server` (the
  preview build carries it; `--server <preview>` validates it), `runner` (runs in
  the runner/host process, so only a local `gh pr checkout` + `--server ''` build
  validates it — the preview's local runner is unfixed), or `both` (spans both —
  treat like `runner`). Judge from `files_changed`; default `server`, use `both`
  when unsure. Tells the write-back which command to render.
- `validation_prompt` — the Step 4.4 paste-to-an-agent prompt that reproduces the
  journey and confirms the fix. Empty when no PR was opened, except for
  workflow-owned author publication: in that mode no PR exists during the agent
  session, but this field must retain the deferred prompt for the publisher and
  Linear write-back.
- `maintainer_review` — who you requested review from in Step 4.5 (the issue
  assignee(s)), or why you couldn't (no assignee / assignee is the author, and
  what you did instead). Empty when no PR was opened.
- `session_id` — the repro session you consumed, carried through so the chain is
  traceable.

Directly published author runs and review runs end the same way: the PR you're
landing (one you opened, or an existing in-repo PR you reviewed and kept) has a
preview, green CI, settled current-head Polly and OCR reviews, a live-validation
command, and the maintainer tagged (Step 4) — or a concrete blocker or actual
execution deadline has left an explicitly incomplete, resumable handoff. The difference is only how
a fix lands (push directly, or — for an unpushable fork PR that needs changes —
take over into your own PR carrying the contributor's commits), and that the direct author path opens a PR while the
review path adopts an existing one. Workflow-owned author publication ends after
the validated body, deferred validation prompt, and final handoff are prepared;
the publisher owns the post-publication loop. A direct draft proposal instead
ends with the incomplete handoff in `resolve-publish`; it makes no readiness or
independent-review claim. `skip_push` and `needs_more_info`
runs end earlier, with no PR to land. In every mode, **you do not merge.**
