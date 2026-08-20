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

const { isLoginRedirect, shouldRestoreDeepLink, registerSessionExpiryReload } = require("../src/session-expiry");

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

describe("shouldRestoreDeepLink", () => {
  const PINNED_ORIGIN = "https://ws.databricks.com";

  it("restores when landed at root and saved at a deep link on the same origin", () => {
    assert.equal(
      shouldRestoreDeepLink({
        savedUrl: "https://ws.databricks.com/ml/omnigents/c/abc123",
        landedUrl: "https://ws.databricks.com/",
        pinnedOrigin: PINNED_ORIGIN,
      }),
      true,
    );
  });

  it("restores when landed at origin-root path (implicit) and saved at a deep link", () => {
    assert.equal(
      shouldRestoreDeepLink({
        savedUrl: "https://ws.databricks.com/ml/omnigents/c/abc123",
        landedUrl: "https://ws.databricks.com",
        pinnedOrigin: PINNED_ORIGIN,
      }),
      true,
    );
  });

  it("does not restore when already on the deep link after re-auth", () => {
    // Auth already returned to the deep link; no restoration needed.
    assert.equal(
      shouldRestoreDeepLink({
        savedUrl: "https://ws.databricks.com/ml/omnigents/c/abc123",
        landedUrl: "https://ws.databricks.com/ml/omnigents/c/abc123",
        pinnedOrigin: PINNED_ORIGIN,
      }),
      false,
    );
  });

  it("does not restore when saved URL was already root", () => {
    // Nothing to restore if we saved root.
    assert.equal(
      shouldRestoreDeepLink({
        savedUrl: "https://ws.databricks.com/",
        landedUrl: "https://ws.databricks.com/",
        pinnedOrigin: PINNED_ORIGIN,
      }),
      false,
    );
  });

  it("does not restore when saved origin differs from pinned origin", () => {
    // Saved URL is on a foreign origin (shouldn't happen, but be safe).
    assert.equal(
      shouldRestoreDeepLink({
        savedUrl: "https://other.databricks.com/ml/omnigents/c/abc123",
        landedUrl: "https://ws.databricks.com/",
        pinnedOrigin: PINNED_ORIGIN,
      }),
      false,
    );
  });

  it("does not restore when landed origin differs from pinned origin", () => {
    // Post-auth navigation went to a foreign origin (shouldn't happen).
    assert.equal(
      shouldRestoreDeepLink({
        savedUrl: "https://ws.databricks.com/ml/omnigents/c/abc123",
        landedUrl: "https://other.databricks.com/",
        pinnedOrigin: PINNED_ORIGIN,
      }),
      false,
    );
  });

  it("does not restore when landed URL is not at root", () => {
    // Post-auth landed at a subpath (not the expected root redirect).
    assert.equal(
      shouldRestoreDeepLink({
        savedUrl: "https://ws.databricks.com/ml/omnigents/c/abc123",
        landedUrl: "https://ws.databricks.com/unexpected/path",
        pinnedOrigin: PINNED_ORIGIN,
      }),
      false,
    );
  });

  it("does not restore when URLs are unparseable", () => {
    assert.equal(
      shouldRestoreDeepLink({
        savedUrl: "not a url",
        landedUrl: "https://ws.databricks.com/",
        pinnedOrigin: PINNED_ORIGIN,
      }),
      false,
    );
    assert.equal(
      shouldRestoreDeepLink({
        savedUrl: "https://ws.databricks.com/ml/omnigents/c/abc123",
        landedUrl: "not a url",
        pinnedOrigin: PINNED_ORIGIN,
      }),
      false,
    );
  });

  it("does not restore when parameters are missing", () => {
    assert.equal(shouldRestoreDeepLink({}), false);
    assert.equal(
      shouldRestoreDeepLink({
        savedUrl: "https://ws.databricks.com/ml/omnigents/c/abc123",
      }),
      false,
    );
    assert.equal(
      shouldRestoreDeepLink({
        landedUrl: "https://ws.databricks.com/",
      }),
      false,
    );
  });

  it("preserves query strings and fragments in saved URL on restore", () => {
    // The comparison is URL-string based, so query and fragment matter.
    // This test documents that we compare full URLs (not just pathname).
    assert.equal(
      shouldRestoreDeepLink({
        savedUrl: "https://ws.databricks.com/ml/omnigents/c/abc123?tab=settings#section",
        landedUrl: "https://ws.databricks.com/",
        pinnedOrigin: PINNED_ORIGIN,
      }),
      true,
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
