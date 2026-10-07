// Runs src/preload.js in a VM with a scripted ipcRenderer, so bridge tests can
// script invoke replies and fire main→renderer events without Electron.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PRELOAD = fs.readFileSync(path.join(__dirname, "../src/preload.js"), "utf8");

/**
 * @param {(channel: string, args: unknown) => unknown} [respond] Reply for
 *   `ipcRenderer.invoke`; the default resolves every channel to null.
 */
function loadPreload(respond = () => null) {
  const exposed = new Map();
  const listeners = new Map();
  const invokes = [];
  const ipcRenderer = {
    invoke: async (channel, args) => {
      invokes.push({ channel, args });
      return respond(channel, args);
    },
    send: () => {},
    on: (channel, listener) =>
      listeners.set(channel, [...(listeners.get(channel) ?? []), listener]),
    removeListener: (channel, listener) => {
      const remaining = (listeners.get(channel) ?? []).filter((l) => l !== listener);
      if (remaining.length === 0) listeners.delete(channel);
      else listeners.set(channel, remaining);
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
  return {
    desktop: exposed.get("omnigentDesktop"),
    emit: (channel, payload) => (listeners.get(channel) ?? []).forEach((l) => l({}, payload)),
    hasListener: (channel) => listeners.has(channel),
    invokes,
  };
}

module.exports = { loadPreload };
