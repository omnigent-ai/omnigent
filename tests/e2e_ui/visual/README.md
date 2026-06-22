# UI diff snapshot tests

A single visual-regression baseline of the default empty view (`/`) — the open
left sidebar plus the `NewChatLandingScreen` ("What should we do?") hero and
composer, captured full-viewport at 1280×800 with the color scheme pinned to
`light` — gated in CI.

The landing's data calls (agent catalog, host list, session list, filesystem)
are stubbed via `page.route` with fixed fixtures, so the rendered view is a pure
function of the committed bundle and needs no element masking. `live_server`
still serves the SPA bundle; only `/v1/info` / `/v1/me` reach the real (and
deterministic) server.

- Test: [`test_landing_snapshot.py`](test_landing_snapshot.py)
- Baseline (committed): `snapshots/test_landing_snapshot/test_empty_landing_matches_baseline/test_empty_landing_matches_baseline[chromium][linux].png`
- Gate workflow: [`.github/workflows/ui-snapshot.yml`](../../../.github/workflows/ui-snapshot.yml)
- Local regen (Docker): [`regen_baseline_docker.sh`](regen_baseline_docker.sh)
- Plugin: [`pytest-playwright-visual-snapshot`](https://github.com/iloveitaly/pytest-playwright-visual-snapshot)

## Why a single pinned renderer

Screenshots differ across rendering environments (font rasterizer, hinting,
anti-aliasing), and no diff threshold can reconcile two rendering engines. So we
render everywhere in **one** environment: a digest-pinned Playwright image
(`mcr.microsoft.com/playwright/python`, which bakes in Chromium + fonts). CI
renders in it, and you can reproduce that exact render locally with Docker — see
[Updating the baseline](#updating-the-baseline). Because the renderer is the
image, your host OS doesn't matter; you just need Docker (or let CI do it).

The test is marked `@pytest.mark.visual`; the main e2e-ui suite (unpinned
`ubuntu-latest`) excludes it via `-m "not visual"`. Only `ui-snapshot.yml` runs
it.

## Is this check merge-blocking?

The check **`UI Snapshot (empty landing)`** blocks merges only if it's listed in
the repo's required-checks set (branch protection / `.github/scripts/merge-ready`,
which is generated and synced separately). Until it's added there it's an
**advisory** red check — visible, but not enforced. Registering it as required is
a one-line change to that synced config, outside this directory.

## How the gate behaves

- On every PR, `ui-snapshot.yml` renders the default `/` view and compares it to
  the committed baseline. Any pixel difference fails the check.
- **Every run (pass or fail)** uploads one artifact and links it in the job
  summary, so the screenshots are always one click away:
  `ui-snapshot-<run_id>` carries this run's render (`snapshots/`); on a mismatch
  `snapshot_failures/` also holds the `expected_` (baseline), `actual_`
  (current) and `diff_` PNGs. That single artifact is baseline + current + diff.
- The baseline is **never** changed by the compare gate. The only ways to change
  it are the update flows below.

## Updating the baseline

When a UI change is intentional, pick whichever path fits — all produce a PNG
rendered in the pinned image, so the regenerated baseline matches the gate.

### Same-repo branch — label the PR (recommended)

1. Push your branch and open the PR.
2. Add the **`update-ui-snapshot`** label.
   [`ui-snapshot-update.yml`](../../../.github/workflows/ui-snapshot-update.yml)
   regenerates the baseline with `--update-snapshots` in the same pinned image
   and commits the new PNG back to your branch, then removes the label and
   comments the result.
3. **Review the committed PNG** in the bot's commit.
4. The bot pushes with the `OMNIGENT_BOT_APP` token, so the push re-fires the
   PR's checks automatically — no manual re-run. (If the App isn't configured it
   falls back to `GITHUB_TOKEN`, which won't re-trigger CI; the bot's comment
   says so and you push any commit to re-run.)

This works for **same-repo branches only** — Actions tokens can't push to a fork.

### Anywhere with Docker — regenerate locally (works for forks)

```bash
tests/e2e_ui/visual/regen_baseline_docker.sh
```

This renders inside the exact pinned image CI uses, so the PNG it writes matches
the gate byte-for-byte. Only Docker is required (it builds the SPA in a Node
container, then renders the baseline). **Review the image**, then commit and push
— your push re-runs the checks. Pass `--skip-build` to reuse an existing
`ap-web` build.

### Fork PR without Docker — adopt the run's render

The failing compare run already rendered your change in the pinned image, so pull
that image into the baseline:

```bash
tests/e2e_ui/visual/update_baseline_from_pr.sh <pr-number>
```

It finds the PR's UI Snapshot run, downloads the artifact, and copies the
runner-rendered `actual_` PNG over the committed baseline. **Review the image**,
then commit and push. (Manual equivalent: download the `ui-snapshot-<run_id>`
artifact, take `snapshot_failures/.../actual_*.png`, and commit it over the
baseline path above.)

### Workflow dispatch (non-PR branches)

GitHub → Actions → **UI Snapshot** → **Run workflow**, set `ref` to your branch
(CLI: `gh workflow run ui-snapshot.yml -f ref=<your-branch>`). It runs with
`--update-snapshots` (intentionally fails); the regenerated PNG is in the
`ui-snapshot-<run_id>` artifact to download, review, and commit. Any collaborator
can dispatch against an arbitrary `ref`, but since the PNG is human-reviewed
before it lands, an unreviewed ref can't change the baseline on its own.

### Failure comments

Whenever the check fails (same-repo or fork),
[`ui-snapshot-fail-comment.yml`](../../../.github/workflows/ui-snapshot-fail-comment.yml)
upserts a PR comment pointing back to these paths. It runs as `workflow_run` so
it can comment without ever executing PR/fork code, which means it only activates
once merged to `main` (it does not fire on its own PR).

## Running locally without Docker (debugging only — never commit the result)

You can exercise the test on the host to debug it, but a baseline rendered
anywhere other than the pinned image will not match the gate, so **never commit
a PNG produced this way** — a stray `git add -A` would commit a wrong-renderer
baseline and break CI. Use the Docker path above to produce a committable PNG.

```bash
uv sync --extra all --extra dev
uv run playwright install --with-deps chromium
cd ap-web && npm ci --legacy-peer-deps && npm run build && cd ..
# First run with no baseline creates one (and fails); subsequent runs compare:
uv run pytest tests/e2e_ui/visual -m visual --ui-skip-build
```
