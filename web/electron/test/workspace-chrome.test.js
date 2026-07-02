// Unit test for the workspace-chrome CSS injection (src/workspace-chrome.js),
// run with `node --test` (no extra deps). It calls the REAL function that
// main.js wires to the webContents `did-finish-load` event, passing a fake
// webContents whose URL is NOT under the workspace mount path.
//
// The original bug gated injection behind `pathname.startsWith(
// WORKSPACE_UI_PATH)`, so on such URLs (auth redirects, path variants) the CSS
// never landed and the Databricks workspace switcher stayed visible.
// Reintroducing any URL/path guard inside applyWorkspaceChromeHideCss stops
// insertCSS from firing here, failing this test.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  applyWorkspaceChromeHideCss,
  registerWorkspaceChromeHide,
  WORKSPACE_CHROME_HIDE_CSS,
} = require("../src/workspace-chrome");

describe("applyWorkspaceChromeHideCss", () => {
  it("injects the chrome-hide CSS even when the URL is not under the workspace path", () => {
    const injected = [];
    const webContents = {
      // A path variant the old guard would have skipped (not /ml/omnigents).
      getURL: () => "https://dbc-x.cloud.databricks.com/dashboard",
      insertCSS: (css) => {
        injected.push(css);
        return Promise.resolve();
      },
    };

    applyWorkspaceChromeHideCss(webContents);

    assert.deepEqual(
      injected,
      [WORKSPACE_CHROME_HIDE_CSS],
      [
        "applyWorkspaceChromeHideCss must inject WORKSPACE_CHROME_HIDE_CSS for ANY loaded",
        "URL, but it did not fire for a non-/ml/omnigents path. A URL/path guard has likely",
        "been reintroduced. That is the original bug: gating injection by path left the",
        "Databricks workspace switcher visible on auth redirects and path variants. Injection",
        "must stay unconditional — the CSS only targets .omnigent-app (workspace-embedded",
        "build), so it is a harmless no-op elsewhere.",
      ].join(" "),
    );
  });
});

describe("registerWorkspaceChromeHide", () => {
  // This is the behavior half of the guard. main.test.js proves main.js still
  // CALLS registerWorkspaceChromeHide; this proves the function, once called,
  // injects exactly once per full document load. We hand it a fake webContents
  // that captures the listener registered via `.on(eventName, listener)`, then
  // fire the event ourselves and assert the CSS landed.
  function fakeWebContents() {
    const listeners = new Map();
    const injected = [];
    return {
      injected,
      emit(eventName) {
        const listener = listeners.get(eventName);
        if (listener) listener();
      },
      on: (eventName, listener) => {
        listeners.set(eventName, listener);
      },
      insertCSS: (css) => {
        injected.push(css);
        return Promise.resolve();
      },
    };
  }

  it("injects nothing until a full load fires", () => {
    const webContents = fakeWebContents();

    registerWorkspaceChromeHide(webContents);

    assert.deepEqual(
      webContents.injected,
      [],
      [
        "registerWorkspaceChromeHide injected CSS at wiring time instead of waiting for a",
        "load event. It must only register a listener; injecting before the document is",
        "ready can no-op against a blank page and leave the workspace chrome visible.",
      ].join(" "),
    );
  });

  it("injects the chrome-hide CSS once when did-finish-load fires", () => {
    const webContents = fakeWebContents();

    registerWorkspaceChromeHide(webContents);
    webContents.emit("did-finish-load");

    assert.deepEqual(
      webContents.injected,
      [WORKSPACE_CHROME_HIDE_CSS],
      [
        "registerWorkspaceChromeHide did not inject WORKSPACE_CHROME_HIDE_CSS exactly once",
        "after did-finish-load fired. Likely the event name was changed (it must stay",
        "'did-finish-load', the full-document-load event), the listener was not registered,",
        "or the injection was dropped. Without this, the Databricks workspace top-nav/switcher",
        "stays visible in the desktop window and users can navigate out of Omnigent.",
      ].join(" "),
    );
  });
});
