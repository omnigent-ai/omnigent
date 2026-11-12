import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { pathToFileURL } from "node:url";
import type { Rectangle } from "electron";

import {
  createBrowserPermissionPrompt,
  promptBounds,
  type PermissionPromptIpcEvent,
  type PermissionPromptOptions,
  type PermissionPromptWindowOptions,
} from "../src/browserPermissionPrompt.ts";

const PAGE = "/app/browser-permission/index.html";
const ORIGIN = "https://login.example.com";

type PermissionRequest = (
  contents: unknown,
  permission: string,
  callback: (granted: boolean) => void,
) => void;

class FakeWebContents extends EventEmitter {
  mainFrame = { url: "" };
  private readonly owner: Window;
  constructor(owner: Window) {
    super();
    this.owner = owner;
  }
  isDestroyed() {
    return this.owner.destroyed;
  }
  setWindowOpenHandler(handler: () => { action: "deny" }) {
    this.owner.openHandler = handler;
  }
  session = {
    setPermissionRequestHandler: (handler: PermissionRequest) => {
      this.owner.permissionRequest = handler;
    },
    setPermissionCheckHandler: (handler: () => boolean) => {
      this.owner.permissionCheck = handler;
    },
  };
}

class Window extends EventEmitter {
  options: Partial<PermissionPromptWindowOptions<Window>>;
  destroyed = false;
  visible = false;
  webContents = new FakeWebContents(this);
  openHandler?: () => { action: "deny" };
  permissionRequest?: PermissionRequest;
  permissionCheck?: () => boolean;
  bounds?: Rectangle;
  file?: string;
  excludedFromShownWindowsMenu?: boolean;
  constructor(options: Partial<PermissionPromptWindowOptions<Window>> = {}) {
    super();
    this.options = options;
  }
  isDestroyed() {
    return this.destroyed;
  }
  getContentBounds() {
    return { x: 100, y: 80, width: 1000, height: 700 };
  }
  setBounds(bounds: Rectangle) {
    this.bounds = bounds;
  }
  loadFile(file: string) {
    this.file = file;
    this.webContents.mainFrame.url = pathToFileURL(file).href;
    return Promise.resolve();
  }
  show() {
    this.visible = true;
  }
  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.emit("closed");
  }
}

type IpcHandler = (event: PermissionPromptIpcEvent, ...args: unknown[]) => unknown;

function harness() {
  const windows: Window[] = [];
  const handlers = new Map<string, IpcHandler>();
  const handler = (name: string): IpcHandler => {
    const found = handlers.get(name);
    assert.ok(found, name);
    return found;
  };
  class BrowserWindow extends Window {
    constructor(options: PermissionPromptWindowOptions<Window>) {
      super(options);
      windows.push(this);
    }
  }
  const controller = createBrowserPermissionPrompt({
    BrowserWindow,
    ipcMain: {
      handle: (name, listener) => {
        handlers.set(name, listener);
      },
    },
    promptPage: PAGE,
    preloadPath: "/app/src/browser_permission_preload.js",
  });
  controller.registerIpc();
  const parent = new Window();
  const abort = new AbortController();
  const show = (overrides: Partial<PermissionPromptOptions<Window>> = {}) =>
    controller.show({
      parent,
      origin: ORIGIN,
      reload: true,
      getAnchorBounds: () => ({ x: 450, y: 64, width: 540, height: 600 }),
      signal: abort.signal,
      ...overrides,
    });
  const event = (win = windows.at(-1)) => {
    assert.ok(win);
    return { sender: win.webContents, senderFrame: win.webContents.mainFrame };
  };
  return {
    controller,
    windows,
    parent,
    abort,
    show,
    event,
    info: (ev: PermissionPromptIpcEvent = event()) =>
      handler("omnigent:browser-permission-info")(ev),
    choose: (choice: string, ev: PermissionPromptIpcEvent = event()) =>
      handler("omnigent:browser-permission-choose")(ev, choice),
  };
}

type Harness = ReturnType<typeof harness>;

describe("browser permission popover", () => {
  it("uses a small shell-owned, isolated child window under the browser toolbar", async () => {
    const h = harness();
    const result = h.show();
    const popup = h.windows[0];
    assert.ok(popup);
    assert.equal(popup.options.parent, h.parent);
    assert.equal(popup.options.frame, false);
    assert.equal(popup.options.transparent, true);
    assert.equal(popup.options.modal, undefined, "not a blocking system dialog");
    assert.deepEqual(popup.options.webPreferences, {
      preload: "/app/src/browser_permission_preload.js",
      partition: "omnigent-browser-permission-ui",
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      devTools: false,
    });
    assert.equal(popup.file, PAGE);
    assert.equal(popup.visible, false);
    popup.emit("ready-to-show");
    assert.equal(popup.visible, true);
    assert.deepEqual(popup.bounds, { x: 638, y: 140, width: 380, height: 250 });
    assert.deepEqual(h.info(), { origin: ORIGIN, reload: true });
    assert.ok(popup.openHandler && popup.permissionCheck && popup.permissionRequest);
    assert.deepEqual(popup.openHandler(), { action: "deny" });
    assert.equal(popup.permissionCheck(), false);
    popup.permissionRequest(null, "media", (granted) => assert.equal(granted, false));
    let prevented = 0;
    popup.webContents.emit("will-navigate", { preventDefault: () => prevented++ });
    popup.webContents.emit("will-frame-navigate", { preventDefault: () => prevented++ });
    assert.equal(prevented, 2);
    h.choose("dismiss");
    assert.equal(await result, "dismiss");
  });

  for (const choice of ["allow-once", "always-allow", "deny", "dismiss"]) {
    it(`accepts ${choice} only from its own main frame and settles once`, async () => {
      const h = harness();
      const result = h.show();
      const event = h.event();
      h.choose(choice);
      assert.equal(await result, choice);
      assert.equal(h.windows[0]?.destroyed, true);
      assert.throws(() => h.choose("always-allow", event), /bundled permission UI/);
      assert.equal(h.parent.listenerCount("closed"), 0);
      assert.equal(h.parent.listenerCount("resize"), 0);
      assert.equal(h.parent.webContents.listenerCount("did-start-navigation"), 0);
    });
  }

  it("rejects a site/server renderer, a matching-URL impostor, subframes, and invalid choices", async () => {
    const h = harness();
    const result = h.show();
    const event = h.event();
    const foreign = new Window();
    foreign.webContents.mainFrame.url = pathToFileURL(PAGE).href;
    assert.throws(() => h.choose("always-allow", h.event(foreign)), /bundled permission UI/);
    assert.throws(
      () => h.info({ ...event, senderFrame: { ...event.senderFrame } }),
      /bundled permission UI/,
    );
    event.senderFrame.url = "https://login.example.com";
    assert.throws(() => h.choose("always-allow", event), /bundled permission UI/);
    event.senderFrame.url = pathToFileURL(PAGE).href;
    assert.throws(() => h.choose("allow-everything"), /Invalid permission choice/);
    h.choose("deny");
    assert.equal(await result, "deny");
  });

  const dismissals: [string, (h: Harness) => void][] = [
    ["clicking outside", (h) => h.windows[0]?.emit("blur")],
    ["pane navigation", (h) => h.abort.abort()],
    ["parent closing", (h) => h.parent.destroy()],
    ["parent hiding", (h) => h.parent.emit("hide")],
    ["parent minimizing", (h) => h.parent.emit("minimize")],
    ["renderer crash", (h) => h.windows[0]?.webContents.emit("render-process-gone")],
    [
      "shell navigation",
      (h) =>
        h.parent.webContents.emit(
          "did-start-navigation",
          {},
          "https://elsewhere.example",
          false,
          true,
        ),
    ],
  ];
  for (const [name, close] of dismissals) {
    it(`dismisses without approval on ${name}`, async () => {
      const h = harness();
      const result = h.show();
      close(h);
      assert.equal(await result, "dismiss");
      assert.equal(h.windows[0]?.destroyed, true);
      h.windows[0]?.emit("ready-to-show");
      assert.equal(h.windows[0]?.visible, false);
    });
  }

  it("does not create a window for an already-canceled request", async () => {
    const h = harness();
    h.abort.abort();
    assert.equal(await h.show(), "dismiss");
    assert.equal(h.windows.length, 0);
  });

  it("replaces a prompt in the same window without affecting another window's prompt", async () => {
    const h = harness();
    const first = h.show();
    const another = h.show({ parent: new Window(), origin: "https://another.example" });
    const second = h.show();
    assert.equal(await first, "dismiss");
    assert.deepEqual(h.info(h.event(h.windows[1])), {
      origin: "https://another.example",
      reload: true,
    });
    h.choose("always-allow", h.event(h.windows[1]));
    assert.equal(await another, "always-allow");
    h.controller.dismiss(h.parent);
    assert.equal(await second, "dismiss");
  });

  it("clamps the popover to its parent and repositions it when the parent moves", async () => {
    assert.deepEqual(promptBounds({ x: 0, y: 0, width: 300, height: 200 }, { x: 900, y: 600 }), {
      x: 0,
      y: 0,
      width: 300,
      height: 200,
    });
    const h = harness();
    const result = h.show();
    h.parent.emit("resize");
    assert.deepEqual(h.windows[0]?.bounds, { x: 638, y: 140, width: 380, height: 250 });
    h.choose("deny");
    await result;
  });
});
