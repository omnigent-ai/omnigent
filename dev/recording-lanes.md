# Recording lanes — how to film a bug's journey on each surface

Shared reference for **repro-agent** and **resolve-agent**. Both film the same
kinds of journeys on the same surfaces; only *which* clip they produce differs:

- **repro-agent** films the product journey — a `reproduced` facet yields a
  **`before`** clip showing the live bug; an `already_fixed` facet yields a
  **`fixed`** clip showing the correct user-visible outcome.
- **resolve-agent** films the product journey after the fix as the **`after`**
  clip (the human-visible half of the fail→pass proof). The recovered test
  verifies the result separately. On the review path it films the reviewed PR
  head.

Everything else below — which surface to drive, how to stand the recorder up, and
the per-surface mechanics — is identical for both. Each agent's own AGENTS.md says
which clip `kind` it produces and where it goes; this file is the how.

## What is expected to yield a recording

A `web` / `mobile` / `terminal` / `cli` / `desktop` facet is expected to yield a recording:
drive it on that surface and film it. Only an `api` facet — a failure no user
observes on any surface (a wrong value in a response, an internal state a pytest
asserts) — legitimately has no recording; `recordings: []` is correct there and is
not a gap. Tests may drive and verify the journey, but the clip itself must show
the product surface and user-visible outcome, never the test process, pytest
output, assertions, logs, or a synthetic evidence-summary slide.

**One verdict-appropriate clip per facet — nothing else.** Every recording must
correspond to a facet, and its `kind` must match that facet's verdict. Do **not**
add a "contrast"/"control" clip of a *different*, working journey next to a
reproduced facet — an unrequested extra video with no facet behind it only
confuses the reader.

Recording is **best-effort**: if the tooling below is missing, or a user-facing
facet's state is genuinely unreachable in this harness, keep `recordings: []` for
that facet and **name the specific blocker** in `recording_unavailable_reason` —
an empty recordings list on a `web`/`mobile`/`terminal`/`cli`/`desktop` facet must
always come with a concrete reason, never a silent skip. Never let recording block
or distort the work itself, and never fabricate a hollow journey that doesn't
reach the failure just to produce a video. Missing or rejected footage never
blocks the verdict, fix, or PR.

Every declared recording includes `capture_mode`, which records how the product
surface was filmed. Use `playwright_ui` for browser-page capture, `electron` for
the desktop harness, `vhs_user_command` for a VHS tape that types the real user
command, and `screen` for a direct device/screen capture. Do not declare a raw
recorder output: copy the selected clip to a stable `<kind>-<facet>.<ext>` path
first, then declare only that path. Undeclared media remains available in the CI
artifact for debugging but is not attached to Linear or a PR.

## The recorder needs its own local server

A `web`/`terminal` recording runs the `tests/e2e_ui/` suite, which drives a live
server. Do **not** point it at the app you were launched against: that app is
typically auth-gated (a Databricks Apps deployment bounces an unauthenticated
Playwright to SSO), so the recorder can't drive it. Let the `tests/e2e_ui/`
fixtures **spawn their own local server + runner** (the default when no
`--ui-base-url` is passed).

## Build the SPA up front — before you run the recorder, not during it

The `tests/e2e_ui/` server serves the SPA from `omnigent/server/static/web-ui/`,
which starts empty in your worktree (the deploy's pre-built bundle lives in the
serving layer, not the source tree). The suite *can* build it lazily on first
boot, but that build pins the machine's cores for a few minutes **while** the
spawned runner is trying to tunnel and go online — on a busy CI box the runner
misses its online deadline and the fixture reports `online: false`, which looks
like an environment failure but is really the build starving the boot. So
**always build the SPA first as its own step**, then run the recorder.

When you are yourself running inside a server-spawned runner (the `--server`
path — check with `echo "$OMNIGENT_RUNNER_ID"`, which is set there), you **must
strip the ambient runner/host env vars** so the fixture's own runner starts clean.
This is not optional: those vars (`OMNIGENT_RUNNER_ZYGOTE*` FDs,
`OMNIGENT_RUNNER_ID`, tunnel/host tokens) leak into the spawned child, make it take
the zygote-fork path and block on control FDs it doesn't have, so it hangs with an
empty `runner.log` and stays `online: false`. A bare `pytest`/`uv run pytest` with
`OMNIGENT_RUNNER_ID` still set is the single most common way the after-clip
silently fails to record. Strip them with `env -u`:

```bash
# Point npm/pnpm at the Databricks registry proxy first (see
# dev/agent-environment.md) or this install fails with an auth error.
pnpm --filter web install && pnpm --filter web run build   # once, up front
env -u OMNIGENT_RUNNER_ID -u OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN \
    -u OMNIGENT_RUNNER_TUNNEL_TOKEN -u OMNIGENT_RUNNER_PARENT_PID \
    -u OMNIGENT_RUNNER_ISOLATE_SESSION -u OMNIGENT_RUNNER_WORKSPACE \
    -u OMNIGENT_HOST_ID -u OMNIGENT_HOST_TOKEN -u OMNIGENT_HOST_NAME \
    -u RUNNER_SERVER_URL -u OMNIGENT_REMOTE_AUTH_TOKEN \
    $(env | grep -oE '^OMNIGENT_RUNNER_ZYGOTE[A-Z_]*' | sed 's/^/-u /' | tr '\n' ' ') \
    OMNIGENT_E2E_RECORD_DIR="$PWD/recordings/<slug>/raw" \
    pytest <test_path> --screenshot on --output recordings/<slug>
```

Only *after* the SPA is built and the env is stripped is an `online: false` a
real environment limit. If you see `online: false` and you have **not** stripped
the leaked vars (`OMNIGENT_RUNNER_ID` was set in your shell), that failure is your
own un-stripped env, not the harness — re-run with the `env -u` prefix before
concluding anything; do **not** file it as an environmental blocker or a "runner
not coming online" env problem. Once the SPA is built, the env is stripped, and the
spawned runner still doesn't reach `online: true` within the fixture's timeout,
capture the tail of the fixture's `runner.log`, treat that lane as genuinely
unreachable here, keep `recordings: []` for it, and say plainly **"recorder's test
server did not come online in time"** with the `runner.log` tail — noting whether
the log was empty (the leaked-env/zygote hang) or showed a later failure, so the
cause is named from what you observed rather than guessed.

## `web` facets

Run the authored Playwright test with recording on.

**Record via `OMNIGENT_E2E_RECORD_DIR`, not `--video on`.** `--video on` only
instruments pytest-playwright's own `page` fixture. Many e2e_ui tests (e.g. the
whole `tests/e2e_ui/start_session/` suite) drive Playwright *manually* —
`async_playwright()` + `browser.new_page()`/`new_context()` — instead of taking
the `page` fixture, and `--video on` records **nothing** for those: the flag never
sees their browser, so you get a green/red test but an empty `recordings/`.
Setting `OMNIGENT_E2E_RECORD_DIR` makes the e2e_ui conftest inject
`record_video_dir` into every page/context the test opens, so the journey is
filmed no matter how the test opened the browser. Playwright writes the `.webm` (a
random hash name) into that dir when the context closes. (If a test already
hard-codes its own `record_video_dir` — some authored reproductions do — that
explicit dir wins and the video lands there instead; check both locations.)

**Move the emitted clip to a stable name.** The video lands under
`OMNIGENT_E2E_RECORD_DIR` (or the test's own dir) as a random hash name; **move**
it (do not copy) to a stable `recordings/<slug>/<kind>-<facet>.webm` and delete
the leftover raw dir, so the same footage isn't collected twice. If that dir has
**no** `.webm` after the run, the recording genuinely didn't happen (the test
errored before opening a page, or the fixture never came online) — capture the
reason per the empty-recordings rule; never report a clip you didn't produce.

## `mobile` facets

The iOS/Android apps are thin native shells that load the *same* server-served SPA
in a WebView, so almost every mobile bug is a web-UI journey that only misbehaves
at a phone viewport or under touch (Enter vs. newline on a small composer, a
control that overflows off-screen, a touch-scroll that doesn't take). Film these on
the **web lane at a mobile device profile** — pytest-playwright ships Playwright's
device registry, so add `--device` to the same recorder invocation and the whole
context becomes mobile (phone viewport, `is_mobile`, touch, a mobile user-agent),
video included:

```bash
OMNIGENT_E2E_RECORD_DIR="$PWD/recordings/<slug>/raw" \
  pytest <test_path> --device "iPhone 13" --screenshot on --output recordings/<slug>
```

Pick a device whose form factor matches the report (`"iPhone 13"`, `"Pixel 7"`, an
`... landscape` variant). Everything else about the `web` lane applies unchanged
(build the SPA first, strip the runner env, record via `OMNIGENT_E2E_RECORD_DIR`,
move the emitted clip to a stable name). Author the test itself with touch/tap
interactions where the journey needs them. Stamp the facet `mobile` (it's a
mobile-surface bug) even though the recorder is the browser at a phone profile —
the surface is where the *user* sees it, not the tool that films it.

A **small minority** of mobile bugs live in native chrome the WebView profile
can't show — iOS safe-area / Dynamic Island insets, the system-browser OIDC hop,
the native setup screen. Those need a real simulator/emulator screen recording,
which no CI path provisions today; keep `recordings: []` for such a facet and name
the missing device toolchain in your evidence (a real environment limit, not a
`not_reproduced`). The authored test still ships.

## `terminal` facets

The pane renders inside the web app, so record it the same way as `web`: the
Playwright test drives the session page with the terminal view shown, and the
pane's contents land in the browser video. Save `tmux capture-pane -e` text dumps
alongside as machine-checkable evidence.

**For a native-harness pane** (claude/codex/cursor/goose/hermes/kiro/… — the bug
is in a real harness CLI's output), don't hand-roll the launch: the existing
render-parity tests already drive the *real* CLI against the mock LLM with the
terminal view shown, so **copy the closest one**
(`tests/e2e_ui/messages/test_native_<harness>_render_parity.py`, which use the
`native_<harness>_session` / `native_<harness>_mock_session` fixtures in
`tests/e2e_ui/conftest.py`) and adapt its scripted turns to your journey. These
tests skip when the harness CLI isn't installed; if the one your bug needs is
unavailable here, keep `recordings: []` and name the missing CLI in your evidence
(a real environment limit, not a `not_reproduced`).

## `cli` facets

Author/replay a VHS tape (`recordings/<slug>/journey.tape`) that replays the SAME
numbered journey steps as the PTY test: `Type`/`Enter` the user's commands,
`Wait /pattern/` on the observable outcome, with an
`Output recordings/<slug>/<kind>-<facet>.mp4` directive. Render with
`vhs recordings/<slug>/journey.tape`. If `vhs` is unavailable, still keep the
tape and note that rendering was skipped.

- **Boot any server/host the journey needs BEFORE the tape drives it** — don't
  make the tape do the slow startup. If the tape's own commands start the server,
  VHS's per-command timeout fires during the multi-second boot and the render dies
  half-way (the cli-lane analog of the web recorder-server race). Start the
  server/host as a prior shell step, then have the tape `Type` only the user's
  journey commands against the already-running process. Keep the tape's own steps
  fast (sub-second each) so no `Wait` straddles a boot.
- **Film the REAL command the user runs — not an API-call stand-in.** When the
  failure surfaces as a command's console output, the tape must run *that command*
  and capture *its* output, even when reproducing it needs a precondition you
  stage first. Do **not** substitute a `curl`/`httpx`/Python poke of the underlying
  endpoint — that films the mechanism, not the failure the user sees. Example: for
  "host 403s after its login expires", seed an **expired** `auth_tokens.json`
  (a past `expires_at`), then `Type` the actual `omnigent host --server <url>` and
  `Wait` for the console failure it prints. Only when the command genuinely can't
  be driven here (name why) fall back to `recordings: []`.
- **End the tape on the OUTPUT, not a fixed timer.** End on a `Wait /<pattern>/`
  matching the observable result line (the error text, the value, the exit
  message), with only a short trailing `Sleep` after it lands — never a bare
  `Sleep`/short total duration as the stop condition. The clip must show the
  outcome, not the moment before it.
- **A clip of the reproduction TEST running is NOT the journey.** If the tape
  won't render (server boot times out, `ttyd` missing, VHS unavailable), do
  **not** substitute a recording of `pytest … FAILS` / an `AssertionError`. That
  films the regression artifact, not the failure a user sees. Keep
  `recordings: []` and name the blocker; the authored test still ships, it just
  isn't the video.

## `desktop` facets (Electron shell)

When the failure lives in the desktop shell itself (the dead-end 401 fallback to
the setup page, in-window IdP rendering, the session-expiry reload, the
OAuth-popup / window-open policy, the native host-enrollment dialog) rather than
in the SPA, the browser tools and the `tests/e2e_ui/` Playwright lane can't reach
it: the defect is in Electron's main process, and Python Playwright has **no**
Electron API. Use the JS desktop lane in `web/electron/e2e/` instead — **copy the
reference test `desktop_connect.e2e.js`**, which launches the REAL packaged shell
under `_electron.launch({ recordVideo })` (via `desktopHarness.js`, which spawns
the same mock-LLM + `omnigent server` pair the Python suite does) and films the
actual window. Pass `serverUrl` to `launchDesktop` when the failure is *past*
connect (boot straight into the shell); omit it to film the connect/setup/fallback
flow itself. Run with `node --test e2e/desktop_<slug>.e2e.js` from `web/electron`,
after building the SPA. On a headless box wrap it in `xvfb-run -a` and set
`OMNIGENT_PW_NO_SANDBOX=1` so Electron's Chromium starts (repro-agent CI sets both
for you, and points `OMNIGENT_PYTHON` at the venv the harness spawns the server
with). This lane needs `electron` + `playwright` on disk (neither is in the fast
`web-test` CI path); the harness **skips** cleanly when they're absent, so if they
can't be installed here keep `recordings: []` and name the missing dep in your
evidence (a real environment limit, not a `not_reproduced`). See
`web/electron/e2e/README.md`.

## Finishing a clip

A recording must end on the outcome the user observes — the failure (wrong screen
state, bad output, error) for a `before` recording, or the correct end state for a
`fixed`/`after` one. Convert to `.mp4` with `ffmpeg` when available; `.webm`/`.gif`
are fine otherwise. Recordings are workspace artifacts exactly like the test —
leave them uncommitted; in CI the artifact bundle collects them.

For each recording, write a short **`caption`** in its handoff entry describing
**the actions that clip performs** — the ordered steps a viewer watches, ending in
what the clip shows: e.g. `"start a session → open the model picker → select the
catalog → picker shows raw IDs"`. This is what a reader sees under the video on
the ticket, so make it read like a journey, not a restatement of the bug title.
