// Run after building web, on Linux with an X display:
//   OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_html_preview_zoom.e2e.js
// OMNIGENT_PYTHON selects the backend; OMNIGENT_DESKTOP_RECORD_DIR keeps a paced recording.

"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { desktopDepsAvailable, launchDesktop, saveRecording } = require("./desktopHarness");
const { eventually, startDesignBackend } = require("./desktopDesignPromptHarness");

const PYTHON = process.env.OMNIGENT_PYTHON || "python3";
const XTEST_KEYS = path.join(__dirname, "fixtures", "xtestKeys.py");
const RECORD_DIR = process.env.OMNIGENT_DESKTOP_RECORD_DIR;
const REPORT_PATH = "q3_report.html";
const REPORT = `<!doctype html><html><head><meta charset="utf-8"><title>Q3 report</title>
<style>body{font:16px system-ui;margin:24px;color:#17243a}table{border-collapse:collapse}
td,th{border:1px solid #ccd5e2;padding:6px 12px;text-align:left}</style></head><body>
<h1>Q3 revenue report</h1>
<p>Revenue grew 12% quarter over quarter, led by the EMEA region.</p>
<table><tr><th>Region</th><th>Revenue</th><th>Change</th></tr>
<tr><td>EMEA</td><td>$4.2M</td><td>+18%</td></tr>
<tr><td>Americas</td><td>$6.1M</td><td>+9%</td></tr>
<tr><td>APAC</td><td>$2.7M</td><td>+7%</td></tr></table>
</body></html>`;

const deps = desktopDepsAvailable();
// Accelerators only fire for OS key events, which this lane injects through XTEST.
const xtestReady =
  process.platform === "linux" &&
  Boolean(process.env.DISPLAY) &&
  spawnSync(PYTHON, [XTEST_KEYS, "--check"]).status === 0;

function pressKeys(...chords) {
  const result = spawnSync(PYTHON, [XTEST_KEYS, ...chords], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
}

// Step times relative to the recording start, for caption cues.
let recordingStart = Date.now();
function step(label) {
  if (RECORD_DIR)
    console.log(`step ${((Date.now() - recordingStart) / 1000).toFixed(1)}s ${label}`);
}

function pause() {
  return new Promise((resolve) => {
    setTimeout(resolve, RECORD_DIR ? 1_200 : 0);
  });
}

it(
  "zooms only the HTML preview for the zoom shortcuts pressed inside it",
  { skip: !deps.ok || !xtestReady, timeout: 240_000 },
  async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-preview-zoom-"));
    const recordDir = RECORD_DIR || path.join(tmpDir, "recordings");
    let backend;
    let app;
    try {
      backend = await startDesignBackend(tmpDir);
      const seeded = await fetch(
        `${backend.serverUrl}/v1/sessions/${backend.sessionId}/resources/environments/default/filesystem/${REPORT_PATH}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: REPORT, encoding: "utf-8" }),
        },
      );
      assert.ok(seeded.ok, `seeding the report: HTTP ${seeded.status}`);

      recordingStart = Date.now();
      app = await launchDesktop({ serverUrl: backend.serverUrl, recordDir });
      const { window: page, electronApp } = app;
      await electronApp.evaluate(({ BrowserWindow }) => {
        const win = BrowserWindow.getAllWindows().find((w) =>
          w.webContents.getURL().startsWith("http"),
        );
        win.setPosition(0, 0);
        win.setContentSize(1600, 900);
        win.show();
        win.focus();
        win.webContents.focus();
      });
      const windowZoom = () =>
        electronApp.evaluate(({ BrowserWindow }) =>
          BrowserWindow.getAllWindows()
            .find((w) => w.webContents.getURL().startsWith("http"))
            .webContents.getZoomFactor(),
        );

      step("open the report");
      await page.goto(`${backend.serverUrl}/c/${backend.sessionId}?file=${REPORT_PATH}`);
      const fileViewer = page.locator('[data-testid="file-viewer"]:visible');
      const preview = fileViewer.frameLocator('iframe[title="HTML preview"]');
      const heading = preview.getByRole("heading", { name: "Q3 revenue report" });
      await heading.waitFor({ timeout: 30_000 });
      // The bridge paints its (empty) comment highlights once the app connects to it.
      await eventually(
        () => preview.locator("body").evaluate(() => CSS.highlights.has("omni-comment")),
        "preview bridge connection",
      );
      const badge = fileViewer.getByTestId("html-preview-zoom");
      // The frame lays out at 1/zoom of the pane and is scaled back up, like browser zoom.
      const frameWidth = () => preview.locator("body").evaluate(() => window.innerWidth);
      const baseWidth = await frameWidth();
      const viewerBefore = await fileViewer.boundingBox();
      const expectPreview = async (percent) => {
        await eventually(
          async () => (await badge.textContent()) === `Preview zoom ${percent}%`,
          `preview zoom ${percent}%`,
        );
        // The frame sees its new width once the resize reaches its document.
        await eventually(async () => {
          const ratio = baseWidth / (await frameWidth());
          return Math.abs(ratio - percent / 100) < 0.02;
        }, `preview scaled to ${percent}%`);
        assert.equal(await windowZoom(), 1, "the app window must not zoom");
        assert.deepEqual(await fileViewer.boundingBox(), viewerBefore);
        await pause();
      };

      step("report rendered");
      await pause();
      step("click into the preview");
      await heading.click();
      await pause();
      // Ctrl+Shift+= types Ctrl+Plus, the View menu's window-zoom accelerator.
      step("ctrl+plus");
      pressKeys("ctrl+shift+equal");
      await expectPreview(110);
      step("ctrl+=");
      pressKeys("ctrl+equal");
      await expectPreview(125);
      step("ctrl+-");
      pressKeys("ctrl+minus");
      await expectPreview(110);
      step("ctrl+0");
      pressKeys("ctrl+0");
      await expectPreview(100);

      // Elsewhere in the app the same shortcut still zooms the window.
      await eventually(async () => (await badge.textContent()) === "", "zoom badge hidden");
      step("click the chat composer");
      await page.getByPlaceholder("Send a message…").click();
      await pause();
      step("ctrl+plus outside");
      pressKeys("ctrl+shift+equal");
      await eventually(async () => (await windowZoom()) > 1, "window zoom in");
      assert.equal(await badge.textContent(), "");
      await pause();
      step("ctrl+0 outside");
      pressKeys("ctrl+0");
      await eventually(async () => (await windowZoom()) === 1, "window zoom reset");
      step("window back to 100%");
      await pause();
      // End the clip on this state rather than on the window closing.
      await app.stopDisplayCapture();
    } finally {
      if (app) {
        await app.electronApp.close();
        await app.stopDisplayCapture();
        if (RECORD_DIR) saveRecording(recordDir, "html-preview-zoom");
        fs.rmSync(app.userDataDir, { recursive: true, force: true });
      }
      if (backend) await backend.close();
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  },
);
