---
name: sync-fork
description: Use when asked to sync this omnigent fork/clone with upstream, pull in upstream changes, update main and the patched branch, or "get up to date with upstream". Fast-forwards the main mirror from omnigent-ai/omnigent and rebases the local patch branch onto it, auto-stashing any work-in-progress.
---

# Sync the omnigent fork with upstream

This clone tracks two remotes and keeps two branches:

| Branch | Tracks | Role |
|--------|--------|------|
| `main` | `upstream/main` (omnigent-ai/omnigent) | pristine mirror, fast-forward only |
| `mine` | `origin/mine` (your fork) | `main` + your cherry-picked patches, rebased onto fresh upstream each sync |

`upstream` = `omnigent-ai/omnigent`, `origin` = your fork (`kzarzycki/omnigent`).

## How to sync

Run the bundled script:

```bash
.claude/skills/sync-fork/sync-fork.sh
```

It performs, in order:
1. Auto-stash the working tree if dirty (untracked included).
2. `git fetch upstream`.
3. Fast-forward `main` to `upstream/main`, push to `origin`.
4. Rebase `mine` onto `upstream/main`, force-push (`--force-with-lease`) to `origin`.
5. **Refresh the local runtime** so the synced code is actually live:
   rebuild the web SPA if `web/` changed (it's a gitignored build artifact),
   re-sync the editable install (`uv tool install --editable . --reinstall`)
   for new deps + an honest `--version`, and `launchctl kickstart` the
   `dev.zarz.omnigent` agent so the running server drops its old imports.
6. Return to the starting branch and restore the stash.

A clean tree is left exactly where it started, now on top of current upstream,
with the web UI, deps, and running server all matching the synced code.

The refresh steps are guarded — web rebuild is skipped when `web/` is
unchanged, and the server restart is skipped when the launchd agent isn't
loaded — so the script still works on a checkout without the local install.

## When the rebase conflicts

The script stops mid-rebase and the EXIT trap can't pop the stash. Recover by hand:

```bash
# resolve conflicts, then:
git rebase --continue          # repeat until done, or: git rebase --abort
git push --force-with-lease origin mine
git checkout mine && git stash pop   # your WIP is in `git stash list`
```

## Notes

- This skill must be committed on `mine` to survive the rebase that the sync itself performs. If it lives only as an uncommitted working-tree file, it gets stashed/restored each run instead of being part of the replayed patch set.
- The script assumes the branch/remote names in the table above. Renaming any of them means editing the four variables at the top of `sync-fork.sh`.
