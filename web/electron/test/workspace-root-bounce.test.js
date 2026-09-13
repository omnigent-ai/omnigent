const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const { registerWorkspaceRootBounce } = require("../src/workspace-root-bounce");

class FakeWebContents extends EventEmitter {
  constructor(url = "") {
    super();
    this.url = url;
    this.loads = [];
  }

  getURL() {
    return this.url;
  }

  async loadURL(url) {
    this.loads.push(url);
    this.url = url;
  }
}

describe("registerWorkspaceRootBounce", () => {
  it("redirects a completed workspace-root navigation to /omnigent", () => {
    const webContents = new FakeWebContents();
    registerWorkspaceRootBounce(webContents, () => "https://ws.cloud.databricks.com");

    webContents.emit("did-navigate", {}, "https://ws.cloud.databricks.com/?o=123#page");

    assert.deepEqual(webContents.loads, ["https://ws.cloud.databricks.com/omnigent?o=123#page"]);
  });

  it("redirects main-frame in-page navigation but ignores subframes", () => {
    const webContents = new FakeWebContents();
    registerWorkspaceRootBounce(webContents, () => "https://ws.azuredatabricks.net");

    webContents.emit("did-navigate-in-page", {}, "https://ws.azuredatabricks.net/", false);
    webContents.emit("did-navigate-in-page", {}, "https://ws.azuredatabricks.net/", true);

    assert.deepEqual(webContents.loads, ["https://ws.azuredatabricks.net/omnigent"]);
  });

  it("does not redirect foreign origins or non-workspace hosts", () => {
    const webContents = new FakeWebContents();
    registerWorkspaceRootBounce(webContents, () => "https://ws.cloud.databricks.com");

    webContents.emit("did-navigate", {}, "https://other.cloud.databricks.com/");
    webContents.emit("did-navigate", {}, "https://example.com/");

    assert.deepEqual(webContents.loads, []);
  });

  it("caps redirects until a pinned non-root page finishes", () => {
    const origin = "https://ws.cloud.databricks.com";
    const webContents = new FakeWebContents();
    registerWorkspaceRootBounce(webContents, () => origin);

    webContents.emit("did-navigate", {}, `${origin}/`);
    webContents.emit("did-navigate", {}, `${origin}/`);
    assert.deepEqual(webContents.loads, [`${origin}/omnigent`]);

    webContents.url = `${origin}/omnigent`;
    webContents.emit("did-finish-load");
    webContents.emit("did-navigate", {}, `${origin}/`);
    assert.deepEqual(webContents.loads, [`${origin}/omnigent`, `${origin}/omnigent`]);
  });
});
