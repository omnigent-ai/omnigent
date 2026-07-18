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

// Guard for the deep-link path join in createWindow. A basename-less SPA path
// (/c/<id>) lives UNDER the server's workspace mount (/ml/omnigents), so it
// must be string-concatenated (resolveServerPath) — NOT resolved with
// `new URL(path, serverUrl)`, which would anchor against the ORIGIN and drop
// the mount, opening the wrong URL for every workspace deep link. This catches
// a "simplification" the behavior tests can't (createWindow isn't unit-tested).
describe("deep-link path join wiring (src/main.js)", () => {
  it("joins opts.path onto opts.serverUrl via resolveServerPath as live code", () => {
    assert.match(
      liveCode,
      /resolveServerPath\(serverUrl, opts\.path\)/,
      [
        "createWindow no longer joins opts.path onto opts.serverUrl via",
        "resolveServerPath. A deep link to a workspace server (origin + /ml/omnigents",
        "mount) would lose the mount and 404. Restore the mount-aware join (see",
        "resolveServerPath); do not replace it with `new URL(path, serverUrl)`.",
      ].join(" "),
    );
  });

  it("stores the clean serverUrl (no conversation path) separately from loadUrl", () => {
    // The window's server IDENTITY (for `omnigent host --server` etc.) must not
    // carry the /c/<id> path. Guard that createWindow sets `serverUrl: serverUrl`
    // (the clean value), not `serverUrl: destination`/`loadUrl`.
    assert.match(
      liveCode,
      /serverUrl:\s*destination\s*\?\s*serverUrl\s*:\s*null/,
      [
        "createWindow no longer stores the clean serverUrl as the window's server",
        "identity — it must keep the /c/<id> path out of `omnigent host --server`.",
        "Restore `serverUrl: destination ? serverUrl : null` in the windows.set call.",
      ].join(" "),
    );
  });
});

// Guards for the deep-link INGESTION + ORCHESTRATION wiring. The pure
// decision logic is unit-tested in deepLink.test.js; these guard that main.js
// still wires the OS entry points (open-url / second-instance / argv), the
// serialized queue, the protocol registration, and the orchestrator — the
// half no behavior test can see. Losing any silently reopens the readiness
// race (macOS open-url before whenReady) or the single-instance funnel.
describe("deep-link ingestion wiring (src/main.js)", () => {
  it("registers open-url with preventDefault + enqueueDeepLink as live code", () => {
    assert.match(
      liveCode,
      /app\.on\("open-url"[\s\S]{0,120}event\.preventDefault\(\)[\s\S]{0,80}enqueueDeepLink\(/,
      [
        "main.js no longer handles the macOS `open-url` event. Without preventDefault",
        "the OS also hands the URL to the default browser, and without enqueueDeepLink",
        "the pre-ready race (open-url can fire before whenReady) touches windows that",
        "don't exist yet. Restore app.on('open-url') → preventDefault + enqueueDeepLink.",
      ].join(" "),
    );
  });

  it("scans second-instance argv for omnigent:// and enqueues as live code", () => {
    assert.match(
      liveCode,
      /app\.on\("second-instance"[\s\S]{0,220}startsWith\("omnigent:\/\/"\)[\s\S]{0,60}enqueueDeepLink\(/,
      [
        "main.js no longer scans second-instance argv for omnigent://. Windows/Linux",
        "warm-start deep links (a second launch funneled by the single-instance lock)",
        "would be ignored. Restore the argv scan → enqueueDeepLink inside second-instance.",
      ].join(" "),
    );
  });

  it("registers the omnigent:// scheme as live code", () => {
    assert.match(
      liveCode,
      /setAsDefaultProtocolClient\("omnigent"\)/,
      [
        "main.js no longer calls app.setAsDefaultProtocolClient('omnigent'), so dev",
        "(`electron .`) clicks on an omnigent:// link won't route to the running dev",
        "instance. The packaged build's manifest registration is separate (package.json",
        "build.protocols). Restore the runtime call.",
      ].join(" "),
    );
  });

  it("gates the launch window on pending deep links as live code", () => {
    assert.match(
      liveCode,
      /pendingDeepLinks\.length > 0[\s\S]{0,80}drainPendingDeepLinks\(\)/,
      [
        "main.js no longer drains pending deep links instead of opening the default",
        "launch window, so a startup deep link would open a redundant default window",
        "next to the deep-link window. Restore the pendingDeepLinks gate in whenReady.",
      ].join(" "),
    );
  });

  it("drains the queue serialized via handleDeepLink as live code", () => {
    assert.match(
      liveCode,
      /void handleDeepLink\(/,
      [
        "main.js no longer calls handleDeepLink from the drain, so queued deep links",
        "would never be opened. Restore `void handleDeepLink(next)` in drainPendingDeepLinks.",
      ].join(" "),
    );
  });

  it("routes in-place navigation through the omnigent:open-path channel", () => {
    assert.match(
      liveCode,
      /send\("omnigent:open-path"/,
      [
        "main.js no longer sends omnigent:open-path to the SPA, so reuse-inplace deep",
        "links would focus a window without navigating it. Restore sendOpenPath's",
        "webContents.send('omnigent:open-path', path).",
      ].join(" "),
    );
  });

  it("decides via chooseDeepLinkStrategy as live code", () => {
    assert.match(
      liveCode,
      /chooseDeepLinkStrategy\(\{[\s\S]{0,80}targetOrigin[\s\S]{0,260}knownOrigins:/,
      [
        "main.js no longer drives deep-link window selection through the PURE",
        "chooseDeepLinkStrategy (unit-tested in deepLink.test.js). Inlining the",
        "decision would lose the reuse/reload/consent table. Restore the call.",
      ].join(" "),
    );
  });

  it("reloads/repoints via loadServerUrl(..., parsed.path) as live code", () => {
    assert.match(
      liveCode,
      /loadServerUrl\(\w+, \w+, parsed\.path\)/,
      [
        "main.js no longer reloads/repoints through loadServerUrl, so the mount-aware",
        "join and the clean-serverUrl identity (no /c/<id>) could be bypassed by a",
        "raw win.loadURL. Restore a loadServerUrl(<win>, <serverUrl>, parsed.path) call.",
      ].join(" "),
    );
  });

  it("runs the workspace mount probe only AFTER consent (no pre-consent SSRF)", () => {
    // The probe (expandDatabricksWorkspaceUrl) makes an HTTP request to the
    // link's host. For an UNKNOWN server that host is attacker-chosen, so the
    // probe must not run until the user has consented — otherwise clicking a
    // link probes an arbitrary host (SSRF / info disclosure) with no approval.
    // Guard that the probe call follows confirmOpenDeepLink inside the
    // consent-unknown branch, and does NOT appear before chooseDeepLinkStrategy.
    assert.match(
      liveCode,
      /confirmOpenDeepLink\(parent, targetOrigin\)[\s\S]{0,300}expandDatabricksWorkspaceUrl\(targetOrigin\)/,
      [
        "handleDeepLink no longer defers expandDatabricksWorkspaceUrl until AFTER",
        "confirmOpenDeepLink. A deep link to an unknown (attacker-chosen) server would",
        "make a pre-consent HTTP request to that host (SSRF / info disclosure). Move the",
        "probe into the consent-unknown branch, after confirmOpenDeepLink — the consent",
        "decision can run on parsed.origin (no fetch) since the probe only appends a path",
        "under the same origin.",
      ].join(" "),
    );
    assert.doesNotMatch(
      liveCode,
      /function handleDeepLink\(raw\)\s*\{[\s\S]{0,500}expandDatabricksWorkspaceUrl\(/,
      [
        "expandDatabricksWorkspaceUrl reappeared in the pre-decision section of",
        "handleDeepLink, reopening the pre-consent SSRF. The probe must run only after",
        "confirmOpenDeepLink (in the consent-unknown branch), not before chooseDeepLinkStrategy.",
      ].join(" "),
    );
  });
});
