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
  OUTPUT_BACKLOG_CHARS,
} = require("../src/arca_connect_window");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

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
  page.calls = [];
  dom.window.arcaConnect = {
    confirm: () => page.calls.push("confirm"),
    cancel: () => page.calls.push("cancel"),
    stop: () => page.calls.push("stop"),
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

it("renders the output view of a background connect with Hide and Stop", (t) => {
  const { document, events, calls } = renderConsole(t, { scrollHeight: 420 });
  const confirm = document.getElementById("confirm");
  const cancel = document.getElementById("cancel");
  const hide = document.getElementById("hide");
  events.Init({ serverUrl: "https://srv.example.com", command: "host command", view: "output" });
  assert.equal(document.getElementById("title").textContent, "Connecting your Arca instance");
  assert.equal(document.getElementById("trust-notice").hidden, true);
  assert.equal(document.getElementById("background-notice").hidden, false);
  assert.equal(confirm.hidden, true);
  assert.equal(hide.hidden, false);
  assert.equal(cancel.textContent, "Stop connecting");

  events.Started({ login: false, command: "host command" });
  assert.equal(confirm.hidden, true);
  hide.click();
  cancel.click();
  assert.deepEqual(calls, ["cancel", "stop"]);

  events.Done({ ok: false, error: "Couldn't reach the Arca instance." });
  assert.equal(hide.hidden, true);
  assert.equal(cancel.textContent, "Close");
  assert.equal(document.getElementById("status").textContent, "Couldn't reach the Arca instance.");
  cancel.click();
  assert.deepEqual(calls, ["cancel", "stop", "cancel"]);
});

it("keeps the trust notice and no Hide button while asking consent", (t) => {
  const { document, events } = renderConsole(t, { scrollHeight: 360 });
  events.Init({ serverUrl: "https://srv.example.com", command: "host command" });
  assert.equal(document.getElementById("trust-notice").hidden, false);
  assert.equal(document.getElementById("background-notice").hidden, true);
  assert.equal(document.getElementById("hide").hidden, true);
  assert.equal(document.getElementById("confirm").hidden, false);
});

/** A fake Electron world: BrowserWindow + ipcMain + a controllable connect. */
function flowHarness() {
  const world = {
    windows: [],
    ipc: new EventEmitter(),
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
    },
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
  return { world, flow };
}

const SERVER = "https://srv.example.com";
const channelPayloads = (win, name) =>
  win.webContents.sent.filter(([channel]) => channel === name).map(([, payload]) => payload);

/** Start a run and confirm it in its consent sheet; returns the run's result promise. */
function confirmedRun(world, flow, options) {
  const result = flow.run(null, SERVER, options);
  const consent = world.windows.at(-1);
  world.sendFrom(consent, "arca-connect:confirm");
  return { result, consent };
}

describe("arca connect console flow", () => {
  it("does not share another workspace's in-flight identity", async () => {
    const { world, flow } = flowHarness();
    const server = "https://account.databricks.com/omnigent?o=123";
    const first = flow.run(null, server);
    world.sendFrom(world.windows[0], "arca-connect:confirm");
    const windowsAfterConfirm = world.windows.length;
    const second = await flow.run(null, server.replace("123", "456"));
    assert.equal(second.ok, false);
    assert.equal(second.identity, undefined);
    assert.equal(world.windows.length, windowsAfterConfirm);
    assert.equal(world.connects.length, 1);
    assert.equal(flow.showOutput(null, server.replace("123", "456")), false);
    const identity = { serverUrl: server, hostId: "a".repeat(32) };
    world.connects[0].finish({ ok: true, identity });
    assert.deepEqual((await first).identity, identity);
  });

  it("asks consent in a modal sheet and releases it once confirmed", () => {
    const { world, flow } = flowHarness();
    const parent = new EventEmitter();
    void flow.run(parent, SERVER);
    const consent = world.windows[0];
    assert.equal(consent.options.modal, true);
    assert.equal(consent.options.parent, parent);
    assert.equal(channelPayloads(consent, "arca-connect:init")[0].view, "consent");
    assert.equal(world.connects.length, 0); // nothing ran yet — consent first

    world.sendFrom(consent, "arca-connect:confirm");
    assert.equal(world.connects.length, 1);
    assert.equal(consent.destroyed, true);
    assert.equal(world.connects[0].canceled, false); // closing the sheet keeps the run going
    const output = world.windows[1];
    assert.equal(output.options.modal, false);
    assert.equal(output.options.parent, parent);
    assert.equal(channelPayloads(output, "arca-connect:init")[0].view, "output");
  });

  it("signs in under the Connect consent, then retries the identical target only once", async () => {
    const { world, flow } = flowHarness();
    const url = "https://account.databricks.com/omnigent?o=123";
    const result = flow.run(null, url);
    const consent = world.windows[0];
    world.ipc.emit("arca-connect:confirm", { sender: { id: 9999 } });
    assert.equal(world.connects.length, 0);
    world.sendFrom(consent, "arca-connect:confirm");
    const output = world.windows[1];
    world.connects[0].finish({ ok: false, authError: true });
    await Promise.resolve();
    assert.equal(world.logins.length, 1);
    assert.equal(world.logins[0].serverUrl, url);
    assert.deepEqual(channelPayloads(output, "arca-connect:started").at(-1), {
      login: true,
      command: `arca ssh isaac omni login ${url}`,
    });
    assert.equal(channelPayloads(output, "arca-connect:done").length, 0);
    world.sendFrom(output, "arca-connect:confirm");
    assert.equal(world.logins.length, 1);
    world.logins[0].finish({ ok: true });
    await Promise.resolve();
    assert.equal(world.connects[1].serverUrl, url);
    world.connects[1].finish({ ok: false, authError: true, error: "still rejected" });
    assert.equal((await result).error, "still rejected");
    world.sendFrom(output, "arca-connect:confirm");
    assert.equal(world.logins.length, 1);
  });

  it("shows a failed sign-in and doesn't retry the host", async () => {
    const { world, flow } = flowHarness();
    const { result } = confirmedRun(world, flow);
    const output = world.windows[1];
    world.connects[0].finish({ ok: false, authError: true });
    await Promise.resolve();
    world.logins[0].finish({ ok: false, authError: true, error: "sign-in timed out" });
    assert.equal((await result).error, "sign-in timed out");
    assert.equal(world.connects.length, 1);
    assert.deepEqual(channelPayloads(output, "arca-connect:done").at(-1), {
      ok: false,
      error: "sign-in timed out",
    });
  });

  it("stops remote login without starting a host after a late successful exit", async () => {
    const { world, flow } = flowHarness();
    const { result } = confirmedRun(world, flow);
    const output = world.windows[1];
    world.connects[0].finish({ ok: false, authError: true });
    await Promise.resolve();
    world.sendFrom(output, "arca-connect:stop");
    assert.equal(world.logins[0].canceled, true);
    world.logins[0].finish({ ok: true });
    assert.equal((await result).canceled, true);
    assert.equal(world.connects.length, 1);
  });

  it("streams output into the output window and resolves the result", async () => {
    const { world, flow } = flowHarness();
    const { result } = confirmedRun(world, flow);
    const output = world.windows[1];
    const init = channelPayloads(output, "arca-connect:init")[0];
    assert.equal(init.command, "arca ssh isaac omni host --server https://srv.example.com …");
    assert.equal(init.reason, null);
    world.connects[0].onOutput("booting…\n");
    assert.deepEqual(channelPayloads(output, "arca-connect:output"), ["booting…\n"]);

    world.connects[0].finish({ ok: true });
    assert.deepEqual(await result, { ok: true, shownInConsole: true });
    assert.deepEqual(channelPayloads(output, "arca-connect:done")[0], { ok: true, error: null });
    assert.equal(output.destroyed, false); // stays open so the output is readable
  });

  it("labels preview recovery as a reconnect before asking consent", () => {
    const { world, flow } = flowHarness();
    void flow.run(null, SERVER, { reason: "preview-reconnect" });
    const win = world.windows[0];
    const init = channelPayloads(win, "arca-connect:init")[0];
    assert.equal(win.options.title, "Reconnect Arca");
    assert.equal(init.reason, "preview-reconnect");
    assert.equal(world.connects.length, 0);
  });

  it("updates consent copy when a preview reattaches before confirmation", () => {
    const { world, flow } = flowHarness();
    void flow.run(null, SERVER);
    const win = world.windows[0];
    void flow.run(null, SERVER, { reason: "preview-reconnect" });
    const init = channelPayloads(win, "arca-connect:init").at(-1);
    assert.equal(win.title, "Reconnect Arca");
    assert.equal(init.reason, "preview-reconnect");
    assert.equal(world.windows.length, 1);
    assert.equal(win.focused, 1);
  });

  it("uses content bounds and honors only its renderer's measured height", () => {
    const { world, flow } = flowHarness();
    void flow.run(null, "https://srv.example.com/a/very/long/path");
    const consent = world.windows[0];
    assert.equal(consent.options.useContentSize, true);
    assert.equal(consent.options.height, MIN_CONTENT_HEIGHT);
    assert.equal(consent.options.resizable, false);

    world.sendFrom(consent, "arca-connect:resize", { height: 100, phase: "running" });
    assert.deepEqual(consent.contentSize, [CONTENT_WIDTH, MIN_CONTENT_HEIGHT]);
    world.sendFrom(consent, "arca-connect:confirm");
    const output = world.windows[1];
    assert.equal(output.options.height, RUNNING_CONTENT_HEIGHT);
    world.sendFrom(output, "arca-connect:resize", { height: 412, phase: "asking" });
    assert.deepEqual(output.contentSize, [CONTENT_WIDTH, RUNNING_CONTENT_HEIGHT]);
    world.sendFrom(output, "arca-connect:resize", { height: 5000, phase: "asking" });
    assert.deepEqual(output.contentSize, [CONTENT_WIDTH, MAX_CONTENT_HEIGHT]);
    world.ipc.emit("arca-connect:resize", { sender: { id: 9999 } }, { height: 333 });
    assert.deepEqual(output.contentSize, [CONTENT_WIDTH, MAX_CONTENT_HEIGHT]);
  });

  it("Close still works after the run finished (routing outlives the result)", async () => {
    const { world, flow } = flowHarness();
    const { result } = confirmedRun(world, flow);
    const output = world.windows[1];
    world.connects[0].finish({ ok: true });
    await result; // the run settled — the console is now just a report

    world.sendFrom(output, "arca-connect:cancel"); // the "Close" button
    assert.equal(output.destroyed, true);
  });

  it("ignores confirm and stop from a foreign webContents (a server page can't consent)", () => {
    const { world, flow } = flowHarness();
    void flow.run(null, SERVER);
    world.ipc.emit("arca-connect:confirm", { sender: { id: 9999 } });
    assert.equal(world.connects.length, 0);
    world.sendFrom(world.windows[0], "arca-connect:confirm");
    world.ipc.emit("arca-connect:stop", { sender: { id: 9999 } });
    assert.equal(world.connects[0].canceled, false);
  });

  it("resolves a silent cancel when closed before confirming", async () => {
    const { world, flow } = flowHarness();
    const resultPromise = flow.run(null, SERVER);
    world.sendFrom(world.windows[0], "arca-connect:cancel");
    const result = await resultPromise;
    assert.equal(result.ok, false);
    // Declining is a deliberate choice, not a failure — flagged so the
    // picker doesn't render an error for it.
    assert.equal(result.canceled, true);
    assert.match(result.error, /wasn't approved/);
    assert.equal(world.connects.length, 0);
  });

  it("flags a failed run as already shown in the console", async () => {
    const { world, flow } = flowHarness();
    const { result } = confirmedRun(world, flow);
    world.connects[0].finish({ ok: false, error: "Couldn't reach the Arca instance." });
    const settled = await result;
    assert.equal(settled.ok, false);
    assert.equal(settled.shownInConsole, true);
  });

  it("keeps running when the output window is hidden and stops only on Stop", async () => {
    const { world, flow } = flowHarness();
    const { result } = confirmedRun(world, flow);
    const firstOutput = world.windows[1];
    world.sendFrom(firstOutput, "arca-connect:cancel"); // Hide
    assert.equal(firstOutput.destroyed, true);
    assert.equal(world.connects[0].canceled, false);

    assert.equal(flow.showOutput(null, SERVER), true);
    const output = world.windows[2];
    world.sendFrom(output, "arca-connect:stop");
    assert.equal(world.connects[0].canceled, true);
    assert.equal(output.destroyed, true, "a stopped run's output window stays open");
    const settled = await result;
    assert.equal(settled.canceled, true);
  });

  it("stops a background run when the window that asked for it closes", async () => {
    const { world, flow } = flowHarness();
    const parent = new EventEmitter();
    const result = flow.run(parent, SERVER, { inlineProgress: true });
    world.sendFrom(world.windows[0], "arca-connect:confirm");
    parent.emit("closed");
    assert.equal(world.connects[0].canceled, true);
    assert.equal((await result).canceled, true);
    assert.equal(parent.listenerCount("closed"), 0);
  });

  it("re-attaches to an in-flight run instead of refusing", async () => {
    const { world, flow } = flowHarness();
    const { result: first } = confirmedRun(world, flow);
    const output = world.windows[1];

    // A refreshed SPA clicks again: the output is surfaced, same outcome shared.
    const second = flow.run(null, SERVER);
    assert.equal(world.windows.length, 2);
    assert.equal(output.focused, 1);

    world.connects[0].finish({ ok: true });
    assert.deepEqual(await first, { ok: true, shownInConsole: true });
    assert.deepEqual(await second, { ok: true, shownInConsole: true });

    // After settling, a new run asks consent again.
    void flow.run(null, SERVER);
    assert.equal(world.windows.length, 3);
    assert.equal(world.windows[2].options.modal, true);
  });

  describe("with inline progress", () => {
    it("runs in the background after consent and opens no window", async () => {
      const { world, flow } = flowHarness();
      const { result, consent } = confirmedRun(world, flow, { inlineProgress: true });
      assert.equal(consent.destroyed, true);
      assert.equal(world.windows.length, 1);
      world.connects[0].onOutput("Attempting to start your Classic Arca instance\n");
      world.connects[0].finish({ ok: true });
      assert.deepEqual(await result, { ok: true, shownInConsole: false });
      assert.equal(world.windows.length, 1);
    });

    it("shows the output on request, replaying what already ran", async () => {
      const { world, flow } = flowHarness();
      const parent = Object.assign(new EventEmitter(), { isDestroyed: () => false });
      const result = flow.run(null, SERVER, { inlineProgress: true });
      world.sendFrom(world.windows[0], "arca-connect:confirm");
      world.connects[0].onOutput("ssh-keyscan could not retrieve the host key\n");
      world.connects[0].onOutput("Attempting to start your Classic Arca instance\n");

      assert.equal(flow.showOutput(parent, SERVER), true);
      const output = world.windows[1];
      assert.equal(output.options.modal, false);
      assert.equal(output.options.parent, parent);
      assert.deepEqual(
        output.webContents.sent.map(([channel]) => channel),
        ["arca-connect:init", "arca-connect:started", "arca-connect:output"],
      );
      assert.equal(channelPayloads(output, "arca-connect:init")[0].view, "output");
      assert.deepEqual(channelPayloads(output, "arca-connect:started")[0], {
        login: false,
        command: "arca ssh isaac omni host --server https://srv.example.com …",
      });
      assert.deepEqual(channelPayloads(output, "arca-connect:output"), [
        "ssh-keyscan could not retrieve the host key\nAttempting to start your Classic Arca instance\n",
      ]);
      world.connects[0].onOutput("host online\n");
      assert.equal(channelPayloads(output, "arca-connect:output").at(-1), "host online\n");

      // A second request surfaces the same window.
      assert.equal(flow.showOutput(parent, SERVER), true);
      assert.equal(world.windows.length, 2);
      assert.equal(output.focused, 1);

      world.connects[0].finish({ ok: true });
      assert.deepEqual(await result, { ok: true, shownInConsole: true });
      assert.equal(flow.showOutput(parent, SERVER), false);
    });

    it("keeps only the tail of a long output for a late window", () => {
      const { world, flow } = flowHarness();
      confirmedRun(world, flow, { inlineProgress: true });
      world.connects[0].onOutput("x".repeat(OUTPUT_BACKLOG_CHARS));
      world.connects[0].onOutput("tail\n");
      flow.showOutput(null, SERVER);
      const [backlog] = channelPayloads(world.windows[1], "arca-connect:output");
      assert.equal(backlog.length, OUTPUT_BACKLOG_CHARS);
      assert.ok(backlog.endsWith("tail\n"));
    });

    it("opens the output with the error when the connect fails", async () => {
      const { world, flow } = flowHarness();
      const { result } = confirmedRun(world, flow, { inlineProgress: true });
      world.connects[0].onOutput("arca: instance unreachable\n");
      world.connects[0].finish({ ok: false, error: "Couldn't reach the Arca instance." });
      const settled = await result;
      assert.equal(settled.shownInConsole, true);
      const output = world.windows[1];
      assert.equal(output.options.modal, false);
      assert.deepEqual(channelPayloads(output, "arca-connect:output"), [
        "arca: instance unreachable\n",
      ]);
      assert.deepEqual(channelPayloads(output, "arca-connect:done")[0], {
        ok: false,
        error: "Couldn't reach the Arca instance.",
      });
    });

    it("opens the output when sign-in needs the browser", async () => {
      const { world, flow } = flowHarness();
      confirmedRun(world, flow, { inlineProgress: true });
      assert.equal(world.windows.length, 1);
      world.connects[0].finish({ ok: false, authError: true });
      await Promise.resolve();
      const output = world.windows[1];
      assert.equal(channelPayloads(output, "arca-connect:started").at(-1).login, true);
    });

    it("surfaces the consent sheet, without confirming, before the user decides", () => {
      const { world, flow } = flowHarness();
      void flow.run(null, SERVER, { inlineProgress: true });
      assert.equal(flow.showOutput(null, SERVER), true);
      assert.equal(world.windows.length, 1);
      assert.equal(world.windows[0].focused, 1);
      assert.equal(world.connects.length, 0);
    });

    it("has nothing to show when no connect is running", () => {
      const { flow } = flowHarness();
      assert.equal(flow.showOutput(null, SERVER), false);
    });
  });
});
