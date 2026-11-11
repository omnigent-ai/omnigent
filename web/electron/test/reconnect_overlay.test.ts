import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import path from "node:path";
import { pathToFileURL } from "node:url";
import type { Rectangle, WebContentsViewConstructorOptions } from "electron";
import {
  createReconnectOverlay,
  type ReconnectOverlayIpcEvent,
  type ReconnectOverlayView,
} from "../src/reconnect_overlay.ts";

const PAGE = path.join(import.meta.dirname, "../reconnect-overlay/index.html");
const PAGE_URL = pathToFileURL(PAGE).href;

interface LoadOptions {
  query: Record<string, string>;
}

class View {
  options: WebContentsViewConstructorOptions;
  openHandler?: () => { action: "deny" };
  background?: string;
  bounds?: Rectangle;
  webContents = Object.assign(new EventEmitter(), {
    destroyed: false,
    loads: [] as [string, LoadOptions][],
    focused: 0,
    isDestroyed: () => this.webContents.destroyed,
    close: () => {
      this.webContents.destroyed = true;
    },
    focus: () => this.webContents.focused++,
    setWindowOpenHandler: (handler: () => { action: "deny" }) => {
      this.openHandler = handler;
    },
    loadFile: (file: string, loadOptions: LoadOptions) => {
      this.webContents.loads.push([file, loadOptions]);
      return Promise.resolve();
    },
  });

  constructor(options: WebContentsViewConstructorOptions) {
    this.options = options;
  }
  setBackgroundColor(color: string) {
    this.background = color;
  }
  setBounds(bounds: Rectangle) {
    this.bounds = bounds;
  }
}

class Window extends EventEmitter {
  destroyed: boolean;
  size: number[];
  children: unknown[];
  contentView: { addChildView: (view: unknown) => void; removeChildView: (view: unknown) => void };
  isFocused?: () => boolean;
  webContents = { focus: () => {} };

  constructor() {
    super();
    this.destroyed = false;
    this.size = [1000, 700];
    this.children = [];
    this.contentView = {
      addChildView: (view) => {
        this.children = [...this.children.filter((child) => child !== view), view];
      },
      removeChildView: (view) => {
        this.children = this.children.filter((child) => child !== view);
      },
    };
  }
  isDestroyed() {
    return this.destroyed;
  }
  getContentSize() {
    return this.size;
  }
}

function harness() {
  const views: View[] = [];
  const handlers = new Map<string, (event: ReconnectOverlayIpcEvent) => void>();
  const cancelled: Window[] = [];
  class WebContentsView extends View {
    constructor(options: WebContentsViewConstructorOptions) {
      super(options);
      views.push(this);
    }
  }
  const overlay = createReconnectOverlay({
    WebContentsView,
    ipcMain: { on: (name, handler) => handlers.set(name, handler) },
    overlayPage: PAGE,
    preloadPath: "/app/src/reconnect_overlay_preload.js",
    onCancel: (parent: Window) => cancelled.push(parent),
  });
  overlay.registerIpc();
  const parent = new Window();
  const sendCancel = (view: ReconnectOverlayView, url = PAGE_URL) =>
    handlers.get("omnigent:reconnect-overlay-cancel")!({
      sender: view.webContents,
      senderFrame: { url },
    });
  return { overlay, views, parent, cancelled, sendCancel };
}

describe("reconnect overlay (src/reconnect_overlay.ts)", () => {
  it("covers the window content with a transparent, isolated view on top", () => {
    const { overlay, views, parent } = harness();
    const pane = {};
    parent.contentView.addChildView(pane);
    overlay.show(parent, "Check your network connection.");
    const [view] = views;
    assert.ok(view);
    assert.deepEqual(parent.children, [pane, view]);
    assert.deepEqual(view.bounds, { x: 0, y: 0, width: 1000, height: 700 });
    assert.equal(view.background, "#00000000");
    assert.equal(view.options.webPreferences?.contextIsolation, true);
    assert.equal(view.options.webPreferences?.nodeIntegration, false);
    assert.equal(view.options.webPreferences?.sandbox, true);
    assert.deepEqual(view.webContents.loads, [
      [PAGE, { query: { hint: "Check your network connection." } }],
    ]);
    assert.equal(view.webContents.focused, 1);
    assert.deepEqual(view.openHandler?.(), { action: "deny" });
    assert.equal(overlay.isShown(parent), true);
  });

  it("follows resizes and stays above panes attached while shown", () => {
    const { overlay, views, parent } = harness();
    overlay.show(parent, "hint");
    parent.size = [800, 500];
    parent.emit("resize");
    assert.deepEqual(views[0]!.bounds, { x: 0, y: 0, width: 800, height: 500 });
    const pane = {};
    parent.contentView.addChildView(pane);
    overlay.raise(parent);
    assert.deepEqual(parent.children, [pane, views[0]]);
  });

  it("reuses one view, reloading only when the hint changes", () => {
    const { overlay, views, parent } = harness();
    overlay.show(parent, "a");
    overlay.show(parent, "a");
    overlay.hide(parent);
    assert.equal(overlay.isShown(parent), false);
    assert.deepEqual(parent.children, []);
    // Raising a hidden overlay does not bring it back.
    overlay.raise(parent);
    assert.deepEqual(parent.children, []);
    overlay.show(parent, "b");
    assert.equal(views.length, 1);
    assert.deepEqual(
      views[0]!.webContents.loads.map(([, options]) => options.query.hint),
      ["a", "b"],
    );
  });

  it("cancels only for its own shown page", () => {
    const { overlay, views, parent, cancelled, sendCancel } = harness();
    overlay.show(parent, "hint");
    const [view] = views;
    assert.ok(view);
    sendCancel(view, "https://workspace.example/");
    sendCancel(new View({}));
    assert.deepEqual(cancelled, []);
    sendCancel(view, `${PAGE_URL}?hint=x`);
    assert.deepEqual(cancelled, [parent]);
    overlay.hide(parent);
    sendCancel(view);
    assert.equal(cancelled.length, 1);
  });

  it("hands focus back to the page when hidden in a focused window", () => {
    const { overlay, parent } = harness();
    let focused = 0;
    parent.isFocused = () => true;
    parent.webContents = { focus: () => focused++ };
    overlay.show(parent, "hint");
    overlay.hide(parent);
    overlay.hide(parent);
    assert.equal(focused, 1);
  });

  it("closes its page with the window", () => {
    const { overlay, views, parent } = harness();
    overlay.show(parent, "hint");
    parent.destroyed = true;
    parent.emit("closed");
    assert.equal(views[0]!.webContents.isDestroyed(), true);
    assert.equal(overlay.isShown(parent), false);
  });
});
