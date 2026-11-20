// The agent tools' tab records in main, and the one native page
// per tab they give a session across shell windows (browserTabs.js).
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { registerBrowserIpc } = require("../src/browserIpc");
const { createBrowserTabs, registerBrowserTabsIpc } = require("../src/browserTabs");

const ORIGIN = "https://omnigent.example";
const TAB = "browser-tab:conv:t1";
const record = (tabs, extra = {}) => ({
  tabs,
  selected: tabs[0] ?? null,
  agentTab: null,
  ...extra,
});

/** A page in a window: what the handlers read and do with it. */
function page(url, log, name) {
  const listeners = new Map();
  return {
    view: {
      webContents: {
        getURL: () => url,
        getTitle: () => `title of ${url}`,
        on: (event, fn) => listeners.set(event, fn),
        executeJavaScript: async () => {
          log.push(`${name} ran a script on ${url}`);
          return "{}";
        },
        capturePage: async () => ({ toPNG: () => Buffer.from(url) }),
        navigationHistory: { canGoBack: () => false, canGoForward: () => false },
      },
    },
    listeners,
  };
}

/** A shell window with a registry holding `pages` ({ viewId: url }). Like
 *  the real registry, a close cancels the view's navigation in flight;
 *  `check` is its address check. */
function shellWindow(
  name,
  pages,
  log,
  origin = ORIGIN,
  { check = async () => ({ ok: true }), serverUrl } = {},
) {
  const entries = new Map(Object.entries(pages).map(([id, url]) => [id, page(url, log, name)]));
  const navigations = new Map();
  const registry = {
    get: (id) => entries.get(id),
    forEach: (fn) => entries.forEach(fn),
    beginAgentNavigation: (id) => {
      const generation = Symbol(id);
      navigations.set(id, generation);
      return () => navigations.get(id) === generation;
    },
    checkAgentNavigation: check,
    openOrNavigate: (id, url) => {
      log.push(`${name} opened ${id} at ${url}`);
      const created = !entries.has(id);
      if (created) entries.set(id, page(url, log, name));
      return { ok: true, created, entry: entries.get(id) };
    },
    close: (id) => {
      navigations.delete(id);
      log.push(`${name} closed ${id}`);
      return { ok: true, removed: entries.delete(id) };
    },
  };
  const sent = [];
  const win = { name, destroyed: false, isDestroyed: () => win.destroyed, sent };
  win.webContents = { send: (channel, payload) => sent.push({ channel, payload }) };
  return { win, state: { origin, serverUrl, browserRegistry: registry }, entries };
}

/** Main with `shells`, as main.js wires it: the tab bridge and browserIpc. */
function mainWith(shells, showsServer) {
  const windows = new Map(shells.map((s) => [s.win, s.state]));
  const handlers = new Map();
  const ipcMain = { handle: (channel, fn) => handlers.set(channel, fn) };
  const windowForEvent = (event) => event.sender.win;
  const tabs = registerBrowserTabsIpc({
    ipcMain,
    isPinnedOriginSender: () => true,
    windows: () => windows,
    windowForEvent,
    ...(showsServer ? { showsServer } : {}),
    // As main.js: a window's server is its full URL (a mount), else its origin.
    serverOf: (state) => state.serverUrl ?? state.origin,
  });
  registerBrowserIpc({
    ipcMain,
    isPinnedOriginSender: () => true,
    getRegistryForEvent: (event) => windows.get(windowForEvent(event))?.browserRegistry ?? null,
    routeViewForEvent: tabs.route,
    whileOpeningView: tabs.whileOpening,
    refuseCloseForEvent: tabs.refuseClose,
  });
  const from = (shell) => ({ sender: { win: shell.win, send: shell.win.webContents.send } });
  return {
    call: (shell, channel, args) => handlers.get(channel)(from(shell), args),
    tabs: (shell, args) => handlers.get("omnigent:browser-tabs")(from(shell), args),
  };
}

describe("tab records", () => {
  const store = () => {
    const sent = [];
    const a = { name: "a" };
    const b = { name: "b" };
    const elsewhere = { name: "other server" };
    const windows = new Map([
      [a, { origin: ORIGIN }],
      [b, { origin: ORIGIN }],
      [elsewhere, { origin: "https://other.example" }],
    ]);
    const tabs = createBrowserTabs({
      windows: () => windows.entries(),
      isDestroyed: () => false,
      send: (win, channel, payload) => sent.push({ to: win.name, channel, payload }),
    });
    return { tabs, a, b, elsewhere, sent };
  };

  it("changes a record only from the version the change was made on", () => {
    const { tabs, a, b } = store();
    const first = tabs.handle(a, {
      op: "update",
      conversationId: "conv",
      expectedVersion: 0,
      next: record(["x"]),
    });
    assert.equal(first.record.version, 1);
    // Window b changes it from what it read before a's change: refused, with the record to redo it on.
    const stale = tabs.handle(b, {
      op: "update",
      conversationId: "conv",
      expectedVersion: 0,
      next: record(["y"]),
    });
    assert.deepEqual([stale.ok, stale.conflict, stale.record.tabs], [false, true, ["x"]]);
    const redone = tabs.handle(b, {
      op: "update",
      conversationId: "conv",
      expectedVersion: 1,
      next: record(["x", "y"]),
    });
    assert.deepEqual(tabs.handle(a, { op: "get", conversationId: "conv" }).record, redone.record);
    assert.deepEqual(redone.record.tabs, ["x", "y"]);
  });

  it("tells the server's windows of each change, and keeps servers apart", () => {
    const { tabs, a, elsewhere, sent } = store();
    tabs.handle(a, {
      op: "update",
      conversationId: "conv",
      expectedVersion: 0,
      next: record(["x"]),
    });
    assert.deepEqual(
      sent.map((m) => `${m.to} ${m.payload.conversationId} v${m.payload.record.version}`),
      ["a conv v1", "b conv v1"],
    );
    assert.equal(tabs.handle(elsewhere, { op: "get", conversationId: "conv" }).record, null);
  });

  it("refuses a change past its deadline, and a malformed record", () => {
    const { tabs, a } = store();
    const update = (next, extra = {}) =>
      tabs.handle(a, { op: "update", conversationId: "conv", expectedVersion: 0, next, ...extra });
    assert.deepEqual(update(record(["x"]), { deadlineAt: Date.now() - 1 }), {
      ok: false,
      expired: true,
    });
    for (const bad of [
      record(["x", "x"]),
      record(["x"], { selected: "y" }),
      record(Array.from({ length: 101 }, (_, i) => `t${i}`)),
      record(["x"], { navs: { x: -1 } }),
      { tabs: "x" },
    ]) {
      assert.equal(update(bad).ok, false, JSON.stringify(bad));
    }
    assert.equal(tabs.handle(a, { op: "get", conversationId: "conv" }).record, null);
  });

  it("drops a server's records on reset, and says so to its windows", () => {
    const { tabs, a, elsewhere, sent } = store();
    tabs.handle(a, {
      op: "update",
      conversationId: "conv",
      expectedVersion: 0,
      next: record(["x"]),
    });
    tabs.handle(elsewhere, {
      op: "update",
      conversationId: "conv",
      expectedVersion: 0,
      next: record(["z"]),
    });
    sent.length = 0;
    tabs.handle(a, { op: "reset" });
    assert.equal(tabs.handle(a, { op: "get", conversationId: "conv" }).record, null);
    assert.deepEqual(tabs.handle(elsewhere, { op: "get", conversationId: "conv" }).record.tabs, [
      "z",
    ]);
    assert.deepEqual(
      sent.map((m) => `${m.to} ${JSON.stringify(m.payload)}`),
      ['a {"reset":true}', 'b {"reset":true}'],
    );
  });
});

describe("one page per tab", () => {
  const withRecord = (main, shell) =>
    main.tabs(shell, {
      op: "update",
      conversationId: "conv",
      expectedVersion: 0,
      next: record(["t1"]),
    });

  it("runs every agent call in the window with the page, whichever window claimed it", async () => {
    const log = [];
    const a = shellWindow("a", { [TAB]: "https://a.example/" }, log);
    const b = shellWindow("b", {}, log);
    const main = mainWith([a, b]);
    withRecord(main, b);
    // Claims alternate between the windows; each call reaches a's page.
    for (const claimant of [b, a, b]) {
      // oxlint-disable-next-line no-await-in-loop -- One call at a time, as claims come.
      await main.call(claimant, "omnigent:browser-perform-action", {
        conversationId: TAB,
        action: "snapshot",
      });
    }
    const shot = await main.call(b, "omnigent:browser-screenshot", { conversationId: TAB });
    assert.equal(
      Buffer.from(shot.dataUrl.split(",")[1], "base64").toString(),
      "https://a.example/",
    );
    const listed = await main.call(b, "omnigent:browser-has-view", { conversationId: TAB });
    assert.deepEqual(listed, {
      exists: false,
      elsewhere: true,
      url: "https://a.example/",
      title: "title of https://a.example/",
    });
    const nav = await main.call(b, "omnigent:browser-open-or-navigate", {
      conversationId: TAB,
      url: "https://next.example/",
      opts: { agent: true },
    });
    assert.equal(nav.ok, true);
    await main.call(b, "omnigent:browser-close", { conversationId: TAB });
    assert.deepEqual(log, [
      "a ran a script on https://a.example/",
      "a ran a script on https://a.example/",
      "a ran a script on https://a.example/",
      `a opened ${TAB} at https://next.example/`,
      `a closed ${TAB}`,
    ]);
    assert.equal(b.entries.size, 0, "no second copy in the claiming window");
  });

  it("tells a user opening the tab in a second window where its page is", async () => {
    const log = [];
    const a = shellWindow("a", { [TAB]: "https://a.example/" }, log);
    const b = shellWindow("b", {}, log);
    const main = mainWith([a, b]);
    withRecord(main, a);
    const typed = await main.call(b, "omnigent:browser-open-or-navigate", {
      conversationId: TAB,
      url: "https://typed.example/",
      opts: { force: true },
    });
    assert.deepEqual([typed.ok, typed.elsewhere], [false, true]);
    assert.match(typed.error, /open in another Omnigent window/);
    assert.deepEqual(log, []);
  });

  it("refuses an agent call while copies from before the record are in two windows", async () => {
    const log = [];
    const a = shellWindow("a", { [TAB]: "https://a.example/" }, log);
    const b = shellWindow("b", { [TAB]: "https://b.example/" }, log);
    const main = mainWith([a, b]);
    withRecord(main, a);
    for (const claimant of [a, b]) {
      // oxlint-disable-next-line no-await-in-loop -- Each window in turn.
      const r = await main.call(claimant, "omnigent:browser-perform-action", {
        conversationId: TAB,
        action: "click",
        params: { selector: "#buy" },
      });
      assert.equal(r.ok, false);
      assert.match(r.error, /more than one Omnigent window/);
    }
    assert.deepEqual(log, [], "neither page was touched");
    // Closing the tab closes every copy, so none lives on unseen.
    const closed = await main.call(b, "omnigent:browser-close", { conversationId: TAB });
    assert.equal(closed.ok, true);
    assert.deepEqual(log.toSorted(), [`a closed ${TAB}`, `b closed ${TAB}`]);
  });

  /** Window a, whose address checks wait until `pass()` (or `fail()`). */
  function slowChecks(log) {
    const waiting = [];
    const a = shellWindow("a", {}, log, ORIGIN, {
      check: () =>
        new Promise((resolve) => {
          waiting.push(resolve);
        }),
    });
    const settle = (verdict) => waiting.splice(0).forEach((resolve) => resolve(verdict));
    return { a, pass: () => settle({ ok: true }), fail: () => settle({ ok: false, error: "no" }) };
  }
  const OPEN = "omnigent:browser-open-or-navigate";
  const tick = () =>
    new Promise((resolve) => {
      setImmediate(resolve);
    });

  it("refuses a second window's copy while the first is still checking the address", async () => {
    const log = [];
    const { a, pass } = slowChecks(log);
    const b = shellWindow("b", {}, log);
    const main = mainWith([a, b]);
    withRecord(main, a);
    const first = main.call(a, OPEN, { conversationId: TAB, url: "https://a.example/" });
    await tick();
    const second = await main.call(b, OPEN, { conversationId: TAB, url: "https://b.example/" });
    assert.deepEqual([second.ok, second.elsewhere], [false, true]);
    pass();
    assert.equal((await first).ok, true);
    assert.deepEqual(log, [`a opened ${TAB} at https://a.example/`]);
    assert.equal(b.entries.size, 0);
  });

  it("cancels an open still checking the address when another window closes the tab", async () => {
    const log = [];
    const { a, pass } = slowChecks(log);
    const b = shellWindow("b", {}, log);
    const main = mainWith([a, b]);
    withRecord(main, a);
    const opening = main.call(a, OPEN, { conversationId: TAB, url: "https://a.example/" });
    await tick();
    assert.equal((await main.call(b, "omnigent:browser-close", { conversationId: TAB })).ok, true);
    pass();
    assert.equal((await opening).ok, false);
    assert.deepEqual(log, [`a closed ${TAB}`], "no page was opened after the close");
    assert.equal(a.entries.size, 0);
  });

  it("lets other windows have the tab once a stalled open's time has run out", async () => {
    const log = [];
    const { a } = slowChecks(log);
    const b = shellWindow("b", {}, log);
    const main = mainWith([a, b]);
    withRecord(main, a);
    // a's lookup never answers.
    void main.call(a, OPEN, {
      conversationId: TAB,
      url: "https://a.example/",
      opts: { agent: true, deadlineAt: Date.now() + 30 },
    });
    await tick();
    const early = await main.call(b, OPEN, { conversationId: TAB, url: "https://b.example/" });
    assert.equal(early.elsewhere, true, "held while the open has time");
    await new Promise((resolve) => {
      setTimeout(resolve, 60);
    });
    const late = await main.call(b, OPEN, { conversationId: TAB, url: "https://b.example/" });
    assert.equal(late.ok, true);
    assert.deepEqual(log, [`b opened ${TAB} at https://b.example/`]);
  });

  it("holds the tab for a user's open until its lookup answers, however long", async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const log = [];
    const { a, pass } = slowChecks(log);
    const b = shellWindow("b", {}, log);
    const main = mainWith([a, b]);
    withRecord(main, a);
    const typed = main.call(a, OPEN, { conversationId: TAB, url: "https://a.example/" });
    await tick();
    t.mock.timers.tick(60_000);
    const second = await main.call(b, OPEN, { conversationId: TAB, url: "https://b.example/" });
    assert.equal(second.elsewhere, true, "still a's, a minute on");
    pass();
    assert.equal((await typed).ok, true);
    assert.deepEqual(log, [`a opened ${TAB} at https://a.example/`]);
  });

  it("lets other windows have the tab once a close cancelled its stalled open", async () => {
    const log = [];
    const { a } = slowChecks(log);
    const b = shellWindow("b", {}, log);
    const main = mainWith([a, b]);
    withRecord(main, a);
    void main.call(a, OPEN, { conversationId: TAB, url: "https://a.example/" });
    await tick();
    await main.call(b, "omnigent:browser-close", { conversationId: TAB });
    const reopened = await main.call(b, OPEN, { conversationId: TAB, url: "https://b.example/" });
    assert.equal(reopened.ok, true);
    assert.deepEqual(log, [`a closed ${TAB}`, `b opened ${TAB} at https://b.example/`]);
  });

  it("leaves the tab to other windows once an open is refused", async () => {
    const log = [];
    const { a, fail } = slowChecks(log);
    const b = shellWindow("b", {}, log);
    const main = mainWith([a, b]);
    withRecord(main, a);
    const refused = main.call(a, OPEN, { conversationId: TAB, url: "https://a.example/" });
    await tick();
    fail();
    assert.equal((await refused).ok, false);
    const opened = await main.call(b, OPEN, { conversationId: TAB, url: "https://b.example/" });
    assert.equal(opened.ok, true);
    assert.deepEqual(log, [`b opened ${TAB} at https://b.example/`]);
  });

  it("keeps a page navigated since an agent's close read the tabs", async () => {
    const log = [];
    const a = shellWindow("a", { [TAB]: "https://a.example/" }, log);
    const main = mainWith([a]);
    main.tabs(a, {
      op: "update",
      conversationId: "conv",
      expectedVersion: 0,
      next: record(["t1"], { navs: { t1: 2 } }),
    });
    const stale = await main.call(a, "omnigent:browser-close", {
      conversationId: TAB,
      expectedNavs: { tabId: "t1", count: 1 },
    });
    assert.deepEqual(stale, { ok: false, removed: false, superseded: true });
    assert.deepEqual(log, [], "the newer page is still there");
    const current = await main.call(a, "omnigent:browser-close", {
      conversationId: TAB,
      expectedNavs: { tabId: "t1", count: 2 },
    });
    assert.deepEqual([current.ok, log], [true, [`a closed ${TAB}`]]);
  });

  const T2 = "browser-tab:conv:t2";
  const T3 = "browser-tab:conv:t3";
  const OPEN_PAGE = "omnigent:browser-open-or-navigate";

  it("keeps a session's new tabs in the window with its other pages, which share its cookies", async () => {
    const log = [];
    const a = shellWindow("a", { [TAB]: "https://a.example/" }, log);
    const b = shellWindow("b", {}, log);
    const main = mainWith([a, b]);
    withRecord(main, b);
    const typed = await main.call(b, OPEN_PAGE, { conversationId: T2, url: "https://b.example/" });
    assert.deepEqual([typed.ok, typed.elsewhere], [false, true]);
    assert.match(typed.error, /This session's browser tabs are open in another Omnigent window/);
    const agent = await main.call(b, OPEN_PAGE, {
      conversationId: T2,
      url: "https://agent.example/",
      opts: { agent: true },
    });
    assert.equal(agent.ok, true);
    assert.deepEqual(log, [`a opened ${T2} at https://agent.example/`]);
    assert.equal(b.entries.size, 0, "no page of the session in a second cookie jar");
  });

  it("opens an agent's new tab beside the agent's tab when the session is split", async () => {
    const log = [];
    const a = shellWindow("a", { [TAB]: "https://a.example/" }, log);
    const b = shellWindow("b", { [T2]: "https://b.example/" }, log);
    const main = mainWith([a, b]);
    let version = 0;
    const withAgentTab = (agentTab) => {
      ({ version } = main.tabs(a, {
        op: "update",
        conversationId: "conv",
        expectedVersion: version,
        next: record(["t1", "t2"], { agentTab }),
      }).record);
    };
    withAgentTab("t2");
    for (const claimant of [a, b]) {
      // oxlint-disable-next-line no-await-in-loop -- One call at a time.
      await main.call(claimant, OPEN_PAGE, {
        conversationId: T3,
        url: "https://new.example/",
        opts: { agent: true },
      });
    }
    assert.deepEqual(log, [
      `b opened ${T3} at https://new.example/`,
      `b opened ${T3} at https://new.example/`,
    ]);
    b.entries.delete(T3);
    // Its agent tab has no page: which window's cookies it meant is unknown.
    withAgentTab("t9");
    const split = await main.call(a, OPEN_PAGE, {
      conversationId: T3,
      url: "https://new.example/",
      opts: { agent: true },
    });
    assert.equal(split.ok, false);
    assert.match(split.error, /open in more than one Omnigent window/);
    assert.equal(log.length, 2);
  });

  it("keeps windows on two servers under one origin apart", async () => {
    const log = [];
    const a = shellWindow("a", { [TAB]: "https://a.example/" }, log, ORIGIN, {
      serverUrl: `${ORIGIN}/one`,
    });
    const b = shellWindow("b", {}, log, ORIGIN, { serverUrl: `${ORIGIN}/two` });
    const main = mainWith([a, b]);
    withRecord(main, a);
    withRecord(main, b);
    assert.equal(main.tabs(a, { op: "get", conversationId: "conv" }).record.version, 1);
    const shot = await main.call(b, "omnigent:browser-perform-action", {
      conversationId: TAB,
      action: "snapshot",
    });
    assert.equal(shot.ok, false, "b's session has no page; a's is another server's");
    const opened = await main.call(b, OPEN_PAGE, {
      conversationId: T2,
      url: "https://b.example/",
    });
    assert.equal(opened.ok, true, "a's pages don't hold b's session's tabs");
    assert.deepEqual(log, [`b opened ${T2} at https://b.example/`]);
  });

  it("closes nothing for an agent close that arrives after its deadline", async () => {
    const log = [];
    const a = shellWindow("a", { [TAB]: "https://a.example/" }, log);
    const main = mainWith([a]);
    withRecord(main, a);
    const late = await main.call(a, "omnigent:browser-close", {
      conversationId: TAB,
      deadlineAt: Date.now() - 1,
    });
    assert.deepEqual(late, { ok: false, removed: false, expired: true });
    assert.deepEqual(log, []);
    const inTime = await main.call(a, "omnigent:browser-close", {
      conversationId: TAB,
      deadlineAt: Date.now() + 10_000,
    });
    assert.deepEqual([inTime.ok, log], [true, [`a closed ${TAB}`]]);
  });

  it("opens an agent's new tab beside the session's other pages, wired to that window", async () => {
    const log = [];
    const a = shellWindow("a", { conv: "https://first.example/" }, log);
    const b = shellWindow("b", {}, log);
    const main = mainWith([a, b]);
    withRecord(main, b);
    const nav = await main.call(b, "omnigent:browser-open-or-navigate", {
      conversationId: TAB,
      url: "https://new.example/",
      opts: { agent: true },
    });
    assert.deepEqual([nav.ok, nav.created], [true, true]);
    assert.deepEqual(log, [`a opened ${TAB} at https://new.example/`]);
    // Its address-bar updates go to the window showing it.
    a.entries.get(TAB).listeners.get("did-navigate")({}, "https://moved.example/");
    assert.deepEqual(
      a.win.sent.map((m) => m.channel).filter((c) => c === "browser-url-changed"),
      ["browser-url-changed"],
    );
    assert.equal(b.win.sent.filter((m) => m.channel === "browser-url-changed").length, 0);
  });

  it("leaves every window its own copy, as without the tools, while the session has no record", async () => {
    const log = [];
    const a = shellWindow("a", { [TAB]: "https://a.example/" }, log);
    const b = shellWindow("b", {}, log);
    const main = mainWith([a, b]);
    const r = await main.call(b, "omnigent:browser-perform-action", {
      conversationId: TAB,
      action: "snapshot",
    });
    assert.deepEqual([r.ok, r.error], [false, "No browser view"]);
    const typed = await main.call(b, "omnigent:browser-open-or-navigate", {
      conversationId: TAB,
      url: "https://typed.example/",
    });
    assert.equal(typed.ok, true);
    assert.deepEqual(await main.call(b, "omnigent:browser-has-view", { conversationId: "conv" }), {
      exists: false,
    });
    assert.deepEqual(log, [`b opened ${TAB} at https://typed.example/`]);
  });

  it("ignores closed windows and windows on another server", async () => {
    const log = [];
    const a = shellWindow("a", { [TAB]: "https://a.example/" }, log);
    const other = shellWindow(
      "other",
      { [TAB]: "https://other.example/" },
      log,
      "https://other.example",
    );
    const b = shellWindow("b", {}, log);
    const main = mainWith([a, other, b]);
    withRecord(main, b);
    a.win.destroyed = true;
    const r = await main.call(b, "omnigent:browser-perform-action", {
      conversationId: TAB,
      action: "snapshot",
    });
    assert.deepEqual([r.ok, r.error], [false, "No browser view"]);
    assert.deepEqual(log, []);
  });

  it("sends nothing to a window that navigated away from the server", async () => {
    const log = [];
    const a = shellWindow("a", {}, log);
    const away = shellWindow("away", { [TAB]: "https://x.example/" }, log);
    const main = mainWith([a, away], (win) => win !== away.win);
    main.tabs(a, {
      op: "update",
      conversationId: "conv",
      expectedVersion: 0,
      next: record(["t1"]),
    });
    assert.equal(a.win.sent.length, 1);
    assert.deepEqual(away.win.sent, []);
  });
});
