# Manual maintenance and provenance

This skill bundles seven personas from
<https://github.com/msitarzewski/agency-agents> at
`bd956db032d907387e6b9ddebff3e18f1de8c679` initially. The authoritative current
pin is `pinned/manifest.json`. All persona Markdown is copied byte for byte,
including frontmatter. `pinned/LICENSE` is the complete upstream MIT license,
copyright (c) 2025 AgentLand Contributors. Polly's overlay is separate and is
not upstream prose. No native Agency harness integration is installed.

| Requested role / stable ID | Actual upstream name | Upstream path |
| --- | --- | --- |
| Frontend Developer / `frontend-developer` | Frontend Developer | `engineering/engineering-frontend-developer.md` |
| Backend Developer / `backend-developer` | Backend Architect | `engineering/engineering-backend-architect.md` |
| Software Architect / `software-architect` | Software Architect | `engineering/engineering-software-architect.md` |
| Mobile App Developer / `mobile-app-developer` | Mobile App Builder | `engineering/engineering-mobile-app-builder.md` |
| Technical Writer / `technical-writer` | Technical Writer | `engineering/engineering-technical-writer.md` |
| Code Reviewer / `code-reviewer` | Code Reviewer | `engineering/engineering-code-reviewer.md` |
| Database Optimizer / `database-optimizer` | Database Optimizer | `engineering/engineering-database-optimizer.md` |

Backend Architect covers API development, server applications and data design;
Mobile App Builder covers native iOS/Android and cross-platform development.
These are the closest actual upstream roles, not invented upstream titles.

## Repository updates are independent

Run commands from an Omnigent checkout with Python 3.12+ and PyYAML installed
(`uv sync --frozen --group dev` for development). Paths below are placeholders;
choose each checkout explicitly. The helper accepts ONE repo per invocation.
There is no schedule, coupled pull, automatic installation or persona refresh.

First inspect each repository, including untracked and ignored files, and its
remote/tracking configuration. `check` fetches remote refs but changes no checkout
files or branch tip. Review the candidate commit and diff before using `update`:

```sh
git -C /path/to/omnigent status --short --untracked-files=all --ignored
git -C /path/to/omnigent remote -v
uv run --no-sync python scripts/polly_agency.py check /path/to/omnigent

git -C /path/to/agency-agents status --short --untracked-files=all --ignored
git -C /path/to/agency-agents remote -v
uv run --no-sync python scripts/polly_agency.py check /path/to/agency-agents
# For EACH repo, use the full head/candidate hashes printed by check:
git -C /path/to/repo log --oneline HEAD..CANDIDATE_SHA
git -C /path/to/repo diff HEAD CANDIDATE_SHA

# Explicitly invoke only for the clean tracking branch you intend to advance:
uv run --no-sync python scripts/polly_agency.py update /path/to/omnigent
uv run --no-sync python scripts/polly_agency.py update /path/to/agency-agents
```

`update` fetches again, rejects dirty/untracked work, detached or non-tracking
branches, local-ahead commits and divergence. It uses fast-forward only,
disables autostash and protects ignored files from incoming tracked-file
collisions. It never resets, cleans or resolves conflicts. Existing user files
must be handled by their owner; do not remove them to make an update pass.
Do not run concurrent Git writers in a repository being updated. If a candidate
changes between inspection and update, inspect the newly printed revision too.
The tool trusts configured Git remotes, hooks and credentials like ordinary Git.

During integration development, demonstrate this only with temporary repositories
(the tests do so). Do not update the original source checkouts.

## Explicit persona upgrade and promotion

Updating Agency alone does NOT change the bundled instructions. A build uses
Git objects at the manifest's full commit SHA, never files from the source
working tree. The manifest records URL, SHA, exact paths, IDs, aliases, names,
descriptions, source/license SHA256 and schema/converter versions. Converter v1
is a lossless copy. `integrity.json` covers all generated artifacts except itself;
these hashes detect accidental corruption, not authenticity against an attacker
who can rewrite the manifest and checksums. Review the upstream diff and source.

From a clean integration branch, use a new staging directory outside the skill
(the example uses a repository-local ignored virtualenv directory). Pin and
build refuse to overwrite an existing destination:

```sh
mkdir .venv/agency-upgrade
uv run --no-sync python scripts/polly_agency.py pin \
  --source /path/to/agency-agents --sha FULL_REVIEWED_COMMIT_SHA \
  --output .venv/agency-upgrade/manifest.json
uv run --no-sync python scripts/polly_agency.py build \
  --source /path/to/agency-agents --manifest .venv/agency-upgrade/manifest.json \
  --output .venv/agency-upgrade/candidate
uv run --no-sync python scripts/polly_agency.py validate .venv/agency-upgrade/candidate

git diff --no-index examples/polly/skills/agency-router/references/pinned \
  .venv/agency-upgrade/candidate
# Review the complete prose/license diff; diff exit 1 means differences exist.
# Then explicitly promote, preserving a rollback directory:
uv run --no-sync python scripts/polly_agency.py promote \
  --candidate .venv/agency-upgrade/candidate \
  --active examples/polly/skills/agency-router/references/pinned \
  --backup .venv/agency-upgrade/previous
uv run --no-sync pytest tests/scripts/test_polly_agency.py --reruns 0 -q
```

Promotion validates both trees and rejects unexpected files/symlinks or an
existing backup. Keep all three directories on the same filesystem; promotion
uses directory renames, restoring the active directory on a failed second
rename. Do not promote while sessions or other processes read/write the skill.
Missing/renamed/deleted sources or renamed persona titles are hard errors:
review and deliberately change mappings/converter and tests in a PR, never
silently substitute another persona. A new metadata schema also needs review.

Commit the new pinned directory, get human PR review/merge, then explicitly
reload/re-upload the Polly bundle and start **new sessions**. A running session
may already hold the old instructions; do not claim it has changed. Updating
Omnigent can deliver changed bundled content, so review its diff too and keep
existing session snapshots until the approved activation boundary.

For local rollback before publishing, reverse the promotion with a fresh backup:

```sh
uv run --no-sync python scripts/polly_agency.py promote \
  --candidate .venv/agency-upgrade/previous \
  --active examples/polly/skills/agency-router/references/pinned \
  --backup .venv/agency-upgrade/rejected
```

After publishing, revert the dedicated persona-upgrade commit on a branch, run
validation and tests, and open a rollback PR. Human merge and bundle reload for
new sessions remain explicit. Keep the previous commit and its complete pinned
resources available; do not depend on a mutable upstream branch for rollback.

## Verify routing manually

In a new Polly session, request: “Explore the mobile app architecture read-only,
using the Mobile App Developer persona.” Confirm the catalog is read first,
only `mobile-app-developer.md` is loaded, the Mobile App Builder mapping is
reported, and an available existing worker receives purpose `explore`, the task
contract, overlay and selected content. Repeat a scoped code review with another
available vendor; ensure it receives the saved diff and contract and remains
read-only. Selection of Code Reviewer alone is not independent review.
