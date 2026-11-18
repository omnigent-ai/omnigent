const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const {
  GLOBAL_SHORTCUTS,
  acceleratorLabel,
  chordKey,
  createGlobalShortcuts,
  createGlobalShowShortcut,
} = require("../src/globalShowShortcut");

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
  const controller = createGlobalShowShortcut({
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
  // The user allows every chord unless a test answers otherwise.
  calls.approvals = [];
  const shortcut = {
    ...controller,
    set: (setting, approve = async () => true) =>
      controller.set(setting, async (accelerator) => {
        calls.approvals.push(accelerator);
        return approve(accelerator);
      }),
  };
  return { calls, shortcut, saved: () => stored, registered: () => [...live] };
}

describe("createGlobalShowShortcut", () => {
  it("is off by default and registers nothing", async () => {
    const h = harness();
    assert.deepEqual(h.shortcut.current(), { enabled: false, accelerator: "Alt+Space" });
    h.shortcut.apply();
    assert.deepEqual(h.calls.registered, []);
  });

  it("registers the chord when enabled, and again on the next launch", async () => {
    const first = harness();
    assert.deepEqual(await first.shortcut.set({ enabled: true, accelerator: "Alt+Space" }), {
      ok: true,
      setting: { enabled: true, accelerator: "Alt+Space" },
    });
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

  it("registers and saves nothing when the user doesn't approve a chord", async () => {
    const h = harness({
      settings: { global_shortcut: { enabled: true, accelerator: "Alt+Space" } },
    });
    h.shortcut.apply();

    const result = await h.shortcut.set(
      { enabled: true, accelerator: "Control+K" },
      async () => false,
    );

    assert.equal(result.ok, false);
    assert.deepEqual(h.calls.approvals, ["Control+K"]);
    assert.deepEqual(h.registered(), ["Alt+Space"]);
    assert.deepEqual(h.calls.saved, []);
    // A chord tried while the shortcut is off needs approval too.
    const off = harness();
    await off.shortcut.set({ enabled: false, accelerator: "Control+K" }, async () => false);
    assert.deepEqual(off.calls.registered, []);
    assert.deepEqual(off.calls.saved, []);
  });

  it("turns off from a window still showing an older chord, keeping the saved one", async () => {
    const h = harness({
      settings: { global_shortcut: { enabled: true, accelerator: "Control+K" } },
      refused: ["Alt+Space"],
    });
    h.shortcut.apply();

    // This Settings window loaded before another one changed the chord from Alt+Space.
    const result = await h.shortcut.set({ enabled: false });

    assert.deepEqual(result, { ok: true, setting: { enabled: false, accelerator: "Control+K" } });
    assert.deepEqual(h.calls.approvals, []);
    assert.deepEqual(h.registered(), []);
    assert.deepEqual(h.saved().global_shortcut, { enabled: false, accelerator: "Control+K" });
  });

  it("keeps the shortcut on when saving its turn-off from a stale window fails", async (t) => {
    t.mock.method(console, "warn", () => {});
    const h = harness({
      settings: { global_shortcut: { enabled: true, accelerator: "Control+K" } },
      saveFails: true,
    });
    h.shortcut.apply();

    const result = await h.shortcut.set({ enabled: false });

    assert.equal(result.ok, false);
    assert.deepEqual(h.calls.approvals, []);
    assert.deepEqual(h.registered(), ["Control+K"]);
  });

  it("asks only when a chord would register, not to turn it off or resave it", async () => {
    const h = harness();
    await h.shortcut.set({ enabled: true, accelerator: "Alt+Space" });
    await h.shortcut.set({ enabled: true, accelerator: "Alt+Space" });
    await h.shortcut.set({ enabled: false });
    assert.deepEqual(h.calls.approvals, ["Alt+Space"]);
    assert.deepEqual(h.saved().global_shortcut, { enabled: false, accelerator: "Alt+Space" });
  });

  // Turning off names no chord, so a window showing an older one can't save it.
  for (const [holder, refused] of [
    ["", []],
    [" while another app holds its chord", ["Control+K"]],
  ]) {
    it(`turns off an already-off shortcut${holder} without asking or registering`, async () => {
      const h = harness({
        settings: { global_shortcut: { enabled: false, accelerator: "Control+K" } },
        refused,
      });
      h.shortcut.apply();

      const result = await h.shortcut.set({ enabled: false });

      assert.deepEqual(result, { ok: true, setting: { enabled: false, accelerator: "Control+K" } });
      assert.deepEqual(h.calls.approvals, []);
      assert.deepEqual(h.calls.registered, []);
      assert.deepEqual(h.saved().global_shortcut, { enabled: false, accelerator: "Control+K" });
    });
  }

  it("moves the registration when the chord changes, and keeps the chord while off", async () => {
    const h = harness();
    await h.shortcut.set({ enabled: true, accelerator: "Alt+Space" });

    assert.deepEqual(await h.shortcut.set({ enabled: true, accelerator: "Control+Shift+K" }), {
      ok: true,
      setting: { enabled: true, accelerator: "Control+Shift+K" },
    });
    assert.deepEqual(h.calls.unregistered, ["Alt+Space"]);
    assert.deepEqual(
      h.calls.registered.map((entry) => entry.accelerator),
      ["Alt+Space", "Control+Shift+K"],
    );

    await h.shortcut.set({ enabled: false });
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
    it(`keeps the saved setting and its registration when Electron ${failure} the new chord`, async () => {
      const h = harness({
        settings: { global_shortcut: { enabled: true, accelerator: "Alt+Space" } },
        ...options,
      });
      h.shortcut.apply();

      const result = await h.shortcut.set({ enabled: true, accelerator: "Control+Shift+K" });

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
    it(`tries ${chord} recorded while off, and keeps the saved one when it can't be used`, async () => {
      const h = harness({
        settings: { global_shortcut: { enabled: false, accelerator: "Alt+Space" } },
        refused: [recorded],
      });
      h.shortcut.apply();

      const result = await h.shortcut.set({ enabled: false, accelerator: recorded });

      assert.equal(result.ok, false);
      assert.match(result.error, /already in use/);
      assert.deepEqual(h.calls.saved, []);
      assert.deepEqual(h.registered(), []);
    });
  }

  it("releases a chord it tried while the shortcut is off", async () => {
    const h = harness();
    assert.deepEqual(await h.shortcut.set({ enabled: false, accelerator: "Control+Shift+K" }), {
      ok: true,
      setting: { enabled: false, accelerator: "Control+Shift+K" },
    });
    assert.deepEqual(h.registered(), []);
    assert.deepEqual(h.saved().global_shortcut, {
      enabled: false,
      accelerator: "Control+Shift+K",
    });
  });

  it("turns off a shortcut whose chord another app took", async () => {
    const h = harness({
      settings: { global_shortcut: { enabled: true, accelerator: "Alt+Space" } },
      refused: ["Alt+Space"],
    });
    h.shortcut.apply();
    assert.deepEqual(await h.shortcut.set({ enabled: false }), {
      ok: true,
      setting: { enabled: false, accelerator: "Alt+Space" },
    });
    assert.deepEqual(h.calls.approvals, []);
    assert.deepEqual(h.saved().global_shortcut, { enabled: false, accelerator: "Alt+Space" });
  });

  for (const [change, next] of [
    ["a new chord", { enabled: true, accelerator: "Control+Shift+K" }],
    ["turning it off", { enabled: false }],
  ]) {
    it(`keeps the previous chord registered when saving ${change} fails`, async (t) => {
      t.mock.method(console, "warn", () => {});
      const h = harness({
        settings: { global_shortcut: { enabled: true, accelerator: "Alt+Space" } },
        saveFails: true,
      });
      h.shortcut.apply();

      const result = await h.shortcut.set(next);

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
    it(`reports a saved shortcut that Electron ${failure} at launch as not working`, async () => {
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

describe("createGlobalShortcuts", () => {
  /**
   * Both shortcuts over one settings store and a fake GlobalShortcut that, like Electron,
   * refuses a chord already registered in any spelling.
   */
  function shortcuts(settings = {}) {
    let stored = structuredClone(settings);
    const live = new Map();
    const built = createGlobalShortcuts({
      globalShortcut: {
        register: (accelerator) => {
          if (live.has(chordKey(accelerator, true))) return false;
          live.set(chordKey(accelerator, true), accelerator);
          return true;
        },
        unregister: (accelerator) => live.delete(chordKey(accelerator, true)),
      },
      loadSettings: () => structuredClone(stored),
      saveSettings: (next) => {
        stored = structuredClone(next);
      },
      isMac: true,
      presses: { show: () => {}, floatingComposer: () => {} },
    });
    return { ...built, saved: () => stored, registered: () => [...live.values()] };
  }

  it("keeps each shortcut under its own settings key", () => {
    const keys = Object.values(GLOBAL_SHORTCUTS).map((definition) => definition.settingKey);
    assert.equal(new Set(keys).size, keys.length);
    assert.equal(GLOBAL_SHORTCUTS.show.settingKey, "global_shortcut");
    assert.throws(
      () => createGlobalShowShortcut({ id: "unknown", show: () => {} }),
      /Unknown global shortcut/,
    );
  });

  it("saving one shortcut leaves the other's setting as it was", async () => {
    const show = { enabled: true, accelerator: "Command+Shift+K" };
    const s = shortcuts({ global_shortcut: show });
    assert.deepEqual(s.floatingComposer.current(), {
      enabled: true,
      accelerator: "CommandOrControl+Shift+Space",
    });
    assert.equal((await s.floatingComposer.set({ enabled: false })).ok, true);
    assert.deepEqual(s.saved().global_shortcut, show);
    assert.deepEqual(s.saved().floating_composer_shortcut, {
      enabled: false,
      accelerator: "CommandOrControl+Shift+Space",
    });
    assert.deepEqual(s.show.current(), show);
  });

  // The recorder writes the default's chord as `Shift+Command+Space` on macOS.
  for (const accelerator of ["CommandOrControl+Shift+Space", "Shift+Command+Space"]) {
    for (const held of ["registered", "on by default"]) {
      it(`refuses ${accelerator} while another shortcut has it ${held}, before asking`, async () => {
        const s = shortcuts();
        if (held === "registered") s.floatingComposer.apply();
        let asked = false;
        const result = await s.show.set({ enabled: true, accelerator }, async () => (asked = true));
        assert.match(result.error, /Another Omnigent shortcut/);
        assert.equal(asked, false);
        assert.equal(s.saved().global_shortcut, undefined);
      });
    }
  }

  it("refuses a chord another shortcut took while the user was deciding", async () => {
    const s = shortcuts({ floating_composer_shortcut: { enabled: false, accelerator: "Alt+K" } });
    const result = await s.show.set({ enabled: true, accelerator: "Alt+K" }, async () => {
      await s.floatingComposer.set({ enabled: true, accelerator: "Alt+K" }, async () => true);
      return true;
    });
    assert.match(result.error, /Another Omnigent shortcut/);
    assert.deepEqual(s.registered(), ["Alt+K"]);
    assert.equal(s.saved().global_shortcut, undefined);
  });

  it("saves another spelling of its own chord without asking or registering it twice", async () => {
    const s = shortcuts();
    s.floatingComposer.apply();
    let asked = false;
    const result = await s.floatingComposer.set(
      { enabled: true, accelerator: "Shift+Command+Space" },
      async () => (asked = true),
    );
    assert.equal(result.ok, true);
    assert.equal(asked, false);
    assert.deepEqual(s.registered(), ["CommandOrControl+Shift+Space"]);
  });

  for (const order of [
    ["show", "floatingComposer"],
    ["floatingComposer", "show"],
  ]) {
    it(`lets a chord the user saved win over another shortcut's default (${order.join(" first, ")} last)`, () => {
      const s = shortcuts({
        global_shortcut: { enabled: true, accelerator: "Shift+Command+Space" },
      });
      for (const id of order) s[id].apply();
      assert.deepEqual(s.registered(), ["Shift+Command+Space"]);
      assert.match(s.floatingComposer.current().error, /Another Omnigent shortcut/);
      assert.equal(s.show.current().error, undefined);
    });
  }

  it("gives a chord both saved on to the first shortcut in the table, in either order", () => {
    for (const order of [
      ["show", "floatingComposer"],
      ["floatingComposer", "show"],
    ]) {
      const s = shortcuts({
        global_shortcut: { enabled: true, accelerator: "Alt+K" },
        floating_composer_shortcut: { enabled: true, accelerator: "Alt+k" },
      });
      for (const id of order) s[id].apply();
      assert.deepEqual(s.registered(), ["Alt+K"]);
      assert.equal(s.show.current().error, undefined);
      assert.match(s.floatingComposer.current().error, /Another Omnigent shortcut/);
    }
  });
});

describe("chordKey", () => {
  it("spells a chord one way for its platform", () => {
    assert.equal(chordKey("CommandOrControl+Shift+Space", true), "shift+command+space");
    assert.equal(chordKey("Shift+Cmd+space", true), "shift+command+space");
    assert.equal(chordKey("Super+Shift+Space", true), "shift+command+space");
    assert.equal(chordKey("CmdOrCtrl+Option+K", false), "control+alt+k");
    assert.equal(chordKey("Ctrl+Alt+K", false), "control+alt+k");
    assert.notEqual(chordKey("Super+K", false), chordKey("Control+K", false));
  });
});

describe("acceleratorLabel", () => {
  it("reads like the Settings page: glyphs on macOS, Ctrl+ elsewhere", () => {
    assert.equal(acceleratorLabel("Control+Alt+Shift+Command+K", true), "⌃⌥⇧⌘K");
    assert.equal(acceleratorLabel("Control+Alt+Space", false), "Ctrl+Alt+Space");
  });
});
