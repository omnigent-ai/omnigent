# Recording lanes — how to film a bug's journey on each surface

Shared reference for **repro-agent**, **resolve-agent**, and **feature-agent**.
They film the same kinds of journeys on the same surfaces; only *which* clip
they produce differs:

- **repro-agent** films the product journey — a `reproduced` or `likely_repro`
  facet yields a **`before`** clip showing the live bug (on the stand-in, when
  one was driven); an `already_fixed` facet yields a **`fixed`** clip showing
  the correct user-visible outcome.
- **resolve-agent** films the product journey after the fix as the **`after`**
  clip (the human-visible half of the fail→pass proof). The recovered test
  verifies the result separately. On the review path it films the reviewed PR
  head.
- **feature-agent** films each covered CUJ as a **`demo`** clip, with the CUJ ID
  as the facet. Its demo is required, not best-effort; its `feature-demo` skill
  replaces "Internal/API-only results" and "If recording is blocked" below.

Everything else below — which surface to drive, how to stand the recorder up, and
the per-surface mechanics — is identical for all of them. Each agent's own AGENTS.md says
which clip `kind` it produces and where it goes; this file is the how.

The recording driver may be an existing test or a temporary journey script,
separate from the selected regression coverage. Retain its source and command
as evidence for before/after replay. Recording does not require committing the
driver as a permanent test.

## When to record

Record the user action and the product's response:

- **Web, mobile, or desktop:** show the user interacting with the product and
  the result on screen.
- **CLI or terminal output:** record the real command and its output, even if the
  output is only an error message, hint, or status line.
- **Internal/API-only results:** written evidence is enough when no user
  interface shows the result. Set `recordings: []`, describe what you observed,
  and explain how you checked it.

For example, if `omnigent host` prints the wrong error after login expires,
record running that command with an expired login and showing its output. See
the `cli` section below for setup steps.

## What the clip must show

- Show the actual product behavior. Tests may drive the interaction and verify
  the result, but the clip must show the product itself.
- Do not substitute a video of pytest output, assertions, test source, debug
  logs, or a slide describing what happened. Do not invent a different journey
  just to produce a video.
- Record one clip per facet, with a `kind` that matches its verdict. Do not add
  unrelated working examples as extra clips.
- An absence is filmable. When the bug is that nothing happens, show the
  triggering action and the screen staying in the stale state, with whatever
  error the product does show, for longer than a healthy response takes.

## If recording is blocked

Recording is **best-effort**:

- If a required tool is missing or the product cannot reach the state you need
  to show, set `recordings: []` for that facet. Name the specific blocker in
  `recording_unavailable_reason`, such as missing `vhs` or `ttyd`, or a server
  that cannot start.
- A blocker is the observed result of a recorder run. When your environment
  check found the recorder available, point it at the facet's lane before
  declaring the facet blocked, and name the command you ran and its observed
  error. A reason that names no attempted recorder run — "the recorder was not
  stood up", a multi-process journey, a flaky boot, a bug that is an absence —
  is insufficient.
- Text-only CLI output is not a reason to skip recording. A missing recording
  from an earlier run is not a reason either.
- Do not block the verdict, fix, or PR because footage is missing or rejected.
  Explain the gap and continue with the available evidence.

## File names and capture modes

Every declared recording includes `capture_mode`, which records how the product
surface was filmed. Use `playwright_ui` for browser-page capture, `electron` for
the desktop harness, `vhs_user_command` for a VHS tape that types the real user
command, and `screen` for a direct device/screen capture. For every lane, save
the selected raw capture as `recordings/<slug>/raw/<kind>-<facet>.<ext>`;
reserve `recordings/<slug>/<kind>-<facet>.mp4` for the captioned render.
Declare only the finished clip after following "Finishing a clip" below.
Undeclared media remains available in the CI
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

Run the selected Playwright journey driver with recording on.

**Record via `OMNIGENT_E2E_RECORD_DIR`, not `--video on`.** `--video on` only
instruments pytest-playwright's own `page` fixture. Many e2e_ui tests (e.g. the
whole `tests/e2e_ui/start_session/` suite) drive Playwright *manually* —
`async_playwright()` + `browser.new_page()`/`new_context()` — instead of taking
the `page` fixture, and `--video on` records **nothing** for those: the flag never
sees their browser, so you get a green/red test but an empty `recordings/`.
Setting `OMNIGENT_E2E_RECORD_DIR` makes the e2e_ui conftest inject
`record_video_dir` into pytest-playwright's `page`/`context` fixtures and into
every page/context the test opens through the async or sync API, so the journey is
filmed no matter how the test opened the browser. Playwright writes the `.webm` (a
random hash name) into that dir when the context closes. (If a test already
hard-codes its own `record_video_dir` — some authored reproductions do — that
explicit dir wins and the video lands there instead; check both locations.)
For pytest-playwright fixtures, `--video` takes precedence over the environment
variable: clips go to its output directory, and `--video retain-on-failure`
discards passing-test videos.

**The clip ends with the test body, not with fixture teardown.** Playwright
writes the video when the recorded context closes, and pytest tears fixtures
down in reverse setup order: the `context` behind `page` is set up before the
session fixtures a test lists after it, so it closes *last* — after those
fixtures have deleted the session or stopped the runner. A clip that keeps
rolling through teardown ends on the SPA's teardown state instead of the state
the test asserted: an `after` clip on the pane greyed out behind "Bridge closed:
terminal session ended", a `before` clip on a search palette that drops to "No
results found" as the fixture deletes the sessions it had just listed. When
recording is requested (`OMNIGENT_E2E_RECORD_DIR`, `--video`, or a
`record_video_dir` in `browser_context_args`), the e2e_ui conftest therefore
closes the pytest-playwright context as soon as the test body finishes, before
any fixture teardown runs. A test that opens its
own pages or contexts must close them itself before its body returns, for the
same reason, and a fixture that touches `page` after `yield` must skip that
work when `page.is_closed()`. Recording configured with a
`@pytest.mark.browser_context_args(record_video_dir=...)` marker follows the
same stop point.

**Move the emitted clip to a stable name.** The video lands under
`OMNIGENT_E2E_RECORD_DIR` (or the test's own dir) as a random hash name; **move**
it (do not copy) to `recordings/<slug>/raw/<kind>-<facet>.webm`. Keep that source
for caption rendering below and declare only the selected finished clip, so raw
footage is retained as supporting evidence rather than attached twice. If that dir has
**no** `.webm` after the run, the recording genuinely didn't happen (the test
errored before opening a page, or the fixture never came online). For a test
using the `page` fixture, retry with `--video on` and check pytest's `--output`
directory before declaring the lane unfilmable. A clip there indicates a recorder
configuration problem. Then capture the reason per the empty-recordings rule;
never report a clip you didn't produce.

**Don't poll clipped or element screenshots while filming.** Chromium takes
`page.screenshot(clip=…)` and `locator.screenshot()` by resizing the view to the
clip for the capture, and the recorder films that resized view: a pixel probe
polled during the journey leaves the footage mostly grey with a shrunken or
magnified strip of the page. The `tests/e2e_ui/` conftest turns a clipped
`page.screenshot` on a recorded Chromium page into a full-viewport capture
cropped to the clip, so viewport clip probes there are safe. `full_page=True`
captures are not covered and can still resize the recorded view. For element
screenshots, or a Playwright script outside that suite, read pixels via
`page.evaluate` (e.g. a canvas
`toDataURL()`) or capture from a second, unrecorded context instead.

Clipped JPEGs on recorded Chromium pages are encoded with Pillow, so they are
not byte-identical to Chromium's native JPEG output. Unsupported formats and
malformed screenshot options use Playwright's native validation and errors.

**Maintenance:** Context closure before fixture teardown and screenshot crop
rounding rely on pytest-playwright and Chromium internals; after Playwright,
pytest-playwright, or Chromium upgrades, rerun
`tests/e2e_ui/test_recorded_page_clip_screenshots.py` and
`tests/e2e_ui/test_recording_stops_before_teardown.py` as canaries.

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
`not_reproduced`). Keep the available test evidence and selected regression
coverage; missing footage does not change which tests belong in the PR.

## `terminal` facets

The pane renders inside the web app, so record it the same way as `web`: the
Playwright test drives the session page with the terminal view shown, and the
pane's contents land in the browser video. Save `tmux capture-pane -e` text dumps
alongside as machine-checkable evidence.

**For a native-harness pane** (claude/codex/cursor/goose/hermes/kiro/… — the bug
is in a real harness CLI's output), don't hand-roll the launch: the existing
render-parity tests already drive the *real* CLI against the mock LLM with the
terminal view shown, so **reuse or adapt the closest one**
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
`Output recordings/<slug>/raw/<kind>-<facet>.mp4` directive. Create the `raw/`
directory first. Render with
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
  `recordings: []` and name the blocker. Keep the test evidence separately and
  commit only the selected permanent regression coverage.

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
`fixed`/`after` one. Keep its capture format under `raw/`; the caption helper
produces the final `.mp4`, so no preliminary conversion is needed.
Recordings are workspace artifacts exactly like the test —
leave them uncommitted; in CI the artifact bundle collects them.

Check the clip's last frame before captioning it. Every clip must stop on the
demonstrated state its caption describes — the failure for a `before` clip, the
healthy state for a `fixed`/`after` clip; a final frame that shows teardown — the
pane greyed out behind "Bridge closed: terminal session ended", a deleted
session, a result list emptied by fixture cleanup — means the recording outlived
the test body. Fix the stop point and re-record; do not caption around it.

Base screen claims on inspected frames or visible-state assertions from that
recording. Backend logs and transcript storage can differ from what the UI has
already displayed. Keep backend findings in `evidence`; describe the visible
outcome in the caption. Check each claimed moment, especially claims that text
or controls are absent. If the frames contradict the caption, correct it or
re-record.

Verify that each captioned element is in the recorded viewport and unobstructed.
Quote only text the viewer can see: DOM attributes and hidden accessibility
labels do not establish what the screen displayed. Describe icon-only controls
by their visible appearance. If frame inspection is unavailable, disclose that
and limit claims to states the recording driver actually verified.

A driver check covers only the element it targets. The screen can show one
state in several places: a session's activity appears as the chat pane's
working indicator, a spinner on its sidebar row, and a sub-agent count badge.
An absence check on the chat pane's indicator supports "the chat pane shows no
working indicator", not "the session shows no activity". Check every such place
before claiming that none shows the state.

### Timed captions inside the video

For every newly captured clip, **burn in step-by-step captions before declaring
the recording**. This applies to Repro's `before`/`fixed` clips and Resolve's
`after` clips on both the author and review paths, across all capture surfaces.
Keep recovered before-clips unchanged. The handoff's prose `caption` appears
below the attachment; it does not replace captions visible during playback.

1. Watch the raw clip and note timestamps relative to its first frame (seconds).
   Timing logged by the driver can help, but check it against the actual video;
   browser creation, navigation and recording do not necessarily start together.
2. Write `recordings/<slug>/<kind>-<facet>.cues.json`, a JSON array of
   `{"start": 0, "end": 3.5, "text": "1. Open the model picker."}` objects.
   Use ordered, non-overlapping intervals within the source duration. Cover each
   meaningful action and the final observed result; leave text up long enough to
   read (usually 2–5 seconds). Use short, plain sentences, at most three lines.
   Whitespace, including explicit newlines, is normalized; the renderer wraps
   text to fit the caption band automatically.
   If the action is too fast to explain, re-record with a brief natural pause.
3. Explain the setup that changes how to interpret the result (OS/version,
   scripted model, injected failure, shortened timeout) in an opening cue.
   Then describe what happens now: "2. Select the catalog", "3. The picker
   shows raw IDs". End with the **observed** result for this attempt, including
   limitations. A passing attempt is not proof that a reported bug never occurs.
   Do not claim that a permission was bypassed, a command ran, or a fix worked
   unless the footage/evidence establishes it. Label any still-image close-up,
   speed change or cut; the helper below makes none of these edits.
4. Render with the shared helper (Python 3.10+ standard library; requires `ffmpeg`
   with `drawtext`, `ffprobe`, and DejaVu Sans Mono; on Ubuntu install `ffmpeg`
   and `fonts-dejavu-core`):

   ```bash
   python3 dev/caption_recording.py \
     --source recordings/<slug>/raw/<kind>-<facet>.webm \
     --cues recordings/<slug>/<kind>-<facet>.cues.json \
     --output recordings/<slug>/<kind>-<facet>.mp4
   ```

   For VHS or device captures already in MP4, use
   `--source recordings/<slug>/raw/<kind>-<facet>.mp4` with the same output path.
   For Electron or direct screen captures, move the selected capture under
   `raw/` first, preserving its extension.

   Run the helper from the Omnigent tools checkout if the target repository is
   different. It pads below the original screen so captions do not cover controls
   or output, retains timing and audio, and refuses to overwrite existing files.
   Media probes time out after 30 seconds and rendering after 10 minutes by
   default. For long or high-resolution clips, increase `--render-timeout`
   (seconds); a stalled encoder still reports a bounded captioning failure.
   Always render from raw footage; choose a fresh output path when revising cues.
   Keep the raw clip, cue JSON and render command as uncommitted workspace
   evidence. Once the selected capture is under `raw/`, retain it for rerendering;
   do not delete it as a duplicate or declare it alongside the finished clip.
5. Play the result and inspect the opening, each caption transition, and the
   final frame. Confirm text is legible, fits in the band, matches the action's
   timing, and describes only what was observed. Declare only the inspected,
   captioned MP4 under the stable `<kind>-<facet>.mp4` name in the handoff and
   PR Demo. CI's later media normalization preserves the burned-in text.

Captioning is best effort, like capture. If the renderer, font or filter is
unavailable, or rendering fails and cannot be repaired, copy the usable source
from `recordings/<slug>/raw/<kind>-<facet>.<ext>` to
`recordings/<slug>/uncaptioned-<kind>-<facet>.<ext>`, preserving its format and
the raw source. Use a new destination if that fallback name already exists;
never overwrite another recording. Inspect the copy, then declare only that
actual fallback path and matching `format` in the handoff and PR Demo. Do not
declare the failed render's path or the raw source as an additional recording.
Explain the captioning blocker in `evidence` and label the attachment as
uncaptioned. This fallback does not occupy the final captioned MP4 path.
Do not discard valid footage or block the verdict, fix, or PR just because
captions could not be rendered. Do not claim an unrendered cue file is a
captioned recording.

For each recording, also write a short **`caption`** in its handoff entry describing
**the actions that clip performs** — the ordered steps a viewer watches, ending in
what the clip shows: e.g. `"start a session → open the model picker → select the
catalog → picker shows raw IDs"`. This is what a reader sees under the video on
the ticket, so make it read like a journey, not a restatement of the bug title.
