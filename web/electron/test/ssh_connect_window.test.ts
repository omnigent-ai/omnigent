import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";

import {
  createSshConnectFlow,
  type SshConnectConsoleHooks,
  type SshConnectConsoleRun,
  type SshConnectIpcEvent,
  type SshConnectRunResult,
  type SshConnectWindowOptions,
} from "../src/ssh_connect_window.ts";

const SERVER = "https://example.cloud.databricks.com/omnigent";

type IpcListener = (event: SshConnectIpcEvent, ...args: unknown[]) => void;

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

interface FakeConnect extends SshConnectConsoleRun {
  serverUrl: string;
  destination: string;
  hooks: SshConnectConsoleHooks;
  canceled: boolean;
  finish: (result: SshConnectRunResult) => void;
}

function harness() {
  const windows: FakeWindow[] = [];
  const connects: FakeConnect[] = [];
  const ipc = new EventEmitter();
  class FakeWindow extends EventEmitter {
    options: SshConnectWindowOptions<unknown>;
    webContents = new FakeWebContents(200 + windows.length);
    destroyed = false;
    focused = 0;

    constructor(options: SshConnectWindowOptions<unknown>) {
      super();
      this.options = options;
      windows.push(this);
    }
    setContentSize() {}
    async loadFile() {
      this.webContents.emit("did-finish-load");
      this.emit("ready-to-show");
    }
    isDestroyed() {
      return this.destroyed;
    }
    show() {}
    focus() {
      this.focused += 1;
    }
    close() {
      this.destroyed = true;
      this.emit("closed");
    }
  }
  const flow = createSshConnectFlow<unknown>({
    BrowserWindow: FakeWindow,
    ipcMain: { on: (channel: string, fn: IpcListener) => ipc.on(channel, fn) },
    pagePath: "/bundle/ssh-connect/index.html",
    preloadPath: "/bundle/src/ssh_connect_preload.js",
    commandLines: (url, destination) => [`ssh ${destination} omnigent login '${url}'`],
    startConnect: (serverUrl, destination, hooks) => {
      let finish: (result: SshConnectRunResult) => void = () => {};
      const promise = new Promise<SshConnectRunResult>((resolve) => {
        finish = resolve;
      });
      const connect: FakeConnect = {
        serverUrl,
        destination,
        hooks,
        canceled: false,
        cancel: () => {
          connect.canceled = true;
        },
        promise,
        finish,
      };
      connects.push(connect);
      return connect;
    },
  });
  const world = {
    windows,
    connects,
    sendFrom: (senderId: number, channel: string, payload?: unknown) =>
      ipc.emit(channel, { sender: { id: senderId } }, payload),
    sent: (win: FakeWindow, channel: string) =>
      win.webContents.sent.filter(([name]) => name === channel).map(([, payload]) => payload),
  };
  return { world, flow };
}

/** The test fails here when `value` is missing. */
function present<Value>(value: Value | undefined): Value {
  assert.ok(value !== undefined);
  return value;
}

describe("createSshConnectFlow", () => {
  it("runs only after consent, and takes prompt answers only from its own window", async () => {
    const { world, flow } = harness();
    const result = flow.run(null, SERVER, "my-ec2");
    const win = present(world.windows[0]);
    const id = win.webContents.id;
    assert.deepEqual(world.sent(win, "ssh-connect:init"), [
      {
        serverUrl: SERVER,
        destination: "my-ec2",
        commands: [`ssh my-ec2 omnigent login '${SERVER}'`],
      },
    ]);
    assert.equal(world.connects.length, 0);

    world.sendFrom(999, "ssh-connect:confirm");
    assert.equal(world.connects.length, 0, "another window can't consent");
    world.sendFrom(id, "ssh-connect:confirm");
    const connect = present(world.connects[0]);
    assert.equal(connect.destination, "my-ec2");

    const password = connect.hooks.prompt({ kind: "secret", text: "password: " });
    const [prompt] = world.sent(win, "ssh-connect:prompt");
    assert.deepEqual(prompt, { id: 1, kind: "secret", text: "password: " });
    world.sendFrom(999, "ssh-connect:answer", { id: 1, answer: "stolen" });
    world.sendFrom(id, "ssh-connect:answer", { id: 1, answer: "pw" });
    assert.equal(await password, "pw");

    const install = connect.hooks.askInstall();
    world.sendFrom(id, "ssh-connect:answer", { id: 2, answer: "yes" });
    assert.equal(await install, true);

    connect.finish({ ok: true, identity: { serverUrl: SERVER, hostId: "h" } });
    assert.deepEqual(await result, {
      ok: true,
      alreadyRunning: false,
      identity: { serverUrl: SERVER, hostId: "h" },
      shownInConsole: true,
    });
    assert.deepEqual(world.sent(win, "ssh-connect:done"), [{ ok: true, error: null }]);
  });

  it("shows remote error text in its own window and never returns it to the page", async () => {
    const { world, flow } = harness();
    const result = flow.run(null, SERVER, "my-ec2");
    const win = present(world.windows[0]);
    world.sendFrom(win.webContents.id, "ssh-connect:confirm");
    const remote = "Signing in on my-ec2 failed: SENTINEL /home/me/.config/secret";

    // A connect result can carry more than the console reads, such as remote output.
    const failed = { ok: false, error: remote, stdout: "SENTINEL" };
    present(world.connects[0]).finish(failed);
    const returned = await result;

    assert.deepEqual(world.sent(win, "ssh-connect:done"), [{ ok: false, error: remote }]);
    assert.equal(returned.ok, false);
    assert.equal(returned.shownInConsole, true);
    assert.doesNotMatch(JSON.stringify(returned), /SENTINEL/);
  });

  it("cancels the connect and any open prompt when the window closes mid-run", async () => {
    const { world, flow } = harness();
    const result = flow.run(null, SERVER, "my-ec2");
    const win = present(world.windows[0]);
    world.sendFrom(win.webContents.id, "ssh-connect:confirm");
    const connect = present(world.connects[0]);
    const pending = connect.hooks.prompt({ kind: "confirm", text: "Trust?" });

    win.close();

    assert.equal(await pending, null);
    assert.equal(connect.canceled, true);
    assert.equal((await result).canceled, true);
  });

  it("takes down a prompt that ssh stopped waiting for", { timeout: 5000 }, async () => {
    const { world, flow } = harness();
    void flow.run(null, SERVER, "my-ec2");
    const win = present(world.windows[0]);
    world.sendFrom(win.webContents.id, "ssh-connect:confirm");
    const connect = present(world.connects[0]);
    const asked = new AbortController();
    const pending = connect.hooks.prompt({
      kind: "secret",
      text: "password: ",
      signal: asked.signal,
    });

    asked.abort();

    assert.equal(await pending, null);
    assert.deepEqual(world.sent(win, "ssh-connect:prompt-closed"), [{ id: 1 }]);
    // An answer typed just too late goes nowhere.
    world.sendFrom(win.webContents.id, "ssh-connect:answer", { id: 1, answer: "pw" });
    assert.deepEqual(world.sent(win, "ssh-connect:prompt-closed"), [{ id: 1 }]);
  });

  it("treats closing before Connect as a decline and refuses a second target meanwhile", async () => {
    const { world, flow } = harness();
    const first = flow.run(null, SERVER, "my-ec2");
    assert.match((await flow.run(null, SERVER, "other-box")).error ?? "", /in progress/);

    present(world.windows[0]).close();

    assert.deepEqual(await first, {
      ok: false,
      canceled: true,
      error: "Connecting over SSH wasn't approved.",
    });
    assert.equal(world.connects.length, 0);
  });

  it("re-attaches a repeat connect to the same machine and shares its result", async () => {
    const { world, flow } = harness();
    const first = flow.run(null, SERVER, "my-ec2");
    const win = present(world.windows[0]);
    world.sendFrom(win.webContents.id, "ssh-connect:confirm");

    // The page asks again (say, after a reload): same window focused, no second connect.
    const second = flow.run(null, SERVER, "my-ec2");
    assert.equal(world.windows.length, 1);
    assert.equal(win.focused, 1);
    assert.equal(world.connects.length, 1);

    present(world.connects[0]).finish({
      ok: true,
      alreadyRunning: false,
      identity: { serverUrl: SERVER, hostId: "h1" },
    });
    const expected = {
      ok: true,
      alreadyRunning: false,
      identity: { serverUrl: SERVER, hostId: "h1" },
      shownInConsole: true,
    };
    assert.deepEqual(await first, expected);
    assert.deepEqual(await second, expected);

    // Once it settles, the next connect opens a fresh window.
    void flow.run(null, SERVER, "my-ec2");
    assert.equal(world.windows.length, 2);
  });
});
