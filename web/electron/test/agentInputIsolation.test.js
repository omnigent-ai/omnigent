"use strict";

// An agent's protocol input stays in the page. Electron 42
// doesn't raise before-input-event for it, but the shell's Ctrl+Tab session
// switcher also ignores it explicitly in case a later Electron does.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { createBrowserViewRegistry } = require("../src/browserViewRegistry");
const { createBrowserViewBoundsController } = require("../src/browserViewBounds");
const {
  AGENT_INPUT_GRACE_MS,
  fileChooserGuard,
  performBrowserInput,
} = require("../src/browserInput");

// Its own test covers the file-chooser guard's listeners.
fileChooserGuard.ms = 0;

function makeRegistry() {
  const listeners = new Map();
  const sent = [];
  const keys = [];
  const dbg = new EventEmitter();
  let attached = false;
  dbg.isAttached = () => attached;
  dbg.attach = () => {
    attached = true;
  };
  dbg.detach = () => {
    attached = false;
  };
  const webContents = {
    loadURL() {},
    close() {},
    removeListener() {},
    on(name, listener) {
      listeners.set(name, listener);
    },
    setWindowOpenHandler() {},
    debugger: dbg,
    executeJavaScript: async () => "ok",
  };
  // Raise before-input-event for protocol key events, as if Electron did.
  dbg.sendCommand = async (method, params) => {
    if (method === "Input.dispatchKeyEvent") {
      keys.push(params);
      listeners.get("before-input-event")?.(
        { preventDefault() {} },
        {
          type: params.type === "keyUp" ? "keyUp" : "keyDown",
          key: params.key,
          code: params.code,
          control: (params.modifiers & 2) !== 0 || params.key === "Control",
        },
      );
    }
    return {};
  };
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: () => ({ setBounds() {}, webContents }),
    createBoundsController: createBrowserViewBoundsController,
    attachToHost() {},
    detachFromHost() {},
    sendToRenderer: (channel, payload) => sent.push({ channel, payload }),
    isHostFocused: () => true,
  });
  registry.openOrNavigate("conv_1", "https://example.com");
  registry.setActive("conv_1");
  registry.setRecentSessionSwitchSupported(true);
  const forwarded = () => sent.filter((item) => item.channel === "browser-recent-session-input");
  return { registry, listeners, forwarded, keys, entry: registry.get("conv_1") };
}

describe("agent key presses and the recent-session switcher", () => {
  for (const key of ["Control+Tab", "Control+Shift+Tab"]) {
    it(`keeps an agent's ${key} out of the shell, and still forwards the user's`, async () => {
      const { listeners, forwarded, keys, entry } = makeRegistry();
      await performBrowserInput(entry, "press_key", { key });
      assert.ok(
        keys.some((k) => k.type !== "keyUp" && k.key === "Tab" && (k.modifiers & 2) !== 0),
        "the agent's Tab went out, with Control held",
      );
      assert.deepEqual(forwarded(), [], "the agent's shortcut reached the page only");
      await new Promise((resolve) => {
        setTimeout(resolve, AGENT_INPUT_GRACE_MS + 50);
      });
      listeners.get("before-input-event")(
        { preventDefault() {} },
        { type: "keyDown", key: "Tab", code: "Tab", control: true },
      );
      assert.equal(forwarded().length, 1, "the user's Ctrl+Tab still opens the switcher");
    });
  }
});
