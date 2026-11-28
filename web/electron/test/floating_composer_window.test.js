"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const {
  FLOATING_COMPOSER_PRELOAD_DELAY_MS,
  FLOATING_COMPOSER_READY_FALLBACK_MS,
  FLOATING_COMPOSER_SHOWN_CHANNEL,
  createFloatingComposerController,
  createFloatingComposerPreloader,
  fadeInFloatingComposer,
  floatingComposerAppPagePath,
  floatingComposerLaunchMessage,
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
      send: (channel) => this.calls.push(`send:${channel}`),
      isDestroyed: () => this.destroyed,
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

function setup({ present, quitting = () => false, saveFrames, loadFrames, autoReady = true } = {}) {
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
  if (autoReady) {
    // These panels' pages have loaded: the one just opened says it's ready as soon as it exists.
    const show = controller.show;
    controller.show = (serverUrl) => {
      const result = show(serverUrl);
      const opened = created.findLast((panel) => panel.serverUrl === serverUrl && !panel.destroyed);
      if (opened) controller.markReady(opened);
      return result;
    };
  }
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
  // Taking the keyboard back is the same call as on open, never one that activates the app.
  assert.deepEqual(panel.calls, ["webContents.focus", `send:${FLOATING_COMPOSER_SHOWN_CHANNEL}`]);
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

test("a launch's first message is rebuilt from its checked fields, and capped in total", () => {
  const file = (size) => ({ name: "a.txt", type: "text/plain", bytes: new Uint8Array(size) });
  const rebuilt = floatingComposerLaunchMessage({
    text: "Read this",
    skill: null,
    files: [{ ...file(3), extra: "dropped" }],
    extra: "dropped",
  });
  assert.deepEqual(rebuilt, { text: "Read this", skill: null, files: [file(3)] });
  // A skill launch carries its name and arguments, never attachments.
  assert.deepEqual(
    floatingComposerLaunchMessage({
      text: "/review 1",
      skill: { name: "review", args: "1", extra: true },
      files: [file(3)],
    }),
    { text: "/review 1", skill: { name: "review", args: "1" }, files: [] },
  );
  // A blank skill launch has nothing a page could send, so it is never offered.
  assert.equal(
    floatingComposerLaunchMessage({ text: " ", skill: { name: "review", args: "" }, files: [] }),
    null,
  );
  const cap = 25 * 1024 * 1024;
  assert.notEqual(
    floatingComposerLaunchMessage({ text: "x", skill: null, files: [file(cap)] }),
    null,
  );
  assert.equal(
    floatingComposerLaunchMessage({
      text: "x",
      skill: null,
      files: [file(cap / 2), file(cap / 2 + 1)],
    }),
    null,
    "one byte past the cap across files",
  );
  for (const bad of [
    null,
    "text",
    { text: 1, skill: null, files: [] },
    { text: "x", skill: "review", files: [] },
    { text: "x", skill: null, files: "a.txt" },
    { text: "x", skill: null, files: [{ name: "a", type: "t", bytes: "xx" }] },
    { text: "  ", skill: null, files: [] },
  ]) {
    assert.equal(floatingComposerLaunchMessage(bad), null, JSON.stringify(bad));
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

test("an open waits for a loading page to say it's ready, then shows it", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { controller, created } = setup({ autoReady: false });
  assert.equal(controller.show("https://a.example"), "shown");
  const [panel] = created;
  assert.equal(panel.visible, false, "its page is still loading");
  // A second press while it waits doesn't queue another open.
  assert.equal(controller.show("https://a.example"), "shown");
  controller.markReady(panel);
  assert.equal(panel.visible, true);
  assert.equal(panel.calls.filter((call) => call === "showInactive").length, 1);
  t.mock.timers.tick(FLOATING_COMPOSER_READY_FALLBACK_MS);
  assert.equal(panel.calls.filter((call) => call === "showInactive").length, 1);
});

test("a page that never says it's ready still shows after a moment, then reopens at once", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { controller, created } = setup({ autoReady: false });
  controller.show("https://a.example");
  t.mock.timers.tick(FLOATING_COMPOSER_READY_FALLBACK_MS - 1);
  assert.equal(created[0].visible, false);
  t.mock.timers.tick(1);
  assert.equal(created[0].visible, true);
  // That document has been on screen; hiding and reopening it needs no wait.
  controller.hide(created[0]);
  controller.show("https://a.example");
  assert.equal(created[0].visible, true);
});

test("an open takes a preloaded panel out of the pool a release or another preload may drop", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { controller, created } = setup({ autoReady: false });
  controller.preload("https://a.example");
  controller.show("https://a.example");
  controller.releaseUnshown("https://a.example");
  controller.preload("https://b.example");
  assert.equal(created[0].destroyed, false, "the user is waiting for this one");
  controller.markReady(created[0]);
  assert.equal(created[0].visible, true);
});

test("opening another server calls off an open still waiting on its page", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { controller, created } = setup({ autoReady: false });
  controller.show("https://a.example");
  controller.preload("https://b.example");
  controller.markReady(created[1]);
  controller.show("https://b.example");
  assert.equal(created[1].visible, true);
  // A's page answering late, or its fallback, must not pop it up over B.
  controller.markReady(created[0]);
  t.mock.timers.tick(FLOATING_COMPOSER_READY_FALLBACK_MS);
  assert.equal(created[0].visible, false);
  // A's open is over too: once B goes, a press doesn't go back to A.
  controller.hide(created[1]);
  assert.equal(controller.openServerUrl(), null);
});

test("opening one server lets go of another server's panel built ahead", () => {
  const { controller, created } = setup({ autoReady: false });
  controller.preload("https://a.example");
  controller.show("https://b.example");
  assert.equal(created[0].destroyed, true);
  assert.equal(created[1].destroyed, false);
});

test("a ready panel shows at once, and readiness ends with its document", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { controller, created } = setup({ autoReady: false });
  controller.preload("https://a.example");
  const [panel] = created;
  controller.markReady(panel);
  controller.show("https://a.example");
  assert.equal(panel.visible, true, "a ready page needs no wait");
  for (const event of ["did-navigate", "render-process-gone"]) {
    controller.hide(panel);
    controller.markReady(panel);
    panel.webContents.emit(event);
    controller.show("https://a.example");
    assert.equal(panel.visible, false, `after ${event} the new document must say it again`);
    controller.markReady(panel);
    assert.equal(panel.visible, true);
  }
});

for (const [how, end] of [
  ["putting it away", (controller, panel) => controller.hide(panel)],
  ["closing it", (controller, panel) => controller.close(panel)],
]) {
  test(`${how} drops an open still waiting on its page`, (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const { controller, created } = setup({ autoReady: false });
    controller.show("https://a.example");
    const [panel] = created;
    end(controller, panel);
    controller.markReady(panel);
    t.mock.timers.tick(FLOATING_COMPOSER_READY_FALLBACK_MS);
    assert.equal(panel.calls.includes("showInactive"), false);
    assert.equal(panel.calls.includes("webContents.focus"), false);
    // Nor is its page told it took the keyboard.
    assert.equal(
      panel.calls.some((call) => call.startsWith("send:")),
      false,
    );
  });
}

test("focusing an Omnigent window drops an open still waiting on its page", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { controller, created } = setup({ autoReady: false });
  controller.show("https://a.example");
  controller.mainWindowFocused();
  controller.markReady(created[0]);
  t.mock.timers.tick(FLOATING_COMPOSER_READY_FALLBACK_MS);
  assert.equal(created[0].visible, false);
});

test("preloading builds the panel hidden; the next open reuses it", () => {
  const { controller, created } = setup({ autoReady: false });
  controller.preload("https://a.example");
  assert.equal(created.length, 1);
  assert.equal(created[0].visible, false);
  controller.preload("https://a.example");
  assert.equal(created.length, 1, "a current panel isn't built twice");
  controller.markReady(created[0]);
  controller.show("https://a.example");
  assert.equal(created.length, 1);
  assert.equal(created[0].visible, true);
});

test("at most one panel waits unshown: preloading another server lets the first go", () => {
  const { controller, created } = setup({ autoReady: false });
  controller.preload("https://a.example");
  controller.preload("https://b.example");
  assert.equal(created[0].destroyed, true);
  assert.equal(created[1].destroyed, false);
  // A panel that was shown keeps its draft when another server preloads.
  controller.markReady(created[1]);
  controller.show("https://b.example");
  controller.preload("https://c.example");
  assert.equal(created[1].destroyed, false);
});

test("releasing a server lets go of its unshown panel, never one that was shown", () => {
  const { controller, created } = setup({ autoReady: false });
  controller.preload("https://a.example");
  controller.releaseUnshown("https://a.example");
  assert.equal(created[0].destroyed, true);
  controller.preload("https://b.example");
  controller.markReady(created[1]);
  controller.show("https://b.example");
  controller.releaseUnshown("https://b.example");
  assert.equal(created[1].destroyed, false, "a shown panel holds the user's draft");
});

test("the preloader builds for the current target once the app is quiet", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const built = [];
  let target = "https://a.example";
  const preloader = createFloatingComposerPreloader({
    target: () => target,
    preload: (serverUrl) => built.push(serverUrl),
  });
  preloader.schedule();
  t.mock.timers.tick(FLOATING_COMPOSER_PRELOAD_DELAY_MS - 1);
  // Another page loading restarts the wait.
  preloader.schedule();
  t.mock.timers.tick(FLOATING_COMPOSER_PRELOAD_DELAY_MS - 1);
  assert.deepEqual(built, []);
  t.mock.timers.tick(1);
  assert.deepEqual(built, ["https://a.example"]);
  // A press before the wait ends builds the panel itself.
  preloader.schedule();
  preloader.cancel();
  t.mock.timers.tick(FLOATING_COMPOSER_PRELOAD_DELAY_MS);
  assert.deepEqual(built, ["https://a.example"]);
  // The target is read when the wait ends: a page that stopped supporting it meanwhile builds nothing.
  preloader.schedule();
  target = null;
  t.mock.timers.tick(FLOATING_COMPOSER_PRELOAD_DELAY_MS);
  assert.deepEqual(built, ["https://a.example"]);
});

test("every open takes the keyboard the same way, and none of them activates the app", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { controller, created } = setup({ autoReady: false });
  const activating = (panel) =>
    panel.calls.filter((call) => ["show", "focus", "moveTop"].includes(call));
  // Ready at once (built ahead).
  controller.preload("https://a.example");
  controller.markReady(created[0]);
  controller.show("https://a.example");
  // Waiting for the page, then ready.
  controller.show("https://b.example");
  controller.markReady(created[1]);
  // Waiting for a page that never says so.
  controller.show("https://c.example");
  t.mock.timers.tick(FLOATING_COMPOSER_READY_FALLBACK_MS);
  // A second press while it's up.
  controller.show("https://c.example");
  for (const panel of created) {
    assert.equal(panel.visible, true);
    assert.deepEqual(activating(panel), [], `${panel.serverUrl} must not activate the app`);
    assert.ok(panel.calls.includes("webContents.focus"));
  }
});

test("a press while a panel is open goes back to it, until the open ends", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { controller, created } = setup({ autoReady: false });
  assert.equal(controller.openServerUrl(), null);
  controller.show("https://a.example");
  assert.equal(controller.openServerUrl(), "https://a.example", "waiting for its page counts");
  controller.markReady(created[0]);
  assert.equal(controller.openServerUrl(), "https://a.example");
  // Each exit ends the open: putting it away, launching, clicking an Omnigent window.
  for (const end of [
    () => controller.hide(created[0]),
    () => controller.hide(created[0], { resetFrame: true }),
    () => controller.mainWindowFocused(),
  ]) {
    controller.show("https://a.example");
    end();
    assert.equal(controller.openServerUrl(), null);
  }
});

test("each way the panel takes the keyboard tells its page right after, once", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { controller, created } = setup({ autoReady: false });
  const shown = `send:${FLOATING_COMPOSER_SHOWN_CHANNEL}`;
  // Ready at once, after ready, by fallback, and a second press.
  controller.preload("https://a.example");
  controller.markReady(created[0]);
  controller.show("https://a.example");
  controller.show("https://b.example");
  controller.markReady(created[1]);
  controller.show("https://c.example");
  t.mock.timers.tick(FLOATING_COMPOSER_READY_FALLBACK_MS);
  controller.show("https://c.example");
  for (const [panel, opens] of [
    [created[0], 1],
    [created[1], 1],
    [created[2], 2],
  ]) {
    assert.equal(panel.calls.filter((call) => call === shown).length, opens, panel.serverUrl);
    panel.calls.forEach((call, index) => {
      if (call === shown) assert.equal(panel.calls[index - 1], "webContents.focus");
    });
  }
});
