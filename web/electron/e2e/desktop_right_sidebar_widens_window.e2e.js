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
// The native shell's Toggle Right Sidebar accelerator.
const TOGGLE_RIGHT_SIDEBAR =
  process.platform === "darwin" ? "Meta+Shift+KeyB" : "Control+Shift+KeyB";
const TOGGLE_RIGHT_SIDEBAR_LABEL = process.platform === "darwin" ? "⌘⇧B" : "Ctrl+Shift+B";

function pause(ms = 1_500) {
  return new Promise((resolve) => {
    setTimeout(resolve, RECORD_DIR ? ms : 0);
  });
}

/** A real wait before a check that the window did not change, past the resize IPC's latency. */
function settle(ms = 1_000) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
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

/** Gives the main process one lookup for the shell window, failing clearly when it's missing. */
async function installShellWindowLookup(electronApp) {
  await electronApp.evaluate(({ BrowserWindow }) => {
    globalThis.shellWindow = () => {
      const win = BrowserWindow.getAllWindows().find((candidate) =>
        candidate.webContents.getURL().startsWith("http"),
      );
      if (!win) throw new Error("shell window not found");
      return win;
    };
  });
}

async function windowState(electronApp) {
  return electronApp.evaluate(({ screen }) => {
    const win = globalThis.shellWindow();
    const bounds = win.getBounds();
    return {
      bounds,
      workArea: screen.getDisplayMatching(bounds).workArea,
      fullScreen: win.isFullScreen(),
    };
  });
}

async function setWindowBounds(electronApp, bounds) {
  await electronApp.evaluate((_electron, next) => globalThis.shellWindow().setBounds(next), bounds);
}

/** Counts the shell window's `setBounds` calls from now on; resolves a reader for the count. */
async function countSetBounds(electronApp) {
  await electronApp.evaluate(() => {
    const win = globalThis.shellWindow();
    globalThis.setBoundsCalls = 0;
    const original = win.setBounds.bind(win);
    win.setBounds = (...args) => {
      globalThis.setBoundsCalls += 1;
      return original(...args);
    };
  });
  return () => electronApp.evaluate(() => globalThis.setBoundsCalls);
}

async function setFullScreen(electronApp, fullScreen) {
  await electronApp.evaluate(
    (_electron, next) => globalThis.shellWindow().setFullScreen(next),
    fullScreen,
  );
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

/** The window's bounds once two reads half a second apart agree. */
async function settledBounds(electronApp) {
  let previous = null;
  let current = (await windowState(electronApp)).bounds;
  await eventually(
    async () => {
      await settle(500);
      previous = current;
      current = (await windowState(electronApp)).bounds;
      return JSON.stringify(current) === JSON.stringify(previous);
    },
    "window bounds settled",
    10_000,
  );
  return current;
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
      await installShellWindowLookup(electronApp);
      const { workArea } = await windowState(electronApp);
      const home = { x: workArea.x + 40, y: workArea.y + 40, width: 960, height: 760 };
      await setWindowBounds(electronApp, home);
      await waitForBounds(electronApp, home, "window at its starting bounds");
      await page.goto(`${serverUrl}/c/${sessionId}`);
      const composer = page.getByRole("textbox", { name: "Message the agent" });
      await composer.waitFor({ timeout: 45_000 });
      const expand = page.getByRole("button", { name: "Expand right panel" });
      const collapse = page.getByRole("button", { name: "Collapse right panel" });
      await expand.waitFor({ timeout: 30_000 });

      // The setting is off by default, so opening the sidebar keeps the window's size.
      await expand.click();
      await collapse.waitFor();
      await settle();
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

        await page.keyboard.press(TOGGLE_RIGHT_SIDEBAR);
        await collapse.waitFor();
        await waitForBounds(electronApp, widened, "hotkey widened the window");
        await eventually(
          async () => Math.abs((await chatWidth(page)) - before) <= 1,
          "chat kept its width on the second open",
          10_000,
        );
        step(`sidebar open with ${TOGGLE_RIGHT_SIDEBAR_LABEL}, window widened`);
        await pause(2_500);
        await page.keyboard.press(TOGGLE_RIGHT_SIDEBAR);
        await expand.waitFor();
        await waitForBounds(electronApp, home, "hotkey narrowed the window");
        step(`sidebar closed with ${TOGGLE_RIGHT_SIDEBAR_LABEL}, window back`);
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
        const fullScreen = await settledBounds(electronApp);
        // Xvfb ignores setBounds on a full-screen window, so also check the shell never called it.
        const setBoundsCalls = await countSetBounds(electronApp);
        step("shell put in full screen");
        await pause(2_000);
        await expand.click();
        await collapse.waitFor();
        await settle(1_500);
        assert.deepEqual((await windowState(electronApp)).bounds, fullScreen);
        assert.equal(await setBoundsCalls(), 0);
        step("sidebar open, full-screen window keeps its size");
        await pause(2_000);
        await collapse.click();
        await expand.waitFor();
        await settle(1_500);
        assert.deepEqual((await windowState(electronApp)).bounds, fullScreen);
        assert.equal(await setBoundsCalls(), 0);
        step("sidebar closed, window size unchanged");
        await pause(2_000);

        // A sidebar closed in full screen gives its width back once the window leaves it.
        await setFullScreen(electronApp, false);
        await eventually(
          async () => !(await windowState(electronApp)).fullScreen,
          "window left full screen",
          10_000,
        );
        await settle();
        await expand.click();
        await collapse.waitFor();
        await eventually(
          async () => (await windowState(electronApp)).bounds.width > home.width,
          "window widened again",
          10_000,
        );
        step("left full screen, sidebar open, window widened");
        await pause(2_500);
        await setFullScreen(electronApp, true);
        await eventually(
          async () => (await windowState(electronApp)).fullScreen,
          "window in full screen again",
          10_000,
        );
        const widenedFullScreen = await settledBounds(electronApp);
        step("full screen with the sidebar open");
        await pause(2_000);
        await collapse.click();
        await expand.waitFor();
        await settle(1_500);
        assert.deepEqual((await windowState(electronApp)).bounds, widenedFullScreen);
        step("sidebar closed in full screen, window unchanged");
        await pause(2_500);
        await setFullScreen(electronApp, false);
        await waitForBounds(electronApp, nearEdge, "window shrank after leaving full screen");
        step("left full screen, window shrank back into place");
        await pause(3_000);
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
