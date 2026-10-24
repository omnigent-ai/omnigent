const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PRELOAD = fs.readFileSync(path.join(__dirname, "../src/preload.js"), "utf8");

function loadPreload({ platform = "linux", executeInMainWorld } = {}) {
  const exposed = new Map();
  const listeners = new Map();
  const invokes = [];
  const sent = [];
  let updateStatus = { state: "idle" };
  const ipcRenderer = {
    invoke: async (channel, args) => {
      invokes.push({ channel, args });
      if (channel === "omnigent:get-update-status") return updateStatus;
      return null;
    },
    send: (channel, ...args) => sent.push({ channel, args }),
    on: (channel, listener) => listeners.set(channel, listener),
    removeListener: (channel, listener) => {
      if (listeners.get(channel) === listener) listeners.delete(channel);
    },
  };
  vm.runInNewContext(PRELOAD, {
    console,
    process: { platform },
    require: (specifier) => {
      assert.equal(specifier, "electron");
      return {
        contextBridge: {
          exposeInMainWorld: (name, value) => exposed.set(name, value),
          ...(executeInMainWorld && { executeInMainWorld }),
        },
        ipcRenderer,
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
    sent,
  };
}

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

describe("WebAuthn request observer", () => {
  // Serializes the observer into a realm with the page's CredentialsContainer, as
  // executeInMainWorld does; only the desktop e2e crosses the real bridge.
  function loadObserver({ report } = {}) {
    let script;
    const h = loadPreload({ executeInMainWorld: (s) => (script = s) });
    assert.equal(typeof script?.func, "function");
    const realm = vm.createContext({ report: report ?? script.args[0] });
    vm.runInContext(
      `function track(options) {
        const call = { options };
        call.promise = new Promise((resolve, reject) => Object.assign(call, { resolve, reject }));
        calls.push(call);
        return call.promise;
      }
      globalThis.CredentialsContainer = class CredentialsContainer {
        get(options) {
          if (options?.illegal) throw new TypeError("Illegal invocation");
          return track(options);
        }
        create(options) {
          return track(options);
        }
      };
      globalThis.calls = [];
      globalThis.credentials = new CredentialsContainer();
      (${script.func})(report);`,
      realm,
    );
    const pending = () => h.sent.filter(({ channel }) => channel === "omnigent:webauthn-pending");
    return { realm, pending: () => pending().map(({ args }) => args[0]) };
  }
  const settle = () =>
    new Promise((resolve) => {
      setImmediate(resolve);
    });

  it("reports every WebAuthn request and the last one settling", async () => {
    const { realm, pending } = loadObserver();
    const first = vm.runInContext("credentials.get({ publicKey: {} })", realm);
    // A later request re-shows a prompt the user dismissed while the first one waits.
    const second = vm.runInContext("credentials.create({ publicKey: {} })", realm);
    assert.deepEqual(pending(), [true, true]);

    vm.runInContext("calls[0].resolve('credential')", realm);
    assert.equal(await first, "credential");
    await settle();
    assert.deepEqual(pending(), [true, true]);

    vm.runInContext("calls[1].reject(new Error('NotAllowedError'))", realm);
    await assert.rejects(second, /NotAllowedError/);
    await settle();
    assert.deepEqual(pending(), [true, true, false]);
  });

  it("does not count a request whose promise it cannot observe", () => {
    const { realm, pending } = loadObserver();
    vm.runInContext("Promise.prototype.constructor = 1", realm);
    const request = vm.runInContext("credentials.get({ publicKey: {} })", realm);
    assert.equal(request, vm.runInContext("calls[0].promise", realm));
    assert.deepEqual(pending(), []);
  });

  it("keeps report errors out of the page", async () => {
    const rejections = [];
    const onRejection = (reason) => rejections.push(reason);
    process.on("unhandledRejection", onRejection);
    try {
      const { realm } = loadObserver({
        report: () => {
          throw new Error("bridge gone");
        },
      });
      const request = vm.runInContext("credentials.get({ publicKey: {} })", realm);
      vm.runInContext("calls[0].resolve('credential')", realm);
      assert.equal(await request, "credential");
      await settle();
    } finally {
      process.off("unhandledRejection", onRejection);
    }
    assert.deepEqual(rejections, []);
  });

  it("ignores requests without a browser prompt and non-WebAuthn requests", async () => {
    const { realm, pending } = loadObserver();
    for (const mediation of ["conditional", "silent", "immediate"]) {
      vm.runInContext(`credentials.get({ publicKey: {}, mediation: "${mediation}" })`, realm);
    }
    vm.runInContext("credentials.get({ password: true })", realm);
    vm.runInContext("credentials.get()", realm);
    await settle();
    assert.deepEqual(pending(), []);

    vm.runInContext("credentials.get({ publicKey: {}, mediation: 'required' })", realm);
    assert.deepEqual(pending(), [true]);
  });

  it("leaves unreadable options and native errors to the page", () => {
    const { realm, pending } = loadObserver();
    const request = vm.runInContext(
      "credentials.get({ get publicKey() { throw new Error('getter'); } })",
      realm,
    );
    assert.equal(request instanceof vm.runInContext("Promise", realm), true);
    assert.throws(
      () => vm.runInContext("credentials.get({ publicKey: {}, illegal: true })", realm),
      /Illegal invocation/,
    );
    assert.deepEqual(pending(), []);
  });

  it("keeps the page-visible methods looking native", () => {
    const { realm } = loadObserver();
    assert.equal(vm.runInContext("CredentialsContainer.prototype.get.name", realm), "get");
    assert.equal(vm.runInContext("CredentialsContainer.prototype.create.length", realm), 1);
    assert.match(
      vm.runInContext("String(CredentialsContainer.prototype.get)", realm),
      /native code/,
    );
  });

  it("is not installed on Windows or without executeInMainWorld", () => {
    let installed = false;
    loadPreload({ platform: "win32", executeInMainWorld: () => (installed = true) });
    assert.equal(installed, false);
    assert.ok(loadPreload().desktop, "the preload still loads without the API");
  });
});
