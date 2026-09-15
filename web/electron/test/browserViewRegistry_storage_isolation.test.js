// Regression test: per-conversation WebContentsViews must each
// receive a distinct storage `partition` so cookie/localStorage/cache stores
// are isolated between agent sessions.
//
// The bug: `getOrCreate` in browserViewRegistry.js passes NO `partition` to
// `WebContentsViewCtor`, which means Electron assigns every view to
// `session.defaultSession` — one shared cookie jar for all agents and the main
// window. Agent A's login bleeds into Agent B's view.
//
// The fix: each view must be constructed with a per-conversation partition,
// e.g. `persist:omnigent-agent-<conversationId>` (or an in-memory one).
//
// How the test works: We replace `WebContentsViewCtor` with a spy that records
// the `webPreferences` opts it receives and expose those via the registry's
// `get()` API. For each of two distinct conversations we:
//  1. Call `openOrNavigate` so the view is created.
//  2. Assert the `webPreferences.partition` passed to the ctor is present and
//     differs between the two conversations.
//
// On the UNFIXED code this test fails because `webPreferences.partition` is
// `undefined` for every view (they all silently land on defaultSession).
// After the fix it passes because each conversation gets its own partition key.

"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { readFileSync } = require("node:fs");
const path = require("node:path");

const { createBrowserViewRegistry, agentPartition } = require("../src/browserViewRegistry");
const { createBrowserViewBoundsController } = require("../src/browserViewBounds");

/**
 * Build a registry that captures the `webPreferences` object passed to
 * `WebContentsViewCtor` for every created view. Returns the registry and a
 * map of { conversationId -> webPreferences } so tests can assert partition
 * assignment.
 *
 * Each created view is keyed by the creation ORDER (first, second, …); we
 * also use `openOrNavigate` which embeds the conversationId so we can confirm
 * isolation per-conversation directly.
 */
function makePartitionCapturingRegistry() {
  // capturedPrefs: conversationId -> webPreferences passed at construction time.
  // Because the registry has no "which conversationId am I creating for?" arg
  // to the ctor, we correlate via call order: the Nth call to ctor corresponds
  // to the Nth distinct conversationId passed to openOrNavigate.
  const createdPrefs = []; // array of webPreferences objects in creation order

  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: (opts) => {
      // Record exactly what webPreferences the registry passed us.
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
  });

  return { registry, createdPrefs };
}

describe("browserViewRegistry — storage partition isolation", () => {
  it("passes a non-empty partition to WebContentsViewCtor for each created view", () => {
    const { registry, createdPrefs } = makePartitionCapturingRegistry();

    // Create a view for conversation A.
    registry.openOrNavigate("conv_A", "https://example.com/");
    // Create a view for conversation B.
    registry.openOrNavigate("conv_B", "https://example.com/");

    assert.equal(createdPrefs.length, 2, "two distinct views should have been created");

    const [prefsA, prefsB] = createdPrefs;

    // Each view MUST be given a partition.
    assert.ok(
      prefsA.partition && prefsA.partition.length > 0,
      `view for conv_A has no partition — got: ${JSON.stringify(prefsA.partition)}`,
    );
    assert.ok(
      prefsB.partition && prefsB.partition.length > 0,
      `view for conv_B has no partition — got: ${JSON.stringify(prefsB.partition)}`,
    );
  });

  it("gives each conversation a DISTINCT partition (no shared cookie jar)", () => {
    const { registry, createdPrefs } = makePartitionCapturingRegistry();

    registry.openOrNavigate("conv_A", "https://example.com/");
    registry.openOrNavigate("conv_B", "https://example.com/");

    assert.equal(createdPrefs.length, 2, "two distinct views should have been created");

    const [prefsA, prefsB] = createdPrefs;

    // The critical assertion: if both partitions are the same (or both
    // undefined), all views share one cookie store — the bug.
    assert.notEqual(
      prefsA.partition,
      prefsB.partition,
      `conv_A and conv_B share partition "${prefsA.partition}" — cookies are NOT isolated`,
    );
  });

  it("encodes the conversationId in the partition so the session is predictable/debuggable", () => {
    const { registry, createdPrefs } = makePartitionCapturingRegistry();

    registry.openOrNavigate("conv_XYZ123", "https://example.com/");

    assert.equal(createdPrefs.length, 1);
    const { partition } = createdPrefs[0];
    assert.ok(
      partition && partition.includes("conv_XYZ123"),
      `partition "${partition}" does not embed the conversationId "conv_XYZ123"`,
    );
  });

  it("does not re-create (and re-partition) an existing entry on repeated openOrNavigate", () => {
    const { registry, createdPrefs } = makePartitionCapturingRegistry();

    // First call creates; subsequent calls should reuse the same view.
    registry.openOrNavigate("conv_A", "https://example.com/page1");
    registry.openOrNavigate("conv_A", "https://example.com/page2");

    assert.equal(
      createdPrefs.length,
      1,
      "WebContentsViewCtor must be called exactly once for a given conversation",
    );
  });
});

describe("browserViewRegistry — partition storage placement", () => {
  it("uses an in-memory partition (no persist: prefix) so agent cookies stay off disk", () => {
    const { createdPrefs, registry } = makePartitionCapturingRegistry();
    registry.openOrNavigate("conv_A", "https://example.com/");
    const { partition } = createdPrefs[0];
    assert.ok(partition, "a partition must be set");
    assert.ok(
      !partition.startsWith("persist:"),
      `partition "${partition}" is persistent — agent-visited cookies would land on disk`,
    );
  });

  it("agentPartition is deterministic per conversation and distinct across conversations", () => {
    assert.equal(agentPartition("w1", "conv_A"), agentPartition("w1", "conv_A"));
    assert.notEqual(agentPartition("w1", "conv_A"), agentPartition("w1", "conv_B"));
    assert.ok(agentPartition("w1", "conv_A").includes("conv_A"));
  });

  it("namespaces partitions per registry so equal conversationIds in two windows don't collide", () => {
    // Electron sessions are app-global but conversation ids are only unique
    // per server — two windows on different servers can carry the same id.
    const one = makePartitionCapturingRegistry();
    const two = makePartitionCapturingRegistry();
    one.registry.openOrNavigate("conv_same", "https://example.com/");
    two.registry.openOrNavigate("conv_same", "https://example.com/");
    assert.notEqual(
      one.createdPrefs[0].partition,
      two.createdPrefs[0].partition,
      "two registries gave the same conversationId one shared partition",
    );
  });
});

describe("agent partition permission hardening wiring (src/main.js)", () => {
  // Agent views moved off session.defaultSession onto per-conversation
  // partitions, so the shell's defaultSession permission handlers no longer
  // cover them — and an Electron session with NO handler auto-grants every
  // permission request. Guard that main.js wires deny-all handlers for each
  // agent partition before constructing the view. Comment-stripped source
  // match (same technique as main.test.js): proves the calls exist as live
  // code, which the unit tests above cannot see.
  const mainSource = readFileSync(path.join(__dirname, "../src/main.js"), "utf8");
  const liveCode = mainSource.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

  it("registers deny-all permission handlers on each agent partition", () => {
    assert.match(
      liveCode,
      /function hardenAgentPartition[\s\S]{0,600}session\.fromPartition\(partition\)[\s\S]{0,300}setPermissionRequestHandler\([\s\S]{0,120}callback\(false\)\)[\s\S]{0,200}setPermissionCheckHandler\(\(\) => false\)/,
    );
  });

  it("hardens the partition before the WebContentsView is constructed", () => {
    assert.match(
      liveCode,
      /hardenAgentPartition\(opts && opts\.webPreferences && opts\.webPreferences\.partition\);[\s\S]{0,120}new WebContentsView\(opts\)/,
    );
  });
});
