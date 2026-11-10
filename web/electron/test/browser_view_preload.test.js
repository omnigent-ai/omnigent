"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PRELOAD = fs.readFileSync(path.join(__dirname, "../src/browser_view_preload.js"), "utf8");

// Serializes the observer into a realm with the page's CredentialsContainer, as
// executeInMainWorld does; only the desktop e2e crosses the real bridge.
function loadObserver({ report } = {}) {
  let script;
  const sent = [];
  vm.runInNewContext(PRELOAD, {
    require: (specifier) => {
      assert.equal(specifier, "electron");
      return {
        contextBridge: { executeInMainWorld: (options) => (script = options) },
        ipcRenderer: { send: (channel, ...args) => sent.push({ channel, args }) },
      };
    },
  });
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
  const pending = () =>
    sent
      .filter(({ channel }) => channel === "omnigent:browser-webauthn-pending")
      .map(({ args }) => args[0]);
  return { realm, pending };
}

const settle = () =>
  new Promise((resolve) => {
    setImmediate(resolve);
  });

describe("embedded browser WebAuthn observer", () => {
  it("reports every WebAuthn request and the last one settling", async () => {
    const { realm, pending } = loadObserver();
    const first = vm.runInContext("credentials.get({ publicKey: {} })", realm);
    // A later request re-shows a notice the user dismissed while the first one waits.
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
});
