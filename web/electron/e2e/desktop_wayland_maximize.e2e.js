// Maximizing the main window on native Wayland must not kill the shell (Chromium
// traps on the zero-height geometry a collapsed update overlay once produced).
// Drives the real shell against a headless stub compositor enforcing xdg-shell.

"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

const PYTHON = process.env.OMNIGENT_PYTHON || "python3";
const COMPOSITOR = path.join(__dirname, "fixtures", "wayland_stub_compositor.py");
const RECORD_DIR = path.join(__dirname, "recordings", "desktop-wayland-maximize");

const SHELL_READY_TEXT = "What should we build?";
const WINDOWS_TIMEOUT_MS = 45_000;
const SHELL_TIMEOUT_MS = 30_000;
const CRASH_WATCH_MS = 8_000;

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function pollUntil(fn, { timeout, interval = 200, label }) {
  const deadline = Date.now() + timeout;
  /* oxlint-disable no-await-in-loop */
  for (;;) {
    const value = await fn();
    if (value) return value;
    if (Date.now() >= deadline) throw new Error(`timed out waiting for ${label}`);
    await sleep(interval);
  }
  /* oxlint-enable no-await-in-loop */
}

// A SIGTRAP kill finalizes Playwright's per-page video slightly after
// electronApp.close() resolves, so wait for the raw clips to stop growing
// before saveRecording renames them.
async function waitForRawVideo(recordDir, timeout = 15_000) {
  const totalSize = () =>
    fs
      .readdirSync(recordDir)
      .filter((f) => (f.startsWith("page@") || f.startsWith("display@")) && f.endsWith(".webm"))
      .reduce((sum, f) => sum + fs.statSync(path.join(recordDir, f)).size, 0);
  const deadline = Date.now() + timeout;
  let last = -1;
  /* oxlint-disable no-await-in-loop */
  while (Date.now() < deadline) {
    const total = totalSize();
    if (total > 0 && total === last) return;
    last = total;
    await sleep(400);
  }
  /* oxlint-enable no-await-in-loop */
}

function pywaylandAvailable() {
  return spawnSync(PYTHON, ["-c", "import pywayland"], { stdio: "ignore" }).status === 0;
}

// The AF_UNIX socket path limit is 108 bytes, so the runtime dir must live
// under /tmp, not the deep workspace tree.
async function startCompositor() {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-wl-"));
  fs.chmodSync(runtimeDir, 0o700);
  const controlDir = path.join(runtimeDir, "control");
  const socket = `omni-${process.pid}`;
  const events = [];

  const proc = spawn(PYTHON, [COMPOSITOR, "--socket", socket, "--control", controlDir], {
    env: { ...process.env, XDG_RUNTIME_DIR: runtimeDir },
    stdio: ["ignore", "pipe", "inherit"],
  });

  let buffer = "";
  proc.stdout.setEncoding("utf8");
  proc.stdout.on("data", (chunk) => {
    buffer += chunk;
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (line) {
        try {
          events.push(JSON.parse(line));
        } catch {
          /* non-JSON diagnostic line */
        }
      }
    }
  });

  await pollUntil(() => fs.existsSync(path.join(controlDir, "ready")), {
    timeout: 15_000,
    label: "compositor ready",
  });

  const command = async (name, selector = {}) => {
    const file = path.join(controlDir, name);
    for (const suffix of [".done", ".taken"]) {
      fs.rmSync(file + suffix, { force: true });
    }
    fs.writeFileSync(file, JSON.stringify(selector));
    const done = await pollUntil(
      () => (fs.existsSync(file + ".done") ? fs.readFileSync(file + ".done", "utf8") : null),
      { timeout: 10_000, label: `compositor ${name}` },
    );
    return JSON.parse(done);
  };

  const stop = async () => {
    if (proc.exitCode === null && proc.signalCode === null) {
      proc.kill("SIGTERM");
      await sleep(200);
      if (proc.exitCode === null) proc.kill("SIGKILL");
    }
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  };

  return { socket, runtimeDir, events, command, stop };
}

// Overlay windows have a parent; the main window does not.
async function describeWindows(electronApp) {
  return electronApp.evaluate(({ BrowserWindow }) =>
    BrowserWindow.getAllWindows().map((win) => ({
      overlay: win.getParentWindow() !== null,
      title: win.getTitle(),
      visible: win.isVisible(),
      maximized: win.isMaximized(),
      bounds: win.getBounds(),
    })),
  );
}

test("desktop shell survives maximize on Wayland", async (t) => {
  if (process.platform !== "linux") {
    return t.skip("Wayland reproduction only runs on Linux");
  }
  const deps = desktopDepsAvailable();
  if (!deps.ok) {
    return t.skip(`missing desktop deps: ${deps.missing.join(", ")}`);
  }
  if (!pywaylandAvailable()) {
    return t.skip("pywayland (stub compositor) not importable");
  }

  fs.mkdirSync(RECORD_DIR, { recursive: true });
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-wl-server-"));

  let server;
  let compositor;
  let electronApp;
  let stopDisplayCapture = async () => {};
  let exitInfo = null;
  const evidence = {};

  try {
    server = await spawnServer(tmpDir);
    compositor = await startCompositor();

    const launched = await launchDesktop({
      recordDir: RECORD_DIR,
      serverUrl: server.serverUrl,
      extraArgs: ["--ozone-platform=wayland", "--disable-gpu"],
      env: {
        WAYLAND_DISPLAY: compositor.socket,
        XDG_RUNTIME_DIR: compositor.runtimeDir,
        XDG_SESSION_TYPE: "wayland",
        GDK_BACKEND: "wayland",
        DISPLAY: undefined,
      },
    });
    electronApp = launched.electronApp;
    stopDisplayCapture = launched.stopDisplayCapture;

    electronApp.process().on("exit", (code, signal) => {
      exitInfo = { code, signal };
    });

    // Wait for the main window and the eagerly-created overlay child window to
    // both exist and be visible.
    await pollUntil(
      async () => {
        const windows = await describeWindows(electronApp);
        return (
          windows.some((w) => !w.overlay && w.visible) &&
          windows.some((w) => w.overlay && w.visible)
        );
      },
      { timeout: WINDOWS_TIMEOUT_MS, label: "main window and update overlay" },
    );

    // firstWindow() is often the empty overlay page; the shell renders on the
    // page navigated to the server URL. Let it show the home screen so the
    // recording shows the real product (non-fatal if it renders slowly).
    const mainPage = await pollUntil(
      () => electronApp.windows().find((p) => p.url().startsWith("http")) ?? null,
      { timeout: WINDOWS_TIMEOUT_MS, label: "main SPA page" },
    );
    await mainPage
      .getByText(SHELL_READY_TEXT)
      .first()
      .waitFor({ state: "visible", timeout: SHELL_TIMEOUT_MS })
      .catch(() => {});
    await sleep(2000);

    evidence.windowsBefore = await describeWindows(electronApp);

    // Maximizing the largest non-child toplevel models a user maximizing the
    // main window (not the overlay); the shell then repositions the overlay.
    const maximize = await compositor.command("maximize");
    evidence.maximizeAction = maximize;
    assert.equal(
      maximize.affected.length,
      1,
      `expected the single main toplevel to be maximized, got ${JSON.stringify(maximize.affected)}`,
    );
    assert.equal(maximize.affected[0].child, false, "maximize must target the parent window");

    // The shell dies within a moment of the configure, or it survives (timeout).
    await pollUntil(() => exitInfo !== null, {
      timeout: CRASH_WATCH_MS,
      label: "shell exit",
    }).catch(() => {});

    evidence.exit = exitInfo;
    evidence.windowsAfter = await describeWindows(electronApp).catch(() => null);
    const geometry = compositor.events.filter((e) => e.event === "set_window_geometry");
    evidence.lastGeometry = geometry.slice(-3);
    evidence.invalidGeometry = compositor.events.filter(
      (e) => e.event === "invalid_window_geometry",
    );

    fs.writeFileSync(path.join(RECORD_DIR, "evidence.json"), JSON.stringify(evidence, null, 2));

    assert.equal(
      exitInfo,
      null,
      `desktop shell died while maximizing on Wayland: ${JSON.stringify(evidence.exit)} ` +
        `(compositor geometry: ${JSON.stringify(evidence.lastGeometry)})`,
    );
    const after = evidence.windowsAfter ?? [];
    assert.ok(
      after.some((w) => !w.overlay && w.maximized),
      "the main window should be maximized and alive after the resize",
    );
    assert.equal(
      evidence.invalidGeometry.length,
      0,
      `zero-sized window geometry reached the compositor: ${JSON.stringify(evidence.invalidGeometry)}`,
    );
  } finally {
    if (electronApp) await electronApp.close().catch(() => {});
    await stopDisplayCapture().catch(() => {});
    if (compositor) await compositor.stop().catch(() => {});
    if (server) await server.close().catch(() => {});
    await waitForRawVideo(RECORD_DIR).catch(() => {});
    saveRecording(RECORD_DIR, "wayland-maximize");
  }
});
