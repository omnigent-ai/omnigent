// Regression guard for how src/main.js WIRES workspace-chrome injection, run
// with `node --test` (no extra deps). The wiring itself lives in
// src/workspace-chrome.js (registerWorkspaceChromeHide registers a
// did-finish-load listener that injects the chrome-hide CSS) and its BEHAVIOR is
// unit-tested in workspace-chrome.test.js. This guards the complementary half
// that no behavior test can see: that main.js still actually INVOKES
// registerWorkspaceChromeHide(win.webContents) as live code — not removed, not
// commented out.
//
// A naive source-string match would pass even if the call were commented out
// (the text still appears in the comment), so we strip comments from the source
// before asserting. URL slashes (`https://`) are preserved by only treating a
// `//` NOT preceded by `:` as a line comment. (This cannot prove the call runs
// at runtime — only an Electron launch could — but it does catch the call being
// removed or commented out, which the behavior test in workspace-chrome.test.js
// cannot, because that test never touches main.js.)

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");

const mainSource = readFileSync(path.join(__dirname, "../src/main.js"), "utf8");

// Strip block comments, then line comments (leaving `://` in URLs intact).
const liveCode = mainSource.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

describe("workspace chrome injection wiring (src/main.js)", () => {
  it("invokes registerWorkspaceChromeHide(win.webContents) as live code", () => {
    assert.match(
      liveCode,
      /registerWorkspaceChromeHide\(win\.webContents\)/,
      [
        "src/main.js no longer has a live registerWorkspaceChromeHide(win.webContents)",
        "call (it was removed or commented out). That call wires the did-finish-load",
        "listener that injects WORKSPACE_CHROME_HIDE_CSS to hide the Databricks workspace",
        "top-nav/switcher in the desktop window. Without it the switcher reappears and users",
        "can navigate out of Omnigent into other workspace apps. Re-add the call (the wiring",
        "is defined in src/workspace-chrome.js); do not delete this test.",
      ].join(" "),
    );
  });

  it("does not gate the wiring behind a URL/path check", () => {
    assert.doesNotMatch(
      liveCode,
      /registerWorkspaceChromeHide[\s\S]{0,200}(WORKSPACE_UI_PATH|pathname|startsWith)/,
      [
        "A URL/path gate was reintroduced around the chrome-hide wiring. It must stay",
        "UNCONDITIONAL: the original bug gated on pathname.startsWith(WORKSPACE_UI_PATH),",
        "which skipped injection on auth redirects and path variants and left the workspace",
        "switcher visible. The CSS targets .omnigent-app (workspace-embedded build only), so",
        "injecting on every load is a safe no-op elsewhere. See src/workspace-chrome.js.",
      ].join(" "),
    );
  });
});

// Wiring guards for the window-open policy (src/popupPolicy.js decides,
// main.js enforces; policy behavior is unit-tested in popupPolicy.test.js).
// Losing any of these silently reopens the chromeless-credential-window
// hole the policy exists to close.
describe("window-open policy wiring (src/main.js)", () => {
  it("routes setWindowOpenHandler decisions through decideWindowOpen as live code", () => {
    assert.match(
      liveCode,
      /setWindowOpenHandler\(\s*\(\{\s*url,\s*disposition,\s*features\s*\}\)\s*=>\s*\{[\s\S]{0,200}decideWindowOpen\(/,
      [
        "src/main.js no longer passes window.open through decideWindowOpen. Either every",
        "popup is denied (OAuth sign-in breaks) or popups open without the",
        "pinned-opener/https/allowlist conditions. Restore the dispatch.",
      ].join(" "),
    );
  });

  it("attaches the no-op popup preload and sandbox to allowed popups", () => {
    assert.match(
      liveCode,
      /preload:\s*POPUP_PRELOAD[\s\S]{0,120}sandbox:\s*true/,
      [
        "Allowed popups no longer force preload: POPUP_PRELOAD + sandbox: true, so a child",
        "window can inherit the SHELL preload's IPC bridges while showing third-party",
        "sign-in pages. Restore both overrides (see popup_preload.js).",
      ].join(" "),
    );
  });

  it("hardens created popups via did-create-window → hardenOauthPopup as live code", () => {
    assert.match(
      liveCode,
      /did-create-window[\s\S]{0,120}hardenOauthPopup\(/,
      [
        "Allowed popups no longer run through hardenOauthPopup (host-stamped title, no",
        "popups-from-popups, localhost-trust registration). Re-add the wiring.",
      ].join(" "),
    );
  });
});

// Guards for the popup ↔ localhost-trust bridge. E2E-verified failure when
// lost: Okta FastPass queries the LNA permission from inside the popup,
// gets "denied" (a popup is not a shell window), and fails closed —
// blocking sign-in for every Okta-fronted provider.
describe("OAuth popup localhost trust wiring (src/main.js)", () => {
  it("registers popups in oauthPopups inside hardenOauthPopup as live code", () => {
    assert.match(
      liveCode,
      /function hardenOauthPopup\(child\)\s*\{[\s\S]{0,120}oauthPopups\.add\(child\)/,
      [
        "hardenOauthPopup no longer registers the popup in oauthPopups, so",
        "isCurrentPopupOrigin never matches and Okta FastPass fails closed inside every",
        "sign-in popup. Restore oauthPopups.add(child) + the closed → delete cleanup.",
      ].join(" "),
    );
  });

  it("extends isLocalhostTrustedOrigin to live popup pages as live code", () => {
    assert.match(
      liveCode,
      /function isLocalhostTrustedOrigin\(origin\)\s*\{[\s\S]{0,300}isCurrentPopupOrigin\(origin\)/,
      [
        "isLocalhostTrustedOrigin no longer consults isCurrentPopupOrigin, so popup IdP",
        "pages get a denied LNA answer and Okta FastPass fails closed. Restore the check.",
      ].join(" "),
    );
  });
});

// Guard for the COOP-strip wiring. E2E-verified failure when lost: a
// COOP: same-origin sign-in hop (slack.com) severs the popup's
// window.opener, so every FIRST sign-in through such a provider fails and
// only retries succeed.
describe("OAuth popup COOP-strip wiring (src/main.js)", () => {
  it("composes popupResponseHeadersHook into the localhost-CORS registration as live code", () => {
    assert.match(
      liveCode,
      /registerLocalhostCors\(\s*session\.defaultSession,\s*isLocalhostTrustedOrigin,\s*popupResponseHeadersHook,?\s*\)/,
      [
        "registerLocalhostAccess no longer passes popupResponseHeadersHook to",
        "registerLocalhostCors (which owns the session's single onHeadersReceived),",
        "so COOP-serving sign-in pages sever window.opener and first-time OAuth",
        "sign-ins fail. Restore the third argument.",
      ].join(" "),
    );
  });

  it("scopes the strip to main-frame responses of tracked popups", () => {
    assert.match(
      liveCode,
      /function popupResponseHeadersHook\(details\)\s*\{[\s\S]{0,200}resourceType[\s\S]{0,240}isOauthPopupWebContentsId\(/,
      [
        "popupResponseHeadersHook lost its mainFrame/tracked-popup scoping — stripping",
        "COOP anywhere else disables a real isolation protection on ordinary browsing.",
        "Restore the resourceType + isOauthPopupWebContentsId guards.",
      ].join(" "),
    );
  });
});
