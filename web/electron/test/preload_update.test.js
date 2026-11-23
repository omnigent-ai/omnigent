const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { floatingComposerSessionPath } = require("../src/floating_composer_window");

const PRELOAD = fs.readFileSync(path.join(__dirname, "../src/preload.js"), "utf8");

function loadPreload({ platform = "linux", fullScreen = false, answers = {} } = {}) {
  const exposed = new Map();
  const listeners = new Map();
  const windowListeners = new Map();
  const invokes = [];
  const sends = [];
  const attributes = new Set();
  let updateStatus = { state: "idle" };
  const ipcRenderer = {
    invoke: async (channel, args, ...rest) => {
      invokes.push({ channel, args, ...(rest.some((arg) => arg !== undefined) ? { rest } : {}) });
      if (channel === "omnigent:get-update-status") return updateStatus;
      if (channel === "omnigent:is-full-screen") return fullScreen;
      return answers[channel] ? answers[channel](args) : null;
    },
    send: (channel, args) => sends.push({ channel, args }),
    on: (channel, listener) => listeners.set(channel, listener),
    removeListener: (channel, listener) => {
      if (listeners.get(channel) === listener) listeners.delete(channel);
    },
  };
  vm.runInNewContext(PRELOAD, {
    console,
    process: { platform },
    document: {
      documentElement: {
        style: { setProperty: () => {} },
        toggleAttribute: (name, force) => {
          if (force) attributes.add(name);
          else attributes.delete(name);
        },
      },
    },
    window: {
      addEventListener: (name, listener) => windowListeners.set(name, listener),
    },
    require: (specifier) => {
      assert.equal(specifier, "electron");
      return {
        contextBridge: { exposeInMainWorld: (name, value) => exposed.set(name, value) },
        ipcRenderer,
        webFrame: { getZoomFactor: () => 1 },
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
    emitWindow: (name) => windowListeners.get(name)?.(),
    hasAttribute: (name) => attributes.has(name),
    hasListener: (channel) => listeners.has(channel),
    invokes,
    sends,
  };
}

describe("macOS fullscreen titlebar bridge", () => {
  it("tracks native fullscreen state on initial load and shell events", async () => {
    const h = loadPreload({ platform: "darwin", fullScreen: true });
    h.emitWindow("DOMContentLoaded");
    await Promise.resolve();
    assert.equal(h.hasAttribute("data-electron-full-screen"), true);

    h.emit("omnigent:full-screen-changed", false);
    assert.equal(h.hasAttribute("data-electron-full-screen"), false);
  });
});

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

  it("forwards modifier-clicked link requests and advertises support", async () => {
    const h = loadPreload();
    const received = [];
    const unsubscribe = h.desktop.onBrowserOpenTab((request) => received.push(request));

    h.emit("browser-open-tab", { conversationId: "browser-tab:s:t", url: "https://example.com/" });
    assert.deepEqual(received, [
      { conversationId: "browser-tab:s:t", url: "https://example.com/" },
    ]);
    unsubscribe();
    assert.equal(h.hasListener("browser-open-tab"), false);

    await h.desktop.browserSetOpenTabSupported(true);
    await h.desktop.browserSetOpenTabSupported(false);
    assert.deepEqual(
      h.invokes
        .filter(({ channel }) => channel === "omnigent:browser-set-open-tab-supported")
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
    h.emit("omnigent:desktop-command", {
      command: "quit",
      quitDetail: "The local server will stop.",
    });
    h.emit("omnigent:desktop-command", { command: "quit", quitDetail: 1 });
    h.emit("omnigent:desktop-command", { command: 5 });
    h.emit("omnigent:desktop-command", null);
    assert.deepEqual(JSON.parse(JSON.stringify(received)), [
      {
        command: "close",
        browserViewId: "browser-tab:s:t",
        closingQuits: true,
        quitDetail: null,
      },
      { command: "new-terminal", browserViewId: null, closingQuits: false, quitDetail: null },
      {
        command: "quit",
        browserViewId: null,
        closingQuits: false,
        quitDetail: "The local server will stop.",
      },
      { command: "quit", browserViewId: null, closingQuits: false, quitDetail: null },
    ]);

    unsubscribe();
    assert.equal(h.hasListener("omnigent:desktop-command"), false);
  });

  it("reports command state as plain values and asks main to close the window or quit", async () => {
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

    await h.desktop.confirmQuit({ dontAskAgain: "yes" });
    assert.deepEqual(JSON.parse(JSON.stringify(h.invokes.at(-1))), {
      channel: "omnigent:confirm-quit",
      args: { dontAskAgain: false },
    });
  });
});

describe("browser view bridge", () => {
  it("asks main to focus a re-attached page only when told to", async () => {
    const h = loadPreload();
    await h.desktop.browserSetActive("browser-tab:s:t");
    await h.desktop.browserSetActive("browser-tab:s:t", { focus: true });
    assert.deepEqual(JSON.parse(JSON.stringify(h.invokes.slice(-2))), [
      {
        channel: "omnigent:browser-set-active",
        args: { conversationId: "browser-tab:s:t", focus: false },
      },
      {
        channel: "omnigent:browser-set-active",
        args: { conversationId: "browser-tab:s:t", focus: true },
      },
    ]);
  });

  it("asks main which page has keyboard focus", async () => {
    const h = loadPreload();
    await h.desktop.browserFocusedView();
    assert.equal(h.invokes.at(-1).channel, "omnigent:browser-focused-view");
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
    // Turning off sends no chord, so the shell keeps its saved one.
    await h.desktop.setGlobalShortcut({ enabled: false, extra: 1 });
    assert.deepEqual(JSON.parse(JSON.stringify(h.invokes.at(-1))), {
      channel: "omnigent:set-global-shortcut",
      args: { enabled: false },
    });
    await h.desktop.setGlobalShortcut({ enabled: false, accelerator: "Alt+J" });
    assert.deepEqual(JSON.parse(JSON.stringify(h.invokes.at(-1))).args, {
      enabled: false,
      accelerator: "Alt+J",
    });

    await h.desktop.recordGlobalShortcut();
    assert.equal(h.invokes.at(-1).channel, "omnigent:record-global-shortcut");
    await h.desktop.cancelGlobalShortcutRecording();
    assert.equal(h.invokes.at(-1).channel, "omnigent:cancel-global-shortcut-recording");
  });

  it("lets the floating panel hear that it took the keyboard, until it stops listening", () => {
    const h = loadPreload({ platform: "darwin" });
    const channel = "omnigent:floating-composer:shown";
    assert.equal(typeof h.desktop.floatingComposer.onShown("not a function"), "function");
    assert.equal(h.hasListener(channel), false);
    let heard = 0;
    const unsubscribe = h.desktop.floatingComposer.onShown(() => (heard += 1));
    h.emit(channel);
    assert.equal(heard, 1);
    unsubscribe();
    assert.equal(h.hasListener(channel), false);
  });

  it("lets a main window take the panel's launches, telling the shell while it listens", async () => {
    const message = { text: "hi", skill: null, files: [] };
    let answer = (id) => (id === 0 ? message : null);
    const h = loadPreload({
      platform: "darwin",
      answers: { "omnigent:floating-composer:take-launch": (id) => answer(id) },
    });
    const channel = "omnigent:floating-composer:launch";
    const takes = () =>
      h.invokes.filter(({ channel: name }) => name === "omnigent:floating-composer:take-launch");
    assert.equal(typeof h.desktop.floatingComposer.onLaunch("not a function"), "function");
    assert.equal(h.hasListener(channel), false);
    const heard = [];
    const stop = h.desktop.floatingComposer.onLaunch((launch) => heard.push(launch));
    assert.equal(h.sends.at(-1).channel, "omnigent:floating-composer:launch-listener");
    assert.equal(h.sends.at(-1).args, true);

    // Taken: the page gets the message. Nothing to take (expired, or another page's): nothing.
    await h.emit(channel, { id: 0, path: "/c/abc" });
    await h.emit(channel, { id: 1, path: "/c/def" });
    assert.equal(heard.length, 1);
    assert.equal(heard[0].path, "/c/abc");
    assert.equal(heard[0].firstMessage, message);

    // Only what the shell opens as a session is taken, so a taken message always has a chat.
    await h.emit(channel, undefined);
    const routes = [
      "/c/conv_A-1",
      `/c/${"a".repeat(128)}`,
      `/c/${"a".repeat(129)}`,
      "/c/",
      "/c/a b",
      "/c/abc/",
      "/settings/general",
      "https://evil.example/c/abc",
    ];
    await Promise.all(
      routes.map((route, index) => h.emit(channel, { id: index + 10, path: route })),
    );
    const taken = new Set(takes().map(({ args }) => args));
    for (const [index, route] of routes.entries()) {
      assert.equal(taken.has(index + 10), floatingComposerSessionPath(route) !== null, route);
    }

    // A message taken as the page stops listening is still handed to it.
    let release = () => {};
    answer = () =>
      new Promise((resolve) => {
        release = () => resolve(message);
      });
    const pending = h.emit(channel, { id: 20, path: "/c/late" });
    stop();
    assert.equal(h.hasListener(channel), false);
    assert.equal(h.sends.at(-1).args, false);
    release();
    await pending;
    assert.equal(heard.at(-1).path, "/c/late");
  });

  it("sends a launched session's first message along when the panel opens it", async () => {
    const h = loadPreload({ platform: "darwin" });
    const message = { text: "hi", skill: null, files: [] };
    await h.desktop.floatingComposer.openInMainWindow("/c/abc", message);
    const call = h.invokes.at(-1);
    assert.equal(call.channel, "omnigent:floating-composer:open-in-main-window");
    assert.equal(call.args, "/c/abc");
    assert.equal(call.rest[0], message);
  });

  it("forwards the floating panel's ready signal on its own channel", () => {
    const h = loadPreload({ platform: "darwin" });
    h.desktop.floatingComposer.ready();
    assert.equal(h.sends.at(-1).channel, "omnigent:floating-composer:ready");
  });

  it("names the shortcut by id for the shell to check, and drops ids that aren't strings", async () => {
    const h = loadPreload();
    const sent = () => JSON.parse(JSON.stringify(h.invokes.at(-1)));

    await h.desktop.getGlobalShortcut("floatingComposer");
    assert.deepEqual(sent(), { channel: "omnigent:get-global-shortcut", args: "floatingComposer" });
    await h.desktop.setGlobalShortcut({ enabled: false }, "floatingComposer");
    assert.deepEqual(sent(), {
      channel: "omnigent:set-global-shortcut",
      args: { enabled: false },
      rest: ["floatingComposer"],
    });
    await h.desktop.recordGlobalShortcut("floatingComposer");
    assert.deepEqual(sent(), {
      channel: "omnigent:record-global-shortcut",
      args: "floatingComposer",
    });
    await h.desktop.getGlobalShortcut({ id: "floatingComposer" });
    assert.deepEqual(sent(), { channel: "omnigent:get-global-shortcut" });
  });
});

describe("system dictation bridge", () => {
  it("is exposed on macOS only", async () => {
    assert.equal(loadPreload({ platform: "linux" }).desktop.startSystemDictation, undefined);
    assert.equal(loadPreload({ platform: "win32" }).desktop.startSystemDictation, undefined);

    const h = loadPreload({ platform: "darwin" });
    await h.desktop.startSystemDictation();
    assert.deepEqual(h.invokes.at(-1), {
      channel: "omnigent:start-system-dictation",
      args: undefined,
    });
  });
});
