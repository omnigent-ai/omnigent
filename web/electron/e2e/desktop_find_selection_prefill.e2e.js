// Cmd/Ctrl+F over text selected in a chat reply opens the desktop find bar searching for it.
// Run: OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_find_selection_prefill.e2e.js
// Env: OMNIGENT_PYTHON (backend), OMNIGENT_DESKTOP_RECORD_DIR (paced recording).

"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { desktopDepsAvailable, launchDesktop, saveRecording } = require("./desktopHarness");
const { eventually, jsonRequest, startDesignBackend } = require("./desktopDesignPromptHarness");

const PYTHON = process.env.OMNIGENT_PYTHON || "python3";
const XTEST_KEYS = path.join(__dirname, "fixtures", "xtestKeys.py");
const RECORD_DIR = process.env.OMNIGENT_DESKTOP_RECORD_DIR;
const PROMPT = "Summarize last night's database job.";
const REPLY =
  "The nightly migration copied 12 tables. Schedule the next migration after the " +
  "rollback window closes, and keep the rollback snapshot until Friday.";

const deps = desktopDepsAvailable();
// Menu accelerators only fire for OS key events, which this lane injects through XTEST.
const xtestReady =
  process.platform === "linux" &&
  Boolean(process.env.DISPLAY) &&
  spawnSync(PYTHON, [XTEST_KEYS, "--check"]).status === 0;

const RAISE_X_WINDOWS = `
import ctypes, ctypes.util, sys
x11 = ctypes.CDLL(ctypes.util.find_library("X11"))
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
display = x11.XOpenDisplay(None)
for window in sys.argv[1:]:
    x11.XRaiseWindow(display, int(window))
x11.XSync(display, 0)
`;

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

function pause(ms = 2_500) {
  return new Promise((resolve) => {
    setTimeout(resolve, RECORD_DIR ? ms : 0);
  });
}

function jsonBody(method, body) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

/** Double-click the `occurrence`th copy of `word` in the reply, like a user selecting it. */
async function doubleClickWord(page, word, occurrence = 0) {
  const point = await page.evaluate(
    ({ reply, target, nth }) => {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (!node.data.includes(reply.slice(0, 20))) continue;
        let index = -1;
        for (let seen = 0; seen <= nth; seen += 1) {
          index = node.data.indexOf(target, index + 1);
          if (index < 0) return null;
        }
        const range = document.createRange();
        range.setStart(node, index);
        range.setEnd(node, index + target.length);
        const box = range.getBoundingClientRect();
        return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
      }
      return null;
    },
    { reply: REPLY, target: word, nth: occurrence },
  );
  assert.ok(point, `reply text with ${word}`);
  await page.mouse.dblclick(point.x, point.y);
  await eventually(
    async () => (await page.evaluate(() => window.getSelection().toString().trim())) === word,
    `${word} selected`,
  );
}

it(
  "opens find with the chat selection from Ctrl+F and Edit > Find",
  {
    skip: deps.ok
      ? xtestReady
        ? false
        : "needs an X display with XTEST"
      : `missing deps: ${deps.missing.join(", ")}`,
    timeout: 240_000,
  },
  async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-find-prefill-"));
    const recordDir = RECORD_DIR || path.join(tmpDir, "recordings");
    let backend;
    let app;
    try {
      backend = await startDesignBackend(tmpDir);
      const { serverUrl, mockUrl, sessionId } = backend;
      const session = `${serverUrl}/v1/sessions/${sessionId}`;
      await jsonRequest(session, jsonBody("PATCH", { title: "Database job" }));
      await jsonRequest(
        `${mockUrl}/mock/set_fallback`,
        jsonBody("POST", { key: "gpt-4o-mini", text: REPLY, stream: true }),
      );
      await jsonRequest(
        `${session}/events`,
        jsonBody("POST", {
          type: "message",
          data: { role: "user", content: [{ type: "input_text", text: PROMPT }] },
        }),
      );
      await eventually(
        async () => JSON.stringify(await jsonRequest(`${session}/items`)).includes(REPLY),
        "agent reply",
        60_000,
      );

      recordingStart = Date.now();
      app = await launchDesktop({
        serverUrl,
        recordDir,
        userDataDir: path.join(tmpDir, "profile"),
      });
      const { window: page, electronApp } = app;
      // No window manager runs under Xvfb: focus the shell where a click on it
      // would, then restack its child find bar above it as a window manager does.
      const focusShell = async () => {
        const children = await electronApp.evaluate(({ BrowserWindow }) => {
          const win = BrowserWindow.getAllWindows().find((w) =>
            w.webContents.getURL().startsWith("http"),
          );
          win.focus();
          win.webContents.focus();
          return win
            .getChildWindows()
            .map((child) => String(child.getNativeWindowHandle().readUInt32LE()));
        });
        if (children.length > 0) {
          const raise = spawnSync(PYTHON, ["-c", RAISE_X_WINDOWS, ...children], {
            encoding: "utf8",
          });
          assert.equal(raise.status, 0, raise.stderr);
        }
      };
      await electronApp.evaluate(({ BrowserWindow }) => {
        const win = BrowserWindow.getAllWindows().find((w) =>
          w.webContents.getURL().startsWith("http"),
        );
        win.setPosition(0, 0);
        win.setContentSize(1280, 800);
        win.show();
      });
      await focusShell();
      await page.goto(`${serverUrl}/c/${sessionId}`);
      await page.getByText(REPLY.slice(0, 30)).first().waitFor({ timeout: 45_000 });
      step("chat open");
      await pause();

      const findBar = async () => {
        let bar;
        await eventually(async () => {
          bar = electronApp.windows().find((w) => w.url().endsWith("/find/index.html"));
          return bar !== undefined;
        }, "find bar window");
        return bar;
      };
      // Chromium starts from the selection, so only the total match count is fixed.
      const expectQuery = async (bar, text, total) => {
        const state = () =>
          bar.evaluate(() => {
            const input = document.getElementById("q");
            return {
              value: input.value,
              selected: input.value.slice(input.selectionStart, input.selectionEnd),
              count: document.getElementById("count").textContent,
            };
          });
        await eventually(
          async () => {
            const shown = await state();
            return (
              shown.value === text && shown.selected === text && shown.count.endsWith(`/${total}`)
            );
          },
          `find bar searching "${text}" with ${total} matches`,
          10_000,
        ).catch(async (error) => {
          error.message += ` (bar shows ${JSON.stringify(await state())})`;
          throw error;
        });
      };

      // CUJ-1: Ctrl+F over a selected word.
      step("double-click migration");
      await doubleClickWord(page, "migration");
      await pause();
      step("press Ctrl+F");
      pressKeys("ctrl+f");
      const bar = await findBar();
      await expectQuery(bar, "migration", 2);
      step("find bar shows migration");
      await pause(4_000);

      // CUJ-2: a new selection replaces the open bar's query...
      await focusShell();
      step("double-click rollback with the bar open");
      await doubleClickWord(page, "rollback", 1);
      await pause();
      step("press Ctrl+F");
      pressKeys("ctrl+f");
      await expectQuery(bar, "rollback", 2);
      step("find bar shows rollback");
      await pause(4_000);
      // ...but Ctrl+F typed inside the bar keeps what the user typed there.
      step("type tables in the bar");
      await bar.keyboard.type("tables", { delay: 120 });
      await eventually(
        async () => (await bar.locator("#count").textContent()) === "1/1",
        "tables searched",
      );
      await pause();
      step("press Ctrl+F inside the bar");
      pressKeys("ctrl+f");
      await expectQuery(bar, "tables", 1);
      step("find bar keeps tables");
      await pause(4_000);

      // CUJ-3: Edit > Find… over a selection, after closing the bar.
      step("press Escape to close the bar");
      pressKeys("Escape");
      await eventually(async () => bar.isClosed(), "find bar closed");
      await pause();
      await focusShell();
      step("double-click snapshot");
      await doubleClickWord(page, "snapshot");
      await pause();
      step("open Edit > Find from the keyboard menu");
      // Linux auto-hides the menu bar: Alt shows it, then the keys walk to Edit > Find….
      pressKeys("alt");
      await pause(800);
      pressKeys("Right");
      await pause(800);
      pressKeys("Down");
      await pause(800);
      pressKeys("Up");
      await pause(1_500);
      pressKeys("Return");
      const menuBar = await findBar();
      await expectQuery(menuBar, "snapshot", 1);
      step("find bar shows snapshot");
      await pause(4_000);
      // End the clip on this state rather than on the window closing.
      await app.stopDisplayCapture();
    } finally {
      if (app) {
        await app.electronApp.close().catch(() => {});
        await app.stopDisplayCapture();
        const saved = saveRecording(recordDir, "find-selection-prefill");
        if (RECORD_DIR) console.log(`Desktop find prefill recordings: ${saved.join(", ")}`);
      }
      await backend?.close();
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  },
);
