// Embedded browser views run on a persistent browser profile shared across
// conversations, windows and app restarts, so a sign-in survives like it does
// in a normal browser. The registry resolves each view's partition through
// `resolvePartition`, which defaults to the default profile.

"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { readFileSync } = require("node:fs");
const path = require("node:path");

const { createBrowserViewRegistry } = require("../src/browserViewRegistry");
const { createBrowserViewBoundsController } = require("../src/browserViewBounds");
const { DEFAULT_BROWSER_PROFILE_ID, browserProfilePartition } = require("../src/browserProfiles");

const DEFAULT_PARTITION = browserProfilePartition(DEFAULT_BROWSER_PROFILE_ID);

/** Registry whose stub ctor records the webPreferences of each created view, in order. */
function makePartitionCapturingRegistry(options = {}) {
  const createdPrefs = [];
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: (opts) => {
      createdPrefs.push((opts && opts.webPreferences) || {});
      return {
        setBounds() {},
        setVisible() {},
        webContents: {
          loadURL() {},
          close() {},
          removeListener() {},
          on() {},
          setWindowOpenHandler() {},
        },
      };
    },
    createBoundsController: createBrowserViewBoundsController,
    attachToHost() {},
    detachFromHost() {},
    sendToRenderer() {},
    getHostZoomFactor: () => 1,
    ...options,
  });
  return { registry, createdPrefs };
}

describe("browserViewRegistry — browser profile storage", () => {
  it("puts agent views and user tabs of every conversation on the default profile", () => {
    const { registry, createdPrefs } = makePartitionCapturingRegistry();
    const ids = ["conv_A", "browser-tab:conv_A:first", "conv_B", "browser-tab:conv_B:first"];
    for (const id of ids) registry.openOrNavigate(id, "https://example.com/");
    assert.deepEqual(
      createdPrefs.map((prefs) => prefs.partition),
      ids.map(() => DEFAULT_PARTITION),
    );
    assert.equal(new Set(ids.map((id) => registry.get(id).view)).size, ids.length);
  });

  it("shares the profile across registries, so every shell window sees one sign-in", () => {
    const one = makePartitionCapturingRegistry();
    const two = makePartitionCapturingRegistry();
    one.registry.openOrNavigate("conv_same", "https://example.com/");
    two.registry.openOrNavigate("conv_other", "https://example.com/");
    assert.equal(one.createdPrefs[0].partition, DEFAULT_PARTITION);
    assert.equal(two.createdPrefs[0].partition, DEFAULT_PARTITION);
  });

  it("persists the profile on disk and keeps views off the shell's default session", () => {
    assert.match(DEFAULT_PARTITION, /^persist:omnigent-browser-/);
  });

  it("resolves view creation and agent DNS checks through an injected resolvePartition", async () => {
    const lookups = [];
    const { registry, createdPrefs } = makePartitionCapturingRegistry({
      resolvePartition: (viewId) => `persist:omnigent-browser-for-${viewId}`,
      resolveHost: async (hostname, partition) => {
        lookups.push({ hostname, partition });
        return ["93.184.215.14"];
      },
    });
    registry.openOrNavigate("conv_A", "https://example.com/");
    assert.equal(createdPrefs[0].partition, "persist:omnigent-browser-for-conv_A");
    const verdict = await registry.checkAgentNavigation("conv_B", "https://example.com/", null);
    assert.equal(verdict.ok, true);
    assert.deepEqual(lookups, [
      { hostname: "example.com", partition: "persist:omnigent-browser-for-conv_B" },
    ]);
  });

  it("does not re-create an existing entry on repeated openOrNavigate", () => {
    const { registry, createdPrefs } = makePartitionCapturingRegistry();
    registry.openOrNavigate("conv_A", "https://example.com/page1");
    registry.openOrNavigate("conv_A", "https://example.com/page2");
    assert.equal(createdPrefs.length, 1);
  });
});

describe("browser profile hardening wiring (src/main.js)", () => {
  // Profile sessions are not session.defaultSession, so the shell's permission
  // handlers don't cover them, and a session with no handler auto-grants every
  // request. Comment-stripped source match (same technique as main.test.js)
  // proves the wiring exists as live code, which the unit tests above can't see.
  const mainSource = readFileSync(path.join(__dirname, "../src/main.js"), "utf8");
  const liveCode = mainSource.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

  it("registers the browser permission policy on each view's partition", () => {
    assert.match(
      liveCode,
      /function hardenAgentPartition[\s\S]{0,200}session\.fromPartition\(partition\)[\s\S]{0,100}registerBrowserPermissions\(ses,/,
    );
  });

  it("hardens the partition before construction and binds the policy to the new view", () => {
    assert.match(
      liveCode,
      /hardenAgentPartition\(opts\.webPreferences\.partition, win, canPrompt,[\s\S]{0,100}const view = new WebContentsView\(opts\);\s*policy\.attach\(view\.webContents\)/,
    );
  });

  it("asks every window's registry about requests on a shared profile session", () => {
    assert.match(
      liveCode,
      /function guardAgentPartitionRequests\(partition, registry\) \{\s*agentGuardRegistries\.add\(registry\);/,
    );
    assert.match(liveCode, /releaseAgentRequestGuard\(browserRegistry\)/);
  });
});
