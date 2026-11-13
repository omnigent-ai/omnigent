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

const check = ({ registry, entry }, extra = {}) =>
  performAgentBrowserAction(
    registry,
    entry,
    "conv_1",
    "check_page",
    { provenance: { sourceHostId: null }, ...extra },
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

  it("refuses a tab whose page changed while it was checked", async () => {
    const tab = userTab("https://example.com/");
    const realCheck = tab.registry.checkAgentNavigation;
    // The public page passes, but the tab moved to a loopback one meanwhile.
    tab.registry.checkAgentNavigation = async (...args) => {
      const verdict = await realCheck(...args);
      tab.entry.view.webContents.loadURL("http://127.0.0.1:8080/admin");
      return verdict;
    };
    const result = await check(tab, { select: true });
    assert.equal(result.ok, false);
    assert.match(result.error, /page changed while it was checked/);
    assert.equal(tab.entry.navigationPolicyEnforced, false, "a refused tab keeps its policy");
  });

  it("gives a selected tab the session's URL policy, but not a listed one", async () => {
    const listed = userTab("https://example.com/");
    assert.equal((await check(listed)).ok, true);
    assert.equal(listed.entry.navigationPolicyEnforced, false);
    const selected = userTab("https://example.com/");
    assert.equal((await check(selected, { select: true })).ok, true);
    assert.equal(selected.entry.navigationPolicyEnforced, true);
    assert.deepEqual(selected.entry.agentContext, { agentRequested: true });
  });
});
