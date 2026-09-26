const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const { serverDisplayLabel, workspaceIdentityKey } = require("../src/url");

describe("main window preload", () => {
  it("evaluates in a sandbox without requiring sibling files", () => {
    const exposed = new Map();
    const source = readFileSync(path.join(__dirname, "../src/preload.js"), "utf8");

    vm.runInNewContext(source, {
      URL,
      URLSearchParams,
      require: (specifier) => {
        assert.equal(specifier, "electron");
        return {
          contextBridge: {
            exposeInMainWorld: (name, value) => exposed.set(name, value),
          },
          ipcRenderer: {
            invoke: () => Promise.resolve(),
            on: () => {},
            removeListener: () => {},
            send: () => {},
          },
        };
      },
    });

    const desktop = exposed.get("omnigentDesktop");
    const setup = exposed.get("omnigentSetup");
    const urls = [
      "https://dbc-a.cloud.databricks.com/omnigent?o=team%2Fblue",
      "https://server.example/path?ignored=yes",
      "not a URL",
    ];
    for (const url of urls) {
      assert.equal(desktop.workspaceIdentityKey(url), workspaceIdentityKey(url));
      assert.equal(desktop.serverDisplayLabel(url), serverDisplayLabel(url));
      assert.equal(setup.serverDisplayLabel(url), serverDisplayLabel(url));
    }
  });
});
