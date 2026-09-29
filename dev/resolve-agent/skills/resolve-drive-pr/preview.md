### 4.1 — Deploy a live preview so the fix can be validated

Add the **`ui-preview`** label so the repo's UI Preview workflow deploys a live
per-PR preview of the app:

```
gh pr edit <pr> --add-label ui-preview
```

Do this on **every** PR you're landing — the one you opened *and* an existing PR
you're reviewing and keeping — and **not only frontend fixes**. Even a backend-only
fix can get a deployed app a reviewer connects a runner to and validates directly
(see the live-validation prompt in 4.4), which is the point of standing the
preview up.

**When you label depends on *whose* code you're deploying.** The `ui-preview`
label is a trust signal: it triggers a `pull_request_target` build+deploy of the
PR's code to a Databricks workspace, so applying it vouches that *this* code is
safe to run there. That trust boundary is about **fork code**, not about CI being
green — so the two paths label at different times:

  - **A PR you authored (author path)** is a branch on `omnigent-ai/omnigent`
    itself — a same-repo PR no outside contributor can push to, carrying code that
    already came through your repro→fix→CI→Polly pipeline. There is no untrusted
    code to gate, so **label it immediately, the moment `gh pr create` returns** —
    right alongside opening the PR, *before* the interim handoff and Step 4's CI
    poll. Front-loading it matters: the deploy takes a few minutes and the session
    can drop mid-Step-4 (the `--server` SSE stream ends the Run step abruptly), so
    a label deferred to "after CI goes green" is exactly what a crash strands.
    Labeling early just means the preview builds while CI runs — for your own
    already-pipelined code that's fine, not a risk.
  - **A PR you're reviewing (review path)** may be a **fork** PR from an outside
    contributor. Here the label *is* the real trust boundary: it green-lights
    deploying fork code, so never apply it until the current head has passed CI and
    a clean Polly review (4.2 + 4.3). And because an attacker can push a new commit
    *after* you label, the fork deploy is backstopped by a human-approved
    Environment that re-gates every commit — but that gate is a safety net, not a
    licence to label early.

If you push (or the author pushes) a further commit after labelling, re-confirm
CI + Polly on the new head before you rely on the preview. Never keep a preview
you're relying on for a PR whose review is still red — on the author path, if CI
later goes red, say so in the handoff rather than pointing a reviewer at a broken
preview.

The workflow deploys for **any labelled PR that isn't a draft — including fork
PRs**; there is no author-membership gate. The label itself *is* the trust
boundary: applying it needs Triage+ on the repo, so an outside contributor
can't self-label their own fork PR — only a maintainer (or a maintainer-
privileged bot identity) can. So the thing that can stop a preview from
appearing is not the author but **whether the label actually got applied**: if
you're running under an identity without label permission, `gh pr edit
--add-label` fails and no preview appears — that is expected; fall back to the
"fails / no URL" handling below rather than looping.
The workflow posts (and updates) a PR comment marked `<!-- ui-preview -->`; it
starts as "being deployed" and flips to "ready" with the preview **URL** once the
Databricks App is up (a few minutes). Poll for the ready comment:

```
gh pr view <pr> --json comments --jq '.comments[] | select(.body | startswith("<!-- ui-preview -->")) | .body'
```

Once it shows a URL, **capture that exact `<url>`** — you will thread it into the
live-validation instructions (4.4) and the maintainer hand-off (4.5) so a reviewer
gets a one-command way to drive the deployed preview, not a vague "connect an app."
Record it in the handoff (`ui_preview`) with the URL, so the workflow's ticket
write-back can surface it too.

The preview ships the **UI only** — no LLM/runner — so a reviewer drives it by
attaching their own host (where their model credentials live). The single command
that both attaches a runner and opens Claude Code on the validation journey
against the preview is:

```
omnigent claude -p '<validation_prompt>' --server <url>
```

`omnigent claude` launches native Claude Code against the remote `--server`
(starting a local runner that carries the reviewer's credentials); `-p` is the
validation prompt from 4.4 (bug-specific) used as the TUI's initial prompt;
`--server <url>` is the preview URL. That one line is what you put in front of the
reviewer (in the PR body's "Validate the fix live" section and the maintainer
comment) — filled in with the real `<url>` and prompt, never left as placeholders.
If the bug is specific to a different harness, use that harness's launcher instead
(e.g. `omnigent codex`), but `omnigent claude` is the default.

**Classify the fix's validation surface — the preview does not always carry your
code.** `--server <preview>` puts the fix only on the **server** side: the preview
deploy runs the PR build, but the reviewer's *local runner* is their **installed**
omnigent, not your branch. So which side your diff runs on decides whether the
preview attach actually exercises the fix. Set a `validation_surface` in your
handoff:

- **`server`** — the fix lives in server/web/UI code (`omnigent/server/**`, `web/**`,
  routing, schemas). The preview build *is* the fix; `--server <preview>` validates
  it end-to-end. This is the default.
- **`runner`** — the fix lives in **runner** code that runs in the host/runner
  process (`omnigent/runner/**`, `omnigent/host/**`, the runner half of a transport
  like `ws_tunnel/serve.py`). Attaching a local runner to the preview runs your
  **fixed server against an unfixed runner** — the fix half never executes, so the
  preview attach proves nothing. The reviewer must run the **PR build on the runner
  side**: `gh pr checkout <pr>` then `omnigent claude --server ""` (a local server
  the same checkout serves), so both halves are your code.
- **`both`** — the diff spans both sides (e.g. a wire-format change touching
  `frames.py` used by server and runner). Treat it like `runner`: only a local PR
  build validates the whole change.

Judge this from `files_changed`, not a guess. When you can't cleanly tell, use
`both` (the safe, self-consistent option). This drives which command you put in
front of the reviewer in 4.4 and 4.5.

If the preview deploy **fails** or never posts a URL (e.g. workspace secrets not
configured in this environment, or the label couldn't be applied under your
identity), don't block on it — note it in the handoff (`ui_preview`)
that no URL was produced, and in 4.4/4.5 fall back to "run against your own app"
with the same `-p` command minus a preview `--server`. The preview is a
convenience, not a gate.
