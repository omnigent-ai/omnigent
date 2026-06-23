# UI diff snapshot tests

One committed visual-regression baseline per page, each captured full-viewport at
1280×800 with the color scheme pinned to `light`, gated together in CI.

Each page's data calls are stubbed via `page.route` with fixed fixtures, so the
rendered view is a pure function of the committed bundle and needs no element
masking. `live_server` still serves the SPA bundle; only `/v1/info` / `/v1/me`
reach the real (and deterministic) server. The shared bits (fixed viewport +
light palette, the JSON-route helper, the pre-capture fonts/caret settle) live in
[`conftest.py`](conftest.py), so each `test_*_snapshot.py` only declares its own
stubs.

Pages covered:

- **Empty landing (`/`)** — the open left sidebar plus the `NewChatLandingScreen`
  ("What should we do?") hero and composer.
  [`test_landing_snapshot.py`](test_landing_snapshot.py)
- **Chat conversation (`/c/{id}`)** — a fully-mocked one-turn transcript (user
  question + assistant markdown reply) rendered as message bubbles, with the
  composer below. [`test_chat_snapshot.py`](test_chat_snapshot.py)

Baselines are committed under `snapshots/<test_module>/<test_name>/<name>[chromium][linux].png`.

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

The check **`UI Snapshot (visual baselines)`** blocks merges only if it's listed in
the repo's required-checks set (branch protection / `.github/scripts/merge-ready`,
which is generated and synced separately). Until it's added there it's an
**advisory** red check — visible, but not enforced. Registering it as required is
a one-line change to that synced config, outside this directory.

## How the gate behaves

- On every PR, `ui-snapshot.yml` renders each page and compares it to its
  committed baseline. Any pixel difference on any page fails the check.
- **Every run (pass or fail)** uploads one artifact and links it in the job
  summary, so the screenshots are always one click away:
  `ui-snapshot-<run_id>` carries this run's renders (`snapshots/`); on a mismatch
  `snapshot_failures/` also holds the `expected_` (baseline), `actual_`
  (current) and `diff_` PNGs for each failing page. That single artifact is
  baseline + current + diff.
- The baselines are **never** changed by the compare gate. The only ways to
  change them are the update flows below.

## Updating the baseline

When a UI change is intentional, pick whichever path fits — all render in the
pinned image, so the result matches the gate. The label and Docker paths rewrite
**only** the baselines that drift (or are missing) and leave already-passing ones
byte-for-byte untouched. Review each changed image before committing.

### Same-repo branch — label the PR (recommended)

1. Push your branch and open the PR.
2. Add the **`update-ui-snapshot`** label.
   [`ui-snapshot-update.yml`](../../../.github/workflows/ui-snapshot-update.yml)
   re-renders in the same pinned image, regenerates only the baselines that drift
   (or are missing) — passing ones are left untouched — and commits the changed
   PNGs back to your branch, then removes the label and comments the result.
3. **Review the committed PNG(s)** in the bot's commit.
4. The bot pushes with the `OMNIGENT_BOT_APP` token, so the push re-fires the
   PR's checks automatically — no manual re-run. (If the App isn't configured it
   falls back to `GITHUB_TOKEN`, which won't re-trigger CI; the bot's comment
   says so and you push any commit to re-run.)

This works for **same-repo branches only** — Actions tokens can't push to a fork.

### Anywhere with Docker — regenerate locally (works for forks)

```bash
tests/e2e_ui/visual/regen_baseline_docker.sh
```

This renders inside the exact pinned image CI uses, so the PNGs it writes match
the gate byte-for-byte. Only Docker is required (it builds the SPA in a Node
container, then renders the suite and rewrites only the baselines that drift —
passing ones stay untouched). **Review the image(s)**, then commit and push —
your push re-runs the checks. Pass `--skip-build` to reuse an existing `ap-web`
build.

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

## Adding a new page snapshot

Each page is one `@pytest.mark.visual` test in its own `test_<page>_snapshot.py`.
The gate, the update flows, and the artifact already cover every test in this
directory, so a new page is just a test + its committed baseline — no workflow
changes.

1. Add `test_<page>_snapshot.py`. Take `snapshot_page` (fixed viewport + light
   palette), `live_server`, `fulfill_json`, `settle_for_snapshot`, and
   `assert_snapshot` from [`conftest.py`](conftest.py).
2. `page.route`-stub **every** call the page makes so the view is a pure function
   of the bundle — no real backend data, no live stream. Drive any dynamic data
   from fixed fixtures; the chat test ([`test_chat_snapshot.py`](test_chat_snapshot.py))
   is the worked example (mocked session/items/agent/health + a `[DONE]` stream).
   Watch for non-determinism: relative timestamps, streaming, working shimmers,
   randomized ids.
3. Navigate, wait for the page's content to finish painting (a stable selector,
   not a timer), call `settle_for_snapshot(page)`, then `assert_snapshot(page)`.
4. Generate the baseline in the pinned image — label the PR or run
   `regen_baseline_docker.sh` — then **review the PNG** and commit it.

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
