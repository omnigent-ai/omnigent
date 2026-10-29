"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const { createSshConnectFlow } = require("../src/ssh_connect_window");

const SERVER = "https://example.cloud.databricks.com/omnigent";

function harness() {
  const world = { windows: [], connects: [], ipc: new EventEmitter() };
  class FakeWindow extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.webContents = new EventEmitter();
      this.webContents.id = 200 + world.windows.length;
      this.webContents.sent = [];
      this.webContents.send = (channel, payload) => this.webContents.sent.push([channel, payload]);
      this.destroyed = false;
      world.windows.push(this);
    }
    setContentSize() {}
    loadFile() {
      this.webContents.emit("did-finish-load");
      this.emit("ready-to-show");
    }
    isDestroyed() {
      return this.destroyed;
    }
    show() {}
    focus() {}
    close() {
      this.destroyed = true;
      this.emit("closed");
    }
  }
  const flow = createSshConnectFlow({
    BrowserWindow: FakeWindow,
    ipcMain: { on: (channel, fn) => world.ipc.on(channel, fn) },
    pagePath: "/bundle/ssh-connect/index.html",
    preloadPath: "/bundle/src/ssh_connect_preload.js",
    commandLines: (url, destination) => [`ssh ${destination} omnigent login '${url}'`],
    startConnect: (serverUrl, destination, hooks) => {
      const connect = { serverUrl, destination, hooks, canceled: false };
      connect.cancel = () => {
        connect.canceled = true;
      };
      connect.promise = new Promise((resolve) => {
        connect.finish = resolve;
      });
      world.connects.push(connect);
      return connect;
    },
  });
  world.sendFrom = (senderId, channel, payload) =>
    world.ipc.emit(channel, { sender: { id: senderId } }, payload);
  world.sent = (win, channel) =>
    win.webContents.sent.filter(([name]) => name === channel).map(([, payload]) => payload);
  return { world, flow };
}

describe("createSshConnectFlow", () => {
  it("runs only after consent, and takes prompt answers only from its own window", async () => {
    const { world, flow } = harness();
    const result = flow.run(null, SERVER, "my-ec2");
    const [win] = world.windows;
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
    const [connect] = world.connects;
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
    const [win] = world.windows;
    world.sendFrom(win.webContents.id, "ssh-connect:confirm");
    const remote = "Signing in on my-ec2 failed: SENTINEL /home/me/.config/secret";

    world.connects[0].finish({ ok: false, error: remote, stdout: "SENTINEL" });
    const returned = await result;

    assert.deepEqual(world.sent(win, "ssh-connect:done"), [{ ok: false, error: remote }]);
    assert.equal(returned.ok, false);
    assert.equal(returned.shownInConsole, true);
    assert.doesNotMatch(JSON.stringify(returned), /SENTINEL/);
  });

  it("cancels the connect and any open prompt when the window closes mid-run", async () => {
    const { world, flow } = harness();
    const result = flow.run(null, SERVER, "my-ec2");
    const [win] = world.windows;
    world.sendFrom(win.webContents.id, "ssh-connect:confirm");
    const [connect] = world.connects;
    const pending = connect.hooks.prompt({ kind: "confirm", text: "Trust?" });

    win.close();

    assert.equal(await pending, null);
    assert.equal(connect.canceled, true);
    assert.equal((await result).canceled, true);
  });

  it("takes down a prompt that ssh stopped waiting for", { timeout: 5000 }, async () => {
    const { world, flow } = harness();
    void flow.run(null, SERVER, "my-ec2");
    const [win] = world.windows;
    world.sendFrom(win.webContents.id, "ssh-connect:confirm");
    const [connect] = world.connects;
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
    assert.match((await flow.run(null, SERVER, "other-box")).error, /in progress/);

    world.windows[0].close();

    assert.deepEqual(await first, {
      ok: false,
      canceled: true,
      error: "Connecting over SSH wasn't approved.",
    });
    assert.equal(world.connects.length, 0);
  });
});

function renderConsole(t) {
  const dom = new JSDOM(
    fs.readFileSync(path.join(__dirname, "../ssh-connect/index.html"), "utf8"),
    { runScripts: "outside-only" },
  );
  t.after(() => dom.window.close());
  const page = {
    document: dom.window.document,
    events: {},
    answers: [],
    confirmed: 0,
    refits: 0,
    resizeCallbacks: new Map(),
  };
  dom.window.sshConnect = {
    confirm: () => {
      page.confirmed += 1;
    },
    cancel() {},
    answer: (payload) => page.answers.push(payload),
    resizeToContent() {},
  };
  for (const event of ["Init", "Started", "Status", "Prompt", "PromptClosed", "Output", "Done"]) {
    dom.window.sshConnect[`on${event}`] = (listener) => {
      page.events[event] = listener;
    };
  }
  dom.window.Terminal = class Terminal {
    loadAddon() {}
    open() {}
    write() {}
  };
  dom.window.FitAddon = {
    FitAddon: class FitAddon {
      fit() {
        page.refits += 1;
      }
    },
  };
  dom.window.ResizeObserver = class ResizeObserver {
    constructor(callback) {
      this.callback = callback;
    }
    observe(element) {
      page.resizeCallbacks.set(element, this.callback);
    }
  };
  dom.window.requestAnimationFrame = (callback) => {
    callback();
    return 1;
  };
  dom.window.cancelAnimationFrame = () => {};
  dom.window.eval(fs.readFileSync(path.join(__dirname, "../ssh-connect/console.js"), "utf8"));
  return page;
}

describe("SSH connect console page", () => {
  it("shows the machine and commands as text, and answers secret, trust and cancel prompts", (t) => {
    const { document, events, answers } = renderConsole(t);
    events.Init({
      serverUrl: SERVER,
      destination: "<b>my-ec2</b>",
      commands: ["ssh my-ec2 omnigent login 'x'", "ssh my-ec2 omnigent host"],
    });
    assert.equal(document.getElementById("destination").textContent, "<b>my-ec2</b>");
    assert.equal(document.querySelectorAll("#commands div").length, 2);
    assert.equal(document.querySelector("b"), null);

    const promptEl = document.getElementById("prompt");
    const input = document.getElementById("prompt-input");
    events.Prompt({ id: 1, kind: "secret", text: "pat@my-ec2's password: " });
    assert.equal(promptEl.hidden, false);
    assert.equal(input.type, "password");
    input.value = "pw";
    promptEl.dispatchEvent(new document.defaultView.Event("submit", { cancelable: true }));
    assert.equal(promptEl.hidden, true);

    events.Prompt({ id: 2, kind: "confirm", text: "ED25519 key fingerprint is SHA256:abc." });
    assert.equal(input.hidden, true);
    assert.equal(document.getElementById("prompt-ok").textContent, "Trust");
    promptEl.dispatchEvent(new document.defaultView.Event("submit", { cancelable: true }));

    events.Prompt({ id: 3, kind: "text", text: "Which project?" });
    document.getElementById("prompt-cancel").click();

    // ssh gave up on prompt 4; a stale close for another prompt changes nothing.
    events.Prompt({ id: 4, kind: "secret", text: "Passphrase: " });
    events.PromptClosed({ id: 3 });
    assert.equal(promptEl.hidden, false);
    events.PromptClosed({ id: 4 });
    assert.equal(promptEl.hidden, true);
    document.getElementById("prompt-cancel").click();

    // Built in the page's realm, so compare plain copies.
    assert.deepEqual(JSON.parse(JSON.stringify(answers)), [
      { id: 1, answer: "pw" },
      { id: 2, answer: "yes" },
      { id: 3, cancel: true },
    ]);
    assert.equal(input.value, "");
  });

  it("shows progress and the final outcome", (t) => {
    const page = renderConsole(t);
    const { document, events } = page;
    events.Init({ serverUrl: SERVER, destination: "my-ec2", commands: [] });
    events.Started({});
    // A prompt shrinks the output panel without resizing the window; the terminal must refit.
    const refitsBefore = page.refits;
    page.resizeCallbacks.get(document.getElementById("terminal"))?.();
    assert.equal(page.refits, refitsBefore + 1);
    events.Status({ step: "waiting-for-browser" });
    assert.match(
      document.getElementById("status").textContent,
      /Finish signing in in your browser/,
    );
    events.Done({ ok: false, error: "my-ec2 didn't accept your sign-in." });
    assert.equal(document.getElementById("status").dataset.kind, "error");
    assert.equal(document.getElementById("cancel").textContent, "Close");
  });
});
