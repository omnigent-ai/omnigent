// The renderer half of the fullscreen bridge: preload.js must read the
// window's fullscreen state over IPC and forward main-process transitions as
// booleans (macOS fullscreen hides the traffic lights, and the web layer
// keys its clearance CSS off this signal).

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PRELOAD = fs.readFileSync(path.join(__dirname, "../src/preload.js"), "utf8");

function loadPreload() {
  const exposed = new Map();
  const listeners = new Map();
  const invokes = [];
  const ipcRenderer = {
    invoke: async (channel) => {
      invokes.push(channel);
      if (channel === "omnigent:window-is-full-screen") return true;
      return null;
    },
    send: () => {},
    on: (channel, listener) => listeners.set(channel, listener),
    removeListener: (channel, listener) => {
      if (listeners.get(channel) === listener) listeners.delete(channel);
    },
  };
  vm.runInNewContext(PRELOAD, {
    console,
    require: (specifier) => {
      assert.equal(specifier, "electron");
      return {
        contextBridge: { exposeInMainWorld: (name, value) => exposed.set(name, value) },
        ipcRenderer,
      };
    },
  });
  return { desktop: exposed.get("omnigentDesktop"), listeners, invokes };
}

describe("desktop fullscreen bridge", () => {
  it("reads the window's fullscreen state over IPC", async () => {
    const h = loadPreload();
    assert.equal(await h.desktop.isFullScreen(), true);
    assert.ok(h.invokes.includes("omnigent:window-is-full-screen"));
  });

  it("forwards transitions as booleans and unsubscribes cleanly", () => {
    const h = loadPreload();
    const seen = [];
    const unsubscribe = h.desktop.onFullScreenChanged((value) => seen.push(value));

    const listener = h.listeners.get("omnigent:full-screen-changed");
    assert.ok(listener, "onFullScreenChanged must subscribe to omnigent:full-screen-changed");
    listener({}, true);
    listener({}, false);
    listener({}, "junk"); // Non-boolean payloads coerce to false, never leak through.
    assert.deepEqual(seen, [true, false, false]);

    unsubscribe();
    assert.equal(h.listeners.has("omnigent:full-screen-changed"), false);
  });
});
