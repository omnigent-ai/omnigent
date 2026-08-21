const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");

const {
  isWebAuthnEscapePage,
  webAuthnTimeoutScript,
  registerWebAuthnTimeout,
} = require("../src/webauthn_timeout");

function installGuard(get, timeoutMs = 10) {
  const context = vm.createContext({
    navigator: { credentials: { get } },
    DOMException,
    Promise,
    setTimeout,
    clearTimeout,
  });
  const report = vm.runInContext(webAuthnTimeoutScript(timeoutMs), context);
  return { report, credentials: context.navigator.credentials };
}

describe("modal WebAuthn timeout", () => {
  it("arms only for a flagged cross-origin IdP or same-origin accounts login", () => {
    assert.equal(
      isWebAuthnEscapePage("https://server.example/login", "https://server.example"),
      true,
    );
    assert.equal(
      isWebAuthnEscapePage(
        "https://server.example/base/login?return_to=%2Fc%2Fcurrent",
        "https://server.example/base",
      ),
      true,
    );
    assert.equal(
      isWebAuthnEscapePage("https://server.example/c/current", "https://server.example"),
      false,
    );
    assert.equal(
      isWebAuthnEscapePage("https://idp.example/login", "https://server.example"),
      false,
    );
    assert.equal(
      isWebAuthnEscapePage("https://idp.example/login", "https://server.example", true),
      true,
    );
    assert.equal(
      isWebAuthnEscapePage("https://server.example/auth/login", "https://server.example"),
      false,
    );
    assert.equal(isWebAuthnEscapePage("file:///tmp/login.html", "https://server.example"), false);
  });

  it("matches mounted accounts routes without accepting origin or path lookalikes", () => {
    assert.equal(
      isWebAuthnEscapePage("https://server.example/base/login", "https://server.example/base/"),
      true,
    );
    assert.equal(
      isWebAuthnEscapePage(
        "https://server.example/base/login/extra",
        "https://server.example/base",
      ),
      false,
    );
    assert.equal(
      isWebAuthnEscapePage("https://server.example/baseball/login", "https://server.example/base"),
      false,
    );
    assert.equal(
      isWebAuthnEscapePage("https://server.example:444/base/login", "https://server.example/base"),
      false,
    );
  });

  it("reports a slow discoverable request without changing its Promise", async () => {
    const originalRequest = new Promise(() => {});
    const { report, credentials } = installGuard(() => originalRequest);
    const request = credentials.get({ publicKey: {}, mediation: undefined });

    assert.equal(request, originalRequest);
    assert.equal((await report).timedOut, true);
  });

  it("leaves an allow-listed roaming-key ceremony completely untouched", async () => {
    const originalRequest = new Promise(() => {});
    const { credentials } = installGuard(() => originalRequest, 10);
    const request = credentials.get({ publicKey: { allowCredentials: [{ id: "usb" }] } });

    assert.equal(request, originalRequest);
  });

  it("leaves conditional mediation untouched", async () => {
    const originalRequest = Promise.resolve("conditional");
    const { credentials } = installGuard(() => originalRequest);
    const request = credentials.get({ publicKey: {}, mediation: "conditional" });

    assert.equal(request, originalRequest);
    assert.equal(await request, "conditional");
  });

  it("does not arm for non-public-key credential requests", async () => {
    const originalRequest = new Promise(() => {});
    const { report, credentials } = installGuard(() => originalRequest, 1);

    assert.equal(credentials.get({ password: true }), originalRequest);
    assert.equal(
      await Promise.race([
        report.then(() => "armed"),
        new Promise((resolve) => {
          setTimeout(() => resolve("untouched"), 10);
        }),
      ]),
      "untouched",
    );
  });

  it("adds no page-global or shell-identifying marker", () => {
    const script = webAuthnTimeoutScript(100);
    assert.doesNotMatch(script, /omnigent/i);
    assert.doesNotMatch(script, /window\.|globalThis\.|postMessage|ipc/i);
  });

  it("notifies main only after the injected guard reports a timeout", async () => {
    class FakeWebContents extends EventEmitter {
      executeJavaScript() {
        return Promise.resolve({ timedOut: true });
      }
    }
    const webContents = new FakeWebContents();
    let notifications = 0;
    registerWebAuthnTimeout(webContents, { onTimeout: () => (notifications += 1) });

    webContents.emit("did-finish-load");
    await new Promise((resolve) => {
      setImmediate(resolve);
    });

    assert.equal(notifications, 1);
  });

  it("does not inject into ordinary server pages", async () => {
    class FakeWebContents extends EventEmitter {
      constructor() {
        super();
        this.injections = 0;
      }

      executeJavaScript() {
        this.injections += 1;
        return Promise.resolve(null);
      }
    }
    const webContents = new FakeWebContents();
    registerWebAuthnTimeout(webContents, {
      shouldInject: () => false,
      onTimeout: () => {},
    });

    webContents.emit("did-finish-load");
    await new Promise((resolve) => {
      setImmediate(resolve);
    });

    assert.equal(webContents.injections, 0);
  });
});
