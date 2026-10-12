## Step 1 — Look for an existing fix PR (this decides your path)

Before writing any code, find out whether someone is **already fixing this bug**.
For GitHub issues and Linear tickets (with or without a GitHub mirror), search
linked PRs and use `gh pr list --repo <repo> --state open --search "<query>"`
with the issue/ticket identifier, symptom keywords, and affected component.
Use `target_repo` when supplied; otherwise use `omnigent-ai/omnigent`.

- Before dismissing a plausible match, read its **full description and relevant
  diff**, not just its title or a truncated summary. Broader fixes may cover the
  reported symptoms even when linked to a different issue.
- Compare its coverage with each reported symptom. If it may fix the bug, use
  Step 2A to validate it; prefer reviewing or extending a sound existing fix.
  If you still author a separate PR, name the candidate and explain what it
  misses or why its approach is unsuitable in `fix_summary` and the PR body.
- An open PR does not mean the bug is already fixed; validate its behavior.

Branch on what you find:

- **A candidate fix PR exists → go to Step 2A (review it).** If that review finds
  the PR's approach isn't a viable base (see 2A.5), you may fall through to Step 2B
  and author your own.
- **None → go to Step 2B (author the fix).**

If there are *multiple* candidate PRs, pick the most recently updated open one to
review and name the others in your output.

**Find the GitHub issue to close (`closing_issue_number`).** GitHub only
auto-closes an issue when the PR body carries a closing keyword pointing at a
GitHub issue *in the same repo* (`Closes #<n>`); a raw Linear URL closes nothing.
Determine the number now so Step 3 and the maintainer handoff can use it:

- If `bug_url` is a **GitHub issue** in this repo, `closing_issue_number` is its
  number.
- If `bug_url` is a **Linear ticket**, look for a mirrored GitHub issue.
  **First, ask Linear for the structured link — don't guess by title.** Linear's
  GitHub sync records the mirror as an **attachment** on the issue (the "Issue
  synced with GitHub #NNNN" row you see in the UI), so query it directly with the
  Linear token you already have (`DATABRICKS_LINEAR_API_KEY`):

  ```
  # GraphQL: the synced GitHub issue is an attachment whose url is the GH link
  query { issue(id:"OMNI-1519") { attachments { nodes { url sourceType } } } }
  ```

  **Discriminate by the URL path, not `sourceType`.** Every GitHub attachment —
  the synced issue *and* any linked PRs — has `sourceType: "github"`, so that field
  doesn't tell them apart. The **mirror is the node whose `url` matches
  `github.com/<owner>/<repo>/issues/<n>`**; nodes matching `/pull/<n>` are PRs
  (often the fix PRs, including your own once you open one — ignore those here).
  Take `<n>` from the `/issues/<n>` node as `closing_issue_number`. This is
  authoritative — prefer it over any search.
- **Only if the API shows no synced attachment**, fall back to a title search —
  **not** the OMNI key. The mirror almost never contains the `OMNI-####` string
  (it's the *same bug reworded*), so an OMNI-key search returns nothing and is not
  evidence the mirror is absent. Search the ticket's distinctive phrase across
  **all** states, trying more than one phrasing:
  `gh issue list --repo <repo> --state all --search "<distinctive words from the title>"`.
  If you find an issue that is clearly the same bug, that is `closing_issue_number`.
- Only after **both** the attachment query and the title search come up empty is
  there **no** `closing_issue_number`. Then the PR body must **not** use a closing
  keyword against the Linear URL — reference the ticket in prose instead
  (e.g. "Resolves OMNI-1234 (Linear)"). Do not claim "no mirror exists" off a
  single OMNI-key search that found nothing.

Record the chosen `closing_issue_number` (or its absence) — you reuse it in the
PR body (Step 3.4) and the maintainer handoff (Step 4.5).
