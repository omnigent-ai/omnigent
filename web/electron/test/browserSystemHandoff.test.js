// Tests for choosing the URL a browser tab hands off to the system browser.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  NAVIGATION_TRAIL_LIMIT,
  isGoogleAccessBlockedUrl,
  recordNavigation,
  signInContinueUrl,
  systemBrowserUrl,
} = require("../src/browserSystemHandoff");

const BLOCKED = "https://access.workspace.google.com/remediate?flow=x";
const DOC = "https://docs.google.com/document/d/1";
const SIGN_IN = `https://accounts.google.com/ServiceLogin?continue=${encodeURIComponent(DOC)}`;

describe("browserSystemHandoff — detection", () => {
  it("flags only the Google Workspace access-blocked host", () => {
    assert.equal(isGoogleAccessBlockedUrl(BLOCKED), true);
    assert.equal(isGoogleAccessBlockedUrl("http://access.workspace.google.com/"), true);
    assert.equal(isGoogleAccessBlockedUrl("https://workspace.google.com/"), false);
    assert.equal(isGoogleAccessBlockedUrl("https://access.workspace.google.com.evil.test/"), false);
    assert.equal(isGoogleAccessBlockedUrl("not a url"), false);
    assert.equal(isGoogleAccessBlockedUrl(undefined), false);
  });
});

describe("browserSystemHandoff — continue URL", () => {
  it("reads an http(s) continue target from a sign-in page", () => {
    assert.equal(signInContinueUrl(SIGN_IN), DOC);
  });

  it("ignores continue targets that are unsafe, sign-in pages, or on other hosts", () => {
    const signIn = (target) =>
      `https://accounts.google.com/signin?continue=${encodeURIComponent(target)}`;
    assert.equal(signInContinueUrl(signIn("javascript:alert(1)")), null);
    assert.equal(signInContinueUrl(signIn("file:///etc/passwd")), null);
    assert.equal(signInContinueUrl(signIn("https://accounts.google.com/x")), null);
    assert.equal(
      signInContinueUrl(`https://example.com/?continue=${encodeURIComponent(DOC)}`),
      null,
    );
    assert.equal(signInContinueUrl("https://accounts.google.com/signin"), null);
  });
});

describe("browserSystemHandoff — navigation trail", () => {
  it("skips non-web URLs and consecutive duplicates, and keeps the newest entries", () => {
    const trail = [];
    recordNavigation(trail, "about:blank");
    recordNavigation(trail, DOC);
    recordNavigation(trail, DOC);
    recordNavigation(trail, SIGN_IN);
    assert.deepEqual(trail, [DOC, SIGN_IN]);
    for (let i = 0; i < NAVIGATION_TRAIL_LIMIT + 5; i += 1) {
      recordNavigation(trail, `https://example.com/${i}`);
    }
    assert.equal(trail.length, NAVIGATION_TRAIL_LIMIT);
    assert.equal(trail.at(-1), `https://example.com/${NAVIGATION_TRAIL_LIMIT + 4}`);
  });
});

describe("browserSystemHandoff — systemBrowserUrl", () => {
  it("opens an ordinary page as is and refuses non-web pages", () => {
    assert.equal(
      systemBrowserUrl({ currentUrl: "https://example.com/a?b=1" }),
      "https://example.com/a?b=1",
    );
    assert.equal(systemBrowserUrl({ currentUrl: "about:blank" }), null);
    assert.equal(systemBrowserUrl({ currentUrl: "file:///tmp/x" }), null);
    assert.equal(systemBrowserUrl({ currentUrl: "" }), null);
  });

  it("sends a blocked sign-in to the page it was continuing to", () => {
    assert.equal(
      systemBrowserUrl({ currentUrl: BLOCKED, navigationTrail: [DOC, SIGN_IN, BLOCKED] }),
      DOC,
    );
    const blockedWithContinue = `https://access.workspace.google.com/remediate?continue=${encodeURIComponent(
      "https://mail.google.com/",
    )}`;
    assert.equal(
      systemBrowserUrl({ currentUrl: blockedWithContinue, navigationTrail: [DOC, SIGN_IN] }),
      "https://mail.google.com/",
    );
  });

  it("falls back to the first non-sign-in page, then to the blocked page itself", () => {
    assert.equal(
      systemBrowserUrl({
        currentUrl: BLOCKED,
        navigationTrail: [DOC, "https://accounts.google.com/signin", BLOCKED],
      }),
      DOC,
    );
    assert.equal(
      systemBrowserUrl({
        currentUrl: BLOCKED,
        navigationTrail: ["https://accounts.google.com/signin", BLOCKED],
      }),
      BLOCKED,
    );
  });
});
