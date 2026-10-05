"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { REPO_ROOT, launchDesktop, saveRecording } = require("./desktopHarness");
const { eventually } = require("./desktopDesignPromptHarness");

const PYTHON = process.env.OMNIGENT_PYTHON || path.join(REPO_ROOT, ".venv/bin/python");

async function focusNativeWindow(windowHandle) {
  await windowHandle.evaluate((win) => {
    win.show();
    win.focus();
  });
  if (process.platform === "linux" && process.env.DISPLAY) {
    // Xvfb has no window manager to honor Electron's activation request.
    // Set real X11 focus so the renderer receives native focus/blur events.
    const id = await windowHandle.evaluate((win) => {
      const handle = win.getNativeWindowHandle();
      return (
        handle.length === 8 ? handle.readBigUInt64LE() : BigInt(handle.readUInt32LE())
      ).toString();
    });
    const focused = spawnSync(
      PYTHON,
      [
        "-c",
        [
          "import ctypes, sys",
          "x = ctypes.CDLL('libX11.so.6')",
          "x.XOpenDisplay.restype = ctypes.c_void_p",
          "x.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]",
          "x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]",
          "x.XCloseDisplay.argtypes = [ctypes.c_void_p]",
          "display = x.XOpenDisplay(None)",
          "assert display, 'X11 display unavailable'",
          "x.XSetInputFocus(display, int(sys.argv[1]), 1, 0)",
          "x.XSync(display, 0)",
          "x.XCloseDisplay(display)",
        ].join("\n"),
        id,
      ],
      { encoding: "utf8" },
    );
    assert.equal(focused.status, 0, focused.stderr);
  }
  await eventually(() => windowHandle.evaluate((win) => win.isFocused()), "native window focus");
}

async function requestJson(serverUrl, route, body, method = "POST") {
  const response = await fetch(`${serverUrl}${route}`, {
    method,
    ...(body === undefined
      ? {}
      : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(30_000),
  });
  const text = await response.text();
  assert.ok(response.ok, `${method} ${route}: ${response.status}: ${text}`);
  return text ? JSON.parse(text) : null;
}

async function seedSession(serverUrl) {
  const bundle = spawnSync(
    PYTHON,
    [
      "-c",
      "import sys; from tests.e2e_ui.conftest import _build_hello_world_bundle; sys.stdout.buffer.write(_build_hello_world_bundle())",
    ],
    { cwd: REPO_ROOT },
  );
  assert.equal(bundle.status, 0, bundle.stderr.toString());
  const form = new FormData();
  form.set("metadata", JSON.stringify({}));
  form.set("bundle", new Blob([bundle.stdout], { type: "application/gzip" }), "agent.tar.gz");
  const created = await fetch(`${serverUrl}/v1/sessions`, { method: "POST", body: form });
  const text = await created.text();
  assert.ok(created.ok, text);
  return JSON.parse(text);
}

it(
  "real desktop clears unread badges after reading and ignores metadata",
  { timeout: 120_000 },
  async () => {
    const serverUrl = process.env.OMNIGENT_REPRO_SERVER_URL;
    assert.ok(serverUrl, "Run this test through verify-env run after building the SPA");
    const evidence =
      process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
      fs.mkdtempSync(path.join(os.tmpdir(), "unread-desktop-"));
    fs.mkdirSync(evidence, { recursive: true });
    const sessionIds = [];
    let desktop;
    let nativeOutput = "";
    try {
      const created = await seedSession(serverUrl);
      const sessionId = created.session_id;
      sessionIds.push(sessionId);
      await requestJson(
        serverUrl,
        `/v1/sessions/${sessionId}`,
        {
          title: "Desktop unread proof",
          runner_id: process.env.OMNIGENT_REPRO_RUNNER_ID,
        },
        "PATCH",
      );
      const appendReply = (text) =>
        requestJson(serverUrl, `/v1/sessions/${sessionId}/events`, {
          type: "external_assistant_message",
          data: { agent: "hello_world", text },
        });
      await appendReply("An answer already read in the desktop app.");

      const userDataDir = fs.mkdtempSync(path.join(evidence, "profile-"));
      desktop = await launchDesktop({
        serverUrl,
        recordDir: evidence,
        userDataDir,
        env: {
          ...Object.fromEntries(
            Object.entries(process.env).filter(
              ([key]) =>
                !/^(OMNIGENT_|RUNNER_|OPENAI_|ANTHROPIC_|DATABRICKS_|MLFLOW_)/.test(key) &&
                key !== "ELECTRON_RUN_AS_NODE",
            ),
          ),
          OMNIGENT_CONFIG_HOME: path.join(userDataDir, "config"),
          OMNIGENT_DATA_DIR: path.join(userDataDir, "data"),
        },
      });
      const { electronApp, window: page } = desktop;
      // Focus emulation belongs to Playwright's original CDP session; a second
      // newCDPSession cannot disable that session's override.
      /* oxlint-disable no-underscore-dangle -- Electron's public driver API has no focus-emulation option. */
      const pageImpl = page._connection.toImpl(page);
      await pageImpl.delegate._mainFrameSession._client.send("Emulation.setFocusEmulationEnabled", {
        enabled: false,
      });
      /* oxlint-enable no-underscore-dangle */
      electronApp.process().stdout.on("data", (chunk) => {
        nativeOutput += chunk.toString();
      });
      electronApp.process().stderr.on("data", (chunk) => {
        nativeOutput += chunk.toString();
      });
      page.on("pageerror", (error) => {
        nativeOutput += `\npageerror: ${error.message}\n`;
      });
      page.on("requestfailed", (request) => {
        nativeOutput += `\nrequestfailed: ${request.url()} ${request.failure()?.errorText}\n`;
      });
      const badge = () => [...nativeOutput.matchAll(/setBadgeCount\((\d+)\)/g)].at(-1)?.[1];
      const waitForBadge = (count) =>
        eventually(() => badge() === String(count), `native unread badge ${count}`);
      const row = page.locator("li").filter({ has: page.locator(`a[href="/c/${sessionId}"]`) });
      const dot = row.locator('[data-testid="session-state-badge"][data-state="unseen"]');
      const nativeWindow = await electronApp.browserWindow(page);
      await focusNativeWindow(nativeWindow);
      await page.getByTestId("inbox-button").waitFor({ state: "visible", timeout: 45_000 });
      await page.goto(`${serverUrl}/c/${sessionId}`);
      await row.waitFor({ state: "visible" });
      await page.evaluate(() => {
        window.unreadFocusEvents = [];
        for (const type of ["focus", "blur", "pointerdown", "keydown"]) {
          window.addEventListener(
            type,
            (event) =>
              window.unreadFocusEvents.push({
                type,
                at: Date.now(),
                hasFocus: document.hasFocus(),
                windowTarget: event.target === window,
              }),
            true,
          );
        }
      });
      const captureState = async (label) => {
        const renderer = await page.evaluate(
          (id) => ({
            hasFocus: document.hasFocus(),
            visibility: document.visibilityState,
            state: JSON.parse(localStorage.getItem("omnigent.readState.v1") || "{}").lastSeen?.[id],
            events: window.unreadFocusEvents,
          }),
          sessionId,
        );
        const summaries = await requestJson(
          serverUrl,
          "/v1/sessions?visibility=all",
          undefined,
          "GET",
        );
        const summary = summaries.data.find((item) => item.id === sessionId);
        nativeOutput += `\nstate ${label}: ${JSON.stringify({ renderer, nativeFocused: await nativeWindow.evaluate((win) => win.isFocused()), lastMessage: summary?.last_message_at, lastSeen: summary?.viewer_last_seen })}\n`;
      };
      await page.getByTestId("inbox-button").click();
      await waitForBadge(0);

      const observedAt = Math.floor(Date.now() / 1000);
      await page.waitForFunction((stamp) => Math.floor(Date.now() / 1000) > stamp, observedAt);
      await requestJson(
        serverUrl,
        `/v1/sessions/${sessionId}`,
        { title: "Metadata changed, messages unchanged" },
        "PATCH",
      );
      await row.getByText("Metadata changed, messages unchanged").waitFor({ state: "visible" });
      assert.equal(await dot.count(), 0);
      assert.equal(badge(), "0");
      await page.screenshot({ path: path.join(evidence, "metadata-stays-read.png") });

      await appendReply("A new visible reply while Inbox is open.");
      await dot.waitFor({ state: "visible" });
      await waitForBadge(1);
      await page.screenshot({ path: path.join(evidence, "new-reply-unread.png") });
      await row.locator(`a[href="/c/${sessionId}"]`).click();
      await dot.waitFor({ state: "detached" });
      await waitForBadge(0);

      // A second native window takes focus while the chat remains open.
      await captureState("before-blur");
      const focusTarget = await electronApp.evaluateHandle(async ({ BrowserWindow }) => {
        const target = new BrowserWindow({ width: 320, height: 180, show: true });
        await target.loadURL("about:blank");
        return target;
      });
      await focusNativeWindow(focusTarget);
      await eventually(() => nativeWindow.evaluate((win) => !win.isFocused()), "desktop blur");
      await captureState("after-blur");
      const blurredAt = Math.floor(Date.now() / 1000);
      await page.waitForFunction((stamp) => Math.floor(Date.now() / 1000) > stamp, blurredAt);
      await appendReply("A visible reply received while the app window was blurred.");
      try {
        await waitForBadge(1);
      } finally {
        await captureState("after-background-reply");
      }
      await focusNativeWindow(nativeWindow);
      await waitForBadge(0);
      await page.getByTestId("inbox-button").click();
      assert.equal(await dot.count(), 0);
      await page.reload();
      await row.waitFor({ state: "visible" });
      assert.equal(await dot.count(), 0);
      await waitForBadge(0);
      await page.screenshot({ path: path.join(evidence, "focus-clears-read-durably.png") });
    } finally {
      if (desktop) {
        const page = desktop.window;
        nativeOutput += `\nfinal windows: ${JSON.stringify(desktop.electronApp.windows().map((item) => item.url()))}\n`;
        await page.screenshot({ path: path.join(evidence, "final-desktop.png") }).catch(() => {});
        fs.writeFileSync(
          path.join(evidence, "final-page.txt"),
          await page
            .locator("body")
            .innerText()
            .catch(() => ""),
        );
        await desktop.electronApp.close();
        await desktop.stopDisplayCapture();
        saveRecording(evidence, "desktop-unread");
      }
      fs.writeFileSync(path.join(evidence, "native-badge.log"), nativeOutput);
      await Promise.all(
        sessionIds.map((id) => requestJson(serverUrl, `/v1/sessions/${id}`, undefined, "DELETE")),
      );
    }
  },
);
