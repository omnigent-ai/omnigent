// Tests for the window-open policy (src/popupPolicy.js), run with
// `node --test`. Pure function — no Electron needed. Security property:
// page content must NEVER get an arbitrary URL opened in a chromeless
// Electron window — a child window is allowed only for the OAuth sign-in
// shape; everything else keeps leaving the shell.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { decideWindowOpen, OAUTH_POPUP_ORIGINS, WEB_SCHEMES } = require("../src/popupPolicy");

const PINNED = "https://my-workspace.cloud.databricks.com";

/** The exact shape web-shared's useForeignOauth window.open produces. */
const OAUTH_FEATURES = "width=680,height=540,left=200,top=100";

/** A well-formed OAuth popup request; tests override single fields. */
function popupDetails(overrides = {}) {
  return {
    url: "https://github.com/login/oauth/authorize?client_id=x",
    disposition: "new-window",
    features: OAUTH_FEATURES,
    ...overrides,
  };
}

function pinnedContext(overrides = {}) {
  return { openerOrigin: PINNED, pinnedOrigin: PINNED, ...overrides };
}

describe("popupPolicy — OAuth popup allow", () => {
  it("allows a popup-styled open from the pinned origin to every allowlisted sign-in host", () => {
    for (const origin of OAUTH_POPUP_ORIGINS) {
      const decision = decideWindowOpen(
        popupDetails({ url: `${origin}/authorize?x=1` }),
        pinnedContext(),
      );
      assert.deepEqual(decision, { kind: "popup" }, `expected popup for ${origin}`);
    }
  });

  it("allows a popup back to the pinned origin itself (workspace-hosted popup pages)", () => {
    const decision = decideWindowOpen(
      popupDetails({ url: `${PINNED}/oauth/callback` }),
      pinnedContext(),
    );
    assert.deepEqual(decision, { kind: "popup" });
  });

  it("normalizes explicit default ports when matching origins", () => {
    const decision = decideWindowOpen(
      popupDetails({ url: "https://github.com:443/login/oauth/authorize" }),
      pinnedContext(),
    );
    assert.deepEqual(decision, { kind: "popup" });
  });

  it("honors settings.json popup_allowed_origins for custom providers", () => {
    const decision = decideWindowOpen(
      popupDetails({ url: "https://sso.my-git-host.example.com/authorize" }),
      pinnedContext({ extraPopupOrigins: ["https://sso.my-git-host.example.com"] }),
    );
    assert.deepEqual(decision, { kind: "popup" });
  });

  it("accepts size features case-insensitively and with either dimension", () => {
    for (const features of ["WIDTH=680", "height=540", " width = 680 ", "left=1,height=2"]) {
      const decision = decideWindowOpen(popupDetails({ features }), pinnedContext());
      assert.deepEqual(decision, { kind: "popup" }, `expected popup for features "${features}"`);
    }
  });
});

describe("popupPolicy — everything else keeps leaving the shell", () => {
  it("sends plain links (foreground-tab) external, even to allowlisted hosts", () => {
    const decision = decideWindowOpen(
      popupDetails({ disposition: "foreground-tab", features: "" }),
      pinnedContext(),
    );
    assert.deepEqual(decision, { kind: "external" });
  });

  it("sends window.open without size features external (not popup-shaped)", () => {
    for (const features of ["", "noopener", "noopener,noreferrer"]) {
      const decision = decideWindowOpen(popupDetails({ features }), pinnedContext());
      assert.deepEqual(
        decision,
        { kind: "external" },
        `expected external for features "${features}"`,
      );
    }
  });

  it("sends popups external when the opener window is unpinned (setup page)", () => {
    const decision = decideWindowOpen(
      popupDetails(),
      pinnedContext({ openerOrigin: null, pinnedOrigin: null }),
    );
    assert.deepEqual(decision, { kind: "external" });
  });

  it("sends popups external when the opener is mid-SSO on a foreign origin", () => {
    const decision = decideWindowOpen(
      popupDetails(),
      pinnedContext({ openerOrigin: "https://idp.example.com" }),
    );
    assert.deepEqual(decision, { kind: "external" });
  });

  it("sends http (non-https) popups external, even to allowlisted hosts", () => {
    const decision = decideWindowOpen(
      popupDetails({ url: "http://github.com/login/oauth/authorize" }),
      pinnedContext(),
    );
    assert.deepEqual(decision, { kind: "external" });
  });

  it("sends popups to non-allowlisted https origins external (the anti-phishing gate)", () => {
    for (const url of [
      "https://evil.example.com/fake-github-login",
      "https://github.com.evil.example.com/login", // lookalike suffix
      "https://gist.github.io/x", // similar but different origin
    ]) {
      const decision = decideWindowOpen(popupDetails({ url }), pinnedContext());
      assert.deepEqual(decision, { kind: "external" }, `expected external for ${url}`);
    }
  });

  it("ignores malformed popup_allowed_origins values instead of widening", () => {
    for (const extra of ["https://sso.example.com", { origin: "x" }, 42, null]) {
      const decision = decideWindowOpen(
        popupDetails({ url: "https://sso.example.com/authorize" }),
        pinnedContext({ extraPopupOrigins: extra }),
      );
      assert.deepEqual(
        decision,
        { kind: "external" },
        `expected external for extraPopupOrigins ${JSON.stringify(extra)} (non-array must be ignored)`,
      );
    }
  });

  it("sends mailto external without consent (WEB_SCHEMES)", () => {
    assert.equal(WEB_SCHEMES.has("mailto:"), true);
    const decision = decideWindowOpen(
      { url: "mailto:someone@example.com", disposition: "foreground-tab", features: "" },
      pinnedContext(),
    );
    assert.deepEqual(decision, { kind: "external" });
  });

  it("routes non-web schemes to the consent dialog", () => {
    const decision = decideWindowOpen(
      { url: "vscode://file/x.py", disposition: "foreground-tab", features: "" },
      pinnedContext(),
    );
    assert.deepEqual(decision, { kind: "protocol-consent", scheme: "vscode:" });
  });

  it("ignores unparseable URLs (nothing safe to open)", () => {
    for (const url of ["", "not a url", "http://"]) {
      const decision = decideWindowOpen(
        { url, disposition: "new-window", features: OAUTH_FEATURES },
        pinnedContext(),
      );
      assert.deepEqual(decision, { kind: "ignore" }, `expected ignore for "${url}"`);
    }
  });
});

describe("popupPolicy — stripCrossOriginOpenerHeaders", () => {
  const { stripCrossOriginOpenerHeaders } = require("../src/popupPolicy");

  it("strips COOP and its Report-Only variant, case-insensitively", () => {
    const stripped = stripCrossOriginOpenerHeaders({
      "Cross-Origin-Opener-Policy": ["same-origin"],
      "cross-origin-opener-policy-report-only": ["same-origin; report-to=coop"],
      "Content-Type": ["text/html"],
    });
    assert.deepEqual(stripped, { "Content-Type": ["text/html"] });
  });

  it("returns null when there is nothing to strip (leave the response untouched)", () => {
    for (const headers of [
      undefined,
      {},
      { "Content-Type": ["text/html"], "Cross-Origin-Embedder-Policy": ["require-corp"] },
    ]) {
      assert.equal(
        stripCrossOriginOpenerHeaders(headers),
        null,
        `expected null for ${JSON.stringify(headers)}`,
      );
    }
  });

  it("preserves every other header exactly", () => {
    const stripped = stripCrossOriginOpenerHeaders({
      "COOP-Unrelated": ["x"],
      "set-cookie": ["a=1", "b=2"],
      "Cross-Origin-Opener-Policy": ["same-origin-allow-popups"],
    });
    assert.deepEqual(stripped, { "COOP-Unrelated": ["x"], "set-cookie": ["a=1", "b=2"] });
  });
});
