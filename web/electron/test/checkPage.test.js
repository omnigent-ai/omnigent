"use strict";

// browser_tabs select's check (check_page in
// src/agentBrowserActions.js) that a user's tab doesn't show a page
// the issuing session's agent couldn't open itself.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { createBrowserViewRegistry } = require("../src/browserViewRegistry");
const { createBrowserViewBoundsController } = require("../src/browserViewBounds");
const { performAgentBrowserAction } = require("../src/agentBrowserActions");

/** A registry with one user tab at `url`, opened from the address bar. */
function userTab(url) {
  let current = "";
  const webContents = {
    loadURL(next) {
      current = next;
    },
    getURL: () => current,
    close() {},
    removeListener() {},
    on() {},
    setWindowOpenHandler() {},
    executeJavaScript: async () => "ok",
  };
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: () => ({ setBounds() {}, webContents }),
    createBoundsController: createBrowserViewBoundsController,
    attachToHost() {},
    detachFromHost() {},
    sendToRenderer() {},
    isHostFocused: () => true,
    resolveHost: async () => ["93.184.216.34"],
  });
  registry.openOrNavigate("conv_1", url);
  return { registry, entry: registry.get("conv_1") };
}

const check = ({ registry, entry }) =>
  performAgentBrowserAction(
    registry,
    entry,
    "conv_1",
    "check_page",
    { provenance: { sourceHostId: null } },
    { agentContextFor: () => ({ agentRequested: true }), deadlineAt: Date.now() + 5000 },
  );

describe("check_page", () => {
  it("refuses a loopback page the user opened, which agents can't open", async () => {
    const result = await check(userTab("http://127.0.0.1:8080/admin"));
    assert.equal(result.ok, false);
    assert.match(result.error, /shows a page agents in this session can't open/);
  });

  it("allows a public page and a blank tab", async () => {
    assert.deepEqual(await check(userTab("https://example.com/")), { ok: true, result: "{}" });
    assert.deepEqual(await check(userTab("about:blank")), { ok: true, result: "{}" });
  });
});
