import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import path from "node:path";

import {
  createFindBar,
  findConversationId,
  FIND_BAR_HEIGHT,
  FIND_BAR_INSET,
  FIND_BAR_WIDTH,
  type FindBarFocusedWindow,
  type FindBarIpcEvent,
  type FindBarParent,
  type FindBarParentContents,
  type FindBarWindow,
  type FindBarWindowOptions,
} from "../src/find_bar.ts";
import type { PushTarget } from "../src/ipc/main.ts";

const FIND_PAGE = path.join(import.meta.dirname, "..", "find", "index.html");
const FIND_FRAME = `file://${FIND_PAGE}`;

class FakeParentContents extends EventEmitter implements FindBarParentContents {
  url: string;
  destroyed = false;
  sent: unknown[][] = [];
  searches: unknown[][] = [];
  stops: string[] = [];

  constructor(url: string) {
    super();
    this.url = url;
  }
  send(...args: unknown[]) {
    this.sent.push(args);
  }
  getURL() {
    return this.url;
  }
  isDestroyed() {
    return this.destroyed;
  }
  findInPage(text: string, options: { forward: boolean; findNext: boolean }) {
    this.searches.push([text, options]);
  }
  stopFindInPage(action: "clearSelection") {
    this.stops.push(action);
  }
}

class FakeParent extends EventEmitter implements FindBarParent {
  webContents: FakeParentContents;
  destroyed = false;
  focused = 0;

  constructor(url = "https://omni.example/c/alpha") {
    super();
    this.webContents = new FakeParentContents(url);
  }
  isDestroyed() {
    return this.destroyed;
  }
  focus() {
    this.focused += 1;
  }
  getContentBounds() {
    return { x: 100, y: 50, width: 1000, height: 700 };
  }
}

class FakeBarContents implements PushTarget {
  sent: unknown[][] = [];
  send(...args: unknown[]) {
    this.sent.push(args);
  }
}

class FakeBar extends EventEmitter implements FindBarWindow, FindBarFocusedWindow {
  options: FindBarWindowOptions<FakeParent>;
  webContents = new FakeBarContents();
  destroyed = false;
  focused = 0;
  shown = 0;
  bounds: unknown[] = [];
  loaded: string[] = [];

  constructor(options: FindBarWindowOptions<FakeParent>) {
    super();
    this.options = options;
  }
  isDestroyed() {
    return this.destroyed;
  }
  focus() {
    this.focused += 1;
  }
  show() {
    this.shown += 1;
  }
  close() {
    this.destroyed = true;
    this.emit("closed");
  }
  setBounds(bounds: unknown) {
    this.bounds.push(bounds);
  }
  async loadFile(filePath: string) {
    this.loaded.push(filePath);
  }
  getParentWindow() {
    return this.options.parent;
  }
}

type IpcListener = (event: FindBarIpcEvent, ...args: unknown[]) => void;

function harness({ parents = [new FakeParent()] }: { parents?: FakeParent[] } = {}) {
  const bars: FakeBar[] = [];
  const ipc = new Map<string, IpcListener>();
  const warnings: string[] = [];
  let focused: FindBarFocusedWindow | null = null;
  const shellWindows = new Set<unknown>(parents);
  const findBar = createFindBar<FakeParent, FakeBar>({
    BrowserWindow: class extends FakeBar {
      constructor(options: FindBarWindowOptions<FakeParent>) {
        super(options);
        bars.push(this);
      }
    },
    ipcMain: { on: (channel, listener) => ipc.set(channel, listener) },
    pagePath: FIND_PAGE,
    preloadPath: "/app/find_preload.js",
    getFocusedWindow: () => focused,
    windowForSender: (sender) => bars.find((bar) => bar.webContents === sender) ?? null,
    isShellWindow: (win): win is FakeParent => shellWindows.has(win),
    activeWindow: () => parents[0] ?? null,
    warn: (message) => warnings.push(message),
  });
  findBar.registerIpc();
  const send = (channel: string, event: FindBarIpcEvent, ...args: unknown[]) => {
    const listener = ipc.get(channel);
    assert.ok(listener, `${channel} is registered`);
    listener(event, ...args);
  };
  return {
    findBar,
    bars,
    parents,
    warnings,
    send,
    fromBar: (bar: FakeBar, url = FIND_FRAME): FindBarIpcEvent => ({
      sender: bar.webContents,
      senderFrame: { url },
    }),
    setFocused: (win: FindBarFocusedWindow | null) => {
      focused = win;
    },
  };
}

describe("find bar teardown", () => {
  it("clears the search and refocuses the parent when the bar closes", () => {
    const h = harness();
    const parent = h.parents[0]!;
    h.findBar.open(parent);
    h.bars[0]!.close();
    assert.deepEqual(parent.webContents.stops, ["clearSelection"]);
    assert.equal(parent.focused, 1);
  });

  it("skips the parent's webContents once Cmd+W has destroyed them", () => {
    const h = harness();
    const parent = h.parents[0]!;
    h.findBar.open(parent);
    parent.webContents.destroyed = true;
    parent.webContents.stopFindInPage = () => {
      throw new TypeError("Object has been destroyed");
    };
    assert.doesNotThrow(() => h.bars[0]!.close());
    assert.equal(parent.focused, 0);
  });

  it("tells the page the bar opened, since the menu sends no page keydown", () => {
    const h = harness();
    const parent = h.parents[0]!;
    h.findBar.open(parent);
    assert.deepEqual(parent.webContents.sent, [["omnigent:find-opened"]]);
  });

  it("closes when the window switches conversation, not within one or in a subframe, under a mount", () => {
    const parent = new FakeParent("https://omni.example/tenant/c/foo/c/alpha");
    const h = harness({ parents: [parent] });
    h.findBar.open(parent);
    const bar = h.bars[0]!;
    parent.webContents.emit(
      "did-navigate-in-page",
      {},
      "https://omni.example/tenant/c/foo/c/alpha?message=m1",
      true,
    );
    parent.webContents.emit("did-navigate-in-page", {}, "https://embed.example/#tab", false);
    assert.equal(bar.isDestroyed(), false);
    parent.webContents.emit(
      "did-navigate-in-page",
      {},
      "https://omni.example/tenant/c/foo/c/beta",
      true,
    );
    assert.equal(bar.isDestroyed(), true);
  });

  it("stops following the parent once the bar has closed", () => {
    const h = harness();
    const parent = h.parents[0]!;
    h.findBar.open(parent);
    h.bars[0]!.close();
    for (const event of ["resize", "move"]) assert.equal(parent.listenerCount(event), 0);
    for (const event of ["found-in-page", "did-navigate-in-page"]) {
      assert.equal(parent.webContents.listenerCount(event), 0);
    }
  });
});

describe("find bar window", () => {
  it("opens one frameless child per window and re-activates it on repeat", () => {
    const h = harness();
    const parent = h.parents[0]!;
    h.findBar.open(parent);
    h.findBar.open(parent);
    assert.equal(h.bars.length, 1);
    const bar = h.bars[0]!;
    assert.equal(bar.options.parent, parent);
    assert.equal(bar.options.frame, false);
    assert.equal(bar.options.show, false);
    assert.equal(bar.options.webPreferences?.preload, "/app/find_preload.js");
    assert.deepEqual(bar.loaded, [FIND_PAGE]);
    assert.equal(bar.focused, 1);
    assert.deepEqual(bar.webContents.sent, [["omnigent:find-activate"]]);
    assert.deepEqual(parent.webContents.sent, [["omnigent:find-opened"], ["omnigent:find-opened"]]);
  });

  it("anchors to the parent's top-right corner when shown and as the parent moves", () => {
    const h = harness();
    const parent = h.parents[0]!;
    h.findBar.open(parent);
    const bar = h.bars[0]!;
    bar.emit("ready-to-show");
    parent.emit("resize");
    parent.emit("move");
    const expected = {
      x: 100 + 1000 - FIND_BAR_WIDTH - FIND_BAR_INSET,
      y: 50 + FIND_BAR_INSET,
      width: FIND_BAR_WIDTH,
      height: FIND_BAR_HEIGHT,
    };
    assert.deepEqual(bar.bounds, [expected, expected, expected]);
    assert.equal(bar.shown, 1);
  });

  it("forwards match counts to the bar", () => {
    const h = harness();
    const parent = h.parents[0]!;
    h.findBar.open(parent);
    parent.webContents.emit("found-in-page", {}, { activeMatchOrdinal: 2, matches: 5 });
    assert.deepEqual(h.bars[0]!.webContents.sent, [
      ["omnigent:find-result", { active: 2, matches: 5 }],
    ]);
  });

  it("opens a new bar when the previous one was destroyed", () => {
    const h = harness();
    const parent = h.parents[0]!;
    h.findBar.open(parent);
    h.bars[0]!.destroyed = true;
    h.findBar.open(parent);
    assert.equal(h.bars.length, 2);
  });

  it("targets the focused bar's own window, else the active window", () => {
    const first = new FakeParent();
    const second = new FakeParent();
    const h = harness({ parents: [first, second] });
    h.findBar.open(second);
    h.setFocused(h.bars[0]!);
    assert.equal(h.findBar.targetForShortcut(), second);
    h.setFocused({ getParentWindow: () => null });
    assert.equal(h.findBar.targetForShortcut(), first);
    h.setFocused(null);
    assert.equal(h.findBar.targetForShortcut(), first);
  });
});

describe("find bar IPC", () => {
  it("runs searches in the bar's parent with the bar's options", () => {
    const h = harness();
    const parent = h.parents[0]!;
    h.findBar.open(parent);
    const event = h.fromBar(h.bars[0]!);
    h.send("omnigent:find-query", event, { text: "abc" });
    h.send("omnigent:find-query", event, { text: "abc", forward: false, findNext: true });
    h.send("omnigent:find-query", event, { text: 5, findNext: "yes" });
    assert.deepEqual(parent.webContents.searches, [
      ["abc", { forward: true, findNext: false }],
      ["abc", { forward: false, findNext: true }],
      ["5", { forward: true, findNext: false }],
    ]);
  });

  it("clears the highlight and zeroes the counter for an empty query", () => {
    const h = harness();
    const parent = h.parents[0]!;
    h.findBar.open(parent);
    const bar = h.bars[0]!;
    h.send("omnigent:find-query", h.fromBar(bar), { text: "" });
    h.send("omnigent:find-query", h.fromBar(bar), null);
    assert.deepEqual(parent.webContents.stops, ["clearSelection", "clearSelection"]);
    assert.deepEqual(parent.webContents.searches, []);
    assert.deepEqual(bar.webContents.sent, [
      ["omnigent:find-result", { active: 0, matches: 0 }],
      ["omnigent:find-result", { active: 0, matches: 0 }],
    ]);
  });

  it("drops messages from frames other than the bundled find page", () => {
    const h = harness();
    const parent = h.parents[0]!;
    h.findBar.open(parent);
    const bar = h.bars[0]!;
    for (const url of [
      "https://evil.example/find/index.html",
      "::::",
      "file:///other/index.html",
    ]) {
      h.send("omnigent:find-query", h.fromBar(bar, url), { text: "x" });
      h.send("omnigent:find-close", h.fromBar(bar, url));
    }
    h.send("omnigent:find-query", { sender: bar.webContents }, { text: "x" });
    assert.deepEqual(parent.webContents.searches, []);
    assert.equal(bar.isDestroyed(), false);
    assert.deepEqual(h.warnings, [
      ...Array.from({ length: 3 }, () => [
        "[omnigent] find-query from untrusted sender dropped",
        "[omnigent] find-close from untrusted sender dropped",
      ]).flat(),
      "[omnigent] find-query from untrusted sender dropped",
    ]);
  });

  it("ignores a query from a find page that isn't an open bar", () => {
    const h = harness();
    const parent = h.parents[0]!;
    h.findBar.open(parent);
    h.send(
      "omnigent:find-query",
      { sender: new FakeBarContents(), senderFrame: { url: FIND_FRAME } },
      {
        text: "x",
      },
    );
    assert.deepEqual(parent.webContents.searches, []);
    assert.deepEqual(h.warnings, []);
  });

  it("closes the sending bar, which then tears down", () => {
    const h = harness();
    const parent = h.parents[0]!;
    h.findBar.open(parent);
    h.send("omnigent:find-close", h.fromBar(h.bars[0]!));
    assert.equal(h.bars[0]!.isDestroyed(), true);
    assert.deepEqual(parent.webContents.stops, ["clearSelection"]);
    assert.equal(parent.focused, 1);
  });
});

describe("findConversationId", () => {
  it("reads the trailing conversation route after any mount", () => {
    assert.equal(findConversationId("https://omni.example/c/alpha"), "alpha");
    assert.equal(findConversationId("https://omni.example/tenant/c/foo/c/beta/"), "beta");
    assert.equal(findConversationId("https://omni.example/c/alpha?message=m1"), "alpha");
    assert.equal(findConversationId("https://omni.example/"), null);
    assert.equal(findConversationId("not a url"), null);
  });
});
