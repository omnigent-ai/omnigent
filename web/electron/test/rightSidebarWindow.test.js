const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_SETTING,
  createRightSidebarWindow,
  narrowedBounds,
  widenedBounds,
} = require("../src/rightSidebarWindow");

const WORK_AREA = { x: 0, y: 25, width: 1920, height: 1055 };

function fakeWindow({
  bounds = { x: 100, y: 50, width: 800, height: 700 },
  minWidth = 720,
  zoom = 1,
  maximized = false,
  fullScreen = false,
} = {}) {
  const state = { bounds: { ...bounds }, maximized, fullScreen, setBounds: [] };
  const win = {
    state,
    isDestroyed: () => false,
    isResizable: () => true,
    isMaximized: () => state.maximized,
    isFullScreen: () => state.fullScreen,
    isSimpleFullScreen: () => false,
    // Maximized and full-screen windows cover the work area; the normal bounds are kept apart.
    getBounds: () => ({ ...(state.maximized || state.fullScreen ? WORK_AREA : state.bounds) }),
    getNormalBounds: () => ({ ...state.bounds }),
    getMinimumSize: () => [minWidth, 600],
    setBounds: (next, animate) => {
      state.setBounds.push({ bounds: { ...next }, animate });
      state.bounds = { ...next };
    },
    webContents: { getZoomFactor: () => zoom },
  };
  return win;
}

function harness({ enabled = true, saveFails = false, workArea = WORK_AREA } = {}) {
  let stored = enabled ? { [WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_SETTING]: true } : {};
  const controller = createRightSidebarWindow({
    loadSettings: () => ({ ...stored }),
    saveSettings: (next) => {
      if (saveFails) throw new Error("EACCES: permission denied");
      stored = { ...next };
    },
    workAreaFor: () => workArea,
  });
  return { controller, settings: () => stored };
}

describe("widenedBounds", () => {
  it("adds the width on the right when the screen has room", () => {
    assert.deepEqual(widenedBounds({ x: 100, y: 50, width: 800, height: 700 }, WORK_AREA, 500), {
      bounds: { x: 100, y: 50, width: 1300, height: 700 },
      growth: { added: 500, shiftX: 0 },
    });
  });

  it("moves the window left when its right edge would pass the screen's", () => {
    assert.deepEqual(widenedBounds({ x: 1000, y: 50, width: 800, height: 700 }, WORK_AREA, 500), {
      bounds: { x: 620, y: 50, width: 1300, height: 700 },
      growth: { added: 500, shiftX: -380 },
    });
  });

  it("caps the width at the work area", () => {
    assert.deepEqual(widenedBounds({ x: 300, y: 50, width: 1600, height: 700 }, WORK_AREA, 500), {
      bounds: { x: 0, y: 50, width: 1920, height: 700 },
      growth: { added: 320, shiftX: -300 },
    });
  });

  it("returns null when the window already fills the work area", () => {
    assert.equal(widenedBounds({ x: 0, y: 50, width: 1920, height: 700 }, WORK_AREA, 500), null);
  });
});

describe("narrowedBounds", () => {
  it("takes the added width off and keeps a resize made meanwhile", () => {
    const growth = { added: 500, shiftX: 0 };
    assert.deepEqual(
      narrowedBounds({ x: 100, y: 50, width: 1400, height: 700 }, WORK_AREA, growth, 720),
      {
        x: 100,
        y: 50,
        width: 900,
        height: 700,
      },
    );
  });

  it("moves a window that moved left to make room back", () => {
    const growth = { added: 500, shiftX: -380 };
    assert.deepEqual(
      narrowedBounds({ x: 620, y: 50, width: 1300, height: 700 }, WORK_AREA, growth, 720),
      {
        x: 1000,
        y: 50,
        width: 800,
        height: 700,
      },
    );
  });

  it("never goes below the window's minimum width", () => {
    const growth = { added: 500, shiftX: 0 };
    assert.equal(
      narrowedBounds({ x: 100, y: 50, width: 900, height: 700 }, WORK_AREA, growth, 720).width,
      720,
    );
  });
});

describe("createRightSidebarWindow", () => {
  it("is off by default and saves the setting", () => {
    const { controller, settings } = harness({ enabled: false });
    assert.equal(controller.enabled(), false);
    assert.equal(controller.setEnabled(true), true);
    assert.equal(controller.enabled(), true);
    assert.equal(settings()[WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_SETTING], true);
    assert.equal(controller.setEnabled(false), true);
    assert.equal(WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_SETTING in settings(), false);
  });

  it("reports a save failure", () => {
    const { controller } = harness({ enabled: false, saveFails: true });
    assert.equal(controller.setEnabled(true), false);
    assert.equal(controller.enabled(), false);
  });

  it("leaves the window alone while the setting is off", () => {
    const { controller } = harness({ enabled: false });
    const win = fakeWindow();
    assert.equal(controller.opened(win, 500), false);
    assert.equal(controller.closed(win), false);
    assert.deepEqual(win.state.setBounds, []);
  });

  it("widens by the sidebar width on open and shrinks back on close", () => {
    const { controller } = harness();
    const win = fakeWindow();
    assert.equal(controller.opened(win, 500), true);
    assert.deepEqual(win.getBounds(), { x: 100, y: 50, width: 1300, height: 700 });
    assert.equal(controller.closed(win), true);
    assert.deepEqual(win.getBounds(), { x: 100, y: 50, width: 800, height: 700 });
    // Only a window that grew shrinks.
    assert.equal(controller.closed(win), false);
  });

  it("converts the page's CSS width with the window's zoom", () => {
    const { controller } = harness();
    const win = fakeWindow({ zoom: 1.25 });
    controller.opened(win, 400);
    assert.equal(win.getBounds().width, 1300);
  });

  it("still shrinks by the added width after the user resized the open window", () => {
    const { controller } = harness();
    const win = fakeWindow();
    controller.opened(win, 500);
    win.state.bounds.width = 1450;
    controller.closed(win);
    assert.equal(win.getBounds().width, 950);
  });

  it("doesn't grow twice for a sidebar that is already open", () => {
    const { controller } = harness();
    const win = fakeWindow();
    controller.opened(win, 500);
    assert.equal(controller.opened(win, 500), false);
    assert.equal(win.getBounds().width, 1300);
  });

  it("keeps a maximized or full-screen window's size", () => {
    const { controller } = harness();
    for (const flags of [{ maximized: true }, { fullScreen: true }]) {
      const win = fakeWindow(flags);
      assert.equal(controller.opened(win, 500), false);
      assert.deepEqual(win.state.setBounds, []);
    }
  });

  it("shrinks a window whose sidebar closed in full screen or maximized once it's restored", () => {
    const { controller } = harness();
    for (const flag of ["fullScreen", "maximized"]) {
      const win = fakeWindow();
      controller.opened(win, 500);
      win.state[flag] = true;
      assert.equal(controller.closed(win), false);
      assert.equal(controller.restored(win), false);
      win.state[flag] = false;
      assert.equal(controller.restored(win), true);
      assert.equal(win.getBounds().width, 800);
      // Only once: later restores and closes leave the window alone.
      assert.equal(controller.restored(win), false);
      assert.equal(controller.closed(win), false);
      assert.equal(win.getBounds().width, 800);
    }
  });

  it("keeps the growth when the sidebar reopens before the window is restored", () => {
    const { controller } = harness();
    const win = fakeWindow();
    controller.opened(win, 500);
    win.state.fullScreen = true;
    controller.closed(win);
    assert.equal(controller.opened(win, 500), false);
    win.state.fullScreen = false;
    assert.equal(controller.restored(win), false);
    assert.equal(win.getBounds().width, 1300);
    assert.equal(controller.closed(win), true);
    assert.equal(win.getBounds().width, 800);
  });

  it("restores a widened window that filled the work area to its normal bounds", () => {
    const { controller } = harness();
    const win = fakeWindow({ bounds: { x: 0, y: 25, width: 1420, height: 1055 } });
    controller.opened(win, 500);
    assert.deepEqual(win.getBounds(), WORK_AREA);
    win.state.maximized = true;
    controller.closed(win);
    win.state.maximized = false;
    assert.equal(controller.restored(win), true);
    assert.deepEqual(win.getBounds(), { x: 0, y: 25, width: 1420, height: 1055 });
  });

  it("forgets growth so a new page or session uses the current width as its baseline", () => {
    const { controller } = harness();
    for (const fullScreenClose of [false, true]) {
      const win = fakeWindow();
      controller.opened(win, 500);
      if (fullScreenClose) {
        win.state.fullScreen = true;
        controller.closed(win);
        win.state.fullScreen = false;
      }
      controller.forget(win);
      assert.equal(controller.restored(win), false);
      assert.equal(controller.closed(win), false);
      assert.equal(win.getBounds().width, 1300);
      // The new page's first open widens the window again.
      assert.equal(controller.opened(win, 500), true);
      assert.equal(win.getBounds().width, 1800);
      assert.equal(controller.closed(win), true);
      assert.equal(win.getBounds().width, 1300);
    }
  });

  it("ignores restores while the sidebar is open", () => {
    const { controller } = harness();
    const win = fakeWindow();
    controller.opened(win, 500);
    assert.equal(controller.restored(win), false);
    assert.equal(win.getBounds().width, 1300);
  });

  it("ignores widths that aren't a sidebar's", () => {
    const { controller } = harness();
    const win = fakeWindow();
    for (const width of [0, -10, Number.NaN, Number.POSITIVE_INFINITY, 20000, "500"]) {
      assert.equal(controller.opened(win, width), false);
    }
    assert.deepEqual(win.state.setBounds, []);
  });

  it("keeps working when the setting turns off while the window is wide", () => {
    const { controller } = harness();
    const win = fakeWindow();
    controller.opened(win, 500);
    controller.setEnabled(false);
    assert.equal(controller.closed(win), true);
    assert.equal(win.getBounds().width, 800);
  });
});
