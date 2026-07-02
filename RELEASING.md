# Releasing omnigent

omnigent ships **three PyPI packages that version-lock together**:

| Package | What it is |
| --- | --- |
| `omnigent` | core wheel (bundles the `web` web UI) |
| `omnigent-client` | Python client SDK |
| `omnigent-ui-sdk` | terminal UI SDK |

`pip install omnigent==X` must resolve `omnigent-client==X` and
`omnigent-ui-sdk==X`. The pins are **lockstep** (the three packages co-version and
pin each other with `==`), so every release builds and publishes **all three at
one identical version**.

## Where things run

- **Source of truth** (versions, tags, GitHub Releases): **`omnigent-ai/omnigent`**
  — use the **OSS GitHub account** (the personal account with push/release rights
  on the public repo).
- **Publishing to PyPI**: the central **secure-release repo**
  **`databricks/secure-public-registry-releases-eng`**, `omnigent` workflow —
  use the **Databricks EMU account**. Publishing runs on hardened runner
  groups with **OIDC Trusted Publishing (no stored secrets)** and a **mandatory
  dependency scan**. This is why we don't publish from `omnigent-ai/omnigent`.

> The exact account handles — and how to request publish access — live in the
> internal release wiki; this public runbook refers to them only by role.
> Substitute your own handles for `<oss-account>` / `<emu-account>` in the
> `gh auth switch --user …` commands below.

The legacy `.github/workflows/release-omnigent.yml` in this repo is a
**deprecated manual fallback only** — its tag-push trigger was removed so a tag
never double-publishes. Use the secure repo for real releases.

> The secure `omnigent` workflow is **manual `workflow_dispatch`** — it can't see
> this repo's tag pushes. You bump + tag here, then dispatch it with that tag.

## Versioning model

- `main` always carries the **next** version with a `.dev0` suffix
  (e.g. `0.2.0.dev0`) — never a clean released number. This matches
  MLflow / Delta / Unity Catalog and keeps every `main` build PEP 440-ordered as
  "ahead of the last release, not yet the next one".
- Releases are cut on **per-minor release branches** (`branch-X.Y`) and tagged
  there (`vX.Y.Z`); patches (`vX.Y.1`, `vX.Y.2`, …) are cherry-picked onto the
  same `branch-X.Y`. `main` is never tagged.

---

## Release steps (example: `v0.2.0`)

### 1. Cut the release branch + tag — `omnigent-ai/omnigent` (OSS account)

Only tag a commit that already has **green CI** — verify `main` is green before
branching:

```bash
gh auth switch --user <oss-account>
git fetch origin
gh run list --repo omnigent-ai/omnigent --branch main --status success --limit 1
git checkout -b branch-0.2 origin/main
```

Set the release version in **all three** `pyproject.toml` files — the
`version` field **and** the cross-package `==` pins — plus `uv.lock`
(`0.2.0.dev0` → `0.2.0`):

- `pyproject.toml` (`version`, `omnigent-client==`, `omnigent-ui-sdk==`)
- `sdks/python-client/pyproject.toml` (`version`, `omnigent==`)
- `sdks/ui/pyproject.toml` (`version`, `omnigent-client==`)
- `uv.lock` — **hand-edit** the three `version = "…"` lines (omnigent,
  omnigent-client, omnigent-ui-sdk) and the one cross-pin `specifier = "==…"`
  (`omnigent-ui-sdk`'s dep on `omnigent-client`). The three packages are
  **editable workspace members** (`source = { editable = … }`), so uv records
  **no wheel `hash` entries** for them, and the other two cross-deps appear as
  `editable = "…"` with no `==` specifier — so only those version/specifier
  strings change, nothing else (no hashes to touch).
  **Do not run `uv lock`** locally: it rewrites every registry URL to the
  internal proxy and that leaks into the lockfile (breaks CI). The published
  lock must use `https://pypi.org/simple`.

Stage exactly the version files (don't `-a`, which would sweep in any stray
local edits), then commit, tag, and push **the branch + only this tag**:

```bash
git add pyproject.toml sdks/python-client/pyproject.toml sdks/ui/pyproject.toml uv.lock
git commit -m "release: v0.2.0"
git tag v0.2.0
git push -u origin branch-0.2 v0.2.0        # explicit tag, NOT --tags; pushing the tag drafts the GitHub Release (step 5)
```

> Pushing the tag also kicks off the **changelog automation** (see step 5):
> `github-release.yml` drafts the Release, then `draft-release-notes.yml` opens a
> `CHANGELOG.md` PR and fills the draft with curated notes — both ready by the time
> you get to step 5.

Keep `main` from re-freezing — bump it to the next dev marker and push:

```bash
git checkout main
# set 0.2.0.dev0 -> 0.3.0.dev0 in the 3 pyprojects (+ pins) and uv.lock.
# Hand-edit uv.lock here too — same rule, do NOT run `uv lock` (it leaks the proxy URL).
git add pyproject.toml sdks/python-client/pyproject.toml sdks/ui/pyproject.toml uv.lock
git commit -m "chore: bump main to 0.3.0.dev0"
git push
```

### 2. Dry-run the gates — secure repo (EMU account)

```bash
gh auth switch --user <emu-account>
gh workflow run omnigent.yml --repo databricks/secure-public-registry-releases-eng \
  -f ref=v0.2.0 -f destination=test-pypi -f dry-run=true
```

Runs build + dependency scan + the gates (lockstep version/pins, web-UI-in-wheel,
`twine check`, smoke-install) and the OIDC token exchange — **without uploading**.

### 3. Publish to TestPyPI + validate

```bash
gh workflow run omnigent.yml --repo databricks/secure-public-registry-releases-eng \
  -f ref=v0.2.0 -f destination=test-pypi -f dry-run=false
```

Validate in a clean venv. **Don't** use `--extra-index-url` with TestPyPI: pip
resolves each name across *both* indexes and picks the highest version, so anyone
squatting `omnigent` / `omnigent-client` / `omnigent-ui-sdk` on real PyPI at a
higher version wins the resolution (dependency confusion). Instead, take **deps
from real PyPI only** and the **candidates from TestPyPI only**, exact-pinned with
`--no-deps`:

```bash
python -m venv /tmp/omni-rc
# 1) seed the dependency closure from REAL PyPI (the last released omnigent):
/tmp/omni-rc/bin/pip install --index-url https://pypi.org/simple/ omnigent
# 2) overlay the candidates from TestPyPI ONLY, exact-pinned, no deps:
/tmp/omni-rc/bin/pip install --index-url https://test.pypi.org/simple/ --no-deps \
  omnigent==0.2.0 omnigent-client==0.2.0 omnigent-ui-sdk==0.2.0
/tmp/omni-rc/bin/omnigent --version    # expect 0.2.0
```

> If this release **adds a new runtime dependency** the previous release didn't
> have, install it explicitly from real PyPI first
> (`/tmp/omni-rc/bin/pip install --index-url https://pypi.org/simple/ <dep>`) —
> never let a `--no-deps` TestPyPI install pull third-party deps from TestPyPI.

### 4. Publish to PyPI (prod)

Requires **admin/maintain** on the secure repo (if you hit a 403, request access
via the secure-release owning team / internal release wiki before proceeding);
binds the per-package `pypi-omnigent`, `pypi-omnigent-client`,
`pypi-omnigent-ui-sdk` Trusted-Publisher environments (may gate on reviewer
approval). The prod path also re-verifies that
`ref` is exactly the `vX.Y.Z` tag and that the tag points at the built commit.

```bash
gh workflow run omnigent.yml --repo databricks/secure-public-registry-releases-eng \
  -f ref=v0.2.0 -f destination=pypi -f dry-run=false

uv tool install omnigent==0.2.0        # final sanity from real PyPI
```

> Note: the dispatch's `-f ref=v0.2.0` is the **omnigent source ref**; it is
> distinct from `gh workflow run --ref`, which selects the branch the *workflow
> definition* runs from (the secure repo's default).

### 5. Publish the GitHub Release — `omnigent-ai/omnigent` (OSS account)

Pushing the `v0.2.0` tag (step 1) set the **changelog automation** in motion —
two workflows have already done the prep for you:

- `github-release.yml` created a **draft** release.
- `draft-release-notes.yml` (fires right after) then:
  1. opened a **`CHANGELOG.md` PR to `main`** — the granular, feature-level log,
     harvested mechanically from each merged PR's `## Changelog` section; and
  2. **filled the draft's body** with concise, curated two-section notes (Major new
     features / Bug fixes & hardening), synthesized by an agent from the merged
     PRs, with the original auto-notes tucked into a collapsed `<details>` for
     reference.

Now:

1. **Merge the `CHANGELOG.md` PR** as part of cutting the release, so the draft's
   `Full Changelog` link (which points at `CHANGELOG.md` on `main`) resolves.
2. Open <https://github.com/omnigent-ai/omnigent/releases>, find the `v0.2.0`
   draft, and **review/trim the curated notes** — they're a strong starting point,
   not the final word. Lead with user-facing highlights; call out breaking changes.
   Whatever you leave here becomes the website post, so curate it well.
3. **Publish the release** (ideally only after the prod PyPI publish in step 4 has
   succeeded, so you never advertise a version that isn't installable).

Publishing a **final** release fires `.github/workflows/publish-changelog.yml`,
which opens **one** PR to review and merge (pre-releases are skipped):

- **`omnigent-site` `/releases/<version>`** — a per-version post mirroring the
  notes you just curated (PR refs and angle/brace characters are made MDX-safe for
  you).

To re-run either half for an already-cut tag: dispatch `draft-release-notes.yml`
with the `tag` (re-opens the CHANGELOG PR; it leaves the notes alone once the
release is published), or `publish-changelog.yml` with the `tag` (re-opens the
site post PR).

If the draft wasn't created (e.g. the workflow was disabled), do it manually:

```bash
gh auth switch --user <oss-account>
gh release create v0.2.0 --repo omnigent-ai/omnigent \
  --draft --verify-tag --generate-notes --title "v0.2.0"
# review/edit, then publish from the Releases page (or `gh release edit v0.2.0 --draft=false`)
```

---

## Patch release (e.g. `v0.2.1`)

Cherry-pick the fix onto the existing `branch-0.2`, then:

1. Confirm CI is green on `branch-0.2` after the cherry-pick
   (`gh run list --repo omnigent-ai/omnigent --branch branch-0.2 --status success --limit 1`).
2. Bump the three versions/pins + `uv.lock` to `0.2.1` (same hand-edit rules as above).
3. Stage explicitly, commit, and tag **on `branch-0.2`**:
   `git add <version files> && git commit -m "release: v0.2.1" && git tag v0.2.1 && git push origin branch-0.2 v0.2.1`.
4. Repeat steps 2–5.

`main` does **not** change for a patch, and a patch never needs a new
`branch-0.Y` — patches always ship from the existing minor branch.

---

## If a publish goes wrong (recovery)

**PyPI releases can't be deleted, only _yanked_**, and a version number once used
can never be reused. So:

- **TestPyPI failed / candidate is bad:** bump to the next number (don't reuse the
  version) and re-run — TestPyPI is disposable.
- **Prod publish partially succeeded** (e.g. two of three packages uploaded):
  **yank** the published version(s) on PyPI (each affected project → *Manage* →
  *Releases* → *Yank*) so installs don't resolve a half-published set, then cut the
  next patch with the fix. Don't try to overwrite — Trusted Publishing / `twine`
  rejects re-uploading an existing version.
- **GitHub Release** for a version you abandoned:
  `gh release delete vX.Y.Z --repo omnigent-ai/omnigent`, and drop the tag if it
  shouldn't exist (`git push origin :refs/tags/vX.Y.Z`); re-tag only the corrected
  commit.
- Publishing uses **OIDC Trusted Publishing (no stored secrets)**, so a failed run
  leaks nothing — just fix forward to the next version.
