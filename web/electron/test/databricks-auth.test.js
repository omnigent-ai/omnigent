"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const {
  DATABRICKS_BROWSER_AUTH_KEY,
  readDatabricksAuthMode,
  usesDatabricksBrowserAuth,
  isDatabricksLoginUrl,
  createDatabricksAuth,
} = require("../src/databricks-auth");

const ORIGIN = "https://workspace.cloud.databricks.com";
const TARGET = `${ORIGIN}/omnigent/c/chat`;
const NOW = 1_800_000_000_000;
const cookie = () => ({
  name: "DBAUTH",
  domain: new URL(ORIGIN).hostname,
  hostOnly: true,
  path: "/",
  value: "test-session",
  expirationDate: NOW / 1000 + 3600,
});
const drain = () =>
  new Promise((resolve) => {
    setImmediate(resolve);
  });

function harness({ mode = "browser", ensureSession, loadRejection } = {}) {
  const timers = new Map();
  const calls = { renew: [], load: [], errors: [] };
  let jar = [cookie()];
  let guard;
  const cookies = Object.assign(new EventEmitter(), { get: async () => jar });
  const session = {
    cookies,
    webRequest: {
      onBeforeRequest: (fn) => {
        guard = fn;
      },
    },
  };
  const windows = new Map();
  const auth = createDatabricksAuth({
    session,
    getWindow: (id) =>
      [...windows.keys()].find((win) => !win.isDestroyed() && win.webContents.id === id),
    getOrigin: (win) =>
      usesDatabricksBrowserAuth(windows.get(win), mode) ? windows.get(win) : null,
    isSetupUrl: (url) => url === "file:///setup/index.html",
    onAuthRequired: (win, url, error) => calls.errors.push({ win, url, error }),
    ensureSession: async (...args) => {
      calls.renew.push(args);
      if (ensureSession) return ensureSession(...args);
      jar = [cookie()];
      return ORIGIN;
    },
    now: () => NOW,
    setTimeoutFn: (fn, delay) => {
      const id = {};
      timers.set(id, { fn, delay });
      return id;
    },
    clearTimeoutFn: (id) => timers.delete(id),
  });
  let nextId = 0;
  function window() {
    const wc = Object.assign(new EventEmitter(), { id: ++nextId });
    const win = {
      webContents: wc,
      isDestroyed: () => false,
      loadURL: async (url) => {
        calls.load.push({ win, url });
        if (loadRejection) throw loadRejection;
      },
    };
    windows.set(win, ORIGIN);
    return win;
  }
  function request(win, url, resourceType = "mainFrame") {
    let result;
    guard({ webContentsId: win.webContents.id, url, resourceType }, (value) => {
      result = value;
    });
    return result;
  }
  return {
    auth,
    window,
    windows,
    calls,
    timers,
    request,
    session,
    removeCookie(cause = "expired") {
      jar = [];
      cookies.emit("changed", {}, cookie(), cause, true);
    },
    setCookie(value) {
      jar = [value];
      cookies.emit("changed", {}, value, "explicit", false);
    },
  };
}

describe("Databricks authentication mode", () => {
  it("registers a true default without overriding an explicit false preference", () => {
    const calls = [];
    assert.equal(
      readDatabricksAuthMode({
        platform: "darwin",
        registerDefaults: (value) => calls.push(value),
        getUserDefault: (key, type) => {
          calls.push([key, type]);
          return false;
        },
      }),
      "embedded",
    );
    assert.deepEqual(calls, [
      { [DATABRICKS_BROWSER_AUTH_KEY]: true },
      [DATABRICKS_BROWSER_AUTH_KEY, "boolean"],
    ]);
  });
  it("defaults to browser mode when the preference is absent or true", () => {
    for (const value of [undefined, true]) {
      let registered;
      assert.equal(
        readDatabricksAuthMode({
          platform: "darwin",
          registerDefaults: (defaults) => {
            registered = defaults;
          },
          getUserDefault: (key) => value ?? registered[key],
        }),
        "browser",
      );
    }
  });
  it("does not implicitly roll back when reading preferences fails", () => {
    assert.equal(
      readDatabricksAuthMode({
        platform: "darwin",
        registerDefaults() {
          throw new Error("unavailable");
        },
        getUserDefault: () => false,
      }),
      "browser",
    );
    assert.equal(
      readDatabricksAuthMode({ platform: "linux", getUserDefault: () => false }),
      "browser",
    );
  });
  it("limits browser mode to HTTPS Databricks workspace/account URLs", () => {
    for (const url of [
      ORIGIN,
      "https://accounts.cloud.databricks.com",
      "https://adb-1.2.azuredatabricks.net/omnigent",
    ]) {
      assert.equal(usesDatabricksBrowserAuth(url, "browser"), true);
      assert.equal(usesDatabricksBrowserAuth(url, "embedded"), false);
    }
    for (const url of [
      "https://demo.databricksapps.com",
      "http://localhost:8000",
      "https://server.example",
      "https://notdatabricks.com",
      `http://${new URL(ORIGIN).host}`,
      `${ORIGIN}:8443`,
      null,
    ]) {
      assert.equal(usesDatabricksBrowserAuth(url, "browser"), false);
    }
  });
  it("recognizes workspace auth gates without classifying app authentication as platform SSO", () => {
    for (const path of [
      "/login",
      "/login/sso?o=1",
      "/login.html",
      "/oidc/v1/authorize",
      "/.auth/callback",
    ]) {
      assert.equal(isDatabricksLoginUrl(`${ORIGIN}${path}`, ORIGIN), true);
    }
    for (const url of [TARGET, `${ORIGIN}/auth/callback`, "https://server.example/login"]) {
      assert.equal(isDatabricksLoginUrl(url, ORIGIN), false);
    }
  });
});

describe("Databricks browser session lifecycle", () => {
  it("renews before cookie expiry without navigation or opening authentication", async (t) => {
    const h = harness();
    t.after(() => h.auth.dispose());
    const win = h.window();
    await h.auth.attach(win, `${ORIGIN}/omnigent`, TARGET);
    assert.equal(h.calls.renew.length, 0);
    const scheduled = [...h.timers.values()][0];
    assert.equal(scheduled.delay, 3_540_000);
    scheduled.fn();
    await drain();
    assert.equal(h.calls.renew.length, 1);
    assert.equal(h.calls.renew[0][2].interactive, false);
    assert.deepEqual(h.calls.load, []);
  });
  it("renews a removed cookie once for concurrent windows, without reloading them", async (t) => {
    const h = harness();
    t.after(() => h.auth.dispose());
    await h.auth.attach(h.window(), `${ORIGIN}/omnigent`);
    await h.auth.attach(h.window(), `${ORIGIN}/omnigent`);
    h.removeCookie();
    await drain();
    assert.equal(h.calls.renew.length, 1);
    assert.deepEqual(h.calls.load, []);
    assert.deepEqual(h.calls.errors, []);
  });
  it("does not renew merely because a cookie is being overwritten", async (t) => {
    const h = harness();
    t.after(() => h.auth.dispose());
    await h.auth.attach(h.window(), `${ORIGIN}/omnigent`);
    h.removeCookie("overwrite");
    h.setCookie(cookie());
    await drain();
    assert.equal(h.calls.renew.length, 0);
  });
  it("blocks login documents and API redirects before embedded SSO loads", async (t) => {
    const h = harness();
    t.after(() => h.auth.dispose());
    const win = h.window();
    await h.auth.attach(win, `${ORIGIN}/omnigent`, TARGET);
    assert.deepEqual(h.request(win, `${ORIGIN}/login/sso`), { cancel: true });
    assert.deepEqual(h.request(win, `${ORIGIN}/login.html`, "xhr"), { cancel: true });
    await drain();
    assert.equal(h.calls.renew.length, 1);
    assert.deepEqual(h.calls.load, [{ win, url: TARGET }]);
  });
  it("requires explicit browser sign-in when silent renewal fails, never an embedded reload", async (t) => {
    const h = harness({
      ensureSession: async () => {
        throw new Error("no stored token");
      },
    });
    t.after(() => h.auth.dispose());
    const win = h.window();
    await h.auth.attach(win, `${ORIGIN}/omnigent`);
    h.removeCookie();
    await drain();
    assert.equal(h.calls.errors.length, 1);
    assert.match(h.calls.errors[0].error.message, /no stored token/);
    assert.deepEqual(h.calls.load, []);
    assert.equal(h.timers.size, 0);
  });
  it("keeps a rejected window blocked after unpinning until a new connection begins", async (t) => {
    const h = harness({
      ensureSession: async () => {
        throw new Error("expired without a refresh token");
      },
    });
    t.after(() => h.auth.dispose());
    const win = h.window();
    await h.auth.attach(win, `${ORIGIN}/omnigent`);
    h.removeCookie();
    await drain();
    h.auth.detach(win);
    h.windows.set(win, null);
    assert.deepEqual(h.request(win, `${ORIGIN}/login/sso`), { cancel: true });
    assert.deepEqual(h.request(win, "https://identity.example/login"), { cancel: true });
    assert.deepEqual(h.request(win, `${ORIGIN}/login`, "subFrame"), { cancel: true });
    assert.deepEqual(h.request(win, "file:///setup/index.html"), {});
    let blocked = 0;
    const event = {
      preventDefault: () => {
        blocked++;
      },
    };
    win.webContents.emit("will-navigate", event, `${ORIGIN}/login/sso`);
    win.webContents.emit("will-redirect", event, "https://identity.example/login");
    win.webContents.emit("will-navigate", event, "file:///setup/index.html");
    assert.equal(blocked, 2);
    h.auth.reset(win);
    assert.equal(win.webContents.listenerCount("will-navigate"), 0);
    assert.equal(win.webContents.listenerCount("will-redirect"), 0);
    h.windows.set(win, "https://server.example");
    assert.deepEqual(h.request(win, "https://server.example/"), {});
  });

  // A navigation outcome is not a credential signal: the cookie was already
  // renewed and verified before the reload starts.
  for (const [label, loadRejection] of [
    [
      "superseded by a newer navigation",
      Object.assign(new Error("ERR_ABORTED (-3) loading 'https://workspace...'"), {
        errno: -3,
        code: "ERR_ABORTED",
      }),
    ],
    [
      "failed to load",
      Object.assign(new Error("ERR_CONNECTION_REFUSED (-102)"), {
        errno: -102,
        code: "ERR_CONNECTION_REFUSED",
      }),
    ],
  ]) {
    it(`keeps the renewed connection when the recovery reload is ${label}`, async (t) => {
      const h = harness({ loadRejection });
      t.after(() => h.auth.dispose());
      const win = h.window();
      await h.auth.attach(win, `${ORIGIN}/omnigent`, TARGET);
      h.request(win, `${ORIGIN}/login`);
      await drain();
      assert.equal(h.calls.renew.length, 1);
      assert.deepEqual(
        h.calls.load.map((l) => l.url),
        [TARGET],
      );
      // Renewal succeeded, so the user must not be sent back to the selector...
      assert.deepEqual(h.calls.errors, []);
      // ...and the connection stays attached with its next renewal armed.
      assert.equal(h.timers.size, 1);
    });
  }

  it("stops repeated rejection rather than entering a renewal/reload loop", async (t) => {
    const h = harness();
    t.after(() => h.auth.dispose());
    const win = h.window();
    await h.auth.attach(win, `${ORIGIN}/omnigent`);
    h.request(win, `${ORIGIN}/login`);
    await drain();
    h.request(win, `${ORIGIN}/login`);
    await drain();
    assert.equal(h.calls.renew.length, 1);
    assert.equal(h.calls.errors.length, 1);
  });
  it("does not use an arbitrary foreign navigation as an expiry signal", async (t) => {
    const h = harness();
    t.after(() => h.auth.dispose());
    const win = h.window();
    await h.auth.attach(win, `${ORIGIN}/omnigent`);
    assert.deepEqual(h.request(win, "https://another.example/"), { cancel: true });
    await drain();
    assert.equal(h.calls.renew.length, 0);
    assert.equal(h.calls.errors.length, 1);
  });
  it("does not interfere with third-party subresources or other auth modes", async (t) => {
    const h = harness();
    t.after(() => h.auth.dispose());
    const win = h.window();
    await h.auth.attach(win, `${ORIGIN}/omnigent`);
    assert.deepEqual(h.request(win, "https://another.example/image.png", "image"), {});
    h.windows.set(win, "https://app.databricksapps.com");
    assert.deepEqual(h.request(win, "https://identity.example/login"), {});
    const legacy = harness({ mode: "embedded" });
    t.after(() => legacy.auth.dispose());
    assert.deepEqual(legacy.request(legacy.window(), `${ORIGIN}/login/sso`), {});
    assert.equal(legacy.calls.renew.length, 0);
  });
  it("does not overwrite navigation completed while renewal was in flight", async (t) => {
    let resolve;
    const h = harness({
      ensureSession: () =>
        new Promise((r) => {
          resolve = r;
        }),
    });
    t.after(() => h.auth.dispose());
    const win = h.window();
    await h.auth.attach(win, `${ORIGIN}/omnigent`, TARGET);
    h.request(win, `${ORIGIN}/login`);
    await drain();
    win.webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent/c/another-chat`);
    resolve(ORIGIN);
    await drain();
    assert.deepEqual(h.calls.load, []);
  });
  it("detaches safely after the BrowserWindow has destroyed its webContents", async (t) => {
    const h = harness();
    t.after(() => h.auth.dispose());
    const win = h.window();
    await h.auth.attach(win, `${ORIGIN}/omnigent`);
    const wc = win.webContents;
    win.isDestroyed = () => true;
    win.webContents = null;
    h.auth.detach(win);
    assert.equal(h.timers.size, 0);
    assert.equal(wc.listenerCount("did-navigate"), 0);
  });

  it("disposes pending recovery on server switch without navigating the new server", async (t) => {
    let resolve;
    const h = harness({
      ensureSession: () =>
        new Promise((r) => {
          resolve = r;
        }),
    });
    t.after(() => h.auth.dispose());
    const win = h.window();
    await h.auth.attach(win, `${ORIGIN}/omnigent`);
    h.request(win, `${ORIGIN}/login`);
    await drain();
    h.auth.detach(win);
    h.windows.set(win, "https://server.example");
    resolve(ORIGIN);
    await drain();
    assert.equal(h.timers.size, 0);
    assert.deepEqual(h.calls.load, []);
    assert.deepEqual(h.calls.errors, []);
  });
});
