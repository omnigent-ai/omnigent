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
  const visibility = []; // booleans passed to any view's setVisible, in order
  const makeStubView = () => ({
    setBounds() {},
    setVisible(v) {
      visibility.push(v);
    },
    webContents: {
      loadURL() {},
      close() {},
      removeListener() {},
      on() {},
      setWindowOpenHandler() {},
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
  return { registry, sent, attached, detached, visibility };
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
    // (the pane's onBrowserViewCreated listener)
    ctx.sent.length = 0;
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    const sawCreate = ctx.sent.find((s) => s.channel === "browser-view-created");
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
        on() {},
        setWindowOpenHandler() {},
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
    const r = registry.openOrNavigate(
      "conv_1",
      "http://169.254.169.254/latest/meta-data/",
      undefined,
      {
        agent: true,
      },
    );
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

// A stub view that captures the webContents event handlers (will-navigate /
// will-redirect / will-frame-navigate) and the window-open handler, so tests
// can fire a redirect and assert whether it was cancelled (preventDefault).
function makeEventCapturingRegistry() {
  const sent = []; // { channel, payload }
  const loaded = []; // loadURL targets on the created view
  const clipboardWrites = []; // copyTextToClipboard payloads
  const externalOpens = []; // openUrlExternal payloads
  const menus = []; // showContextMenu item lists
  let copyCalls = 0; // webContents.copy() invocations
  let handlers; // { [event]: fn } for the single created view
  let windowOpenHandler;
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: () => {
      handlers = {};
      return {
        setBounds() {},
        webContents: {
          loadURL(url) {
            loaded.push(url);
          },
          close() {},
          copy() {
            copyCalls += 1;
          },
          removeListener() {},
          on(event, fn) {
            handlers[event] = fn;
          },
          setWindowOpenHandler(fn) {
            windowOpenHandler = fn;
          },
        },
      };
    },
    createBoundsController: createBrowserViewBoundsController,
    attachToHost() {},
    detachFromHost() {},
    sendToRenderer: (channel, payload) => sent.push({ channel, payload }),
    getHostZoomFactor: () => 1,
    openUrlExternal: (url) => externalOpens.push(url),
    copyTextToClipboard: (text) => clipboardWrites.push(text),
    showContextMenu: (items) => menus.push(items),
  });
  return {
    registry,
    sent,
    loaded,
    clipboardWrites,
    externalOpens,
    menus,
    copyCalls: () => copyCalls,
    fire: (event, targetUrl) => {
      const ev = {
        url: targetUrl,
        prevented: false,
        preventDefault() {
          this.prevented = true;
        },
      };
      handlers[event](ev, targetUrl);
      return ev;
    },
    fireContextMenu: (params) => handlers["context-menu"]({}, params),
    windowOpen: (url) => windowOpenHandler({ url }),
  };
}

describe("browserViewRegistry — redirect/nav guard (SSRF: allowlist on every hop)", () => {
  it("blocks an agent-locked will-redirect to the cloud-metadata IP", () => {
    const { registry, sent, fire } = makeEventCapturingRegistry();
    // Agent navigates to an allowed host (locks the view to agent policy).
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    // The server 302s to the metadata endpoint — the guard must cancel it.
    const ev = fire("will-redirect", "http://169.254.169.254/latest/meta-data/");
    assert.equal(ev.prevented, true, "will-redirect to metadata must be preventDefault'd");
    const blocked = sent.filter((s) => s.channel === "browser-nav-blocked");
    assert.equal(blocked.length, 1, "a browser-nav-blocked signal is emitted");
    assert.match(blocked[0].payload.error, /navigation blocked/);
  });

  it("blocks an agent-locked will-navigate to a loopback host", () => {
    const { registry, fire } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    const ev = fire("will-navigate", "http://127.0.0.1:8080/");
    assert.equal(ev.prevented, true);
  });

  it("blocks an agent-locked will-navigate to an RFC-1918 private host", () => {
    const { registry, fire } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    const ev = fire("will-navigate", "http://10.1.2.3/admin");
    assert.equal(ev.prevented, true);
  });

  it("allows an agent-locked redirect between normal public https hosts", () => {
    const { registry, sent, fire } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    const ev = fire("will-redirect", "https://cdn.example.net/asset");
    assert.equal(ev.prevented, false, "a normal https→https redirect must not be blocked");
    assert.equal(sent.filter((s) => s.channel === "browser-nav-blocked").length, 0);
  });

  it("does NOT block redirects on a user-typed (non-agent) navigation", () => {
    const { registry, fire } = makeEventCapturingRegistry();
    // User types a URL (no agent flag) → the view is NOT agent-locked, so its
    // redirects — even to an internal host (auth-redirect chains) — are allowed.
    registry.openOrNavigate("conv_1", "https://intranet.example.com/", undefined, {
      force: true,
    });
    const ev = fire("will-redirect", "http://10.0.0.5/sso/callback");
    assert.equal(ev.prevented, false, "user-driven nav stays permissive");
  });

  it("re-locks correctly: a user nav after an agent nav flips enforcement off", () => {
    const { registry, fire } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    // Then the user types a new address on the same view.
    registry.openOrNavigate("conv_1", "https://intranet.example.com/", undefined, {
      force: true,
    });
    const ev = fire("will-redirect", "http://192.168.1.10/");
    assert.equal(ev.prevented, false, "the later user nav unlocked the view");
  });
});

describe("browserViewRegistry — child window.open is denied (S3)", () => {
  it("never opens a window: every window.open target is answered with deny", () => {
    const { registry, windowOpen } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    assert.deepEqual(windowOpen("https://evil.example.com/popup"), { action: "deny" });
    assert.deepEqual(windowOpen("https://example.com/ok"), { action: "deny" });
  });

  it("navigates the SAME view in place for an http(s) link target", () => {
    const { registry, windowOpen, loaded } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    loaded.length = 0; // ignore the initial openOrNavigate load
    assert.deepEqual(windowOpen("https://example.com/linked-page"), { action: "deny" });
    assert.deepEqual(loaded, ["https://example.com/linked-page"]);
  });

  it("blocks an agent-locked window.open to an internal host (no in-place nav)", () => {
    const { registry, windowOpen, loaded, sent } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    loaded.length = 0;
    assert.deepEqual(windowOpen("http://169.254.169.254/latest/meta-data/"), {
      action: "deny",
    });
    assert.deepEqual(loaded, [], "no in-place navigation to a blocked host");
    const blocked = sent.filter((s) => s.channel === "browser-nav-blocked");
    assert.equal(blocked.length, 1, "the SSRF block surfaces to the renderer");
  });

  it("still denies non-web schemes with no in-place nav", () => {
    const { registry, windowOpen, loaded } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    loaded.length = 0;
    assert.deepEqual(windowOpen("file:///etc/passwd"), { action: "deny" });
    assert.deepEqual(windowOpen("javascript:alert(1)"), { action: "deny" });
    assert.deepEqual(loaded, []);
  });
});

describe("browserViewRegistry — pane context menu", () => {
  it("offers Open Link in Browser + Copy Link Address over a link", () => {
    const { registry, fireContextMenu, menus, externalOpens, clipboardWrites } =
      makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    fireContextMenu({ linkURL: "https://example.com/page", selectionText: "" });
    assert.equal(menus.length, 1);
    assert.deepEqual(
      menus[0].map((item) => item.label),
      ["Open Link in Browser", "Copy Link Address"],
    );
    menus[0][0].click();
    menus[0][1].click();
    assert.deepEqual(externalOpens, ["https://example.com/page"]);
    assert.deepEqual(clipboardWrites, ["https://example.com/page"]);
  });

  it("omits Open Link in Browser for a non-web link but still offers Copy Link Address", () => {
    const { registry, fireContextMenu, menus } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    fireContextMenu({ linkURL: "javascript:alert(1)", selectionText: "" });
    assert.deepEqual(
      menus[0].map((item) => item.label),
      ["Copy Link Address"],
    );
  });

  it("offers Copy over selected text and routes it to webContents.copy()", () => {
    const { registry, fireContextMenu, menus, copyCalls } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    fireContextMenu({ linkURL: "", selectionText: "hello" });
    assert.deepEqual(
      menus[0].map((item) => item.label),
      ["Copy"],
    );
    menus[0][0].click();
    assert.equal(copyCalls(), 1);
  });

  it("shows nothing over dead space", () => {
    const { registry, fireContextMenu, menus } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    fireContextMenu({ linkURL: "", selectionText: "   " });
    assert.equal(menus.length, 0);
  });
});

describe("browserViewRegistry — overlay suppression (#3980)", () => {
  let ctx;
  beforeEach(() => {
    ctx = makeRegistry();
  });

  it("toggles setVisible on the active view when suppressed/unsuppressed", () => {
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    ctx.registry.setActive("conv_1");
    ctx.visibility.length = 0; // ignore any visibility calls during attach
    ctx.registry.setSuppressed(true);
    assert.deepEqual(ctx.visibility, [false], "suppress hides the active view");
    ctx.registry.setSuppressed(false);
    assert.deepEqual(ctx.visibility, [false, true], "unsuppress shows it again");
    assert.equal(ctx.registry.isSuppressed(), false);
  });

  it("keeps a view hidden when it becomes active while suppression is on (sticky)", () => {
    // Suppress with nothing active, then attach a view — it must attach hidden.
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    ctx.registry.setSuppressed(true);
    ctx.visibility.length = 0;
    ctx.registry.setActive("conv_1");
    assert.ok(
      ctx.visibility.includes(false) && !ctx.visibility.includes(true),
      "a view attaching under active suppression stays hidden",
    );
  });

  it("is a no-op (no throw) when no view is active", () => {
    assert.deepEqual(ctx.registry.setSuppressed(true), { ok: true });
    assert.equal(ctx.registry.isSuppressed(), true);
    assert.equal(ctx.visibility.length, 0, "nothing to toggle with no active view");
  });
});
