### 4.5 — Submit the final review verdict, then tag the maintainer

When the branch is **mergeable** (4.2 — re-check `mergeable` now; `main` may have
moved again since your last push), CI is green (4.2), **and** the automated review
is clean (4.3), you're done iterating — now record your verdict and hand off to a
human. A `CONFLICTING`/`DIRTY` branch is **not** `fixed`: rebase and resolve
(4.2) before you submit a verdict, or, if you truly can't, downgrade the outcome
and say the PR needs a conflict resolution the maintainer must do.

**Gate: the shared impact assessment covers the current candidate.** Confirm
its base/head and tested contents still match, all required checks passed, and
`uncovered_boundaries` is empty. Otherwise follow its blocked/partial verdict
rules; do not approve or call the PR fully verified.

**Gate: the after-fix clip is present, or its absence is named — no silent skip.**
Before you tag anyone, confirm the deliverable carries the before/after proof
(2B.5 / 2A.3): the PR's **Demo** section shows the `after` clip (and the `before`
when one was recovered), and `recordings` in your handoff lists an `after` entry
for **every** `web`/`mobile`/`terminal`/`cli`/`desktop` facet. You **added the
reproduction test** — that is the driver the recorder needs, so on a web/mobile
fix the after-clip is obtainable here; produce it (build the SPA, record via
`OMNIGENT_E2E_RECORD_DIR` per `dev/recording-lanes.md`) rather than linking only
the repro run and a manual "run it yourself" command. Omit the after-clip **only**
for a genuine, named environmental blocker (recorder tooling missing, fixture
won't come online after the SPA build, `api`-surface facet with nothing to film) —
and when you omit it, **say which blocker, with the evidence**, in both the PR's
Demo section and the handoff (a `recordings` prose note, or `maintainer_review`).
A missing upstream before-clip is never that blocker. Never report an after-clip
you didn't actually produce, and never drop it silently.

**First, submit your final review** per the verdict rule in 2A.5 (review path
only): **approve** when you were a pure reviewer and the PR is `fixed` (you pushed
nothing); **request-changes** when it's `not_fixed` / `partially_fixed`; a plain
**comment** when you pushed to or took over the code (no self-approval). On the
author path, there's no self-review — your own PR just gets tagged. Remember: a bot
approval is only an indicator; a human maintainer's approval is always what merges.

**Then hand the PR to a human** — the **person the bug is assigned to** — on
**both paths** (a PR you authored and an existing PR you reviewed and kept).
**Never pick a reviewer arbitrarily.** Determine the assignee in this priority:

1. **The `bug_url` assignee is authoritative.** When `bug_url` is a **Linear
   ticket**, its assignee is the one to tag — read it from Linear (GraphQL
   `issue(id:"OMNI-XXXX"){ assignee { displayName email } }`) and map to their
   GitHub login (by matching the mirrored issue's assignee, or the email/handle).
   When `bug_url` is a **GitHub issue**, its own assignee is authoritative.
2. **Fallback to the mirrored GitHub issue's assignee.** If the Linear ticket has
   **no** assignee (or you can't map it to a GitHub login), fall back to the
   `closing_issue_number` issue's assignee — often the same person, since the
   mirror is assigned to whoever owns the Linear ticket.

Read the chosen assignee and request their review:

```
# Linear ticket → its assignee (authoritative); else the mirrored issue's assignee
gh issue view <closing_issue_number> --json assignees --jq '.assignees[].login'
gh pr edit <pr> --add-reviewer <login>
```

`gh pr edit --add-reviewer` is a write — it needs `GH_TOKEN` set in your shell
(see "Get the GitHub write token" in Step 3). If it fails with a permission error,
recover the token as shown there and retry; don't record a "read-only/expired
token" excuse.

- If there are **multiple assignees**, request all of them.
- If there is **no assignee on the Linear ticket and no `closing_issue_number`**
  (so there's no assignee to read anywhere), the
  assignee **is the PR author** (you can't request review from the author — common
  on the review path, where the assignee often *is* whoever opened the PR you
  reviewed), or the issue has **no assignee**, don't force a reviewer — instead
  post an `@mention` comment asking them (or, with no assignee/non-issue bug,
  noting the PR is ready for a maintainer):
  ```
  gh pr comment <pr> --body '@<login> this fixes #<closing_issue_number> — CI is green and the automated review is clean. Ready for your review. Try it live: `omnigent claude -p '\''<validation_prompt>'\'' --server <url>` (the UI preview from the ui-preview comment). See "Validate the fix live" (in the PR body, or the comment above on a reviewed PR).'
  ```

When there is a preview `<url>`, always include the ready-to-run
`omnigent claude -p '<validation_prompt>' --server <url>` line in this comment with
the real URL — that is the reviewer's fastest path to see the fix work. Drop
`--server <url>` when no preview was produced.

Record who you tagged in the handoff (`maintainer_review`). Only tag once the PR
is genuinely green and clean — don't ping a human to look at a red PR. You still do
**not** merge.
