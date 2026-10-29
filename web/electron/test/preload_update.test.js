const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PRELOAD = fs.readFileSync(path.join(__dirname, "../src/preload.js"), "utf8");

function loadPreload() {
  const exposed = new Map();
  const listeners = new Map();
  const invokes = [];
  const sends = [];
  let updateStatus = { state: "idle" };
  const ipcRenderer = {
    invoke: async (channel, args) => {
      invokes.push({ channel, args });
      if (channel === "omnigent:get-update-status") return updateStatus;
      return null;
    },
    send: (channel, args) => sends.push({ channel, args }),
    on: (channel, listener) => listeners.set(channel, listener),
    removeListener: (channel, listener) => {
      if (listeners.get(channel) === listener) listeners.delete(channel);
    },
  };
  vm.runInNewContext(PRELOAD, {
    console,
    process: { platform: "linux" },
    require: (specifier) => {
      assert.equal(specifier, "electron");
      return {
        contextBridge: { exposeInMainWorld: (name, value) => exposed.set(name, value) },
        ipcRenderer,
        webUtils: { getPathForFile: (file) => file.path },
      };
    },
  });
  return {
    desktop: exposed.get("omnigentDesktop"),
    setStatus: (status) => {
      updateStatus = status;
    },
    emit: (channel, payload) => listeners.get(channel)?.({}, payload),
    hasListener: (channel) => listeners.has(channel),
    invokes,
    sends,
  };
}

describe("local folder bridge", () => {
  it("forwards the folder chooser and resolves dropped file paths", async () => {
    const h = loadPreload();
    await h.desktop.chooseDirectory("/Users/me");
    assert.deepEqual(h.invokes.at(-1), { channel: "omnigent:choose-directory", args: "/Users/me" });
    // main, not the preload, decides whether the page may see the path.
    await h.desktop.getPathForFile({ path: "/Users/me/app" });
    assert.deepEqual(h.invokes.at(-1), {
      channel: "omnigent:dropped-file-path",
      args: "/Users/me/app",
    });
  });
});

describe("browser action bridge", () => {
  it("forwards typed actions to main", async () => {
    const h = loadPreload();
    const params = { ref: 2, text: "hello" };
    await h.desktop.browserPerformAction("conversation", "type", params);
    const { channel, args } = h.invokes.at(-1);
    assert.equal(channel, "omnigent:browser-perform-action");
    assert.equal(args.conversationId, "conversation");
    assert.equal(args.action, "type");
    assert.deepEqual(args.params, params);
    await h.desktop.browserExecute("conversation", "legacy input");
    assert.equal(h.invokes.at(-1).channel, "omnigent:browser-execute");
    assert.equal(h.invokes.at(-1).args.js, "legacy input");
  });
});

describe("server-page update bridge", () => {
  it("hides every shell-owned update prompt state, including download progress", async () => {
    const h = loadPreload();
    async function expectHidden(state, lastError) {
      h.setStatus({ state, lastError });
      const status = await h.desktop.updates.getStatus();
      assert.equal(status.state, "idle");
      assert.equal(status.progress, undefined);
      assert.equal(status.info, undefined);
    }

    await expectHidden("available");
    await expectHidden("downloading");
    await expectHidden("downloaded");
    await expectHidden("error-security", "signature failed");
  });

  it("forwards embedded Browser recent-session input and unsubscribes", () => {
    const h = loadPreload();
    const received = [];
    const unsubscribe = h.desktop.onBrowserRecentSessionInput((input) => received.push(input));

    h.emit("browser-recent-session-input", { type: "keydown", key: "Tab", ctrlKey: true });
    assert.deepEqual(received, [{ type: "keydown", key: "Tab", ctrlKey: true }]);

    unsubscribe();
    assert.equal(h.hasListener("browser-recent-session-input"), false);
  });

  it("asks the main process to cancel a declined recent-session switch", async () => {
    const h = loadPreload();

    await h.desktop.browserCancelRecentSessionSwitch();

    assert.ok(
      h.invokes.some(({ channel }) => channel === "omnigent:browser-cancel-recent-session-switch"),
    );
  });

  it("advertises recent-session switch support to the main process", async () => {
    const h = loadPreload();

    await h.desktop.browserSetRecentSessionSwitchSupported(true);
    await h.desktop.browserSetRecentSessionSwitchSupported(false);

    assert.deepEqual(
      h.invokes
        .filter(({ channel }) => channel === "omnigent:browser-set-recent-session-switch-supported")
        .map(({ args }) => args.supported),
      [true, false],
    );
  });
});

describe("desktop command bridge", () => {
  it("forwards menu commands with a validated payload and unsubscribes", () => {
    const h = loadPreload();
    const received = [];
    const unsubscribe = h.desktop.onDesktopCommand((payload) => received.push(payload));

    h.emit("omnigent:desktop-command", {
      command: "close",
      browserViewId: "browser-tab:s:t",
      closingQuits: true,
    });
    h.emit("omnigent:desktop-command", { command: "new-terminal", browserViewId: 7 });
    h.emit("omnigent:desktop-command", { command: 5 });
    h.emit("omnigent:desktop-command", null);
    assert.deepEqual(JSON.parse(JSON.stringify(received)), [
      { command: "close", browserViewId: "browser-tab:s:t", closingQuits: true },
      { command: "new-terminal", browserViewId: null, closingQuits: false },
    ]);

    unsubscribe();
    assert.equal(h.hasListener("omnigent:desktop-command"), false);
  });

  it("reports command state as plain values and asks main to close the window", async () => {
    const h = loadPreload();
    h.desktop.setDesktopCommandState({
      commands: ["close", 3],
      sessionPinned: 1,
      sessionArchived: true,
    });
    h.desktop.setDesktopCommandState(undefined);
    assert.deepEqual(JSON.parse(JSON.stringify(h.sends)), [
      {
        channel: "omnigent:set-desktop-command-state",
        args: { commands: ["close", "3"], sessionPinned: false, sessionArchived: true },
      },
      {
        channel: "omnigent:set-desktop-command-state",
        args: { commands: [], sessionPinned: false, sessionArchived: false },
      },
    ]);

    await h.desktop.closeWindow();
    assert.deepEqual(h.invokes.at(-1), { channel: "omnigent:close-window", args: undefined });
  });
});

describe("global show shortcut bridge", () => {
  it("reads the saved shortcut and forwards only plain setting values", async () => {
    const h = loadPreload();

    await h.desktop.getGlobalShortcut();
    assert.equal(h.invokes.at(-1).channel, "omnigent:get-global-shortcut");

    await h.desktop.setGlobalShortcut({ enabled: true, accelerator: "Control+Shift+K", extra: 1 });
    assert.deepEqual(JSON.parse(JSON.stringify(h.invokes.at(-1))), {
      channel: "omnigent:set-global-shortcut",
      args: { enabled: true, accelerator: "Control+Shift+K" },
    });

    await h.desktop.recordGlobalShortcut();
    assert.equal(h.invokes.at(-1).channel, "omnigent:record-global-shortcut");
    await h.desktop.cancelGlobalShortcutRecording();
    assert.equal(h.invokes.at(-1).channel, "omnigent:cancel-global-shortcut-recording");
  });
});
