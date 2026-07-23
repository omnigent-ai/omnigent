const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  ArtifactSurfaceManager,
  navigationAllowed,
  normalizeArtifactBounds,
  parseArtifactPreviewUrl,
} = require("../src/artifact_surface");

function fakeWindow() {
  const children = [];
  return {
    id: 7,
    contentView: {
      addChildView(view) {
        children.push(view);
      },
      removeChildView(view) {
        const index = children.indexOf(view);
        if (index >= 0) children.splice(index, 1);
      },
    },
    getContentBounds: () => ({ x: 0, y: 0, width: 900, height: 700 }),
    children,
  };
}

function fakeViewFactory(created) {
  return (options) => {
    const listeners = new Map();
    const webContents = {
      options,
      loaded: [],
      closed: false,
      setWindowOpenHandler: (handler) => {
        webContents.windowOpenHandler = handler;
      },
      on: (event, handler) => listeners.set(event, handler),
      loadURL: async (url) => webContents.loaded.push(url),
      executeJavaScript: async () => ({ x: 42, y: 84 }),
      inspectElement: (x, y) => {
        webContents.inspected = { x, y };
      },
      close: () => {
        webContents.closed = true;
      },
    };
    const view = {
      webContents,
      bounds: null,
      visible: true,
      setBounds: (bounds) => {
        view.bounds = bounds;
      },
      setVisible: (visible) => {
        view.visible = visible;
      },
      emit: (event, ...args) => listeners.get(event)?.(...args),
    };
    created.push(view);
    return view;
  };
}

describe("artifact preview URL policy", () => {
  it("accepts capability-scoped HTTP URLs", () => {
    expectPreview(
      parseArtifactPreviewUrl("http://preview.localhost:6767/p/token-1/artifacts/a/index.html"),
      {
        origin: "http://preview.localhost:6767",
        capabilityPrefix: "/p/token-1/",
      },
    );
  });

  it("rejects non-capability and non-http URLs", () => {
    assert.throws(() => parseArtifactPreviewUrl("http://preview.localhost:6767/artifacts/a"));
    assert.throws(() => parseArtifactPreviewUrl("file:///tmp/index.html"));
  });

  it("allows only the original origin and capability prefix", () => {
    const policy = parseArtifactPreviewUrl(
      "http://preview.localhost:6767/p/token-1/artifacts/a/index.html",
    );
    assert.equal(
      navigationAllowed("http://preview.localhost:6767/p/token-1/artifacts/a/app.js", policy),
      true,
    );
    assert.equal(
      navigationAllowed("http://preview.localhost:6767/p/token-2/artifacts/a/index.html", policy),
      false,
    );
    assert.equal(
      navigationAllowed("http://localhost:6767/p/token-1/artifacts/a/index.html", policy),
      false,
    );
  });
});

describe("artifact bounds", () => {
  it("rounds and clamps renderer bounds to the window content", () => {
    assert.deepEqual(
      normalizeArtifactBounds(
        { x: -4.4, y: 20.2, width: 1000.9, height: 800 },
        { width: 900, height: 700 },
      ),
      { x: 0, y: 20, width: 900, height: 680 },
    );
  });
});

describe("ArtifactSurfaceManager", () => {
  it("creates an isolated surface and blocks navigation outside its grant", async () => {
    const win = fakeWindow();
    const created = [];
    const deniedPermissions = [];
    const manager = new ArtifactSurfaceManager({
      createView: fakeViewFactory(created),
      configureSession: (ses) => deniedPermissions.push(ses),
    });

    await manager.sync(win, {
      id: "artifact-1",
      url: "http://preview.localhost:6767/p/grant-a/artifacts/a/index.html",
      visible: true,
      bounds: { x: 10, y: 20, width: 500, height: 400 },
    });

    const view = created[0];
    assert.equal(win.children[0], view);
    assert.deepEqual(view.bounds, { x: 10, y: 20, width: 500, height: 400 });
    assert.equal(view.webContents.options.webPreferences.contextIsolation, true);
    assert.equal(view.webContents.options.webPreferences.nodeIntegration, false);
    assert.equal(view.webContents.options.webPreferences.sandbox, true);
    assert.match(
      view.webContents.options.webPreferences.partition,
      /^omnigent-artifact-preview-7-/,
    );
    assert.equal(deniedPermissions.length, 1);
    assert.deepEqual(view.webContents.windowOpenHandler(), { action: "deny" });

    const event = {
      prevented: false,
      preventDefault() {
        this.prevented = true;
      },
    };
    view.emit(
      "will-navigate",
      event,
      "http://preview.localhost:6767/p/grant-b/artifacts/b/index.html",
    );
    assert.equal(event.prevented, true);
  });

  it("ignores stale destroy ids and cleans up the active view", async () => {
    const win = fakeWindow();
    const created = [];
    const manager = new ArtifactSurfaceManager({ createView: fakeViewFactory(created) });
    await manager.sync(win, {
      id: "current",
      url: "http://preview.localhost:6767/p/grant-a/artifacts/a/index.html",
      visible: true,
      bounds: { x: 0, y: 0, width: 100, height: 100 },
    });

    manager.destroy(win, "stale");
    assert.equal(created[0].webContents.closed, false);
    manager.destroy(win, "current");
    assert.equal(created[0].webContents.closed, true);
    assert.equal(win.children.length, 0);
  });

  it("destroys the surface during window cleanup", async () => {
    const win = fakeWindow();
    const created = [];
    const manager = new ArtifactSurfaceManager({ createView: fakeViewFactory(created) });
    await manager.sync(win, {
      id: "current",
      url: "http://preview.localhost:6767/p/grant-a/artifacts/a/index.html",
      visible: true,
      bounds: { x: 0, y: 0, width: 100, height: 100 },
    });
    manager.destroyWindow(win);
    assert.equal(created[0].webContents.closed, true);
    assert.equal(win.children.length, 0);
  });

  it("runs the picker and inspects the selected node", async () => {
    const win = fakeWindow();
    const created = [];
    const manager = new ArtifactSurfaceManager({ createView: fakeViewFactory(created) });
    await manager.sync(win, {
      id: "current",
      url: "http://preview.localhost:6767/p/grant-a/artifacts/a/index.html",
      visible: true,
      bounds: { x: 0, y: 0, width: 100, height: 100 },
    });

    assert.equal(await manager.inspect(win, "stale"), false);
    assert.equal(await manager.inspect(win, "current"), true);
    assert.deepEqual(created[0].webContents.inspected, { x: 42, y: 84 });
  });
});

function expectPreview(actual, expected) {
  assert.equal(actual.origin, expected.origin);
  assert.equal(actual.capabilityPrefix, expected.capabilityPrefix);
}
