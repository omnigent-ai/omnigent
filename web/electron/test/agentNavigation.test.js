"use strict";

// The registry's agent viewport sizing and history navigation
// (src/agentNavigation.js), driven through the registry.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { createBrowserViewRegistry } = require("../src/browserViewRegistry");
const { createBrowserViewBoundsController } = require("../src/browserViewBounds");

describe("browserViewRegistry — agent history navigation", () => {
  /** A registry whose one view has a scripted history and load events. */
  function makeHistoryRegistry({
    history,
    active,
    allowsLocalhost,
    onReload,
    resolveHost = async () => ["93.184.216.34"],
    vetoed = false,
    stays = false,
    historyLoadTimeoutMs = 300,
  } = {}) {
    const listeners = new Map();
    const emit = (event, ...args) => {
      for (const fn of [...(listeners.get(event) ?? [])]) fn({}, ...args);
    };
    const calls = [];
    const scripts = [];
    let index = active;
    let loading = false;
    const setLoading = (value) => {
      loading = value;
    };
    const webContents = {
      loadURL() {},
      close() {},
      setWindowOpenHandler() {},
      on(event, fn) {
        if (!listeners.has(event)) listeners.set(event, new Set());
        listeners.get(event).add(fn);
      },
      once(event, fn) {
        this.on(event, fn);
      },
      removeListener(event, fn) {
        listeners.get(event)?.delete(fn);
      },
      getURL: () => history[index],
      getTitle: () => `Page ${index}`,
      isLoading: () => loading,
      stop() {
        calls.push("stop");
        loading = false;
      },
      executeJavaScript(script) {
        scripts.push(script);
        return Promise.resolve();
      },
      reload() {
        calls.push("reload");
        // A reload commits the page's document again.
        if (onReload) onReload({ emit, setLoading });
        else emit("did-navigate", history[index]);
      },
      navigationHistory: {
        length: () => history.length,
        getActiveIndex: () => index,
        getEntryAtIndex: (i) => ({ url: history[i] }),
        goBack() {
          calls.push("goBack");
          if (vetoed) {
            // The page's beforeunload objects; Electron keeps it.
            emit("will-prevent-unload");
            return;
          }
          if (stays) return;
          index -= 1;
          loading = true;
          setTimeout(() => {
            loading = false;
            emit("did-stop-loading");
          }, 5);
        },
        goForward() {
          calls.push("goForward");
          index += 1;
          loading = true;
          setTimeout(() => {
            loading = false;
            emit("did-stop-loading");
          }, 5);
        },
      },
    };
    const registry = createBrowserViewRegistry({
      WebContentsViewCtor: () => ({ setBounds() {}, webContents }),
      createBoundsController: createBrowserViewBoundsController,
      attachToHost() {},
      detachFromHost() {},
      sendToRenderer() {},
      allowsLocalhost,
      resolveHost,
      historyLoadTimeoutMs,
    });
    registry.getOrCreate("conv_1");
    const moveTo = (url) => {
      history.splice(index + 1, history.length, url);
      index = history.length - 1;
    };
    return { registry, calls, scripts, entry: registry.get("conv_1"), moveTo, emit };
  }

  it("reports a load that never finishes, or fails, instead of success", async () => {
    const hanging = makeHistoryRegistry({
      history: ["https://example.com/"],
      active: 0,
      onReload: ({ setLoading }) => setLoading(true),
    });
    const timedOut = await hanging.registry.agentNavigateHistory("conv_1", "reload");
    assert.equal(timedOut.ok, false);
    assert.match(timedOut.error, /didn't finish loading within \d+ seconds/);

    // A subframe's in-page navigation during the main-frame load isn't completion.
    const iframeHash = makeHistoryRegistry({
      history: ["https://example.com/"],
      active: 0,
      onReload: ({ emit, setLoading }) => {
        setLoading(true);
        setTimeout(() => emit("did-navigate-in-page", "https://ads.example/#x", false), 5);
      },
    });
    assert.equal((await iframeHash.registry.agentNavigateHistory("conv_1", "reload")).ok, false);

    const failed = makeHistoryRegistry({
      history: ["https://example.com/"],
      active: 0,
      onReload: ({ emit, setLoading }) => {
        setLoading(true);
        setTimeout(() => {
          emit("did-fail-load", -3, "ERR_ABORTED", "https://example.com/", true);
          emit("did-fail-load", -105, "ERR_NAME_NOT_RESOLVED", "https://example.com/", true);
        }, 5);
      },
    });
    assert.deepEqual(await failed.registry.agentNavigateHistory("conv_1", "reload"), {
      ok: false,
      error: "the page failed to load: ERR_NAME_NOT_RESOLVED",
    });

    const mainFrameHash = makeHistoryRegistry({
      history: ["https://example.com/"],
      active: 0,
      onReload: ({ emit, setLoading }) => {
        setLoading(true);
        setTimeout(() => emit("did-navigate-in-page", "https://example.com/#top", true), 5);
      },
    });
    // A same-document change in the old document isn't a reload.
    const result = await mainFrameHash.registry.agentNavigateHistory("conv_1", "reload");
    assert.equal(result.ok, false);
    assert.match(result.error, /didn't finish loading/);
  });

  it("stops a load that outlasts its timeout, so it can't replace the page later", async () => {
    const hanging = makeHistoryRegistry({
      history: ["https://example.com/"],
      active: 0,
      onReload: ({ setLoading }) => setLoading(true),
    });
    const result = await hanging.registry.agentNavigateHistory("conv_1", "reload");
    assert.equal(result.ok, false);
    assert.ok(hanging.calls.includes("stop"));
  });

  it("drops the last snapshot's refs after a history step", async () => {
    const back = makeHistoryRegistry({
      history: ["https://example.com/a", "https://example.com/b"],
      active: 1,
    });
    const result = await back.registry.agentNavigateHistory("conv_1", "go_back");
    assert.equal(result.ok, true);
    assert.equal(back.scripts.length, 1);
    assert.match(back.scripts[0], /delete window\.__omni_refs__/);
    assert.match(back.scripts[0], /delete window\.__omni_snapshot_id__/);
  });

  for (const newer of ["navigation", "in-page move"]) {
    it(`cancels a history step whose URL check outlived a newer ${newer}`, async () => {
      let release;
      const { registry, calls, moveTo } = makeHistoryRegistry({
        history: ["https://example.com/a", "https://example.com/b"],
        active: 1,
        resolveHost: () =>
          new Promise((resolve) => {
            release = () => resolve(["93.184.216.34"]);
          }),
      });
      const back = registry.agentNavigateHistory("conv_1", "go_back");
      await new Promise((resolve) => {
        setImmediate(resolve);
      });
      assert.equal(typeof release, "function", "the URL check is pending");
      // A newer navigation lands on a new page while the check is pending.
      if (newer === "navigation") registry.beginAgentNavigation("conv_1");
      moveTo("https://example.com/new");
      release();
      assert.deepEqual(await back, { ok: false, error: "navigation superseded or closed" });
      assert.deepEqual(calls, []);
    });
  }

  it("never navigates when the URL check answers after the step's deadline", async () => {
    let release;
    const { registry, calls } = makeHistoryRegistry({
      history: ["https://example.com/a", "https://example.com/b"],
      active: 1,
      resolveHost: () =>
        new Promise((resolve) => {
          release = () => resolve(["93.184.216.34"]);
        }),
    });
    const back = await registry.agentNavigateHistory("conv_1", "go_back", {
      deadlineAt: Date.now() + 30,
    });
    assert.equal(back.ok, false);
    assert.match(back.error, /didn't respond in time, so the tab wasn't moved/);
    release();
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    assert.deepEqual(calls, [], "no history step after the deadline");
  });

  it("cancels a pending agent navigation when a history step starts", async () => {
    const { registry } = makeHistoryRegistry({
      history: ["https://example.com/a", "https://example.com/b"],
      active: 1,
    });
    const older = registry.beginAgentNavigation("conv_1");
    const back = await registry.agentNavigateHistory("conv_1", "go_back");
    assert.equal(back.ok, true);
    assert.equal(older(), false);
  });

  it("reports a redirect the agent policy blocked instead of success", async () => {
    const { registry } = makeHistoryRegistry({
      history: ["https://example.com/"],
      active: 0,
      onReload: ({ emit, setLoading }) => {
        setLoading(true);
        setTimeout(() => {
          emit("will-redirect", "http://169.254.169.254/latest/meta-data");
          emit("did-fail-load", -3, "ERR_ABORTED", "https://example.com/", true);
          setLoading(false);
          emit("did-stop-loading");
        }, 5);
      },
    });
    const reload = await registry.agentNavigateHistory("conv_1", "reload");
    assert.equal(reload.ok, false);
    assert.match(reload.error, /redirected to a blocked address/);
  });

  it("reports a step the page refused, or that never happened, instead of success", async () => {
    const history = ["https://example.com/a", "https://example.com/b"];
    const vetoed = makeHistoryRegistry({ history, active: 1, vetoed: true });
    const back = await vetoed.registry.agentNavigateHistory("conv_1", "go_back");
    assert.equal(back.ok, false);
    assert.match(back.error, /the page asked to stay .*nothing changed/);
    const reload = makeHistoryRegistry({
      history,
      active: 1,
      onReload: ({ emit }) => emit("will-prevent-unload"),
    });
    assert.match(
      (await reload.registry.agentNavigateHistory("conv_1", "reload")).error,
      /the page asked to stay/,
    );
    // A reload that never commits hits the load limit.
    const stuck = makeHistoryRegistry({ history, active: 1, stays: true, onReload: () => {} });
    assert.match(
      (await stuck.registry.agentNavigateHistory("conv_1", "reload")).error,
      /didn't finish loading/,
    );
    // Idle and still on the same entry past the 1 s stay check: it didn't happen.
    const unmoved = makeHistoryRegistry({
      history,
      active: 1,
      stays: true,
      historyLoadTimeoutMs: 1500,
    });
    assert.match(
      (await unmoved.registry.agentNavigateHistory("conv_1", "go_back")).error,
      /stayed where it was/,
    );
  });

  it("goes back and forward, waiting for the load, and reports the page", async () => {
    const { registry, calls, entry } = makeHistoryRegistry({
      history: ["https://example.com/a", "https://example.com/b"],
      active: 1,
    });
    assert.deepEqual(await registry.agentNavigateHistory("conv_1", "go_back"), {
      ok: true,
      url: "https://example.com/a",
      title: "Page 0",
    });
    assert.equal(
      entry.navigationPolicyEnforced,
      true,
      "redirects of the history load stay guarded",
    );
    assert.deepEqual(await registry.agentNavigateHistory("conv_1", "go_forward"), {
      ok: true,
      url: "https://example.com/b",
      title: "Page 1",
    });
    assert.equal((await registry.agentNavigateHistory("conv_1", "reload")).ok, true);
    assert.deepEqual(calls, ["goBack", "goForward", "reload"]);
  });

  it("refuses at the ends of history and for unknown views or actions", async () => {
    const { registry, calls } = makeHistoryRegistry({
      history: ["https://example.com/"],
      active: 0,
    });
    assert.deepEqual(await registry.agentNavigateHistory("conv_1", "go_back"), {
      ok: false,
      error: "no previous page in this tab",
    });
    assert.deepEqual(await registry.agentNavigateHistory("conv_1", "go_forward"), {
      ok: false,
      error: "no next page in this tab",
    });
    assert.equal((await registry.agentNavigateHistory("nope", "reload")).error, "No browser view");
    assert.equal((await registry.agentNavigateHistory("conv_1", "stop")).ok, false);
    assert.deepEqual(calls, []);
  });

  it("applies the agent URL policy to the destination a user browsed to", async () => {
    const { registry, calls } = makeHistoryRegistry({
      history: ["http://192.168.1.1/admin", "https://example.com/"],
      active: 1,
    });
    const back = await registry.agentNavigateHistory("conv_1", "go_back");
    assert.equal(back.ok, false);
    assert.match(back.error, /internal host/);
    const onInternalPage = makeHistoryRegistry({ history: ["http://10.0.0.5/"], active: 0 });
    assert.equal(
      (await onInternalPage.registry.agentNavigateHistory("conv_1", "reload")).ok,
      false,
    );
    assert.deepEqual([...calls, ...onInternalPage.calls], []);
  });

  it("lets an eligible loopback context go back to its loopback page", async () => {
    const context = { sourceHostId: "preview-host" };
    const { registry, entry } = makeHistoryRegistry({
      history: ["http://localhost:5173/", "https://example.com/"],
      active: 1,
      allowsLocalhost: (ctx) => ctx === context,
    });
    entry.agentContext = { agentRequested: false };
    assert.equal(
      (await registry.agentNavigateHistory("conv_1", "go_back", { agentContext: context })).ok,
      true,
    );
    // Its redirects and requests are checked against that context too.
    assert.equal(entry.agentContext, context);
  });

  it("checks a history step against the issuing session, not the tab's address-bar context", async () => {
    // The address bar's context may open localhost; this session's can't.
    const addressBar = { agentRequested: false };
    const session = { agentRequested: true, sourceHostId: "remote-host" };
    const { registry, entry, calls } = makeHistoryRegistry({
      history: ["http://localhost:5173/", "https://example.com/"],
      active: 1,
      allowsLocalhost: (ctx) => ctx === addressBar,
    });
    entry.agentContext = addressBar;
    for (const opts of [{ agentContext: session }, {}]) {
      // oxlint-disable-next-line no-await-in-loop -- One context at a time.
      const back = await registry.agentNavigateHistory("conv_1", "go_back", opts);
      assert.equal(back.ok, false);
      assert.match(back.error, /localhost|loopback|not allowed|blocked/i);
    }
    assert.deepEqual(calls, [], "the tab never moved");
    assert.equal(entry.agentContext, addressBar);
  });
});

describe("browserViewRegistry — agent viewport", () => {
  function makeSizedRegistry() {
    const sizes = [];
    let bounds = { x: 0, y: 0, width: 0, height: 0 };
    const registry = createBrowserViewRegistry({
      WebContentsViewCtor: () => ({
        getBounds: () => bounds,
        setBounds(next) {
          bounds = next;
          sizes.push(next);
        },
        webContents: {
          loadURL() {},
          close() {},
          on() {},
          removeListener() {},
          setWindowOpenHandler() {},
        },
      }),
      createBoundsController: createBrowserViewBoundsController,
      attachToHost() {},
      detachFromHost() {},
      sendToRenderer() {},
    });
    return { registry, sizes };
  }

  it("lays out a view the user has not shown so agent input has a viewport", () => {
    const { registry, sizes } = makeSizedRegistry();
    registry.openOrNavigate("conv_1", "https://example.com");
    registry.ensureAgentViewport("conv_1");
    assert.deepEqual(sizes, [{ x: 0, y: 0, width: 1280, height: 800 }]);
    registry.ensureAgentViewport("conv_1");
    assert.equal(sizes.length, 1, "an already-sized view keeps its bounds");
    registry.ensureAgentViewport("missing");
  });

  it("leaves the active view to the pane's own bounds", () => {
    const { registry, sizes } = makeSizedRegistry();
    registry.openOrNavigate("conv_1", "https://example.com");
    registry.setActive("conv_1");
    sizes.length = 0;
    registry.ensureAgentViewport("conv_1");
    assert.deepEqual(sizes, []);
  });
});
