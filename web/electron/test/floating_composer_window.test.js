"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const {
  createFloatingComposerController,
  fadeInFloatingComposer,
  floatingComposerAppPagePath,
  floatingComposerSessionPath,
  floatingComposerWindowOptions,
} = require("../src/floating_composer_window");

class FakePanel extends EventEmitter {
  constructor(serverUrl) {
    super();
    this.serverUrl = serverUrl;
    this.visible = false;
    this.destroyed = false;
    this.calls = [];
    this.bounds = { x: 0, y: 0, width: 620, height: 360 };
    this.webContents = Object.assign(new EventEmitter(), {
      focus: () => this.calls.push("webContents.focus"),
    });
  }
  isDestroyed() {
    return this.destroyed;
  }
  isVisible() {
    return this.visible;
  }
  setAlwaysOnTop(flag, level) {
    this.calls.push(`setAlwaysOnTop:${flag}:${level}`);
  }
  setVisibleOnAllWorkspaces(flag, options) {
    this.calls.push(`setVisibleOnAllWorkspaces:${flag}:${options.visibleOnFullScreen}`);
  }
  setBounds(bounds) {
    this.bounds = bounds;
    this.calls.push("setBounds");
  }
  getBounds() {
    return this.bounds;
  }
  showInactive() {
    this.visible = true;
    this.calls.push("showInactive");
  }
  show() {
    this.visible = true;
    this.calls.push("show");
  }
  hide() {
    this.visible = false;
    this.calls.push("hide");
  }
  moveTop() {
    this.calls.push("moveTop");
  }
  focus() {
    this.calls.push("focus");
  }
  /** As Electron does: "close" can be cancelled, and only an uncancelled one closes. */
  close() {
    let prevented = false;
    this.emit("close", { preventDefault: () => (prevented = true) });
    if (!prevented) this.destroy();
  }
  destroy() {
    this.destroyed = true;
    this.calls.push("destroy");
    this.emit("closed");
  }
}

const laptop = { id: 1, workArea: { x: 0, y: 25, width: 1512, height: 920 } };
const external = { id: 2, workArea: { x: 1512, y: 0, width: 2560, height: 1415 } };

function setup({ present, quitting = () => false, saveFrames, loadFrames } = {}) {
  const created = [];
  const stale = new Set();
  let frames = {};
  const controller = createFloatingComposerController({
    createPanel: (serverUrl) => {
      const panel = new FakePanel(serverUrl);
      created.push(panel);
      return panel;
    },
    screen: {
      getAllDisplays: () => [laptop, external],
      getCursorScreenPoint: () => ({ x: 2000, y: 100 }),
      getDisplayNearestPoint: () => external,
    },
    loadFrames: loadFrames ?? (() => frames),
    saveFrames:
      saveFrames ??
      ((next) => {
        frames = next;
      }),
    isCurrent: (panel) => !stale.has(panel),
    quitting,
    ...(present ? { present } : {}),
  });
  return { controller, created, stale, frames: () => frames };
}

test("panel options float without a frame at the panel's sizes, outside the Window menu", () => {
  const options = floatingComposerWindowOptions();
  assert.equal(options.type, "panel");
  assert.equal(options.frame, false);
  assert.equal(options.show, false);
  assert.equal(options.excludedFromShownWindowsMenu, true);
  // See-through: the page paints its own tint.
  assert.equal(options.transparent, true);
  assert.equal(options.backgroundColor, "#00000000");
  assert.deepEqual(
    [options.minWidth, options.minHeight, options.maxWidth, options.maxHeight],
    [620, 300, 1000, 800],
  );
});

test("shows the panel on the pointer's display without activating the app", () => {
  const { controller, created } = setup();
  assert.equal(controller.show("https://a.example"), "shown");
  const [panel] = created;
  assert.deepEqual(panel.calls.slice(0, 2), [
    "setAlwaysOnTop:true:floating",
    "setVisibleOnAllWorkspaces:true:true",
  ]);
  assert.ok(panel.calls.includes("showInactive"));
  assert.ok(!panel.calls.includes("show"), "show() would activate the app");
  assert.ok(!panel.calls.includes("focus"), "focus() would activate the app");
  assert.equal(panel.bounds.x >= external.workArea.x, true);
});

test("a hidden panel comes on screen through present; showing a visible one focuses it", () => {
  const presented = [];
  const { controller, created } = setup({
    present: (panel, show) => {
      presented.push(panel);
      show();
    },
  });
  controller.show("https://a.example");
  const [panel] = created;
  assert.deepEqual(presented, [panel]);
  assert.ok(panel.calls.includes("showInactive"));
  panel.calls.length = 0;
  assert.equal(controller.show("https://a.example"), "focused");
  assert.deepEqual(panel.calls, ["moveTop", "focus", "webContents.focus"]);
  assert.equal(presented.length, 1);
  assert.equal(panel.visible, true, "a second press never closes the panel");
});

test("keeps one panel per server identity", () => {
  const { controller, created } = setup();
  controller.show("https://a.example");
  controller.show("https://b.example/mount");
  controller.show("https://a.example");
  assert.deepEqual(
    created.map((panel) => panel.serverUrl),
    ["https://a.example", "https://b.example/mount"],
  );
  assert.equal(controller.serverUrlFor(created[1]), "https://b.example/mount");
  assert.equal(controller.serverUrlFor({}), null);
});

test("focusing an Omnigent window hides visible panels and remembers where they were", () => {
  const { controller, created, frames } = setup();
  controller.show("https://a.example");
  const [panel] = created;
  panel.bounds = { x: 1600, y: 50, width: 700, height: 400 };
  controller.mainWindowFocused();
  assert.equal(panel.visible, false);
  assert.equal(panel.destroyed, false);
  assert.deepEqual(frames()["https://a.example"], panel.bounds);
  controller.show("https://a.example");
  assert.deepEqual(panel.bounds, { x: 1600, y: 50, width: 700, height: 400 });
});

test("an open file picker keeps the panel up when focus moves", () => {
  const { controller, created } = setup();
  controller.show("https://a.example");
  const [panel] = created;
  controller.filePickerOpened(panel);
  controller.mainWindowFocused();
  assert.equal(panel.visible, true);
  controller.filePickerClosed(panel);
  controller.mainWindowFocused();
  assert.equal(panel.visible, false);
});

for (const [event, what] of [
  ["did-navigate", "reloaded"],
  ["render-process-gone", "lost its renderer"],
]) {
  test(`a picker its page left open stops keeping the panel up once the page ${what}`, () => {
    const { controller, created } = setup();
    controller.show("https://a.example");
    const [panel] = created;
    controller.filePickerOpened(panel);
    panel.webContents.emit(event);
    controller.mainWindowFocused();
    assert.equal(panel.visible, false);
  });
}

test("hide keeps the panel for its draft and remembers where it was", () => {
  const { controller, created, frames } = setup();
  controller.show("https://a.example");
  const [panel] = created;
  panel.bounds = { x: 1600, y: 100, width: 900, height: 650 };
  assert.equal(controller.hide(panel), true);
  assert.equal(panel.destroyed, false);
  assert.deepEqual(frames()["https://a.example"], panel.bounds);
  assert.equal(controller.hide({}), false);
});

test("a frame that can't be saved never keeps the panel up", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  t.mock.method(console, "warn", () => {});
  const { controller, created } = setup({
    saveFrames: () => {
      throw new Error("disk full");
    },
  });
  controller.show("https://a.example");
  const [panel] = created;
  assert.equal(controller.hide(panel), true);
  assert.equal(panel.visible, false);
  controller.show("https://a.example");
  controller.mainWindowFocused();
  assert.equal(panel.visible, false);
  controller.show("https://a.example");
  panel.emit("moved");
  assert.doesNotThrow(() => t.mock.timers.tick(300));
  controller.hide(panel, { resetFrame: true });
  assert.equal(panel.visible, false);
});

for (const [what, loadFrames] of [
  ["can't be read", () => JSON.parse("{")],
  ["isn't an object", () => null],
]) {
  test(`remembered frames that ${what} never stop the panel opening or hiding`, () => {
    const { controller, created } = setup({ loadFrames });
    assert.equal(controller.show("https://a.example"), "shown");
    const [panel] = created;
    assert.equal(controller.hide(panel), true);
    assert.equal(panel.visible, false);
  });
}

test("a picker its page never reported closed stops keeping the panel up once it hides", () => {
  const { controller, created } = setup();
  controller.show("https://a.example");
  const [panel] = created;
  controller.filePickerOpened(panel);
  controller.hide(panel);
  controller.show("https://a.example");
  controller.mainWindowFocused();
  assert.equal(panel.visible, false);
});

test("closing a panel that was replaced leaves the one that replaced it", () => {
  const { controller, created, stale } = setup();
  controller.show("https://a.example");
  stale.add(created[0]);
  controller.show("https://a.example");
  const [old, replacement] = created;
  controller.close(old);
  assert.equal(replacement.destroyed, false);
  assert.equal(controller.isPanel(replacement), true);
  controller.close(replacement);
  assert.equal(replacement.destroyed, true);
});

test("closing the panel hides it, keeping its draft, unless the app is quitting", () => {
  let quitting = false;
  const { controller, created } = setup({ quitting: () => quitting });
  controller.show("https://a.example");
  const [panel] = created;
  panel.close();
  assert.equal(panel.visible, false);
  assert.equal(panel.destroyed, false);
  assert.equal(controller.isPanel(panel), true);
  quitting = true;
  panel.close();
  assert.equal(panel.destroyed, true);
  assert.equal(controller.isPanel(panel), false);
});

test("hiding after a launch resets the frame, even with a move still waiting to save", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { controller, created, frames } = setup();
  controller.show("https://a.example");
  const [panel] = created;
  panel.bounds = { x: 1600, y: 100, width: 900, height: 650 };
  controller.hide(panel);
  controller.show("https://a.example");
  panel.emit("resized");
  controller.hide(panel, { resetFrame: true });
  t.mock.timers.tick(300);
  assert.equal(frames()["https://a.example"], undefined);
});

test("a panel that left its page is rebuilt instead of reopened", () => {
  const { controller, created, stale } = setup();
  controller.show("https://a.example");
  stale.add(created[0]);
  controller.show("https://a.example");
  assert.equal(created.length, 2);
  assert.equal(created[0].destroyed, true);
  assert.equal(created[1].visible, true);
});

test("sign-out closes that server's panels; the next open builds a fresh one", () => {
  const { controller, created } = setup();
  controller.show("https://a.example/ws");
  controller.show("https://b.example");
  // By origin, as sign-out matches them.
  controller.closePanels((serverUrl) => new URL(serverUrl).origin === "https://a.example");
  assert.deepEqual(
    created.map((panel) => panel.destroyed),
    [true, false],
  );
  controller.show("https://a.example/ws");
  assert.equal(created.length, 3);
});

test("a closed panel is rebuilt on the next open", () => {
  const { controller, created } = setup();
  controller.show("https://a.example");
  created[0].destroy();
  controller.show("https://a.example");
  assert.equal(created.length, 2);
  assert.equal(controller.isPanel(created[0]), false);
  assert.equal(controller.isPanel(created[1]), true);
});

test("is unavailable without a server", () => {
  const { controller, created } = setup();
  assert.equal(controller.show(""), "unavailable");
  assert.equal(created.length, 0);
});

test("only a session route may be opened from the panel", () => {
  assert.equal(floatingComposerSessionPath("/c/sess_123-abc"), "/c/sess_123-abc");
  for (const bad of [
    "/settings",
    "/c/",
    "/c/a/b",
    "https://evil.example/c/x",
    "/c/x?y=1",
    7,
    null,
  ]) {
    assert.equal(floatingComposerSessionPath(bad), null, String(bad));
  }
});

test("the panel's links open only the app pages it offers", () => {
  for (const page of ["/settings/integrations", "/settings/import", "/settings/general"]) {
    assert.equal(floatingComposerAppPagePath(page), page);
  }
  for (const bad of ["/settings", "/settings/general?x=1", "/settings/account", "/", 7, null]) {
    assert.equal(floatingComposerAppPagePath(bad), null, String(bad));
  }
});

function fadingPanel() {
  return {
    opacities: [],
    destroyed: false,
    setOpacity(value) {
      this.opacities.push(value);
    },
    isDestroyed() {
      return this.destroyed;
    },
  };
}

function manualClock() {
  let time = 0;
  const timers = [];
  return {
    now: () => time,
    setTimer: (callback, ms) => timers.push({ callback, at: time + ms }),
    advance(ms) {
      time += ms;
      for (const timer of timers.splice(0)) {
        if (timer.at <= time) timer.callback();
        else timers.push(timer);
      }
    },
    pending: () => timers.length,
  };
}

test("an opening panel starts clear, comes on screen, then eases to fully opaque", () => {
  const panel = fadingPanel();
  const clock = manualClock();
  const order = [];
  fadeInFloatingComposer(panel, () => order.push(`show at ${panel.opacities.at(-1)}`), {
    reduceMotion: false,
    durationMs: 120,
    ...clock,
  });
  assert.deepEqual(order, ["show at 0"]);
  for (let elapsed = 0; elapsed < 60; elapsed += 10) clock.advance(10);
  // Eases out: well past halfway by the time half the duration has gone.
  const halfway = panel.opacities.at(-1);
  assert.ok(halfway > 0.8 && halfway < 1, `opacity at 60 ms: ${halfway}`);
  for (let step = 0; step < 20 && clock.pending() > 0; step += 1) clock.advance(10);
  assert.equal(panel.opacities.at(-1), 1);
  assert.equal(clock.pending(), 0);
});

test("with Reduce motion the panel just appears, fully opaque", () => {
  const panel = fadingPanel();
  const clock = manualClock();
  let shown = false;
  fadeInFloatingComposer(panel, () => (shown = true), { reduceMotion: true, ...clock });
  assert.equal(shown, true);
  assert.deepEqual(panel.opacities, [1]);
  assert.equal(clock.pending(), 0);
});

test("a panel destroyed mid-fade stops fading", () => {
  const panel = fadingPanel();
  const clock = manualClock();
  fadeInFloatingComposer(panel, () => {}, { reduceMotion: false, durationMs: 120, ...clock });
  clock.advance(10);
  panel.destroyed = true;
  const steps = panel.opacities.length;
  clock.advance(10);
  assert.equal(panel.opacities.length, steps);
  assert.equal(clock.pending(), 0);
});

test("reopening mid-fade stops the earlier fade, so the opacity never goes backwards", () => {
  const panel = fadingPanel();
  const clock = manualClock();
  const options = { reduceMotion: false, durationMs: 120, ...clock };
  fadeInFloatingComposer(panel, () => {}, options);
  clock.advance(60);
  fadeInFloatingComposer(panel, () => {}, options);
  const restartedAt = panel.opacities.length - 1;
  for (let step = 0; step < 30 && clock.pending() > 0; step += 1) clock.advance(10);
  const second = panel.opacities.slice(restartedAt);
  assert.equal(second[0], 0);
  for (let index = 1; index < second.length; index += 1) {
    assert.ok(second[index] >= second[index - 1], `opacity fell: ${second.join(", ")}`);
  }
  assert.equal(panel.opacities.at(-1), 1);
});

test("Reduce motion stops a fade still running from an earlier show", () => {
  const panel = fadingPanel();
  const clock = manualClock();
  fadeInFloatingComposer(panel, () => {}, { reduceMotion: false, durationMs: 120, ...clock });
  clock.advance(10);
  const before = panel.opacities.length;
  fadeInFloatingComposer(panel, () => {}, { reduceMotion: true, ...clock });
  for (let step = 0; step < 20 && clock.pending() > 0; step += 1) clock.advance(10);
  assert.deepEqual(panel.opacities.slice(before), [1]);
});
