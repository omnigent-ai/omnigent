// Unit tests for expired-session recovery (src/session-expiry.js), run with
// `node --test` (no extra deps). Covers the pure redirect matcher and the
// onBeforeRedirect wiring against a fake webRequest.
//
// The real signal (from a live expired Databricks SSO session): every API
// call gets a 303 redirect to the gate's `login.html`. The shell reloads the
// window on that so the gate can re-challenge, since a desktop user has no
// address bar to refresh manually.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const {
  isLoginRedirect,
  isOidcLoginNavigation,
  registerSessionExpiryReload,
  registerOidcSessionExpiryHandoff,
} = require("../src/session-expiry");

describe("isLoginRedirect", () => {
  it("matches a 303 redirect to the login page", () => {
    assert.equal(
      isLoginRedirect({
        statusCode: 303,
        redirectURL: "https://dbc-x.cloud.databricks.com/login.html?next_url=%2Fajax-api%2F2.0",
      }),
      true,
    );
  });

  it("matches other 3xx codes to the login page", () => {
    assert.equal(
      isLoginRedirect({ statusCode: 302, redirectURL: "https://ws.databricks.com/login.html" }),
      true,
    );
  });

  it("ignores a redirect that is not to the login page", () => {
    // An ordinary same-origin API-to-API redirect must be left alone.
    assert.equal(
      isLoginRedirect({ statusCode: 303, redirectURL: "https://ws.databricks.com/ajax-api/2.0/x" }),
      false,
    );
  });

  it("ignores non-redirect status codes", () => {
    assert.equal(
      isLoginRedirect({ statusCode: 200, redirectURL: "https://ws.databricks.com/login.html" }),
      false,
    );
  });

  it("ignores a missing or unparseable redirect URL", () => {
    assert.equal(isLoginRedirect({ statusCode: 303 }), false);
    assert.equal(isLoginRedirect({ statusCode: 303, redirectURL: "not a url" }), false);
  });

  it("does not match a path that merely contains 'login.html' mid-path", () => {
    // Only a pathname ending in /login.html counts, so an unrelated route
    // like /docs/login.html.md or a query-only match won't trip it.
    assert.equal(
      isLoginRedirect({
        statusCode: 303,
        redirectURL: "https://ws.databricks.com/x?p=/login.html",
      }),
      false,
    );
  });
});

/** A fake session whose onBeforeRedirect listener can be driven by tests. */
function fakeSession() {
  let listener = null;
  return {
    webRequest: {
      onBeforeRedirect: (cb) => {
        listener = cb;
      },
    },
    emit: (details) => listener?.(details),
  };
}

describe("registerSessionExpiryReload", () => {
  const LOGIN_REDIRECT = {
    url: "https://ws.databricks.com/ajax-api/2.0/omnigents/v1/sessions",
    statusCode: 303,
    redirectURL: "https://ws.databricks.com/login.html?next_url=%2Fajax-api",
  };

  it("reloads the connected origin on a login redirect", () => {
    const ses = fakeSession();
    const reloaded = [];
    registerSessionExpiryReload(
      ses,
      (origin) => origin === "https://ws.databricks.com",
      (origin) => reloaded.push(origin),
    );

    ses.emit(LOGIN_REDIRECT);

    assert.deepEqual(reloaded, ["https://ws.databricks.com"]);
  });

  it("ignores a login redirect for an origin no window is connected to", () => {
    const ses = fakeSession();
    const reloaded = [];
    registerSessionExpiryReload(
      ses,
      () => false, // nothing is a connected server
      (origin) => reloaded.push(origin),
    );

    ses.emit(LOGIN_REDIRECT);

    assert.deepEqual(reloaded, []);
  });

  it("ignores a non-login redirect", () => {
    const ses = fakeSession();
    const reloaded = [];
    registerSessionExpiryReload(
      ses,
      () => true,
      (origin) => reloaded.push(origin),
    );

    ses.emit({
      url: "https://ws.databricks.com/ajax-api/2.0/x",
      statusCode: 303,
      redirectURL: "https://ws.databricks.com/ajax-api/2.0/y",
    });

    assert.deepEqual(reloaded, []);
  });

  it("ignores a login redirect whose originating URL is unparseable", () => {
    // A malformed request URL must not throw out of the listener; it's
    // simply skipped (no origin to attribute the reload to).
    const ses = fakeSession();
    const reloaded = [];
    registerSessionExpiryReload(
      ses,
      () => true,
      (origin) => reloaded.push(origin),
    );

    ses.emit({ ...LOGIN_REDIRECT, url: "not a url" });

    assert.deepEqual(reloaded, []);
  });
});

describe("self-hosted OIDC session expiry", () => {
  it("matches only the pinned server's exact OIDC login route", () => {
    assert.equal(
      isOidcLoginNavigation(
        "https://server.example/auth/login?return_to=%2Fc%2Fcurrent",
        "https://server.example",
      ),
      true,
    );
    assert.equal(
      isOidcLoginNavigation(
        "https://server.example/base/auth/login?return_to=%2Fc%2Fcurrent",
        "https://server.example/base",
      ),
      true,
    );
    assert.equal(
      isOidcLoginNavigation("https://server.example/login", "https://server.example"),
      false,
    );
    assert.equal(
      isOidcLoginNavigation("https://idp.example/auth/login", "https://server.example"),
      false,
    );
    assert.equal(
      isOidcLoginNavigation(
        "https://server.example/baseball/auth/login",
        "https://server.example/base",
      ),
      false,
    );
    assert.equal(
      isOidcLoginNavigation(
        "https://server.example:444/base/auth/login",
        "https://server.example/base",
      ),
      false,
    );
  });

  it("blocks live-renderer login navigation and preserves the exact current route", async () => {
    class FakeWebContents extends EventEmitter {
      getURL() {
        return "https://server.example/c/current?tab=artifacts#latest";
      }
    }
    const webContents = new FakeWebContents();
    const handoffs = [];
    registerOidcSessionExpiryHandoff(
      webContents,
      () => "https://server.example",
      async (params) => handoffs.push(params),
    );
    const event = {
      prevented: false,
      preventDefault() {
        this.prevented = true;
      },
    };

    webContents.emit(
      "will-navigate",
      event,
      "https://server.example/auth/login?return_to=%2Fc%2Fcurrent",
    );
    await new Promise((resolve) => {
      setImmediate(resolve);
    });

    assert.equal(event.prevented, true);
    assert.deepEqual(handoffs, [
      {
        serverUrl: "https://server.example",
        returnUrl: "https://server.example/c/current?tab=artifacts#latest",
      },
    ]);
  });

  it("also blocks a main-frame redirect but ignores subframes", async () => {
    class FakeWebContents extends EventEmitter {
      getURL() {
        return "https://server.example/c/current";
      }
    }
    const webContents = new FakeWebContents();
    let handoffs = 0;
    registerOidcSessionExpiryHandoff(
      webContents,
      () => "https://server.example",
      async () => {
        handoffs += 1;
      },
    );
    const mainEvent = {
      prevented: false,
      preventDefault() {
        this.prevented = true;
      },
    };
    const subframeEvent = {
      prevented: false,
      preventDefault() {
        this.prevented = true;
      },
    };

    webContents.emit(
      "will-redirect",
      subframeEvent,
      "https://server.example/auth/login",
      false,
      false,
    );
    webContents.emit("will-redirect", mainEvent, "https://server.example/auth/login", false, true);
    await new Promise((resolve) => {
      setImmediate(resolve);
    });

    assert.equal(subframeEvent.prevented, false);
    assert.equal(mainEvent.prevented, true);
    assert.equal(handoffs, 1);
  });

  it("deduplicates repeated expiry navigation while a handoff is in flight", async () => {
    class FakeWebContents extends EventEmitter {
      getURL() {
        return "https://server.example/base/c/current";
      }
    }
    const webContents = new FakeWebContents();
    const pending = Promise.withResolvers();
    let handoffs = 0;
    registerOidcSessionExpiryHandoff(
      webContents,
      () => "https://server.example/base",
      async () => {
        handoffs += 1;
        await pending.promise;
      },
    );
    const event = () => ({ preventDefault() {} });

    webContents.emit("will-navigate", event(), "https://server.example/base/auth/login");
    webContents.emit("will-navigate", event(), "https://server.example/base/auth/login");
    await new Promise(setImmediate);
    assert.equal(handoffs, 1);

    pending.resolve();
    await new Promise(setImmediate);
    webContents.emit("will-navigate", event(), "https://server.example/base/auth/login");
    await new Promise(setImmediate);
    assert.equal(handoffs, 2);
  });
});
