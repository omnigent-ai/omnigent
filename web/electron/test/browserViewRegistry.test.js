// Tests for the per-conversation browser-view registry (src/browserViewRegistry.js),
// run with `node --test` (no extra deps). The registry is a pure factory — all
// Electron deps are injected — so we drive it with stub views + spies.
//
// The load-bearing case is the FIRST-navigate activation signal: on a fresh
// conversation, openOrNavigate creates the view DETACHED (activeConversationId
// is null), so no `browser-host-active-changed` fires. Without the
// `browser-view-created` emit the React pane would never learn a view exists,
// never mount its placeholder, and never call setActive — the pane would stay
// invisible (the bug this test guards against).

const { describe, it, beforeEach } = require("node:test");
const assert = require("node:assert/strict");

const { createBrowserViewRegistry } = require("../src/browserViewRegistry");
const { createBrowserViewBoundsController } = require("../src/browserViewBounds");

/** Build a registry with spy-backed injected deps. Returns the registry plus
 *  the recorded renderer sends / attach / detach calls for assertions. */
function makeRegistry() {
  const sent = []; // { channel, payload }
  const attached = [];
  const detached = [];
  const makeStubView = () => ({
    setBounds() {},
    webContents: {
      loadURL() {},
      close() {},
      removeListener() {},
    },
  });
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: () => makeStubView(),
    createBoundsController: createBrowserViewBoundsController,
    attachToHost: (view) => attached.push(view),
    detachFromHost: (view) => detached.push(view),
    sendToRenderer: (channel, payload) => sent.push({ channel, payload }),
    getHostZoomFactor: () => 1,
  });
  return { registry, sent, attached, detached };
}

describe("browserViewRegistry — first-navigate activation signal", () => {
  let ctx;
  beforeEach(() => {
    ctx = makeRegistry();
  });

  it("emits browser-view-created on first openOrNavigate for a fresh conversation", () => {
    const r = ctx.registry.openOrNavigate("conv_1", "https://example.com");
    assert.equal(r.ok, true);
    assert.equal(r.created, true);
    const created = ctx.sent.filter((s) => s.channel === "browser-view-created");
    assert.equal(created.length, 1, "exactly one create event");
    assert.deepEqual(created[0].payload, { conversationId: "conv_1" });
  });

  it("creates the view DETACHED when the conversation isn't active (no attach, no host-active event)", () => {
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    // Fresh conversation → activeConversationId stayed null → never attached.
    assert.equal(ctx.attached.length, 0, "view is created detached");
    assert.equal(ctx.registry.activeConversationId(), null);
    const active = ctx.sent.filter((s) => s.channel === "browser-host-active-changed");
    assert.equal(active.length, 0, "no host-active event on detached create");
  });

  it("does NOT re-emit browser-view-created on a subsequent navigate of the same conversation", () => {
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    ctx.registry.openOrNavigate("conv_1", "https://example.org");
    const created = ctx.sent.filter((s) => s.channel === "browser-view-created");
    assert.equal(created.length, 1, "create fires once, on first create only");
  });

  it("setActive after create attaches the view and fires host-active-changed", () => {
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    const r = ctx.registry.setActive("conv_1");
    assert.equal(r.ok, true);
    assert.equal(ctx.attached.length, 1, "setActive attaches the view");
    assert.equal(ctx.registry.activeConversationId(), "conv_1");
    const active = ctx.sent.filter(
      (s) => s.channel === "browser-host-active-changed" && s.payload.conversationId === "conv_1",
    );
    assert.equal(active.length, 1);
  });

  it("has() reports view existence for the re-mount probe", () => {
    assert.equal(ctx.registry.has("conv_1"), false);
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    assert.equal(ctx.registry.has("conv_1"), true);
  });

  it("full first-navigate path: create signal → setActive → attached + bounds synced", () => {
    // Mirrors what BrowserPane does: it learns of the view from the create
    // event, mounts the placeholder, then setActive attaches + syncs bounds.
    let sawCreate = null;
    // (the pane's onBrowserViewCreated listener)
    ctx.sent.length = 0;
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    sawCreate = ctx.sent.find((s) => s.channel === "browser-view-created");
    assert.ok(sawCreate, "pane would receive the create event");

    // Pane reacts: setActive(conversationId) then a resize (bounds).
    ctx.registry.setActive("conv_1");
    const entry = ctx.registry.get("conv_1");
    assert.ok(entry, "entry resolvable for resize");
    entry.boundsController.setRendererBounds({
      x: 10,
      y: 20,
      width: 300,
      height: 400,
      devicePixelRatio: 1,
    });
    assert.equal(ctx.attached.length, 1, "view attached to host");
  });

  it("close() detaches any design-mode console listener stored on the entry", () => {
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    const entry = ctx.registry.get("conv_1");
    // Simulate what browserIpc's enable-design-mode handler does: stash a
    // listener + its webContents on the entry. close() must detach it so a
    // destroyed view leaves no dangling console-message listener.
    let removed = null;
    const handler = () => {};
    entry.designModeListener = handler;
    entry.designModeWebContents = {
      removeListener: (evt, fn) => {
        removed = { evt, fn };
      },
    };
    const r = ctx.registry.close("conv_1");
    assert.equal(r.removed, true);
    assert.deepEqual(removed, { evt: "console-message", fn: handler });
    assert.equal(entry.designModeListener, null);
    assert.equal(entry.designModeWebContents, null);
  });
});

// Build a registry whose stub views record every loadURL call, so we can
// assert the agent-navigation allowlist blocks BEFORE loadURL is reached.
function makeLoadTrackingRegistry() {
  const loaded = []; // urls passed to loadURL
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: () => ({
      setBounds() {},
      webContents: {
        loadURL(u) {
          loaded.push(u);
        },
        close() {},
        removeListener() {},
      },
    }),
    createBoundsController: createBrowserViewBoundsController,
    attachToHost() {},
    detachFromHost() {},
    sendToRenderer() {},
    getHostZoomFactor: () => 1,
  });
  return { registry, loaded };
}

describe("browserViewRegistry — agent-navigation allowlist", () => {
  it("rejects agent navigation to file:// and never calls loadURL", () => {
    const { registry, loaded } = makeLoadTrackingRegistry();
    const r = registry.openOrNavigate("conv_1", "file:///home/user/.ssh/id_rsa", undefined, {
      agent: true,
    });
    assert.equal(r.ok, false);
    assert.match(r.error, /navigation blocked/);
    assert.equal(loaded.length, 0, "loadURL must not run on a rejected agent nav");
  });

  it("rejects agent navigation to the cloud-metadata IP (169.254.169.254)", () => {
    const { registry, loaded } = makeLoadTrackingRegistry();
    const r = registry.openOrNavigate("conv_1", "http://169.254.169.254/latest/meta-data/", undefined, {
      agent: true,
    });
    assert.equal(r.ok, false);
    assert.equal(loaded.length, 0);
  });

  it("rejects agent navigation to localhost", () => {
    const { registry, loaded } = makeLoadTrackingRegistry();
    const r = registry.openOrNavigate("conv_1", "http://localhost:6767/health", undefined, {
      agent: true,
    });
    assert.equal(r.ok, false);
    assert.equal(loaded.length, 0);
  });

  it("allows agent navigation to a normal public https URL (loadURL runs)", () => {
    const { registry, loaded } = makeLoadTrackingRegistry();
    const r = registry.openOrNavigate("conv_1", "https://example.com/", undefined, {
      agent: true,
    });
    assert.equal(r.ok, true);
    assert.deepEqual(loaded, ["https://example.com/"]);
  });

  it("does NOT gate the user-typed URL-bar path (agent flag absent) — file:// still loads", () => {
    // The URL bar is user-initiated; the strict allowlist is scoped to the
    // agent path only. A user who types a local address keeps that ability.
    const { registry, loaded } = makeLoadTrackingRegistry();
    const r = registry.openOrNavigate("conv_1", "file:///tmp/report.html", undefined, {
      force: true,
    });
    assert.equal(r.ok, true);
    assert.deepEqual(loaded, ["file:///tmp/report.html"]);
  });
});
