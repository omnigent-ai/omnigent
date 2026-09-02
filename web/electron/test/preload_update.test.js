const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PRELOAD = fs.readFileSync(path.join(__dirname, "../src/preload.js"), "utf8");

function loadPreload() {
  const exposed = new Map();
  let updateStatus = { state: "idle" };
  const ipcRenderer = {
    invoke: async (channel) => {
      if (channel === "omnigent:get-update-status") return updateStatus;
      return null;
    },
    send: () => {},
    on: () => {},
    removeListener: () => {},
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
    setStatus: (status) => {
      updateStatus = status;
    },
  };
}

describe("server-page update bridge", () => {
  it("hides every shell-owned update prompt state, including download progress", async () => {
    const h = loadPreload();
    async function expectHidden(state, lastError) {
      h.setStatus({ state, lastError });
      const status = await h.desktop.updates.getStatus();
      assert.equal(status.state, "idle");
      assert.equal(status.progress, undefined);
      assert.equal(status.info, undefined);
    }

    await expectHidden("available");
    await expectHidden("downloading");
    await expectHidden("downloaded");
    await expectHidden("error-security", "signature failed");
  });
});
