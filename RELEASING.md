# Releasing omnigent

omnigent ships **three PyPI packages that version-lock together**:

| Package | What it is |
| --- | --- |
| `omnigent` | core wheel (bundles the `ap-web` web UI) |
| `omnigent-client` | Python client SDK |
| `omnigent-ui-sdk` | terminal UI SDK |

`pip install omnigent==X` must resolve `omnigent-client==X` and
`omnigent-ui-sdk==X`, and the pins are **circular**, so every release builds and
publishes **all three at one identical version**.

## Where things run

- **Source of truth** (versions, tags, GitHub Releases): **`omnigent-ai/omnigent`**
  — use the `dhruv0811` (OSS) GitHub account.
- **Publishing to PyPI**: the central **secure-release repo**
  **`databricks/secure-public-registry-releases-eng`**, `omnigent` workflow —
  use the EMU account (`dhruv-gupta_data`). Publishing runs on hardened runner
  groups with **OIDC Trusted Publishing (no stored secrets)** and a **mandatory
  dependency scan**. This is why we don't publish from `omnigent-ai/omnigent`.

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

### 1. Cut the release branch + tag — `omnigent-ai/omnigent` (`dhruv0811`)

```bash
gh auth switch --user dhruv0811
git fetch origin && git checkout -b branch-0.2 origin/main
```

Set the release version in **all three** `pyproject.toml` files — the
`version` field **and** the cross-package `==` pins — plus `uv.lock`
(`0.2.0.dev0` → `0.2.0`):

- `pyproject.toml` (`version`, `omnigent-client==`, `omnigent-ui-sdk==`)
- `sdks/python-client/pyproject.toml` (`version`, `omnigent==`)
- `sdks/ui/pyproject.toml` (`version`, `omnigent-client==`)
- `uv.lock` — **hand-edit** the three `version = "…"` lines (omnigent,
  omnigent-client, omnigent-ui-sdk) and the one `specifier = "==…"`.
  **Do not run `uv lock`** locally: it rewrites every registry URL to the
  internal proxy and that leaks into the lockfile (breaks CI). The published
  lock must use `https://pypi.org/simple`.

```bash
git commit -am "release: v0.2.0"
git tag v0.2.0
git push -u origin branch-0.2 --tags        # pushing the tag drafts the GitHub Release (step 5)
```

Keep `main` from re-freezing — bump it to the next dev marker and push:

```bash
git checkout main
# set 0.2.0.dev0 -> 0.3.0.dev0 in the 3 pyprojects (+ pins) and uv.lock (hand-edit)
git commit -am "chore: bump main to 0.3.0.dev0"
git push
```

### 2. Dry-run the gates — secure repo (`dhruv-gupta_data`)

```bash
gh auth switch --user dhruv-gupta_data
gh workflow run omnigent.yml --repo databricks/secure-public-registry-releases-eng \
  -f ref=v0.2.0 -f destination=test-pypi -f dry-run=true
```

Runs build + dependency scan + the gates (lockstep version/pins, web-UI-in-wheel,
`twine check`, smoke-install) and the OIDC token exchange — **without uploading**.

### 3. Publish to TestPyPI + validate

```bash
gh workflow run omnigent.yml --repo databricks/secure-public-registry-releases-eng \
  -f ref=v0.2.0 -f destination=test-pypi -f dry-run=false

# validate in a clean venv (TestPyPI for our packages, real PyPI for deps):
python -m venv /tmp/omni-rc && /tmp/omni-rc/bin/pip install \
  -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple omnigent==0.2.0
/tmp/omni-rc/bin/omnigent --version    # expect 0.2.0; resolves client/ui-sdk ==0.2.0
```

### 4. Publish to PyPI (prod)

Requires **admin/maintain** on the secure repo; binds the per-package
`pypi-omnigent`, `pypi-omnigent-client`, `pypi-omnigent-ui-sdk` Trusted-Publisher
environments (may gate on reviewer approval). The prod path also re-verifies that
`ref` is exactly the `vX.Y.Z` tag and that the tag points at the built commit.

```bash
gh workflow run omnigent.yml --repo databricks/secure-public-registry-releases-eng \
  -f ref=v0.2.0 -f destination=pypi -f dry-run=false

uv tool install omnigent==0.2.0        # final sanity from real PyPI
```

> Note: the dispatch's `-f ref=v0.2.0` is the **omnigent source ref**; it is
> distinct from `gh workflow run --ref`, which selects the branch the *workflow
> definition* runs from (the secure repo's default).

### 5. Publish the GitHub Release — `omnigent-ai/omnigent` (`dhruv0811`)

Pushing the `v0.2.0` tag (step 1) triggered `.github/workflows/github-release.yml`,
which created a **draft** release with auto-generated notes (PRs since the
previous tag). Now:

1. Open <https://github.com/omnigent-ai/omnigent/releases> and find the `v0.2.0`
   draft.
2. **Verify and edit the notes** — lead with user-facing highlights, call out
   breaking changes and any upgrade steps, and trim noise from the auto-generated
   list. The notes are a draft, not the final word.
3. **Publish the release** (ideally only after the prod PyPI publish in step 4 has
   succeeded, so you never advertise a version that isn't installable).

If the draft wasn't created (e.g. the workflow was disabled), do it manually:

```bash
gh auth switch --user dhruv0811
gh release create v0.2.0 --repo omnigent-ai/omnigent \
  --draft --verify-tag --generate-notes --title "v0.2.0"
# review/edit, then publish from the Releases page (or `gh release edit v0.2.0 --draft=false`)
```

---

## Patch release (e.g. `v0.2.1`)

Cherry-pick the fix onto the existing `branch-0.2`, bump the three versions/pins
+ `uv.lock` to `0.2.1`, commit, tag `v0.2.1` on `branch-0.2`, push, then repeat
steps 2–5. `main` does not change for a patch.
