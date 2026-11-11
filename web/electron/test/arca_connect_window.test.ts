import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";

import {
  createArcaConnectFlow,
  CONTENT_WIDTH,
  MIN_CONTENT_HEIGHT,
  RUNNING_CONTENT_HEIGHT,
  MAX_CONTENT_HEIGHT,
  type ArcaConnectConsoleRun,
  type ArcaConnectIpcEvent,
  type ArcaConnectRunResult,
  type ArcaConnectWindowOptions,
} from "../src/arca_connect_window.ts";

type IpcListener = (event: ArcaConnectIpcEvent, ...args: unknown[]) => void;

/** The test fails here when `value` is missing. */
function present<Value>(value: Value | undefined): Value {
  assert.ok(value !== undefined);
  return value;
}

/** A field of a pushed payload. */
function fieldOf(payload: unknown, key: string): unknown {
  assert.ok(payload !== null && typeof payload === "object");
  return (payload as Record<string, unknown>)[key];
}

class FakeWebContents extends EventEmitter {
  id: number;
  sent: [string, unknown][] = [];

  constructor(id: number) {
    super();
    this.id = id;
  }

  send(channel: string, payload: unknown) {
    this.sent.push([channel, payload]);
  }
}

interface FakeRun extends ArcaConnectConsoleRun {
  serverUrl: string;
  onOutput: ((text: string) => void) | undefined;
  canceled: boolean;
  finish: (result: ArcaConnectRunResult) => void;
}

function fakeRun(serverUrl: string, onOutput?: (text: string) => void): FakeRun {
  let finish: (result: ArcaConnectRunResult) => void = () => {};
  const promise = new Promise<ArcaConnectRunResult>((resolve) => {
    finish = resolve;
  });
  const run: FakeRun = {
    serverUrl,
    onOutput,
    canceled: false,
    cancel: () => {
      run.canceled = true;
    },
    promise,
    finish,
  };
  return run;
}

/** A fake Electron world: BrowserWindow + ipcMain + a controllable connect. */
function flowHarness() {
  const windows: FakeWindow[] = [];
  const connects: FakeRun[] = [];
  const logins: FakeRun[] = [];
  const ipc = new EventEmitter();

  class FakeWindow extends EventEmitter {
    options: ArcaConnectWindowOptions<unknown>;
    webContents = new FakeWebContents(100 + windows.length);
    destroyed = false;
    shown = 0;
    focused = 0;
    contentSize: [number, number] | null = null;
    title: string | undefined;

    constructor(options: ArcaConnectWindowOptions<unknown>) {
      super();
      this.options = options;
      windows.push(this);
    }
    setContentSize(width: number, height: number) {
      this.contentSize = [width, height];
    }
    async loadFile() {
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
    setTitle(title: string) {
      this.title = title;
    }
    close() {
      this.destroyed = true;
      this.emit("closed");
    }
  }
  const flow = createArcaConnectFlow<unknown>({
    BrowserWindow: FakeWindow,
    ipcMain: { on: (channel: string, fn: IpcListener) => ipc.on(channel, fn) },
    pagePath: "/bundle/arca-connect/index.html",
    preloadPath: "/bundle/src/arca_connect_preload.js",
    commandLine: (url) => `arca ssh isaac omni host --server ${url} …`,
    loginCommandLine: (url) => `arca ssh isaac omni login ${url}`,
    startLogin: (serverUrl) => {
      const login = fakeRun(serverUrl);
      logins.push(login);
      return login;
    },
    startConnect: (serverUrl, onOutput) => {
      const connect = fakeRun(serverUrl, onOutput);
      connects.push(connect);
      return connect;
    },
  });

  const world = {
    windows,
    connects,
    logins,
    ipc,
    /** Emit a preload→main message as if it came from `win`'s webContents. */
    sendFrom: (win: FakeWindow, channel: string, payload?: unknown) =>
      ipc.emit(channel, { sender: { id: win.webContents.id } }, payload),
  };
  return { world, flow };
}

describe("arca connect console flow", () => {
  it("does not share another workspace's in-flight identity", async () => {
    const { world, flow } = flowHarness();
    const server = "https://account.databricks.com/omnigent?o=123";
    const first = flow.run(null, server);
    const win = present(world.windows[0]);
    world.sendFrom(win, "arca-connect:confirm");
    const second = await flow.run(null, server.replace("123", "456"));
    assert.equal(second.ok, false);
    assert.equal(second.identity, undefined);
    assert.equal(world.windows.length, 1);
    assert.equal(world.connects.length, 1);
    const identity = { serverUrl: server, hostId: "a".repeat(32) };
    present(world.connects[0]).finish({ ok: true, identity });
    assert.deepEqual((await first).identity, identity);
  });

  it("signs in under the Connect consent, then retries the identical target only once", async () => {
    const { world, flow } = flowHarness();
    const url = "https://account.databricks.com/omnigent?o=123";
    const result = flow.run(null, url);
    const win = present(world.windows[0]);
    world.ipc.emit("arca-connect:confirm", { sender: { id: 9999 } });
    assert.equal(world.connects.length, 0);
    world.sendFrom(win, "arca-connect:confirm");
    present(world.connects[0]).finish({ ok: false, authError: true });
    await Promise.resolve();
    assert.equal(world.logins.length, 1);
    assert.equal(present(world.logins[0]).serverUrl, url);
    const started = win.webContents.sent.filter(([channel]) => channel === "arca-connect:started");
    assert.deepEqual(present(started.at(-1))[1], {
      login: true,
      command: `arca ssh isaac omni login ${url}`,
    });
    assert.equal(
      win.webContents.sent.some(([channel]) => channel === "arca-connect:done"),
      false,
    );
    world.sendFrom(win, "arca-connect:confirm");
    assert.equal(world.logins.length, 1);
    present(world.logins[0]).finish({ ok: true });
    await Promise.resolve();
    assert.equal(present(world.connects[1]).serverUrl, url);
    present(world.connects[1]).finish({ ok: false, authError: true, error: "still rejected" });
    assert.equal((await result).error, "still rejected");
    world.sendFrom(win, "arca-connect:confirm");
    assert.equal(world.logins.length, 1);
  });

  it("shows a failed sign-in and doesn't retry the host", async () => {
    const { world, flow } = flowHarness();
    const result = flow.run(null, "https://account.databricks.com/omnigent?o=123");
    const win = present(world.windows[0]);
    world.sendFrom(win, "arca-connect:confirm");
    present(world.connects[0]).finish({ ok: false, authError: true });
    await Promise.resolve();
    present(world.logins[0]).finish({ ok: false, authError: true, error: "sign-in timed out" });
    assert.equal((await result).error, "sign-in timed out");
    assert.equal(world.connects.length, 1);
    const done = win.webContents.sent.filter(([channel]) => channel === "arca-connect:done");
    assert.deepEqual(present(done.at(-1))[1], { ok: false, error: "sign-in timed out" });
  });

  it("cancels remote login without starting a host after a late successful exit", async () => {
    const { world, flow } = flowHarness();
    const result = flow.run(null, "https://account.databricks.com/omnigent?o=123");
    const win = present(world.windows[0]);
    world.sendFrom(win, "arca-connect:confirm");
    present(world.connects[0]).finish({ ok: false, authError: true });
    await Promise.resolve();
    win.close();
    assert.equal(present(world.logins[0]).canceled, true);
    present(world.logins[0]).finish({ ok: true });
    assert.equal((await result).canceled, true);
    assert.equal(world.connects.length, 1);
  });
  it("shows the command, runs only after confirm, streams output, resolves the result", async () => {
    const { world, flow } = flowHarness();
    const resultPromise = flow.run(null, "https://srv.example.com");
    const win = present(world.windows[0]);

    const init = win.webContents.sent.find(([c]) => c === "arca-connect:init");
    assert.equal(
      fieldOf(present(init)[1], "command"),
      "arca ssh isaac omni host --server https://srv.example.com …",
    );
    assert.equal(fieldOf(present(init)[1], "reason"), null);
    assert.equal(world.connects.length, 0); // nothing ran yet — consent first

    world.sendFrom(win, "arca-connect:confirm");
    assert.equal(world.connects.length, 1);
    world.sendFrom(win, "arca-connect:resize", { height: 420, phase: "running" });
    assert.deepEqual(win.contentSize, [CONTENT_WIDTH, RUNNING_CONTENT_HEIGHT]);
    present(present(world.connects[0]).onOutput)("booting…\n");
    assert.ok(
      win.webContents.sent.some(([c, p]) => c === "arca-connect:output" && p === "booting…\n"),
    );

    present(world.connects[0]).finish({ ok: true });
    assert.deepEqual(await resultPromise, { ok: true, shownInConsole: true });
    const done = win.webContents.sent.find(([c]) => c === "arca-connect:done");
    assert.deepEqual(present(done)[1], { ok: true, error: null });
    assert.equal(win.destroyed, false); // stays open so the output is readable
  });

  it("labels preview recovery as a reconnect before asking consent", () => {
    const { world, flow } = flowHarness();
    void flow.run(null, "https://srv.example.com", { reason: "preview-reconnect" });
    const win = present(world.windows[0]);
    const init = win.webContents.sent.find(([channel]) => channel === "arca-connect:init");
    assert.equal(win.options.title, "Reconnect Arca");
    assert.equal(fieldOf(present(init)[1], "reason"), "preview-reconnect");
    assert.equal(world.connects.length, 0);
  });

  it("updates consent copy when a preview reattaches before confirmation", () => {
    const { world, flow } = flowHarness();
    void flow.run(null, "https://srv.example.com");
    const win = present(world.windows[0]);
    void flow.run(null, "https://srv.example.com", { reason: "preview-reconnect" });
    const init = win.webContents.sent.filter(([channel]) => channel === "arca-connect:init").at(-1);
    assert.equal(win.title, "Reconnect Arca");
    assert.equal(fieldOf(present(init)[1], "reason"), "preview-reconnect");
    assert.equal(world.windows.length, 1);
  });

  it("uses content bounds and honors only its renderer's measured height", () => {
    const { world, flow } = flowHarness();
    void flow.run(null, "https://srv.example.com/a/very/long/path");
    const win = present(world.windows[0]);
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
    const win = present(world.windows[0]);
    world.sendFrom(win, "arca-connect:confirm");
    present(world.connects[0]).finish({ ok: true });
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
    world.sendFrom(present(world.windows[0]), "arca-connect:cancel");
    const result = await resultPromise;
    assert.equal(result.ok, false);
    // Declining is a deliberate choice, not a failure — flagged so the
    // picker doesn't render an error for it.
    assert.equal(result.canceled, true);
    assert.match(present(result.error), /wasn't approved/);
  });

  it("flags a failed run as already shown in the console", async () => {
    const { world, flow } = flowHarness();
    const resultPromise = flow.run(null, "https://srv.example.com");
    world.sendFrom(present(world.windows[0]), "arca-connect:confirm");
    present(world.connects[0]).finish({ ok: false, error: "Couldn't reach the Arca instance." });
    const result = await resultPromise;
    assert.equal(result.ok, false);
    assert.equal(result.shownInConsole, true);
  });

  it("kills the command when the console is closed mid-run", async () => {
    const { world, flow } = flowHarness();
    const resultPromise = flow.run(null, "https://srv.example.com");
    const win = present(world.windows[0]);
    world.sendFrom(win, "arca-connect:confirm");
    win.close();
    assert.equal(present(world.connects[0]).canceled, true);
    const result = await resultPromise;
    assert.equal(result.canceled, true);
  });

  it("re-attaches to an in-flight console instead of refusing", async () => {
    const { world, flow } = flowHarness();
    const first = flow.run(null, "https://srv.example.com");
    const win = present(world.windows[0]);
    world.sendFrom(win, "arca-connect:confirm");

    // A refreshed SPA clicks again: same console focused, same outcome shared.
    const second = flow.run(null, "https://srv.example.com");
    assert.equal(world.windows.length, 1);
    assert.equal(win.focused, 1);

    present(world.connects[0]).finish({ ok: true });
    assert.deepEqual(await first, { ok: true, shownInConsole: true });
    assert.deepEqual(await second, { ok: true, shownInConsole: true });

    // After settling, a new run opens a fresh console.
    void flow.run(null, "https://srv.example.com");
    assert.equal(world.windows.length, 2);
  });
});
