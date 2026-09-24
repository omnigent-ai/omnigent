### 4.2 — Keep the branch mergeable, then drive CI to green

**First, the branch must merge cleanly into latest `origin/main`.** A PR can pass
CI and still be un-landable because `main` moved under it — GitHub reports this as
`mergeable: CONFLICTING` / `mergeStateStatus: DIRTY`, and a conflicted branch is
**not done** no matter how green its checks look (its CI and preview ran against a
stale base). Never report `fixed` on a branch that doesn't merge. Check it, and
re-check after every push and again right before the final verdict (4.5):

```
gh pr view <pr> --json mergeable,mergeStateStatus,baseRefName
```

- **`mergeable: MERGEABLE`** (clean) → proceed to CI below.
- **`CONFLICTING` / `DIRTY`, or behind by enough to matter** → **rebase onto latest
  `origin/main` and resolve the conflicts** before anything else. On a branch you
  can push to (your PR, or an in-repo branch):

  ```
  git fetch origin main
  git rebase origin/main        # resolve conflicts: edit, `git add`, `git rebase --continue`
  # re-run the repro test + your targeted tests after resolving, then:
  git push --force-with-lease
  ```

  Resolve conflicts by **understanding both sides**, not by blindly taking one —
  the incoming `main` change may interact with the fix. After resolving, **re-run
  the repro test and your targeted tests** (the merge may have silently broken the
  fix), then force-push. On a **fork PR you can't push to**, a conflict is one more
  reason to **take over** into your own PR (Step 4 preamble): branch off latest
  `main`, replay their commits (`git cherry-pick` / `am`), resolve there, and
  continue on your PR. If a rebase is beyond mechanical resolution (a deep semantic
  conflict you can't confidently settle), don't guess — say so in the handoff and
  leave it for the maintainer rather than force-pushing a bad merge.

  `mergeable` can read `UNKNOWN` briefly while GitHub computes it — re-poll a few
  seconds later before concluding.

**Then drive CI to green.** Watch the PR's checks and don't consider the work done
until they pass:

```
gh pr checks <pr> --watch --json name,state,bucket,link
```

`bucket` is `pass` / `fail` / `pending` / `skipping` / `cancel`. When everything
settles:

- **All pass** → CI is green; move on.
- **A check fails** → read *why* before touching anything. Pull the failing run's
  log (`gh run view <run-id> --log-failed`, the run id is in the check `link`).
  Decide honestly whether the failure is **caused by your diff** or is
  **pre-existing / flaky / infra** (a failure unrelated to the files you touched,
  a known-flaky suite, a runner/secret problem):
  - **The diff caused it** → fix the code (not the test), re-run the relevant
    tests locally to confirm, then land the fix per the **push-or-take-over rule**:
    `git commit` + `git push` when it's your PR or an in-repo branch you can push
    to (the push re-runs CI); on a **fork PR** you can't push to, take over and
    open your own PR carrying their commits + your fix (see the Step 4 preamble),
    then continue this loop on **your** PR.
  - **Pre-existing / flaky / infra** → do **not** chase it or paper over it. Note
    it in the handoff (`ci_status`) as an unrelated failure and, if it's a flake,
    you may re-run that job (`gh run rerun <run-id> --failed`) once. Don't loop on
    someone else's red.

Re-poll after each push (or, if you took over a fork PR, on your own PR's checks).
Stay in this loop (within the round cap) until the checks you're responsible for
are green.
