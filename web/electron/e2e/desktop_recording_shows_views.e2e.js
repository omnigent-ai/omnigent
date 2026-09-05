// Desktop-shell recording lane: the saved primary clip must show the journey
// content rendered inside the shell's embedded browser views.
//
// Journey: boot the shell connected to a local server → a conversation opens an
// embedded browser view and paints a distinctive solid-green banner (standing in
// for any user-visible outcome that happens inside a view) → the lane saves its
// recording → a viewer opens the primary saved clip expecting to watch that
// journey. The clip must actually contain the banner. Playwright's per-page
// `recordVideo` films each webContents separately: the shell window's own page
// becomes the largest clip (which `saveRecording` promotes to the bare primary
// name), while every `WebContentsView` composited over the window is filmed as
// its own contextless clip — so the primary clip silently omits the very content
// the recording claims to show.
//
// Run from web/electron after building the SPA:
//   OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_recording_shows_views.e2e.js

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
const RECORD_DIR = path.join(__dirname, "recordings", "desktop-recording-shows-views");

/** Whether ffmpeg is on PATH — needed to decode the recorded clip's frames. */
function ffmpegAvailable() {
  try {
    return spawnSync("ffmpeg", ["-version"], { stdio: "ignore" }).status === 0;
  } catch {
    return false;
  }
}

const skip = !deps.ok
  ? `missing deps: ${deps.missing.join(", ")}`
  : ffmpegAvailable()
    ? false
    : "ffmpeg not available to decode the recording";

/**
 * Fraction of banner-green pixels in every sampled frame of `clip`, decoded
 * via ffmpeg to raw RGB so no image library is needed. Per-pixel (not a
 * whole-frame mean) so the banner is found whether it fills the frame (a
 * per-page clip) or covers only the view's region of a composited display
 * capture.
 *
 * @param {string} clip Path to a video file.
 * @returns {number[]} One banner-green pixel fraction per sampled frame.
 */
function bannerFractions(clip, fps = 2, width = 160, height = 120) {
  const out = spawnSync(
    "ffmpeg",
    [
      "-v",
      "error",
      "-i",
      clip,
      "-vf",
      `fps=${fps},scale=${width}:${height}`,
      "-f",
      "rawvideo",
      "-pix_fmt",
      "rgb24",
      "pipe:1",
    ],
    { maxBuffer: 1024 * 1024 * 1024 },
  );
  assert.equal(out.status, 0, `ffmpeg failed on ${clip}: ${out.stderr}`);
  const buf = out.stdout;
  const frameBytes = width * height * 3;
  const fractions = [];
  for (let off = 0; off + frameBytes <= buf.length; off += frameBytes) {
    let green = 0;
    for (let i = off; i < off + frameBytes; i += 3) {
      const r = buf[i];
      const g = buf[i + 1];
      const b = buf[i + 2];
      if (g > 90 && g > r * 1.4 && g > b * 1.4) green += 1;
    }
    fractions.push(green / (frameBytes / 3));
  }
  return fractions;
}

// Drive the preload-exposed browser APIs from the shell renderer (same helpers
// as desktop_cookie_isolation.e2e.js — they run in the SPA window's context).
async function openView(window, conversationId, url) {
  return window.evaluate(
    ([cid, u]) =>
      window.omnigentDesktop.browserOpenOrNavigate(cid, u, {
        x: 40,
        y: 80,
        width: 900,
        height: 520,
      }),
    [conversationId, url],
  );
}

async function execInView(window, conversationId, js) {
  return window.evaluate(
    ([cid, code]) => window.omnigentDesktop.browserExecute(cid, code),
    [conversationId, js],
  );
}

async function setActive(window, conversationId) {
  return window.evaluate((cid) => window.omnigentDesktop.browserSetActive(cid), conversationId);
}

// Poll until the view has actually LANDED on the target origin, racing each
// probe with a short timeout (an execute against a mid-navigation view can
// hang until the load completes).
async function waitForViewOnOrigin(window, conversationId, origin, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  let last = "no probe completed";
  for (;;) {
    // oxlint-disable-next-line no-await-in-loop -- Poll each navigation probe sequentially.
    const r = await Promise.race([
      execInView(window, conversationId, "location.origin"),
      new Promise((resolve) => {
        setTimeout(() => resolve(null), 3_000);
      }),
    ]);
    if (r && r.ok && String(r.result).startsWith(origin)) return;
    if (r) last = JSON.stringify(r);
    if (Date.now() > deadline) {
      throw new Error(`view ${conversationId} never landed on ${origin} — last: ${last}`);
    }
    // oxlint-disable-next-line no-await-in-loop -- Wait before starting the next probe.
    await window.waitForTimeout(500);
  }
}

describe(
  "desktop shell — the saved primary recording shows the browser-view journey",
  { skip },
  () => {
    let tmpDir;
    let server;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-e2e-"));
      server = await spawnServer(tmpDir);
    });

    after(async () => {
      if (server) await server.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it(
      "the primary saved clip contains the content painted inside the browser view",
      { timeout: 180_000 },
      async () => {
        const { electronApp, window, userDataDir, stopDisplayCapture } = await launchDesktop({
          recordDir: RECORD_DIR,
        });
        let saved;
        try {
          // Connect interactively (the same proven journey as
          // desktop_connect.e2e.js), then drive the browser APIs.
          const urlField = window.locator("#url");
          await urlField.waitFor({ state: "visible", timeout: 15_000 });
          await urlField.fill(server.serverUrl);
          await window.locator("#connect").click();
          await window
            .getByText("What should we build?")
            .waitFor({ state: "visible", timeout: 20_000 });
          await window.waitForFunction(
            () =>
              !!window.omnigentDesktop &&
              typeof window.omnigentDesktop.browserOpenOrNavigate === "function",
            null,
            { timeout: 20_000 },
          );

          // A conversation opens its embedded browser view and paints a
          // full-viewport solid-green banner — the user-visible content the
          // recording is supposed to capture.
          const site = `${server.serverUrl}/health`;
          const opened = await openView(window, "conv_A", site);
          assert.equal(opened.ok, true, `conv_A open failed: ${opened.error}`);
          await setActive(window, "conv_A");
          await waitForViewOnOrigin(window, "conv_A", server.serverUrl);
          const painted = await execInView(
            window,
            "conv_A",
            `document.body.innerHTML = '<div style="position: fixed; inset: 0; background: #1a7f37; color: white; font: 28px sans-serif; padding: 40px;">' +
              '<h1>Browser view content</h1>' +
              '<p style="font-size: 20px">This banner must appear in the saved desktop recording.</p></div>';
            location.origin;`,
          );
          assert.equal(painted.ok, true, `conv_A execute failed: ${painted.error}`);
          // Hold the banner on screen so the recording has ample frames of it.
          await window.waitForTimeout(5_000);
        } finally {
          await electronApp.close();
          await stopDisplayCapture();
          saved = saveRecording(RECORD_DIR, "recording-under-test");
          fs.rmSync(userDataDir, { recursive: true, force: true });
        }

        // The lane claims a desktop recording was produced…
        assert.ok(saved && saved.length > 0, "no desktop recording was produced");
        // …and the PRIMARY clip (the bare-name one a handoff declares and a
        // ticket attaches) must actually show the journey: the green banner
        // painted inside the view has to appear in its frames. Require it to
        // occupy a visible region (>= 5% of some frame), not a stray pixel.
        const primary = saved[0];
        const fractions = bannerFractions(primary);
        assert.ok(fractions.length > 0, `primary clip ${primary} decoded to zero frames`);
        const maxFraction = Math.max(...fractions);
        assert.ok(
          maxFraction >= 0.05,
          `the primary saved clip (${path.basename(primary)}) never shows the browser ` +
            `view's banner: the recording films only the shell window's own webContents, ` +
            `not the WebContentsView content composited over it ` +
            `(${fractions.length} frames sampled, max banner-green fraction ` +
            `${maxFraction.toFixed(4)})`,
        );
      },
    );
  },
);
