"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const {
  createArcaConnectFlow,
  CONTENT_WIDTH,
  MIN_CONTENT_HEIGHT,
  RUNNING_CONTENT_HEIGHT,
  MAX_CONTENT_HEIGHT,
} = require("../src/arca_connect_window");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const flushPromises = () =>
  new Promise((resolve) => {
    setImmediate(resolve);
  });

/** Load the console page with a fake bridge; jsdom has no layout, so scrollHeight is stubbed. */
function renderConsole(t, { scrollHeight }) {
  const dom = new JSDOM(
    fs.readFileSync(path.join(__dirname, "../arca-connect/index.html"), "utf8"),
    {
      runScripts: "outside-only",
    },
  );
  t.after(() => dom.window.close());
  const page = { document: dom.window.document, events: {}, sizes: [] };
  // Electron supplies this bridge; no remote process is launched by a renderer test.
  dom.window.arcaConnect = {
    confirm() {},
    cancel() {},
    resizeToContent: (size) => page.sizes.push(size),
  };
  dom.window.Terminal = class Terminal {
    loadAddon() {}
    open() {}
    write() {}
  };
  dom.window.FitAddon = {
    FitAddon: class FitAddon {
      fit() {}
    },
  };
  for (const event of ["Init", "Started", "Output", "Done"]) {
    dom.window.arcaConnect[`on${event}`] = (listener) => {
      page.events[event] = listener;
    };
  }
  dom.window.requestAnimationFrame = (callback) => {
    callback();
    return 1;
  };
  dom.window.cancelAnimationFrame = () => {};
  Object.defineProperty(page.document.documentElement, "scrollHeight", { value: scrollHeight });
  dom.window.eval(fs.readFileSync(path.join(__dirname, "../arca-connect/console.js"), "utf8"));
  return page;
}

for (const copy of [
  {
    name: "ordinary connection",
    reason: null,
    title: "Connect your Arca instance?",
    intro: "This runs the command below over ",
    button: "Connect",
  },
  {
    name: "preview reconnect",
    reason: "preview-reconnect",
    title: "Reconnect Arca for this preview?",
    intro: "To load this preview, run the command below over ",
    button: "Reconnect",
  },
]) {
  it(`renders consent, sign-in, retry, and completion for ${copy.name}`, (t) => {
    const { document, events, sizes } = renderConsole(t, { scrollHeight: 360 });
    const confirm = document.getElementById("confirm");
    const command = document.getElementById("command");
    events.Init({
      serverUrl: "https://account.databricks.com/omnigent?o=123",
      command: "host command",
      reason: copy.reason,
    });
    assert.equal(document.getElementById("title").textContent, copy.title);
    assert.equal(document.getElementById("intro").textContent, copy.intro);
    assert.equal(confirm.textContent, copy.button);
    assert.equal(sizes.at(-1).height, 360);
    assert.equal(sizes.at(-1).phase, "asking");
    confirm.click();
    events.Started({ login: false, command: "host command" });
    assert.equal(confirm.disabled, true);
    // The console can go straight from the connect to sign-in.
    events.Started({ login: true, command: "login command" });
    assert.equal(command.textContent, "login command");
    assert.equal(confirm.textContent, "Signing in…");
    assert.equal(confirm.disabled, true);
    events.Started({ login: false, command: "host command" });
    assert.equal(command.textContent, "host command");
    assert.equal(confirm.textContent, "Connecting…");
    events.Done({ ok: true });
    assert.equal(confirm.hidden, true);
    assert.equal(document.getElementById("cancel").textContent, "Close");
    assert.match(document.getElementById("status").textContent, /Connected/);
  });
}

describe("arca connect console failure display", () => {
  const failed = {
    ok: false,
    error: "Arca sign-in timed out. Check Arca Companion, finish browser sign-in, and try again.",
    errorMessage: "Arca sign-in timed out.",
    errorAction: "Check Arca Companion, finish browser sign-in, and try again.",
    canCopyDiagnostics: true,
  };

  it("shows the failure and the next step on separate lines with distinct styling hooks", (t) => {
    const page = renderConsole(t, { scrollHeight: 400 });
    page.events.Done(failed);
    const status = page.document.getElementById("status");
    assert.equal(status.dataset.kind, "error");
    assert.equal(page.document.getElementById("status-message").textContent, failed.errorMessage);
    assert.equal(page.document.getElementById("status-action").textContent, failed.errorAction);
    assert.equal(page.document.getElementById("diagnostics").hidden, false);
    const css = fs.readFileSync(path.join(__dirname, "../arca-connect/index.html"), "utf8");
    // Only the failure line is red; the action inherits the neutral foreground.
    assert.match(css, /#status\[data-kind="error"\] #status-message\s*\{\s*color: var\(--danger\)/);
    assert.match(css, /#status-action\s*\{\s*color: var\(--fg\)/);
  });

  it("keeps a legacy combined error on the failure line and hides empty action", (t) => {
    const page = renderConsole(t, { scrollHeight: 400 });
    page.events.Done({ ok: false, error: "Connecting to Arca failed (exit code 1)." });
    assert.equal(
      page.document.getElementById("status-message").textContent,
      "Connecting to Arca failed (exit code 1).",
    );
    assert.equal(page.document.getElementById("status-action").textContent, "");
    assert.equal(page.document.getElementById("diagnostics").hidden, true);
  });

  it("reports Copied only when the shell confirms the clipboard write", async (t) => {
    const page = renderConsole(t, { scrollHeight: 400 });
    const results = [{ ok: false }, { ok: true }];
    page.document.defaultView.arcaConnect.copyDiagnostics = async () => results.shift();
    page.events.Done(failed);
    const button = page.document.getElementById("copy-diagnostics");
    const state = page.document.getElementById("copy-state");
    button.click();
    await flushPromises();
    assert.match(state.textContent, /Couldn't copy/);
    button.click();
    await flushPromises();
    assert.equal(state.textContent, "Copied");
  });

  it("reports a failed copy when the bridge call rejects", async (t) => {
    const page = renderConsole(t, { scrollHeight: 400 });
    page.document.defaultView.arcaConnect.copyDiagnostics = async () => {
      throw new Error("ipc gone");
    };
    page.events.Done(failed);
    page.document.getElementById("copy-diagnostics").click();
    await flushPromises();
    assert.match(page.document.getElementById("copy-state").textContent, /Couldn't copy/);
  });
});

/** A fake Electron world: BrowserWindow + ipcMain + a controllable connect. */
function flowHarness({ clipboard } = {}) {
  const world = {
    windows: [],
    ipc: new EventEmitter(),
    handlers: {},
    connects: [],
    logins: [],
  };

  class FakeWindow extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.webContents = new EventEmitter();
      this.webContents.id = 100 + world.windows.length;
      this.webContents.sent = [];
      this.webContents.send = (channel, payload) => {
        this.webContents.sent.push([channel, payload]);
      };
      this.destroyed = false;
      this.shown = 0;
      this.focused = 0;
      this.contentSize = null;
      world.windows.push(this);
    }
    setContentSize(width, height) {
      this.contentSize = [width, height];
    }
    loadFile() {
      // The page "loads" immediately: fire the init + show hooks.
      this.webContents.emit("did-finish-load");
      this.emit("ready-to-show");
    }
    isDestroyed() {
      return this.destroyed;
    }
    show() {
      this.shown += 1;
    }
    focus() {
      this.focused += 1;
    }
    setTitle(title) {
      this.title = title;
    }
    close() {
      this.destroyed = true;
      this.emit("closed");
    }
  }
  const flow = createArcaConnectFlow({
    BrowserWindow: FakeWindow,
    ipcMain: {
      on: (channel, fn) => world.ipc.on(channel, fn),
      handle: (channel, fn) => {
        world.handlers[channel] = fn;
      },
    },
    clipboard,
    pagePath: "/bundle/arca-connect/index.html",
    preloadPath: "/bundle/src/arca_connect_preload.js",
    commandLine: (url) => `arca ssh isaac omni host --server ${url} …`,
    loginCommandLine: (url) => `arca ssh isaac omni login ${url}`,
    startLogin: (serverUrl) => {
      const login = {
        serverUrl,
        canceled: false,
        cancel() {
          this.canceled = true;
        },
      };
      login.promise = new Promise((resolve) => {
        login.finish = resolve;
      });
      world.logins.push(login);
      return login;
    },
    startConnect: (serverUrl, onOutput) => {
      const connect = {
        serverUrl,
        onOutput,
        canceled: false,
        cancel() {
          this.canceled = true;
        },
      };
      connect.promise = new Promise((resolve) => {
        connect.finish = resolve;
      });
      world.connects.push(connect);
      return connect;
    },
  });

  /** Emit a preload→main message as if it came from `win`'s webContents. */
  world.sendFrom = (win, channel, payload) =>
    world.ipc.emit(channel, { sender: { id: win.webContents.id } }, payload);
  /** Invoke an ipcMain.handle channel as if from `win`'s webContents. */
  world.invokeFrom = (win, channel, payload) =>
    world.handlers[channel]({ sender: { id: win.webContents.id } }, payload);
  return { world, flow };
}

describe("arca connect console flow", () => {
  it("does not share another workspace's in-flight identity", async () => {
    const { world, flow } = flowHarness();
    const server = "https://account.databricks.com/omnigent?o=123";
    const first = flow.run(null, server);
    const win = world.windows[0];
    world.sendFrom(win, "arca-connect:confirm");
    const second = await flow.run(null, server.replace("123", "456"));
    assert.equal(second.ok, false);
    assert.equal(second.identity, undefined);
    assert.equal(world.windows.length, 1);
    assert.equal(world.connects.length, 1);
    const identity = { serverUrl: server, hostId: "a".repeat(32) };
    world.connects[0].finish({ ok: true, identity });
    assert.deepEqual((await first).identity, identity);
  });

  it("signs in under the Connect consent, then retries the identical target only once", async () => {
    const { world, flow } = flowHarness();
    const url = "https://account.databricks.com/omnigent?o=123";
    const result = flow.run(null, url);
    const win = world.windows[0];
    world.ipc.emit("arca-connect:confirm", { sender: { id: 9999 } });
    assert.equal(world.connects.length, 0);
    world.sendFrom(win, "arca-connect:confirm");
    world.connects[0].finish({ ok: false, authError: true });
    await Promise.resolve();
    assert.equal(world.logins.length, 1);
    assert.equal(world.logins[0].serverUrl, url);
    const started = win.webContents.sent.filter(([channel]) => channel === "arca-connect:started");
    assert.deepEqual(started.at(-1)[1], {
      login: true,
      command: `arca ssh isaac omni login ${url}`,
    });
    assert.equal(
      win.webContents.sent.some(([channel]) => channel === "arca-connect:done"),
      false,
    );
    world.sendFrom(win, "arca-connect:confirm");
    assert.equal(world.logins.length, 1);
    world.logins[0].finish({ ok: true });
    await Promise.resolve();
    assert.equal(world.connects[1].serverUrl, url);
    world.connects[1].finish({ ok: false, authError: true, error: "still rejected" });
    assert.equal((await result).error, "still rejected");
    world.sendFrom(win, "arca-connect:confirm");
    assert.equal(world.logins.length, 1);
  });

  it("shows a failed sign-in and doesn't retry the host", async () => {
    const { world, flow } = flowHarness();
    const result = flow.run(null, "https://account.databricks.com/omnigent?o=123");
    const win = world.windows[0];
    world.sendFrom(win, "arca-connect:confirm");
    world.connects[0].finish({ ok: false, authError: true });
    await Promise.resolve();
    world.logins[0].finish({ ok: false, authError: true, error: "sign-in timed out" });
    assert.equal((await result).error, "sign-in timed out");
    assert.equal(world.connects.length, 1);
    const done = win.webContents.sent.filter(([channel]) => channel === "arca-connect:done");
    assert.deepEqual(done.at(-1)[1], {
      ok: false,
      error: "sign-in timed out",
      canCopyDiagnostics: false,
    });
  });

  it("signs in before connecting when a connect already failed for sign-in", async () => {
    const { world, flow } = flowHarness();
    const url = "https://account.databricks.com/omnigent?o=123";
    const result = flow.run(null, url, { signInFirst: true });
    const win = world.windows[0];
    assert.equal(world.logins.length, 0);
    world.sendFrom(win, "arca-connect:confirm");
    assert.equal(world.connects.length, 0);
    assert.equal(world.logins.length, 1);
    world.logins[0].finish({ ok: true });
    await Promise.resolve();
    assert.equal(world.connects[0].serverUrl, url);
    // The sign-in already ran, so a connect that still needs one doesn't start another.
    world.connects[0].finish({ ok: false, authError: true, error: "still rejected" });
    assert.equal((await result).error, "still rejected");
    assert.equal(world.logins.length, 1);
  });

  it("cancels remote login without starting a host after a late successful exit", async () => {
    const { world, flow } = flowHarness();
    const result = flow.run(null, "https://account.databricks.com/omnigent?o=123");
    const win = world.windows[0];
    world.sendFrom(win, "arca-connect:confirm");
    world.connects[0].finish({ ok: false, authError: true });
    await Promise.resolve();
    win.close();
    assert.equal(world.logins[0].canceled, true);
    world.logins[0].finish({ ok: true });
    assert.equal((await result).canceled, true);
    assert.equal(world.connects.length, 1);
  });
  it("shows the command, runs only after confirm, streams output, resolves the result", async () => {
    const { world, flow } = flowHarness();
    const resultPromise = flow.run(null, "https://srv.example.com");
    const win = world.windows[0];

    const init = win.webContents.sent.find(([c]) => c === "arca-connect:init");
    assert.equal(init[1].command, "arca ssh isaac omni host --server https://srv.example.com …");
    assert.equal(init[1].reason, null);
    assert.equal(world.connects.length, 0); // nothing ran yet — consent first

    world.sendFrom(win, "arca-connect:confirm");
    assert.equal(world.connects.length, 1);
    world.sendFrom(win, "arca-connect:resize", { height: 420, phase: "running" });
    assert.deepEqual(win.contentSize, [CONTENT_WIDTH, RUNNING_CONTENT_HEIGHT]);
    world.connects[0].onOutput("booting…\n");
    assert.ok(
      win.webContents.sent.some(([c, p]) => c === "arca-connect:output" && p === "booting…\n"),
    );

    world.connects[0].finish({ ok: true });
    assert.deepEqual(await resultPromise, { ok: true, shownInConsole: true });
    const done = win.webContents.sent.find(([c]) => c === "arca-connect:done");
    assert.deepEqual(done[1], { ok: true, error: null });
    assert.equal(win.destroyed, false); // stays open so the output is readable
  });

  it("labels preview recovery as a reconnect before asking consent", () => {
    const { world, flow } = flowHarness();
    void flow.run(null, "https://srv.example.com", { reason: "preview-reconnect" });
    const win = world.windows[0];
    const init = win.webContents.sent.find(([channel]) => channel === "arca-connect:init");
    assert.equal(win.options.title, "Reconnect Arca");
    assert.equal(init[1].reason, "preview-reconnect");
    assert.equal(world.connects.length, 0);
  });

  it("updates consent copy when a preview reattaches before confirmation", () => {
    const { world, flow } = flowHarness();
    void flow.run(null, "https://srv.example.com");
    const win = world.windows[0];
    void flow.run(null, "https://srv.example.com", { reason: "preview-reconnect" });
    const init = win.webContents.sent.filter(([channel]) => channel === "arca-connect:init").at(-1);
    assert.equal(win.title, "Reconnect Arca");
    assert.equal(init[1].reason, "preview-reconnect");
    assert.equal(world.windows.length, 1);
  });

  it("uses content bounds and honors only its renderer's measured height", () => {
    const { world, flow } = flowHarness();
    void flow.run(null, "https://srv.example.com/a/very/long/path");
    const win = world.windows[0];
    assert.equal(win.options.useContentSize, true);
    assert.equal(win.options.height, MIN_CONTENT_HEIGHT);
    assert.equal(win.options.resizable, false);

    world.sendFrom(win, "arca-connect:resize", { height: 100, phase: "running" });
    assert.deepEqual(win.contentSize, [CONTENT_WIDTH, MIN_CONTENT_HEIGHT]);
    world.sendFrom(win, "arca-connect:confirm");
    world.sendFrom(win, "arca-connect:resize", { height: 412, phase: "asking" });
    assert.deepEqual(win.contentSize, [CONTENT_WIDTH, RUNNING_CONTENT_HEIGHT]);
    world.sendFrom(win, "arca-connect:resize", { height: 5000, phase: "asking" });
    assert.deepEqual(win.contentSize, [CONTENT_WIDTH, MAX_CONTENT_HEIGHT]);
    world.ipc.emit("arca-connect:resize", { sender: { id: 9999 } }, { height: 333 });
    assert.deepEqual(win.contentSize, [CONTENT_WIDTH, MAX_CONTENT_HEIGHT]);
  });

  it("Close still works after the run finished (routing outlives the result)", async () => {
    const { world, flow } = flowHarness();
    const resultPromise = flow.run(null, "https://srv.example.com");
    const win = world.windows[0];
    world.sendFrom(win, "arca-connect:confirm");
    world.connects[0].finish({ ok: true });
    await resultPromise; // the run settled — the console is now just a report

    world.sendFrom(win, "arca-connect:cancel"); // the "Close" button
    assert.equal(win.destroyed, true);
  });

  it("ignores confirm from a foreign webContents (a server page can't consent)", () => {
    const { world, flow } = flowHarness();
    void flow.run(null, "https://srv.example.com");
    world.ipc.emit("arca-connect:confirm", { sender: { id: 9999 } });
    assert.equal(world.connects.length, 0);
  });

  it("resolves a silent cancel when closed before confirming", async () => {
    const { world, flow } = flowHarness();
    const resultPromise = flow.run(null, "https://srv.example.com");
    world.sendFrom(world.windows[0], "arca-connect:cancel");
    const result = await resultPromise;
    assert.equal(result.ok, false);
    // Declining is a deliberate choice, not a failure — flagged so the
    // picker doesn't render an error for it.
    assert.equal(result.canceled, true);
    assert.match(result.error, /wasn't approved/);
  });

  it("flags a failed run as already shown in the console", async () => {
    const { world, flow } = flowHarness();
    const resultPromise = flow.run(null, "https://srv.example.com");
    world.sendFrom(world.windows[0], "arca-connect:confirm");
    world.connects[0].finish({ ok: false, error: "Couldn't reach the Arca instance." });
    const result = await resultPromise;
    assert.equal(result.ok, false);
    assert.equal(result.shownInConsole, true);
  });

  it("kills the command when the console is closed mid-run", async () => {
    const { world, flow } = flowHarness();
    const resultPromise = flow.run(null, "https://srv.example.com");
    const win = world.windows[0];
    world.sendFrom(win, "arca-connect:confirm");
    win.close();
    assert.equal(world.connects[0].canceled, true);
    const result = await resultPromise;
    assert.equal(result.canceled, true);
  });

  it("re-attaches to an in-flight console instead of refusing", async () => {
    const { world, flow } = flowHarness();
    const first = flow.run(null, "https://srv.example.com");
    const win = world.windows[0];
    world.sendFrom(win, "arca-connect:confirm");

    // A refreshed SPA clicks again: same console focused, same outcome shared.
    const second = flow.run(null, "https://srv.example.com");
    assert.equal(world.windows.length, 1);
    assert.equal(win.focused, 1);

    world.connects[0].finish({ ok: true });
    assert.deepEqual(await first, { ok: true, shownInConsole: true });
    assert.deepEqual(await second, { ok: true, shownInConsole: true });

    // After settling, a new run opens a fresh console.
    void flow.run(null, "https://srv.example.com");
    assert.equal(world.windows.length, 2);
  });

  it("copies shell-built diagnostics without raw output, tickets or the OAuth URL", async () => {
    const written = [];
    const { world, flow } = flowHarness({ clipboard: { writeText: (text) => written.push(text) } });
    const url = "https://account.databricks.com/omnigent?o=123&ticket=SECRET-TICKET";
    const result = flow.run(null, url);
    const win = world.windows[0];
    world.sendFrom(win, "arca-connect:confirm");
    world.connects[0].onOutput("https://idp.example.com/auth?token=RAWTOKEN /home/alice/.ssh");
    world.connects[0].finish({
      ok: false,
      errorKind: "timeout",
      error:
        "Connecting to Arca timed out. The instance may still be starting. Check `arca status`.",
      errorMessage: "Connecting to Arca timed out. The instance may still be starting.",
      errorAction: "Check `arca status` and try again.",
      stdout: "https://idp.example.com/auth?token=RAWTOKEN",
      stderr: "/home/alice/.ssh/id_rsa",
    });
    await result;
    const done = win.webContents.sent.filter(([channel]) => channel === "arca-connect:done");
    assert.equal(
      done.at(-1)[1].errorMessage,
      "Connecting to Arca timed out. The instance may still be starting.",
    );
    assert.equal(done.at(-1)[1].errorAction, "Check `arca status` and try again.");
    assert.equal(done.at(-1)[1].canCopyDiagnostics, true);
    assert.deepEqual(await world.invokeFrom(win, "arca-connect:copy-diagnostics"), { ok: true });
    assert.equal(written.length, 1);
    const text = written[0];
    assert.match(text, /Failure kind: timeout/);
    assert.match(text, /Failure: Connecting to Arca timed out\./);
    assert.match(text, /Next step: Check `arca status` and try again\./);
    assert.match(text, /Stage: connect/);
    assert.match(text, /Server: https:\/\/account\.databricks\.com\n/);
    for (const secret of ["SECRET-TICKET", "RAWTOKEN", "/home/alice", "idp.example.com", "o=123"]) {
      assert.ok(!text.includes(secret), `diagnostics leaked ${secret}`);
    }
  });

  it("reports a failed copy and refuses foreign senders or a clean run", async () => {
    const failing = flowHarness({
      clipboard: {
        writeText: () => {
          throw new Error("denied");
        },
      },
    });
    const result = failing.flow.run(null, "https://srv.example.com");
    const win = failing.world.windows[0];
    // Nothing to copy until a failure exists.
    assert.deepEqual(await failing.world.invokeFrom(win, "arca-connect:copy-diagnostics"), {
      ok: false,
    });
    failing.world.sendFrom(win, "arca-connect:confirm");
    failing.world.connects[0].finish({ ok: false, error: "legacy failure" });
    await result;
    assert.deepEqual(await failing.world.invokeFrom(win, "arca-connect:copy-diagnostics"), {
      ok: false,
    });
    assert.deepEqual(
      await failing.world.handlers["arca-connect:copy-diagnostics"]({ sender: { id: 9999 } }),
      { ok: false },
    );
  });

  it("withholds legacy error text from diagnostics", async () => {
    const written = [];
    const { world, flow } = flowHarness({ clipboard: { writeText: (text) => written.push(text) } });
    const result = flow.run(null, "https://srv.example.com");
    const win = world.windows[0];
    world.sendFrom(win, "arca-connect:confirm");
    world.connects[0].finish({
      ok: false,
      error: "Connecting to Arca failed: Permission denied (publickey) for /home/alice",
    });
    await result;
    await world.invokeFrom(win, "arca-connect:copy-diagnostics");
    assert.match(written[0], /Failure kind: unknown/);
    assert.match(written[0], /Failure: details withheld/);
    assert.ok(!written[0].includes("alice"));
  });
});
