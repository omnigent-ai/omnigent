### 4.3 — Address the automated (Polly) review until it's clean

The repo's **Polly AI Review** runs automatically on a ready PR and posts its
findings as a PR comment marked `<!-- polly-review-bot -->`, structured as
**Blocking issues**, **Security vulnerabilities**, **Non-blocking notes**, and a
**Summary**. Each review run posts a **fresh** comment, so always read the
**most recent** one:

```
gh pr view <pr> --json comments \
  --jq '[.comments[] | select(.body | startswith("<!-- polly-review-bot -->"))] | last | .body'
```

**A green "Polly AI Review" check is not proof Polly reviewed anything.** On a
**fork PR** the automatic `pull_request` run has no LLM credentials (GitHub
withholds secrets from fork events), so the workflow's credentials gate skips every
real step and the check still reports `pass` in a few seconds — a green check with
no review behind it. Never read the check status as "Polly is clean." The **only**
evidence of a real review is a fresh `<!-- polly-review-bot -->` **comment** for
the current head; if the query above returns empty, Polly has **not** reviewed this
head, no matter what `gh pr checks` says.

Polly runs automatically when the PR first becomes ready, but on a **reviewed PR**
that already opened before you arrived — and on **every fork PR**, where the
automatic run always skips — it will not have posted a real review for the current
head, so you must kick one off yourself. **Trigger it via the workflow's
`workflow_dispatch` entry point, not a `/review` comment**: Polly's comment handler
**ignores `/review` from `[bot]` accounts, and you are one** (`omni-resolve-agent[bot]`),
so a `/review` comment you post is silently dropped. `workflow_dispatch` has no
bot/association gate, and it reviews a prefetched diff so it works even for a fork
PR whose automatic run skipped:

```
gh workflow run polly-review.yml -R omnigent-ai/omnigent -f pr=<pr>
```

Polly reviews scope as part of its ordinary prose findings. Clearly unrelated
changes belong under **Blocking issues**; uncertain scope belongs under
**Non-blocking notes** as clarification questions. A missing issue link alone
is not a finding. On the existing-PR review path, resolve those questions
against the reported bug before approving. Review findings do not fail the
Polly workflow, so a green check alone does not mean the review is clean.

Your App token carries `actions: write`, so this dispatch is expected to succeed;
a `403` means the App lost that permission — record `polly_review` as "could not
dispatch — App lacks actions:write" and flag it, rather than falling back to the
green check as if the review were clean.

Wait for a **new** `<!-- polly-review-bot -->` comment to land (a few minutes) —
never treat the review as done on the green check alone — then **triage every
finding under *all* headings, not just Blocking/Security**. Polly's bucketing is a hint, not a
verdict: a real defect regularly lands under **Non-blocking notes** (a missed edge
case, a subtly wrong condition, a dropped error path), and "non-blocking" is not a
licence to ignore it. Go through the newest comment finding by finding — Blocking
issues, Security vulnerabilities, **and** Non-blocking notes — and for each one do
exactly one of:

- **Fix it** — the default for anything that is, or might be, a real defect or a
  cheap correctness/robustness win. Fix at the root (same fail→pass discipline as
  Step 2B — add/adjust a targeted test where it makes sense), re-run the affected
  tests, then land the fix per the **push-or-take-over rule**: `git commit` +
  `git push` when you can push to the branch; on a **fork PR** you can't push to,
  take over into your own PR (Step 4 preamble) and continue on it. **Re-assess a
  non-blocking note as if it were blocking** — decide by whether it's *correct*,
  not by which heading Polly filed it under.
- **Justify skipping it** — only when it is genuinely not actionable in this PR: a
  false positive, purely stylistic/subjective, or out of scope (a pre-existing
  issue your diff didn't introduce). State *which* finding and *why* — in a PR
  reply to Polly's comment and in the handoff (`polly_review`). Never skip a
  finding silently, and never skip one merely because it's labelled non-blocking.

After **pushing** any fix (to your PR or an in-repo branch), **re-trigger the
review** — another `gh workflow run polly-review.yml -f pr=<pr>` (again: not a
`/review` comment from you) — then poll for a **new** `<!-- polly-review-bot -->`
comment and triage it again the same way.

Repeat push → re-trigger → re-read within the round cap until **every** finding on
the newest review is either fixed or has a recorded justification — no unaddressed
notes of any severity remain. Record the final state in the handoff
(`polly_review`), including which non-blocking notes you fixed vs. justified.
