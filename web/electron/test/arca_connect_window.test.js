"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { createArcaConnectFlow } = require("../src/arca_connect_window");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

it("renders consent, sign-in, retry, and completion", (t) => {
  const dom = new JSDOM(
    fs.readFileSync(path.join(__dirname, "../arca-connect/index.html"), "utf8"),
    {
      runScripts: "outside-only",
    },
  );
  t.after(() => dom.window.close());
  const events = {};
  let confirms = 0;
  // Electron supplies this bridge; no remote process is launched by a renderer test.
  dom.window.arcaConnect = { confirm: () => confirms++, cancel() {} };
  for (const event of ["Init", "Started", "Output", "Done"]) {
    dom.window.arcaConnect[`on${event}`] = (listener) => {
      events[event] = listener;
    };
  }
  dom.window.requestAnimationFrame = () => {};
  dom.window.eval(fs.readFileSync(path.join(__dirname, "../arca-connect/console.js"), "utf8"));
  const document = dom.window.document;
  const confirm = document.getElementById("confirm");
  const command = document.getElementById("command");
  events.Init({
    serverUrl: "https://account.databricks.com/omnigent?o=123",
    command: "host command",
  });
  confirm.click();
  events.Started({ login: false, command: "host command" });
  assert.equal(confirm.disabled, true);
  events.Done({
    ok: false,
    authRequired: true,
    command: "login command",
    error: "Sign-in required",
  });
  assert.equal(confirm.hidden, false);
  assert.equal(confirm.textContent, "Sign in and retry");
  assert.equal(document.activeElement, confirm);
  confirm.click();
  assert.equal(confirms, 2);
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

/** A fake Electron world: BrowserWindow + ipcMain + a controllable connect. */
function flowHarness() {
  const world = {
    windows: [],
    ipc: new EventEmitter(),
    connects: [],
    logins: [],
  };

  class FakeWindow extends EventEmitter {
    constructor() {
      super();
      this.webContents = new EventEmitter();
      this.webContents.id = 100 + world.windows.length;
      this.webContents.sent = [];
      this.webContents.send = (channel, payload) => {
        this.webContents.sent.push([channel, payload]);
      };
      this.destroyed = false;
      this.shown = 0;
      this.focused = 0;
      this.size = null;
      world.windows.push(this);
    }
    setSize(width, height) {
      this.size = [width, height];
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
  world.sendFrom = (win, channel) =>
    world.ipc.emit(channel, { sender: { id: win.webContents.id } });
  return { world, flow };
}

describe("arca connect console flow", () => {
  it("requires shell consent to sign in, then retries the identical target only once", async () => {
    const { world, flow } = flowHarness();
    const url = "https://account.databricks.com/omnigent?o=123";
    const result = flow.run(null, url);
    const win = world.windows[0];
    world.sendFrom(win, "arca-connect:confirm");
    world.connects[0].finish({ ok: false, authError: true });
    await Promise.resolve();
    assert.equal(world.logins.length, 0);
    assert.equal(win.webContents.sent.at(-1)[1].authRequired, true);
    world.ipc.emit("arca-connect:confirm", { sender: { id: 9999 } });
    assert.equal(world.logins.length, 0);
    world.sendFrom(win, "arca-connect:confirm");
    world.sendFrom(win, "arca-connect:confirm");
    assert.equal(world.logins.length, 1);
    assert.equal(world.logins[0].serverUrl, url);
    world.logins[0].finish({ ok: true });
    await Promise.resolve();
    assert.equal(world.connects[1].serverUrl, url);
    world.connects[1].finish({ ok: false, authError: true, error: "still rejected" });
    assert.equal((await result).error, "still rejected");
    world.sendFrom(win, "arca-connect:confirm");
    assert.equal(world.logins.length, 1);
  });

  it("cancels remote login without starting a host after a late successful exit", async () => {
    const { world, flow } = flowHarness();
    const result = flow.run(null, "https://account.databricks.com/omnigent?o=123");
    const win = world.windows[0];
    world.sendFrom(win, "arca-connect:confirm");
    world.connects[0].finish({ ok: false, authError: true });
    await Promise.resolve();
    world.sendFrom(win, "arca-connect:confirm");
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
    assert.equal(world.connects.length, 0); // nothing ran yet — consent first

    world.sendFrom(win, "arca-connect:confirm");
    assert.equal(world.connects.length, 1);
    assert.deepEqual(win.size, [720, 500]); // grows to reveal the terminal pane
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
});
