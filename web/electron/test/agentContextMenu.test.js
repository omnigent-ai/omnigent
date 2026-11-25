"use strict";

// An agent's right-click (browserInput.js) must not open the shell's own
// context menu over the page it is driving.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { createBrowserViewRegistry } = require("../src/browserViewRegistry");
const { createBrowserViewBoundsController } = require("../src/browserViewBounds");

function makeRegistry() {
  const menus = [];
  let handlers = {};
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: () => {
      handlers = {};
      return {
        setBounds() {},
        webContents: {
          id: 1,
          loadURL() {},
          close() {},
          removeListener() {},
          on(event, fn) {
            handlers[event] = fn;
          },
          setWindowOpenHandler() {},
        },
      };
    },
    createBoundsController: createBrowserViewBoundsController,
    attachToHost() {},
    detachFromHost() {},
    sendToRenderer() {},
    getHostZoomFactor: () => 1,
    showContextMenu: (items) => menus.push(items),
  });
  return { registry, menus, fireContextMenu: (params) => handlers["context-menu"]({}, params) };
}

describe("browserViewRegistry — agent right-click", () => {
  it("does not open the shell's context menu while an agent right-click is in flight", () => {
    const { registry, fireContextMenu, menus } = makeRegistry();
    registry.openOrNavigate("conv_1", "https://example.com");
    const params = { linkURL: "https://example.com/next", selectionText: "" };
    registry.get("conv_1").suppressContextMenuUntil = Date.now() + 1000;
    fireContextMenu(params);
    assert.equal(menus.length, 0);
    registry.get("conv_1").suppressContextMenuUntil = Date.now() - 1;
    fireContextMenu(params);
    assert.equal(menus.length, 1);
  });
});
