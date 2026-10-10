// The desktop mic follows the server's dictation capability (GET /v1/info).
// Run from web/electron after building the SPA (see e2e/README.md):
//   OMNIGENT_PW_NO_SANDBOX=1 OMNIGENT_PYTHON=../../.venv/bin/python xvfb-run -a node --test e2e/desktop_dictation_availability.e2e.js

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  REPO_ROOT,
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
const RECORD_DIR =
  process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
  path.join(__dirname, "recordings", "desktop-dictation");
const PYTHON = process.env.OMNIGENT_PYTHON || "python3";

const COMPOSER_LABEL = "Describe a task to start a new session…";
const MIC_NAME = "Voice dictation";
// An engine name the server does not know always reports dictation_available:
// false; the empty default selects sherpa, which is available where its models are.
const UNAVAILABLE_ENGINE = "unavailable-for-test";
// Same handshake budget the button gives a server take (a cold model load).
const TAKE_TIMEOUT_MS = 40_000;
const TRANSCRIPT_TIMEOUT_MS = 15_000;

/** The sentence the server's fake engine transcribes. */
function fakeScript() {
  const result = spawnSync(
    PYTHON,
    ["-c", "from omnigent.server.dictation import FAKE_SCRIPT; print(FAKE_SCRIPT)"],
    { encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO_ROOT } },
  );
  assert.equal(result.status, 0, `could not read FAKE_SCRIPT: ${result.stderr}`);
  return result.stdout.trim();
}

async function serverInfo(serverUrl) {
  const response = await fetch(`${serverUrl}/v1/info`);
  assert.ok(response.ok, `GET /v1/info failed: ${response.status}`);
  return response.json();
}

/** Poll `probe` until it is truthy or the deadline passes; returns the last result. */
async function pollUntil(window, probe, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    // oxlint-disable-next-line no-await-in-loop -- Sequential polling by design.
    const result = await probe();
    if (result || Date.now() >= deadline) return result;
    // oxlint-disable-next-line no-await-in-loop -- Sequential polling by design.
    await window.waitForTimeout(250);
  }
}

/** True once the SPA's capability probe (GET /v1/info) has completed in this window. */
function capabilityProbeDone(window) {
  return window.evaluate(() =>
    performance.getEntriesByType("resource").some((entry) => /\/v1\/info(?:\?|$)/.test(entry.name)),
  );
}

/** Tear the shell down; every step runs even when an earlier one throws. */
async function teardownDesktop({ electronApp, stopDisplayCapture, userDataDir }, clipName) {
  // Stop filming first so the clip ends on the asserted state, not on teardown.
  await stopDisplayCapture().catch((err) => console.warn("stopDisplayCapture failed:", err));
  await electronApp.close().catch((err) => console.warn("electronApp.close failed:", err));
  try {
    return saveRecording(RECORD_DIR, clipName);
  } finally {
    fs.rmSync(userDataDir, { recursive: true, force: true });
  }
}

/** Boot the shell straight into the connected home composer. */
async function openHomeComposer(serverUrl, fakeMicPreload, clipName) {
  const launched = await launchDesktop({
    recordDir: RECORD_DIR,
    serverUrl,
    preload: [fakeMicPreload],
  });
  try {
    const composer = launched.window.getByLabel(COMPOSER_LABEL).first();
    await composer.waitFor({ state: "visible", timeout: 45_000 });
    return { ...launched, composer };
  } catch (err) {
    // Keep the launch error visible even if the teardown fails too.
    await teardownDesktop(launched, `${clipName}-launch-failed`).catch((teardownErr) =>
      console.warn("teardown after the failed launch also failed:", teardownErr),
    );
    throw err;
  }
}

describe(
  "desktop shell — composer mic follows server dictation",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let fakeMicPreload;

    before(() => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-dictation-"));
      fakeMicPreload = path.join(tmpDir, "fake-mic.cjs");
      fs.writeFileSync(
        fakeMicPreload,
        'require("electron").app.commandLine.appendSwitch("use-fake-device-for-media-stream");\n',
      );
    });

    after(() => {
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it("offers no mic when the server has no dictation", async () => {
      const clipName = "mic-hidden-without-server-dictation";
      const server = await spawnServer(fs.mkdtempSync(path.join(tmpDir, "no-dictation-")), {
        env: () => ({ OMNIGENT_DICTATION_ENGINE: UNAVAILABLE_ENGINE }),
      });
      let saved;
      try {
        const info = await serverInfo(server.serverUrl);
        assert.equal(info.dictation_available, false, "precondition: server offers no dictation");

        const desktop = await openHomeComposer(server.serverUrl, fakeMicPreload, clipName);
        try {
          const { window } = desktop;
          // The mic could only appear once the capability probe has resolved.
          const probed = await pollUntil(window, () => capabilityProbeDone(window), 15_000);
          assert.ok(probed, "the SPA never completed its GET /v1/info capability probe");
          // Watch for a mic through the whole hold that the clip shows, not at one instant.
          const mic = window.getByRole("button", { name: MIC_NAME });
          const appeared = await pollUntil(window, async () => (await mic.count()) > 0, 3000);
          assert.equal(appeared, false, "a mic was offered although no dictation path can work");
        } finally {
          saved = await teardownDesktop(desktop, clipName);
        }
      } finally {
        await server.close();
      }
      assert.ok(saved && saved.length > 0, "no desktop recording was produced");
    });

    it("dictates through the server when it advertises dictation", async () => {
      const clipName = "mic-dictates-through-server";
      const script = fakeScript();
      const server = await spawnServer(fs.mkdtempSync(path.join(tmpDir, "fake-engine-")), {
        env: () => ({ OMNIGENT_DICTATION_ENGINE: "fake" }),
      });
      let saved;
      try {
        const info = await serverInfo(server.serverUrl);
        assert.equal(info.dictation_available, true, "precondition: server offers dictation");

        const desktop = await openHomeComposer(server.serverUrl, fakeMicPreload, clipName);
        try {
          const { window, composer } = desktop;
          const mic = window.getByRole("button", { name: MIC_NAME }).first();
          await mic.waitFor({ state: "visible", timeout: 15_000 });
          await mic.click();

          const listening = await pollUntil(
            window,
            async () => (await mic.getAttribute("aria-pressed")) === "true",
            TAKE_TIMEOUT_MS,
          );
          assert.ok(
            listening,
            `the take never started (title: ${await mic.getAttribute("title")})`,
          );

          // The fake engine finalizes its script after ~0.5 s of audio.
          const transcribed = await pollUntil(
            window,
            async () => (await composer.inputValue()).includes(script),
            TRANSCRIPT_TIMEOUT_MS,
          );
          assert.ok(
            transcribed,
            `transcript did not land; composer: ${await composer.inputValue()}`,
          );

          await mic.click();
          const stopped = await pollUntil(
            window,
            async () => (await mic.getAttribute("aria-pressed")) === "false",
            TAKE_TIMEOUT_MS,
          );
          assert.ok(stopped, "the take did not stop");
          assert.ok((await composer.inputValue()).includes(script), "stopping clobbered the text");
          // Hold the final state on camera so the clip ends on the asserted result.
          await window.waitForTimeout(2000);
        } finally {
          saved = await teardownDesktop(desktop, clipName);
        }
      } finally {
        await server.close();
      }
      assert.ok(saved && saved.length > 0, "no desktop recording was produced");
    });
  },
);
