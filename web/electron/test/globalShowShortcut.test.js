const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { createGlobalShowShortcut } = require("../src/globalShowShortcut");

/**
 * A settings store and a fake GlobalShortcut. `refused` chords make register
 * return false; `throws` chords make it throw.
 */
function harness({ settings = {}, refused = [], throws = [] } = {}) {
  let stored = structuredClone(settings);
  const calls = { registered: [], unregistered: [], saved: [], shown: 0 };
  const globalShortcut = {
    register: (accelerator, press) => {
      if (throws.includes(accelerator)) throw new TypeError(`Invalid accelerator: ${accelerator}`);
      if (refused.includes(accelerator)) return false;
      calls.registered.push({ accelerator, press });
      return true;
    },
    unregister: (accelerator) => {
      calls.unregistered.push(accelerator);
    },
  };
  const shortcut = createGlobalShowShortcut({
    globalShortcut,
    loadSettings: () => structuredClone(stored),
    saveSettings: (next) => {
      stored = structuredClone(next);
      calls.saved.push(structuredClone(next));
    },
    show: () => {
      calls.shown += 1;
    },
  });
  return { calls, shortcut, saved: () => stored };
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
    it(`keeps the saved setting and re-registers it when Electron ${failure} the new chord`, () => {
      const h = harness({
        settings: { global_shortcut: { enabled: true, accelerator: "Alt+Space" } },
        ...options,
      });
      h.shortcut.apply();

      const result = h.shortcut.set({ enabled: true, accelerator: "Control+Shift+K" });

      assert.equal(result.ok, false);
      assert.match(result.error, /already in use/);
      assert.deepEqual(h.calls.unregistered, ["Alt+Space"]);
      assert.deepEqual(
        h.calls.registered.map((entry) => entry.accelerator),
        ["Alt+Space", "Alt+Space"],
      );
      assert.deepEqual(h.calls.saved, []);
      assert.deepEqual(h.saved().global_shortcut, { enabled: true, accelerator: "Alt+Space" });
    });
  }
});
