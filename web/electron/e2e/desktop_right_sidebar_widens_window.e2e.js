// With the setting on, the right sidebar widens the window instead of squeezing the chat.
// Run under xvfb-run with OMNIGENT_PW_NO_SANDBOX=1; OMNIGENT_DESKTOP_RECORD_DIR films each journey.

"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { desktopDepsAvailable, launchDesktop, startDisplayCapture } = require("./desktopHarness");
const { eventually, startDesignBackend } = require("./desktopDesignPromptHarness");

const RECORD_DIR = process.env.OMNIGENT_DESKTOP_RECORD_DIR;
const deps = desktopDepsAvailable();

function pause(ms = 1_500) {
  return new Promise((resolve) => {
    setTimeout(resolve, RECORD_DIR ? ms : 0);
  });
}

/** Films one journey from the composited display, after its setup is done. */
async function filmJourney(name, journey) {
  if (!RECORD_DIR || !process.env.DISPLAY) {
    await journey(() => {});
    return;
  }
  const dir = path.join(RECORD_DIR, name);
  fs.mkdirSync(dir, { recursive: true });
  const capture = startDisplayCapture(dir, process.env.DISPLAY);
  const start = Date.now();
  const step = (label) =>
    console.log(`${name} ${((Date.now() - start) / 1000).toFixed(1)}s ${label}`);
  try {
    await pause(1_000);
    await journey(step);
  } finally {
    await capture?.stop();
  }
}

async function windowState(electronApp) {
  return electronApp.evaluate(({ BrowserWindow, screen }) => {
    const win = BrowserWindow.getAllWindows().find((candidate) =>
      candidate.webContents.getURL().startsWith("http"),
    );
    const bounds = win.getBounds();
    return {
      bounds,
      workArea: screen.getDisplayMatching(bounds).workArea,
      fullScreen: win.isFullScreen(),
    };
  });
}

async function setWindowBounds(electronApp, bounds) {
  await electronApp.evaluate(({ BrowserWindow }, next) => {
    BrowserWindow.getAllWindows()
      .find((candidate) => candidate.webContents.getURL().startsWith("http"))
      .setBounds(next);
  }, bounds);
}

async function setFullScreen(electronApp, fullScreen) {
  await electronApp.evaluate(({ BrowserWindow }, next) => {
    BrowserWindow.getAllWindows()
      .find((candidate) => candidate.webContents.getURL().startsWith("http"))
      .setFullScreen(next);
  }, fullScreen);
}

async function waitForBounds(electronApp, expected, label) {
  await eventually(
    async () => {
      const { bounds } = await windowState(electronApp);
      return Object.entries(expected).every(([key, value]) => bounds[key] === value);
    },
    label,
    10_000,
  );
}

async function chatWidth(page) {
  return page
    .locator("main")
    .first()
    .evaluate((main) => main.getBoundingClientRect().width);
}

it(
  "widens and narrows the window with the right sidebar once the setting is on",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`, timeout: 300_000 },
  async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-sidebar-window-"));
    let backend;
    let app;
    try {
      backend = await startDesignBackend(tmpDir);
      const { serverUrl, sessionId } = backend;
      const userDataDir = path.join(tmpDir, "profile");
      app = await launchDesktop({
        serverUrl,
        recordDir: path.join(tmpDir, "pages"),
        userDataDir,
        displayCapture: false,
      });
      const { window: page, electronApp } = app;
      const { workArea } = await windowState(electronApp);
      const home = { x: workArea.x + 40, y: workArea.y + 40, width: 960, height: 760 };
      await setWindowBounds(electronApp, home);
      await page.goto(`${serverUrl}/c/${sessionId}`);
      const composer = page.getByRole("textbox", { name: "Message the agent" });
      await composer.waitFor({ timeout: 45_000 });
      const expand = page.getByRole("button", { name: "Expand right panel" });
      const collapse = page.getByRole("button", { name: "Collapse right panel" });
      await expand.waitFor({ timeout: 30_000 });

      // The setting is off by default, so opening the sidebar keeps the window's size.
      await expand.click();
      await collapse.waitFor();
      assert.deepEqual((await windowState(electronApp)).bounds, home);
      await collapse.click();
      await expand.waitFor();

      await filmJourney("demo-cuj-1", async (step) => {
        step("chat open");
        await page.getByRole("link", { name: "Settings", exact: true }).click();
        const toggle = page.getByTestId("widen-window-for-right-sidebar-toggle");
        await toggle.waitFor({ timeout: 20_000 });
        await toggle.scrollIntoViewIfNeeded();
        assert.equal(await toggle.getAttribute("aria-checked"), "false");
        step("settings, desktop app group, setting off");
        await pause(3_000);
        await toggle.click();
        await eventually(
          async () => (await toggle.getAttribute("aria-checked")) === "true",
          "setting switched on",
        );
        step("setting on");
        await pause(2_500);
        await page.reload();
        await toggle.waitFor({ timeout: 20_000 });
        await toggle.scrollIntoViewIfNeeded();
        await eventually(
          async () => (await toggle.getAttribute("aria-checked")) === "true",
          "setting still on after reload",
        );
        step("reloaded, setting still on");
        await pause(3_000);
      });
      const settingsFile = path.join(userDataDir, "Omnigent Dev", "settings.json");
      const saved = JSON.parse(fs.readFileSync(settingsFile, "utf8"));
      assert.equal(saved.widen_window_for_right_sidebar, true);

      await page.goto(`${serverUrl}/c/${sessionId}`);
      await expand.waitFor({ timeout: 30_000 });
      await filmJourney("demo-cuj-2", async (step) => {
        const before = await chatWidth(page);
        step(`chat open, window ${home.width}px wide`);
        await pause(2_000);
        await expand.click();
        await collapse.waitFor();
        await eventually(
          async () => (await windowState(electronApp)).bounds.width > home.width,
          "window widened",
          10_000,
        );
        const widened = (await windowState(electronApp)).bounds;
        assert.equal(widened.x, home.x);
        await eventually(
          async () => Math.abs((await chatWidth(page)) - before) <= 1,
          "chat kept its width",
          10_000,
        );
        step(
          `sidebar open from the header, window ${widened.width}px, chat ${Math.round(before)}px`,
        );
        await pause(3_000);
        await collapse.click();
        await expand.waitFor();
        await waitForBounds(electronApp, home, "window back to its width");
        step(`sidebar closed, window ${home.width}px`);
        await pause(2_500);

        await page.keyboard.press("Control+Shift+KeyB");
        await collapse.waitFor();
        await waitForBounds(electronApp, widened, "hotkey widened the window");
        step("sidebar open with Ctrl+Shift+B, window widened");
        await pause(2_500);
        await page.keyboard.press("Control+Shift+KeyB");
        await expand.waitFor();
        await waitForBounds(electronApp, home, "hotkey narrowed the window");
        step("sidebar closed with Ctrl+Shift+B, window back");
        await pause(2_500);
      });

      const nearEdge = { ...home, x: workArea.x + workArea.width - home.width - 60 };
      await setWindowBounds(electronApp, nearEdge);
      await waitForBounds(electronApp, nearEdge, "window near the right edge");
      await filmJourney("demo-cuj-3", async (step) => {
        step("window 60px from the right edge");
        await pause(2_000);
        await expand.click();
        await collapse.waitFor();
        await eventually(
          async () => {
            const { bounds } = await windowState(electronApp);
            return (
              bounds.width > home.width && bounds.x + bounds.width === workArea.x + workArea.width
            );
          },
          "window moved left to stay on screen",
          10_000,
        );
        const moved = (await windowState(electronApp)).bounds;
        assert.ok(moved.x < nearEdge.x);
        step(`sidebar open, window moved left to x=${moved.x}, right edge on the screen edge`);
        await pause(3_000);
        await collapse.click();
        await expand.waitFor();
        await waitForBounds(electronApp, nearEdge, "window moved back");
        step("sidebar closed, window back in place");
        await pause(2_500);

        await setFullScreen(electronApp, true);
        await eventually(
          async () => (await windowState(electronApp)).fullScreen,
          "window in full screen",
          10_000,
        );
        await pause(1_500);
        const fullScreen = (await windowState(electronApp)).bounds;
        step("shell put in full screen");
        await pause(2_000);
        await expand.click();
        await collapse.waitFor();
        await pause(1_500);
        assert.deepEqual((await windowState(electronApp)).bounds, fullScreen);
        step("sidebar open, full-screen window keeps its size");
        await pause(2_000);
        await collapse.click();
        await expand.waitFor();
        await pause(1_500);
        assert.deepEqual((await windowState(electronApp)).bounds, fullScreen);
        step("sidebar closed, window size unchanged");
        await pause(2_500);
      });
    } finally {
      if (app) {
        await app.electronApp.close().catch(() => {});
        await app.stopDisplayCapture();
      }
      await backend?.close();
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  },
);
