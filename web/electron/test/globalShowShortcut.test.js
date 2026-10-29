const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const { createGlobalShowShortcut } = require("../src/globalShowShortcut");

/**
 * A settings store and a fake GlobalShortcut. `refused` chords make register
 * return false; `throws` chords make it throw; `saveFails` makes saving throw.
 */
function harness({
  settings = {},
  refused = [],
  throws = [],
  saveFails = false,
  isMac = false,
} = {}) {
  let stored = structuredClone(settings);
  const calls = { registered: [], unregistered: [], saved: [], shown: 0 };
  const live = new Set();
  const globalShortcut = {
    register: (accelerator, press) => {
      if (throws.includes(accelerator)) throw new TypeError(`Invalid accelerator: ${accelerator}`);
      if (refused.includes(accelerator)) return false;
      calls.registered.push({ accelerator, press });
      live.add(accelerator);
      return true;
    },
    unregister: (accelerator) => {
      calls.unregistered.push(accelerator);
      live.delete(accelerator);
    },
  };
  const shortcut = createGlobalShowShortcut({
    globalShortcut,
    loadSettings: () => structuredClone(stored),
    saveSettings: (next) => {
      if (saveFails) throw new Error("EACCES: permission denied");
      stored = structuredClone(next);
      calls.saved.push(structuredClone(next));
    },
    show: () => {
      calls.shown += 1;
    },
    isMac,
  });
  return { calls, shortcut, saved: () => stored, registered: () => [...live] };
}

describe("createGlobalShowShortcut", () => {
  it("is off by default and registers nothing", () => {
    const h = harness();
    assert.deepEqual(h.shortcut.current(), { enabled: false, accelerator: "Alt+Space" });
    h.shortcut.apply();
    assert.deepEqual(h.calls.registered, []);
  });

  it("registers the chord when enabled, and again on the next launch", () => {
    const first = harness();
    assert.deepEqual(first.shortcut.set({ enabled: true, accelerator: "Alt+Space" }), { ok: true });
    assert.deepEqual(
      first.calls.registered.map((entry) => entry.accelerator),
      ["Alt+Space"],
    );

    const nextLaunch = harness({ settings: first.saved() });
    nextLaunch.shortcut.apply();
    assert.deepEqual(
      nextLaunch.calls.registered.map((entry) => entry.accelerator),
      ["Alt+Space"],
    );
    nextLaunch.calls.registered[0].press();
    assert.equal(nextLaunch.calls.shown, 1);
  });

  it("moves the registration when the chord changes, and keeps the chord while off", () => {
    const h = harness();
    h.shortcut.set({ enabled: true, accelerator: "Alt+Space" });

    assert.deepEqual(h.shortcut.set({ enabled: true, accelerator: "Control+Shift+K" }), {
      ok: true,
    });
    assert.deepEqual(h.calls.unregistered, ["Alt+Space"]);
    assert.deepEqual(
      h.calls.registered.map((entry) => entry.accelerator),
      ["Alt+Space", "Control+Shift+K"],
    );

    h.shortcut.set({ enabled: false, accelerator: "Control+Shift+K" });
    assert.deepEqual(h.calls.unregistered, ["Alt+Space", "Control+Shift+K"]);
    assert.deepEqual(h.saved().global_shortcut, {
      enabled: false,
      accelerator: "Control+Shift+K",
    });
  });

  for (const [failure, options] of [
    ["refuses", { refused: ["Control+Shift+K"] }],
    ["throws on", { throws: ["Control+Shift+K"] }],
  ]) {
    it(`keeps the saved setting and its registration when Electron ${failure} the new chord`, () => {
      const h = harness({
        settings: { global_shortcut: { enabled: true, accelerator: "Alt+Space" } },
        ...options,
      });
      h.shortcut.apply();

      const result = h.shortcut.set({ enabled: true, accelerator: "Control+Shift+K" });

      assert.equal(result.ok, false);
      assert.match(result.error, /already in use/);
      // The saved chord is never released, so another app can't take it meanwhile.
      assert.deepEqual(h.calls.unregistered, []);
      assert.deepEqual(h.registered(), ["Alt+Space"]);
      assert.deepEqual(h.calls.saved, []);
      assert.deepEqual(h.saved().global_shortcut, { enabled: true, accelerator: "Alt+Space" });
    });
  }

  for (const [chord, recorded] of [
    ["a new chord", "Control+Shift+K"],
    ["the saved chord again", "Alt+Space"],
  ]) {
    it(`tries ${chord} recorded while off, and keeps the saved one when it can't be used`, () => {
      const h = harness({
        settings: { global_shortcut: { enabled: false, accelerator: "Alt+Space" } },
        refused: [recorded],
      });
      h.shortcut.apply();

      const result = h.shortcut.set({ enabled: false, accelerator: recorded });

      assert.equal(result.ok, false);
      assert.match(result.error, /already in use/);
      assert.deepEqual(h.calls.saved, []);
      assert.deepEqual(h.registered(), []);
    });
  }

  it("releases a chord it tried while the shortcut is off", () => {
    const h = harness();
    assert.deepEqual(h.shortcut.set({ enabled: false, accelerator: "Control+Shift+K" }), {
      ok: true,
    });
    assert.deepEqual(h.registered(), []);
    assert.deepEqual(h.saved().global_shortcut, {
      enabled: false,
      accelerator: "Control+Shift+K",
    });
  });

  it("turns off a shortcut whose chord another app took", () => {
    const h = harness({
      settings: { global_shortcut: { enabled: true, accelerator: "Alt+Space" } },
      refused: ["Alt+Space"],
    });
    h.shortcut.apply();
    assert.deepEqual(h.shortcut.set({ enabled: false, accelerator: "Alt+Space" }), { ok: true });
    assert.deepEqual(h.saved().global_shortcut, { enabled: false, accelerator: "Alt+Space" });
  });

  for (const [change, next] of [
    ["a new chord", { enabled: true, accelerator: "Control+Shift+K" }],
    ["turning it off", { enabled: false, accelerator: "Alt+Space" }],
  ]) {
    it(`keeps the previous chord registered when saving ${change} fails`, (t) => {
      t.mock.method(console, "warn", () => {});
      const h = harness({
        settings: { global_shortcut: { enabled: true, accelerator: "Alt+Space" } },
        saveFails: true,
      });
      h.shortcut.apply();

      const result = h.shortcut.set(next);

      assert.equal(result.ok, false);
      assert.match(result.error, /Couldn't save/);
      assert.deepEqual(h.registered(), ["Alt+Space"]);
      // It stayed registered throughout, so another app couldn't take it.
      assert.equal(h.calls.unregistered.includes("Alt+Space"), false);
    });
  }

  for (const [failure, options] of [
    ["refuses", { refused: ["Alt+Space"] }],
    ["throws on", { throws: ["Alt+Space"] }],
  ]) {
    it(`reports a saved shortcut that Electron ${failure} at launch as not working`, () => {
      const h = harness({
        settings: { global_shortcut: { enabled: true, accelerator: "Alt+Space" } },
        ...options,
      });
      h.shortcut.apply();

      const status = h.shortcut.current();
      assert.equal(status.enabled, true);
      assert.match(status.error, /isn't working/);
      assert.deepEqual(h.registered(), []);
    });
  }
});

describe("recording a chord", () => {
  /** Sends one key press through `before-input-event`; returns whether it was taken. */
  function press(contents, input) {
    let taken = false;
    const event = {
      preventDefault: () => {
        taken = true;
      },
    };
    contents.emit("before-input-event", event, {
      type: "keyDown",
      control: false,
      alt: false,
      shift: false,
      meta: false,
      ...input,
    });
    return taken;
  }

  it("takes the next chord before the page and the menu see it", async () => {
    const h = harness();
    const contents = new EventEmitter();
    const recorded = h.shortcut.record(contents);

    assert.equal(press(contents, { key: "Tab", code: "Tab" }), false);
    assert.equal(press(contents, { key: "Control", code: "ControlLeft", control: true }), false);
    assert.equal(press(contents, { key: "K", code: "KeyK", control: true, shift: true }), true);

    assert.equal(await recorded, "Control+Shift+K");
    assert.equal(contents.listenerCount("before-input-event"), 0);
  });

  it("names the Command key on macOS", async () => {
    const h = harness({ isMac: true });
    const contents = new EventEmitter();
    const recorded = h.shortcut.record(contents);
    press(contents, { key: " ", code: "Space", meta: true, alt: true });
    assert.equal(await recorded, "Alt+Command+Space");
  });

  it("ends when the page that asked navigates away or its renderer exits", async () => {
    const h = harness();
    const contents = new EventEmitter();

    const reloaded = h.shortcut.record(contents);
    // A same-page navigation (a hash change) or a frame's own navigation keeps recording.
    contents.emit("did-start-navigation", { isMainFrame: true, isSameDocument: true });
    contents.emit("did-start-navigation", { isMainFrame: false, isSameDocument: false });
    assert.equal(contents.listenerCount("before-input-event"), 1);
    contents.emit("did-start-navigation", { isMainFrame: true, isSameDocument: false });
    assert.equal(contents.listenerCount("before-input-event"), 0);
    assert.equal(await reloaded, null);
    // The replacement page's keys reach it.
    assert.equal(press(contents, { key: "K", code: "KeyK", control: true }), false);

    const crashed = h.shortcut.record(contents);
    contents.emit("render-process-gone", {}, { reason: "crashed" });
    assert.equal(contents.listenerCount("before-input-event"), 0);
    assert.equal(await crashed, null);
    for (const event of ["before-input-event", "did-start-navigation", "render-process-gone"]) {
      assert.equal(contents.listenerCount(event), 0, event);
    }
  });

  it("ends without a chord on Escape, a cancel, a newer recording, or a closed page", async () => {
    const h = harness();
    const contents = new EventEmitter();

    const escaped = h.shortcut.record(contents);
    assert.equal(press(contents, { key: "Escape", code: "Escape" }), true);
    assert.equal(await escaped, null);

    const cancelled = h.shortcut.record(contents);
    h.shortcut.cancelRecording();
    assert.equal(await cancelled, null);

    const replaced = h.shortcut.record(contents);
    const closed = h.shortcut.record(contents);
    assert.equal(await replaced, null);
    contents.emit("destroyed");
    assert.equal(await closed, null);
    assert.equal(contents.listenerCount("before-input-event"), 0);
  });
});
