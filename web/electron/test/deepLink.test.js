// Unit tests for the deep-link decision logic (src/deepLink.js), run with
// `node --test` (no extra deps, no Electron). Two pure functions:
//   - parseOmnigentDeepLink: scheme inference + path validation
//   - chooseDeepLinkStrategy: the window-selection decision table
//
// The behavior of the inferred http(s) scheme mirrors src/url.js (loopback →
// http, remote → https); the workspace-mount discovery and the actual
// window/IPC wiring live in main.js and are covered by main.test.js guards.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { parseOmnigentDeepLink, chooseDeepLinkStrategy } = require("../src/deepLink");

describe("parseOmnigentDeepLink", () => {
  it("parses a loopback host with a port as http", () => {
    assert.deepEqual(parseOmnigentDeepLink("omnigent://localhost:8000/c/conv_abc"), {
      origin: "http://localhost:8000",
      path: "/c/conv_abc",
    });
    assert.deepEqual(parseOmnigentDeepLink("omnigent://127.0.0.1:8000/c/x"), {
      origin: "http://127.0.0.1:8000",
      path: "/c/x",
    });
  });

  it("parses a remote host as https", () => {
    assert.deepEqual(parseOmnigentDeepLink("omnigent://my-workspace.cloud.databricks.com/c/x"), {
      origin: "https://my-workspace.cloud.databricks.com",
      path: "/c/x",
    });
  });

  it("preserves a non-default port on a remote host", () => {
    assert.deepEqual(parseOmnigentDeepLink("omnigent://example.com:8443/c/x"), {
      origin: "https://example.com:8443",
      path: "/c/x",
    });
  });

  it("accepts an IPv6 loopback host as http", () => {
    assert.deepEqual(parseOmnigentDeepLink("omnigent://[::1]:8000/c/x"), {
      origin: "http://[::1]:8000",
      path: "/c/x",
    });
  });

  it("accepts an optional trailing slash on the path", () => {
    assert.equal(
      parseOmnigentDeepLink("omnigent://localhost:8000/c/conv_abc/").path,
      "/c/conv_abc/",
    );
  });

  it("drops a query string and hash (v1 forwards only the path)", () => {
    const parsed = parseOmnigentDeepLink("omnigent://localhost:8000/c/conv_abc?reply=1#frag");
    assert.equal(parsed.path, "/c/conv_abc");
    assert.equal(parsed.origin, "http://localhost:8000");
  });

  it("rejects a non-omnigent scheme", () => {
    assert.equal(parseOmnigentDeepLink("https://localhost:8000/c/x"), null);
    assert.equal(parseOmnigentDeepLink("vscode://localhost/c/x"), null);
  });

  it("rejects a link with no host", () => {
    assert.equal(parseOmnigentDeepLink("omnigent://"), null);
    assert.equal(parseOmnigentDeepLink("omnigent:///c/x"), null);
  });

  it("rejects non-/c/<id> paths", () => {
    assert.equal(parseOmnigentDeepLink("omnigent://localhost:8000/inbox"), null);
    assert.equal(parseOmnigentDeepLink("omnigent://localhost:8000/settings/appearance"), null);
    assert.equal(parseOmnigentDeepLink("omnigent://localhost:8000/c/"), null); // empty id
    assert.equal(parseOmnigentDeepLink("omnigent://localhost:8000/c/a/b"), null); // nested path
    assert.equal(parseOmnigentDeepLink("omnigent://localhost:8000/"), null);
  });

  it("rejects unparseable / non-string input", () => {
    assert.equal(parseOmnigentDeepLink("not a url"), null);
    assert.equal(parseOmnigentDeepLink(null), null);
    assert.equal(parseOmnigentDeepLink(undefined), null);
    assert.equal(parseOmnigentDeepLink(123), null);
  });
});

describe("chooseDeepLinkStrategy", () => {
  const ORIGIN = "https://my-workspace.cloud.databricks.com";
  const FOREIGN = "https://company.okta.com"; // a mid-auth IdP origin

  /** A window snapshot. */
  function win(origin, currentOrigin = origin) {
    return { origin, currentOrigin };
  }

  it("reuses a pinned window currently on the origin in-place", () => {
    const decision = chooseDeepLinkStrategy({
      targetOrigin: ORIGIN,
      windows: [win(ORIGIN)],
      knownOrigins: [ORIGIN],
    });
    assert.deepEqual(decision, { strategy: "reuse-inplace", windowIndex: 0 });
  });

  it("reloads a pinned window that is mid-auth (off-origin)", () => {
    const decision = chooseDeepLinkStrategy({
      targetOrigin: ORIGIN,
      windows: [win(ORIGIN, FOREIGN)],
      knownOrigins: [ORIGIN],
    });
    assert.deepEqual(decision, { strategy: "reuse-reload", windowIndex: 0 });
  });

  it("prefers the focused window among several pinned to the origin", () => {
    const decision = chooseDeepLinkStrategy({
      targetOrigin: ORIGIN,
      windows: [win(ORIGIN), win(ORIGIN)],
      knownOrigins: [ORIGIN],
      focusedIndex: 1,
    });
    assert.deepEqual(decision, { strategy: "reuse-inplace", windowIndex: 1 });
  });

  it("prefers a pinned window currently on the origin over a focused one that is mid-auth", () => {
    // Two windows pinned to the origin: index 0 is on it, index 1 (focused) is mid-auth.
    // On-origin wins over focus to avoid interrupting the focused window's auth flow.
    const decision = chooseDeepLinkStrategy({
      targetOrigin: ORIGIN,
      windows: [win(ORIGIN), win(ORIGIN, FOREIGN)],
      knownOrigins: [ORIGIN],
      focusedIndex: 1,
    });
    assert.deepEqual(decision, { strategy: "reuse-inplace", windowIndex: 0 });
  });

  it("falls back to the first pinned window when none are currently on the origin", () => {
    const decision = chooseDeepLinkStrategy({
      targetOrigin: ORIGIN,
      windows: [win(ORIGIN, FOREIGN), win(ORIGIN, FOREIGN)],
      knownOrigins: [ORIGIN],
    });
    assert.deepEqual(decision, { strategy: "reuse-reload", windowIndex: 0 });
  });

  it("opens a new window for a known server with no live window", () => {
    const decision = chooseDeepLinkStrategy({
      targetOrigin: ORIGIN,
      windows: [win("https://other.example.com")],
      knownOrigins: [ORIGIN, "https://other.example.com"],
    });
    assert.deepEqual(decision, { strategy: "open-known" });
  });

  it("requires consent for a never-connected server with no live window", () => {
    const decision = chooseDeepLinkStrategy({
      targetOrigin: ORIGIN,
      windows: [],
      knownOrigins: ["https://other.example.com"],
    });
    assert.deepEqual(decision, { strategy: "consent-unknown" });
  });

  it("ignores setup-page (unpinned) windows even when the server is unknown", () => {
    const decision = chooseDeepLinkStrategy({
      targetOrigin: ORIGIN,
      windows: [win(null, null)], // a window still on the setup page
      knownOrigins: [],
    });
    assert.deepEqual(decision, { strategy: "consent-unknown" });
  });

  it("treats a pinned window as reuse even when the server is not in knownOrigins", () => {
    // A window CAN be pinned to an origin the user connected to this session
    // only (ephemeral window) — it's still trusted for in-place routing.
    const decision = chooseDeepLinkStrategy({
      targetOrigin: ORIGIN,
      windows: [win(ORIGIN)],
      knownOrigins: [],
    });
    assert.deepEqual(decision, { strategy: "reuse-inplace", windowIndex: 0 });
  });
});
