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
const liveCode = mainSource
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/(^|[^:])\/\/.*$/gm, "$1");

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
