---
name: audit-sync
description: Audited upstream sync for the omnigent fork. Detects the new upstream commits, audits that delta for supply-chain / security / breaking-change risk, and applies the sync-fork update ONLY when the audit passes (HOLD otherwise). Use when asked to safely sync/update omnigent to newest, manually or on a schedule.
---

# Audited sync of the omnigent fork

One cycle: **detect the delta → audit it → apply only if clean.** This gates the
`sync-fork` skill (the mechanical apply) behind an audit of what upstream is
introducing. Run by an agent, not a dumb script — the audit needs judgement.

Repo: `~/dev/ext/omnigent/omnigent`. Branches/remotes are as in `sync-fork`
(`upstream` = omnigent-ai, `mine` = the patched branch).

## Procedure (what the agent does each run)

1. **Detect the delta.**
   ```bash
   cd ~/dev/ext/omnigent/omnigent
   git fetch upstream -q
   BASE=$(git merge-base mine upstream/main)
   NEW=$(git rev-parse upstream/main)
   ```
   If `BASE == NEW`, already current — report "up to date" and stop.

2. **Audit `BASE..NEW`** — the upstream commits about to be replayed under your
   patches (`git log --stat BASE..NEW`, `git diff BASE..NEW`, `git show` on
   anything suspicious). Use the `update-delta-audit` skill if available; the
   criteria are self-contained here so it works without it. Flag **HIGH**
   severity for any of:
   - new or widened network egress; telemetry / phone-home
   - risky new or bumped dependencies (`pyproject.toml`, `uv.lock`, `web/package.json`)
   - auth / permission / sandbox changes
   - code executing at import or install time (build/postinstall hooks)
   - breaking changes to the server/CLI paths this setup relies on

3. **Gate.**
   - **No high-severity finding → APPLY:** run `bash .claude/skills/sync-fork/sync-fork.sh`.
     It rebases `mine`, rebuilds the UI if `web/` changed, reinstalls, and
     **auto-skips the server restart when inside omnigent** (push is non-fatal).
   - **Any high-severity finding, or an inconclusive audit → HOLD:** do NOT run
     sync-fork. Leave the tree, install, and server untouched. Report the
     concern and the offending commits.

4. **Report** — a concise markdown summary of the delta and findings, ending
   with a final line `VERDICT: PASS` or `VERDICT: HOLD <reason>`.

## Running it on a schedule

`auto-sync.sh` (next to this file) is the launchd entrypoint. It runs **outside**
any omnigent session so it can restart the server the audit session runs under:

1. Detects the delta; exits early if none (never wakes an agent for nothing).
2. Drives a headless `omnigent run --harness claude -p …` session through the
   procedure above (the apply runs inside omnigent, so sync-fork skips its own
   restart).
3. If `mine` advanced (audit passed and applied), restarts the launchd server
   **here** to load the new backend, and posts a notification. Held / no-op →
   no restart.

Enable it with the `dev.zarz.omnigent-autosync` launchd agent (daily). The
report and full log land in `~/.omnigent/logs/auto-sync/`.

## Notes

- **HOLD is the safe default** — never apply on an uncertain audit.
- The restart is the *only* self-terminating step; it is owned exclusively by
  `auto-sync.sh` (outside omnigent), never by the in-session apply.
- Committed on `mine` (with `sync-fork`) so both replay across the rebase they
  perform.
