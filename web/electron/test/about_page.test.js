const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PAGE_SCRIPT = fs.readFileSync(path.join(__dirname, "../about/index.js"), "utf8");

class FakeElement {
  constructor() {
    this.textContent = "";
    this.dataset = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.listeners = new Map();
    this.attributes = new Map();
    this.src = "";
    this.title = "";
  }

  addEventListener(name, listener) {
    this.listeners.set(name, listener);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }
}

function makePage(initialStatus = { state: "idle" }, getStatus) {
  const ids = [
    "app-icon",
    "product-kind",
    "desktop-heading",
    "desktop-version",
    "desktop-actions",
    "desktop-check",
    "desktop-update-now",
    "desktop-restart",
    "desktop-result",
    "desktop-progress",
    "desktop-progress-percent",
    "desktop-progress-bar",
    "cli-badge",
    "cli-version",
    "cli-path",
    "close",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new FakeElement()]));
  const calls = { download: 0, install: 0 };
  const order = [];
  let statusListener = null;
  const api = {
    getInfo: async () => ({
      desktopVersion: "0.13.0",
      platformName: "macOS",
      appIconDataUrl: "data:image/png;base64,icon",
      cli: { installed: false, path: null, version: null },
    }),
    getDesktopUpdateStatus: async () => {
      order.push("snapshot");
      return getStatus ? getStatus() : initialStatus;
    },
    onDesktopUpdateStatus: (listener) => {
      order.push("subscribe");
      statusListener = listener;
      return () => {
        statusListener = null;
      };
    },
    checkDesktopUpdates: async () => ({ state: "none" }),
    downloadDesktopUpdate: async () => {
      calls.download += 1;
    },
    installDesktopUpdate: async () => {
      calls.install += 1;
    },
    close: () => {},
  };
  const documentListeners = new Map();
  const windowListeners = new Map();
  const context = {
    console,
    document: {
      getElementById: (id) => elements[id],
      addEventListener: (name, listener) => documentListeners.set(name, listener),
    },
    window: {
      omnigentAbout: api,
      addEventListener: (name, listener) => windowListeners.set(name, listener),
    },
  };
  vm.runInNewContext(PAGE_SCRIPT, context, { filename: "about/index.js" });
  return {
    api,
    calls,
    elements,
    order,
    emitStatus: (status) => statusListener(status),
  };
}

async function flushPromises() {
  await new Promise((resolve) => {
    setImmediate(resolve);
  });
}

describe("About page desktop update flow", () => {
  it("subscribes before reading status and shows Update now when available", async () => {
    const page = makePage({ state: "available", info: { version: "0.14.0" } });
    await flushPromises();

    assert.deepEqual(page.order, ["subscribe", "snapshot"]);
    assert.equal(page.elements["product-kind"].textContent, "macOS");
    assert.equal(page.elements["desktop-heading"].textContent, "Omnigent macOS app");
    assert.equal(page.elements["desktop-check"].hidden, true);
    assert.equal(page.elements["desktop-update-now"].hidden, false);
    assert.equal(
      page.elements["desktop-result"].textContent,
      "Desktop update 0.14.0 is available.",
    );

    await page.elements["desktop-update-now"].listeners.get("click")();
    assert.equal(page.calls.download, 1);
  });

  it("does not let a stale snapshot overwrite live download progress", async () => {
    let resolveSnapshot;
    const snapshot = new Promise((resolve) => {
      resolveSnapshot = resolve;
    });
    const page = makePage({ state: "idle" }, () => snapshot);

    page.emitStatus({ state: "downloading", progress: { percent: 37 } });
    resolveSnapshot({ state: "available", info: { version: "0.14.0" } });
    await flushPromises();

    assert.equal(page.elements["desktop-progress"].hidden, false);
    assert.equal(page.elements["desktop-progress-percent"].textContent, "37%");
    assert.equal(page.elements["desktop-update-now"].hidden, true);
  });

  it("does not let a stale check response overwrite live progress", async () => {
    const page = makePage();
    await flushPromises();
    let resolveCheck;
    page.api.checkDesktopUpdates = () =>
      new Promise((resolve) => {
        resolveCheck = resolve;
      });

    const click = page.elements["desktop-check"].listeners.get("click")();
    page.emitStatus({ state: "downloading", progress: { percent: 24 } });
    resolveCheck({ state: "available", info: { version: "0.14.0" } });
    await click;

    assert.equal(page.elements["desktop-progress"].hidden, false);
    assert.equal(page.elements["desktop-progress-percent"].textContent, "24%");
    assert.equal(page.elements["desktop-update-now"].hidden, true);
  });

  it("recovers from a failed download after progress has started", async () => {
    const page = makePage({ state: "available", info: { version: "0.14.0" } });
    await flushPromises();
    let rejectDownload;
    page.api.downloadDesktopUpdate = () =>
      new Promise((_resolve, reject) => {
        rejectDownload = reject;
      });

    const click = page.elements["desktop-update-now"].listeners.get("click")();
    page.emitStatus({ state: "downloading", progress: { percent: 42 } });
    rejectDownload(new Error("download interrupted"));
    await click;

    assert.equal(page.elements["desktop-progress"].hidden, true);
    assert.equal(page.elements["desktop-check"].hidden, false);
    assert.equal(page.elements["desktop-result"].textContent, "Unable to download the update.");
  });

  it("renders normalized download progress and then Restart to update", async () => {
    const page = makePage();
    await flushPromises();

    page.emitStatus({ state: "downloading", progress: { percent: 42.4 } });
    assert.equal(page.elements["desktop-actions"].hidden, true);
    assert.equal(page.elements["desktop-progress"].hidden, false);
    assert.equal(page.elements["desktop-progress"].attributes.get("aria-valuenow"), "42");
    assert.equal(page.elements["desktop-progress-percent"].textContent, "42%");
    assert.equal(page.elements["desktop-progress-bar"].style.transform, "scaleX(0.42)");

    page.emitStatus({ state: "downloading", progress: { percent: 140 } });
    assert.equal(page.elements["desktop-progress-percent"].textContent, "100%");
    page.emitStatus({ state: "downloading", progress: { percent: -20 } });
    assert.equal(page.elements["desktop-progress-percent"].textContent, "0%");
    page.emitStatus({ state: "downloading", progress: { percent: Number.NaN } });
    assert.equal(page.elements["desktop-progress-percent"].textContent, "0%");

    page.emitStatus({ state: "downloaded", info: { version: "0.14.0" } });
    assert.equal(page.elements["desktop-actions"].hidden, false);
    assert.equal(page.elements["desktop-progress"].hidden, true);
    assert.equal(page.elements["desktop-restart"].hidden, false);
    assert.equal(
      page.elements["desktop-result"].textContent,
      "Desktop update 0.14.0 is ready to install.",
    );

    await page.elements["desktop-restart"].listeners.get("click")();
    assert.equal(page.calls.install, 1);
  });
});
