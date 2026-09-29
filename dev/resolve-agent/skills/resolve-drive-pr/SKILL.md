---
name: resolve-drive-pr
description: Drive an open PR through current CI, independent Polly review, preview, and maintainer handoff.
---

# Drive an open PR

Read each resource when reaching its substep, preserving the publication mode:

| Substep | Resource |
| --- | --- |
| 4.2, current CI and mergeability | [ci.md](ci.md) |
| 4.3, independent Polly review | [polly.md](polly.md) |
| 4.1, preview after CI and Polly are clean | [preview.md](preview.md) |
| 4.4, live-validation instructions | [validation-prompt.md](validation-prompt.md) |
| 4.5, final review and maintainer handoff | [final-review.md](final-review.md) |

Use `read_skill_file` with this skill name and the relative filename, or read
relative to the directory supplied by the native Skill tool. Workflow-owned
author runs read only `validation-prompt.md` for deferred body preparation;
local-only author runs skip this skill. Review-remediation follows its mode's
exemptions. Load `resolve-handoff` before any interim or final handoff.

## Step 4 — Land the PR: preview, green CI, clean review, hand it to the maintainer

This step applies to **any PR you are driving toward landable** — the one you
opened (author path, Step 2B/3) **and** the existing PR you reviewed and kept as
the fix (review path, Step 2A, when its approach was sound). The goal is identical
either way: a live preview, green CI, a clean automated review, a copy-paste
live-validation command, and a maintainer tagged. `skip_push` runs (author path
that only committed locally) have no PR to land, so skip Step 4 entirely.
Workflow-owned author runs also have no PR during the agent session: perform only
the deferred body/prompt preparation called out in Step 4.4 before the final
handoff, and leave preview, CI, Polly, GitHub comments, and maintainer tagging to
the post-publication workflow. Once a directly published or reviewed PR is up you
**stay on it** until CI is green and the review is clean, then hand it to a
human. The sub-steps overlap in time (kick off the preview and the first review,
then poll), so don't serialize what can run concurrently.

Refresh the shared impact assessment after changes made in this loop, including
CI/review fixes and conflict resolution. Earlier green checks do not cover a new
head automatically.

**Whose branch — push or take over.** On the **author path** the PR is yours: push
fix commits freely. On the **review path** the PR is someone else's; whether you
can land a fix depends on where its branch lives:

- **In-repo PR branch** (the head branch is on `omnigent-ai/omnigent`, not a fork)
  → you have write access. Push fixes the same as the author path, then re-check.
  Say in your review comments that you pushed, so the author isn't surprised.
- **Fork PR** (the head branch is on a contributor's fork, `head.repo.fork ==
  true`) → the App token cannot push there, but CI may provide an isolated
  maintainer credential specifically for that transport. Read
  `maintainerCanModify`, `headRepositoryOwner`, `headRepository`, and
  `headRefName` from `gh pr view`.
  - If `maintainerCanModify` is true and
    `git config --get omnigent.forkPushTokenFile` names a readable file, preserve
    the contributor's branch. Keep the App token exported as `GH_TOKEN` for all
    visible actions. For the push only, use CI's credential-isolating helper:
    ```bash
    omnigent-push-fork --repo <owner>/<repo> --branch <head-branch>
    ```
    The helper uses `GIT_ASKPASS`, clears the checkout's App-token extraheader
    only for that push, and keeps the maintainer token out of command arguments,
    remote URLs, and git's failure output. Never read or copy the token file
    yourself, and never use it for `gh pr comment`/`review`/`create`; those
    commands must remain bot-attributed through the App token.
  - **If the fork PR needs a fix** and maintainer edits are disabled, the token
    file is absent, or the isolated push returns a real permission error →
    **take over: open your own PR**
    that includes their work plus your fix. This is the same mechanic as the
    "approach is wrong" escape hatch (Step 2A), but the reason is different — the
    approach is fine, you just can't push the fix to a fork. Build it so the
    contributor keeps credit:
    - Branch off `main`, cherry-pick the fork PR's commits (`gh pr checkout <pr>`
      then replay onto your branch, or `git cherry-pick`), then add your fix on top.
    - Credit the original author on the commits (`Co-authored-by: <name> <email>`,
      read from `gh pr view <pr> --json commits`).
    - In your PR body, put `Supersedes #<pr>` on its own line, credit
      `@<author>`, and say why you re-opened it (couldn't push to the fork). The
      duplicate-PR workflow exempts trusted resolve-agent replacement PRs.
    - Comment on the fork PR as soon as yours opens, but leave it open while the
      replacement is being reviewed:
      ```
      gh pr comment <fork-pr> --body 'Replacement #<your-pr> is open because maintainer fork updates were unavailable. Your commits are carried over with credit. This PR will remain open until the replacement merges. Thanks @<author>!'
      ```
      The trusted post-merge workflow reads the `Supersedes #<pr>` marker and
      closes the contributor PR only after your replacement is merged. Never
      close a contributor PR merely because a replacement was opened.
    - Set `mode: "authored_fix"`, record the fork PR's number in `reviewed_pr_url`,
      and drive **your** PR through the rest of Step 4 (you can push to it).
  - **If the fork PR needs no fix** (repro passes against it, CI green, review
    clean) → there's nothing to push, so keep it. Since you're a pure independent
    reviewer here, **submit an approving review** (2A.5) with the findings + the
    try-it-out command, then tag the maintainer. No takeover needed. (The approval
    is a bot indicator — the maintainer's approval still merges it.)

Throughout, address the PR you're landing by its number `<pr>`. This whole step is
a **bounded loop** — cap it at **~6 fix→(push-or-takeover)→re-check rounds**. A
fork **takeover** is not one of those rounds: it opens a fresh PR and restarts CI +
Polly from scratch on it, so treat it as a **reset** — the ~6-round budget applies
to the new PR from that point, rather than being consumed by the takeover itself.
If you're still red or still getting blocking findings after the budget, stop,
leave the PR open with an honest summary comment of what's unresolved, and report
`outcome: "partially_fixed"` with the specifics (see Output). Never loosen a test,
skip a check, or merge to force green.
