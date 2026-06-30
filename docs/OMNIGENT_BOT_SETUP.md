# Omnigent bot identities & attribution setup

This doc explains how commits and automated PR reviews in the
`omnigent-ai/omnigent` repo are attributed, and how the supporting GitHub App
was set up. There are **two deliberately distinct identities** — do not
conflate them:

| Identity | Used for | Why this identity |
| --- | --- | --- |
| `omnigent <noreply@omnigent.ai>` | Co-author trailer on commits authored by **polly's coding sub-agents** | These commits are produced by `git commit` in a worker's local worktree — they are **not** GitHub Actions runs, so a plain org co-author is the honest attribution. No GitHub App user is involved. |
| `omnigent-ci[bot]` (GitHub App) | **CI automation**: lockfile-regen commits/PRs **and** automated PR-review comments | These actions genuinely run inside GitHub Actions, where the App's private key lives and a short-lived installation token is minted per run. The App is an org-owned, least-privilege identity. |

> **Why two identities and not one?** An earlier draft of this work tried to use
> `omnigent-ci[bot]` everywhere, including the sub-agent commit trailer. That was
> corrected: polly's workers don't run in Actions and never touch the App key, so
> attributing their commits to the Actions-minted bot user was misleading. Local
> work → plain org co-author; Actions-minted work → the App bot.

---

## The GitHub App: `omnigent-ci[bot]`

> **Naming note.** The App was registered as **`omnigent-ci`** (the bare
> `omnigent` name was unavailable), so GitHub renders the actor as
> **`omnigent-ci[bot]`**.

| Field | Value |
| --- | --- |
| App name | `omnigent-ci` |
| Bot actor | `omnigent-ci[bot]` |
| App ID | `4082516` |
| Bot numeric user ID | `294685417` |
| CI git author email | `294685417+omnigent-ci[bot]@users.noreply.github.com` |

The numeric user ID (`294685417`) is what links GitHub's no-reply commit email
back to the bot's profile; it is distinct from the App ID (`4082516`), which is
used only to mint installation tokens.

> **Why a GitHub App (not a PAT or a plain machine user)?** An App is an
> org-owned identity with scoped, least-privilege permissions and a short-lived
> installation token minted per run — no long-lived personal credential to leak.

---

## Org-admin setup (one-time, completed)

These steps required org-admin and are **done**. They are recorded here because
they are not captured anywhere in the repo and would otherwise have to be
reverse-engineered.

### 1. Create the App

Created at `https://github.com/organizations/omnigent-ai/settings/apps/new`:

- **GitHub App name:** `omnigent-ci` → actor `omnigent-ci[bot]`.
- **Homepage URL:** any valid URL.
- **Webhook:** **Active** unchecked — token-minting only, no webhook.
- **Repository permissions** (least privilege):
  - **Contents:** Read and write — push branches / commits.
  - **Pull requests:** Read and write — open/update PRs **and post reviews**.
  - **Metadata:** Read-only (mandatory).
  - Everything else **No access**.
- **Where can this App be installed?** Only on `omnigent-ai`.
- Installed into `omnigent-ai`, scoped to the `omnigent` repo.

**App ID `4082516`.** A private key (`.pem`) was generated and stored as a
secret (step 3).

> Optional/cosmetic: App settings → **Display information** → upload a square
> logo. Purely visual — does not affect the user ID, attribution, or wiring.

### 2. Resolve the bot's numeric user ID

GitHub's no-reply commit email embeds a numeric user ID assigned after install:

```bash
gh api users/omnigent-ci%5Bbot%5D --jq '.id'
# -> 294685417
```

(`%5Bbot%5D` is the URL-encoding of `[bot]`.)

### 3. Store the App credentials

In **`omnigent-ai/omnigent` → Settings → Secrets and variables → Actions**:

- **Variable** `OMNIGENT_BOT_APP_ID` = `4082516`
- **Secret** `OMNIGENT_BOT_APP_KEY` = the App's `.pem` private key

> The workflows gate on `vars.OMNIGENT_BOT_APP_ID != ''`. If the variable is
> absent or misnamed, the token-mint step is skipped and the workflow falls back
> to `GITHUB_TOKEN` (attributing the action to `github-actions[bot]`) — so the
> exact names matter.

> **`omnigent-ci[bot]` replaced the old OSS regen bot.** The previous
> `OSS_REGEN_APP_ID` / `OSS_REGEN_APP_KEY` config and its App have been
> **retired**; nothing in the repo references `OSS_REGEN_*` anymore.

---

## In-repo wiring (shipped)

### Sub-agent commit co-author trailer

polly never commits directly; its coding sub-agents (`claude_code`, `codex`,
`pi`) run `git commit` / `gh pr create` in their own worktrees. Each such commit
ends with a co-author trailer attributing it to the org:

```
Co-authored-by: omnigent <noreply@omnigent.ai>
```

This requirement lives in the worker IMPLEMENT instructions:

- `examples/polly/agents/claude_code/config.yaml`
- `examples/polly/agents/codex/config.yaml`
- `examples/polly/agents/pi/config.yaml`

> A `Co-authored-by` trailer is GitHub's lightweight attribution mechanism — it
> attributes the commit to the org in addition to the worker author and needs no
> signing key. It is **not** cryptographic signing (GPG/sigstore), which is a
> separate, heavier concern.

> The packaged copies under `omnigent/resources/examples/polly/...` are a
> **symlink** to the `examples/polly/...` source, so there is a single source of
> truth — no dual copies to keep in sync.

### CI commits/PRs as `omnigent-ci[bot]`

The lockfile-regen workflows (`.github/workflows/oss-regenerate-and-smoke.yml`
and `oss-regen-on-comment.yml`) mint the App token and set the git identity so
regen commits/PRs are authored by the bot:

```yaml
- name: Mint App token
  id: app-token
  if: vars.OMNIGENT_BOT_APP_ID != ''
  uses: actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1 # v3.2.0
  with:
    app-id: ${{ vars.OMNIGENT_BOT_APP_ID }}
    private-key: ${{ secrets.OMNIGENT_BOT_APP_KEY }}
```

```bash
git config user.name  "omnigent-ci[bot]"
git config user.email "294685417+omnigent-ci[bot]@users.noreply.github.com"
```

The push uses `steps.app-token.outputs.token || secrets.GITHUB_TOKEN`, so a
missing App config falls back to `github-actions[bot]` rather than failing.

### Automated PR review posted as `omnigent-ci[bot]`

`.github/workflows/polly-review.yml` runs a full cross-vendor Polly review of a
PR diff (on PR open/reopen/ready, a `/review` comment from a write-access user,
or `workflow_dispatch`) and posts the findings as a PR comment. It mints the App
token and posts the review **as `omnigent-ci[bot]`**:

```yaml
- name: Mint App token
  id: app-token
  if: steps.trigger.outputs.skip != 'true' && steps.creds.outputs.available == 'true' && vars.OMNIGENT_BOT_APP_ID != ''
  uses: actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1 # v3.2.0
  with:
    app-id: ${{ vars.OMNIGENT_BOT_APP_ID }}
    private-key: ${{ secrets.OMNIGENT_BOT_APP_KEY }}

- name: Post review comment
  env:
    GH_TOKEN: ${{ steps.app-token.outputs.token || github.token }}
    # ...
```

> **Fail-open by design.** If the App vars are absent, the post falls back to
> `github.token` and the review still posts (as `github-actions[bot]`). The
> review *content* is valuable regardless of who signs it — this is deliberately
> the opposite of a fail-closed posting gate. The workflow always checks out the
> **trusted default branch** and fetches the PR diff via the API; PR-authored
> code is never executed, and the minted token is scoped to the post step only.

---

## Quick reference

| Surface | Identity on the artifact | Where it's wired |
| --- | --- | --- |
| polly sub-agent commits | `omnigent <noreply@omnigent.ai>` (co-author trailer) | `examples/polly/agents/*/config.yaml` |
| Lockfile-regen commits/PRs | `omnigent-ci[bot]` | `oss-regenerate-and-smoke.yml`, `oss-regen-on-comment.yml` |
| Automated PR review comments | `omnigent-ci[bot]` (fallback `github-actions[bot]`) | `polly-review.yml` |

**Config:** variable `OMNIGENT_BOT_APP_ID` = `4082516`, secret
`OMNIGENT_BOT_APP_KEY` = App private key. The old `OSS_REGEN_APP_*` config and
App are retired.
