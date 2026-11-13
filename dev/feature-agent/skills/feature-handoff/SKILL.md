---
name: feature-handoff
description: Emit the complete feature handoff with exact modes, outcomes, CUJs, demos, and publication state.
---

# The feature handoff

The **last thing in your final message** must be exactly one fenced ```json
block, with nothing after its closing fence. The caller reads the last valid
block, so an interim handoff right after opening the PR (with `pr_url` and a
provisional `outcome`) is fine; the final block supersedes it. Emit JSON, never
YAML, and include **every** key below, even when empty (`""`, `[]`).

- `mode` is exactly `"authored_feature"` or `"review_remediation"`.
- `outcome` is exactly one of `"implemented"`, `"partially_implemented"`,
  `"not_implemented"`, `"nothing_to_build"`, `"needs_split"`, or
  `"needs_more_info"`.

```json
{
  "feature_url": "https://linear.app/omnigent/issue/OMNI-1234",
  "mode": "authored_feature",
  "outcome": "implemented",
  "feature_summary": "People can now rename a session from the session list instead of opening it first.",
  "ticket_comment": "",
  "proposed_children": [],
  "requirements": [
    "A session can be renamed from the session list",
    "The new name appears everywhere the session is listed"
  ],
  "design": {
    "chosen": "Add a rename action to the list row that calls the existing session update API.",
    "alternatives": [
      {"option": "A separate rename dialog page", "rejected_because": "Adds navigation for a one-field edit."}
    ],
    "risks": ["Concurrent renames: the last write wins, matching the existing update API."]
  },
  "cujs": [
    {
      "id": "CUJ-1",
      "journey": "user opens the session list → chooses Rename on a row → types a name → the row shows the new name",
      "surface": "web",
      "covered": true,
      "tests": ["tests/e2e_ui/sessions/test_rename_from_list.py::test_rename_from_list"],
      "recordings": ["recordings/omni-1234/demo-cuj-1.mp4"],
      "recording_blocker": "",
      "manual_test": "",
      "not_covered_reason": ""
    },
    {
      "id": "CUJ-2",
      "journey": "user renames a session from the mobile app",
      "surface": "mobile",
      "covered": false,
      "tests": [],
      "recordings": [],
      "recording_blocker": "",
      "manual_test": "",
      "not_covered_reason": "The mobile list has no row actions yet; noted as a follow-up."
    }
  ],
  "recordings": [
    {"cuj": "CUJ-1", "surface": "web", "path": "recordings/omni-1234/demo-cuj-1.mp4",
     "format": "mp4", "capture_mode": "playwright_ui",
     "caption": "open the session list → choose Rename → type a name → the row shows the new name"}
  ],
  "files_changed": ["web/src/sessions/SessionRow.tsx"],
  "tests": {"e2e": "tests/e2e_ui/sessions/test_rename_from_list.py", "added": []},
  "test_audit": "CUJ-1 is covered by a new browser test because the rename crosses the browser boundary; it fails with the rename action removed.",
  "impact_assessment": {
    "base_sha": "<full target-branch tip SHA>",
    "head_sha": "<full candidate HEAD SHA>",
    "worktree_state": "clean",
    "risks": [
      {
        "files": ["web/src/sessions/SessionRow.tsx"],
        "behavior": "Row actions can change selection or navigation",
        "consumers": ["session list", "session sidebar"],
        "invariant": "Clicking a row still opens the session",
        "check": "<exact command>",
        "result": "passed",
        "evidence": "<retained output reference, tested build and environment>"
      }
    ],
    "uncovered_boundaries": [],
    "not_applicable_reason": ""
  },
  "remaining_work": [],
  "pr_url": "https://github.com/omnigent-ai/omnigent/pull/4200",
  "reviewed_pr_url": "",
  "pushed_branch": "feat/omni-1234",
  "ci_status": "green (all required checks pass)",
  "polly_review": "clean on <full candidate HEAD SHA>",
  "ocr_review": "clean on <full candidate HEAD SHA>: no findings",
  "review_cycle": {
    "head_sha": "<full candidate HEAD SHA>",
    "fingerprint": "<fingerprint from the final review snapshot>",
    "dispositions": [
      {"key": "comment:<id>", "status": "addressed", "reason": "Fixed in <commit>; <test> passes."}
    ]
  },
  "ui_preview": "labeled ui-preview; preview at https://…",
  "maintainer_review": "requested review from @<maintainer>",
  "review_fingerprint": "",
  "handled_review_ids": [],
  "last_pushed_sha": "",
  "session_id": "<this session id>"
}
```

## Field meanings

- `outcome`:
  - `implemented`: every success condition in AGENTS.md holds, including the
    live Polly/OCR gate.
  - `partially_implemented`: the feature is built and committed but something
    is missing, such as a blocked recording, an unresolved finding, failing CI,
    or, in local-only and workflow-owned runs, the publication and PR review you
    did not run. Name each gap in `remaining_work`.
  - `not_implemented`: you tried and could not build it; explain why.
  - `nothing_to_build`: already delivered; name the PR or commit.
  - `needs_split`: stopped before building because the ticket is too big;
    `ticket_comment` and `proposed_children` are required.
  - `needs_more_info`: stopped before building because the request is unclear,
    inputs or access are missing, or a design choice needs a human;
    `ticket_comment` gives the options and your recommendation.
- `feature_summary`: plain English for a product user: what they can now do. No
  file names or CI details.
- `ticket_comment` / `proposed_children`: the comment the workflow posts on the
  ticket, and for `needs_split` each child's `title`, `description`, `cujs`, and
  `depends_on` (titles of earlier children). Empty otherwise.
- `requirements`, `design`, `cujs`: as written in `feature-requirements` and
  `feature-design`. Every covered CUJ lists at least one test and at least one
  recording path, or a `recording_blocker` plus `manual_test`. Every
  not-covered CUJ has a `not_covered_reason`.
- `recordings`: one entry per declared clip, with the inspected captioned MP4
  (or the guide's uncaptioned fallback) as `path`.
- `tests`: committed checks as bare repository-relative paths or node IDs.
  `e2e` is the retained E2E path or `""`; `added` lists the other selected
  checks. Put reasoning and gaps in `test_audit`.
- `impact_assessment`: as `otto-impact-assessment` describes.
- `pr_url`, `pushed_branch`, `ci_status`, `polly_review`, `ocr_review`,
  `review_cycle`, `ui_preview`, `maintainer_review`: the publication state.
  `review_cycle` must pass `review_cycle.py check` for `implemented`. A
  local-only (`skip_push`) run leaves `pr_url` empty, sets `pushed_branch` to the
  committed local branch for a human to push, and lists publishing and the PR
  loop in `remaining_work`.
- `reviewed_pr_url`, `review_fingerprint`, `handled_review_ids`,
  `last_pushed_sha`: set in `review_remediation` mode (with `reviewed_pr_url`
  and `pr_url` both the `review_pr` URL); otherwise `""` or `[]`. In this mode,
  `feature_url` carries the PR's attached ticket, which the shared remediation
  skill calls `bug_url`.
