const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const {
  menuStateChanged,
  registerDesktopCommandStateReset,
  registerPickListMenuYield,
  sanitizeDesktopCommandState,
} = require("../src/desktopCommands");

describe("sanitizeDesktopCommandState", () => {
  it("keeps known commands and strict booleans", () => {
    const state = sanitizeDesktopCommandState({
      commands: ["close", "new-browser-tab", "rm -rf", 42, "close"],
      sessionPinned: true,
      sessionArchived: "yes",
    });
    assert.deepEqual([...state.commands], ["close", "new-browser-tab"]);
    assert.equal(state.sessionPinned, true);
    assert.equal(state.sessionArchived, false);
    assert.equal(state.pickListFocused, false);
    assert.equal(sanitizeDesktopCommandState({ pickListFocused: true }).pickListFocused, true);
  });

  it("reports nothing for a non-object and no commands for a missing list", () => {
    assert.equal(sanitizeDesktopCommandState(null), null);
    assert.equal(sanitizeDesktopCommandState("close"), null);
    assert.deepEqual([...sanitizeDesktopCommandState({}).commands], []);
  });
});

describe("menuStateChanged", () => {
  const state = (overrides = {}) =>
    sanitizeDesktopCommandState({ commands: ["close", "go-back"], ...overrides });

  it("ignores a report that only moves pick-list focus", () => {
    assert.equal(menuStateChanged(state(), state({ pickListFocused: true })), false);
    assert.equal(menuStateChanged(state(), state({ commands: ["go-back", "close"] })), false);
  });

  it("sees what the menu shows change: its items, Pin and Archive, or a first report", () => {
    assert.equal(menuStateChanged(state(), state({ commands: ["close"] })), true);
    assert.equal(menuStateChanged(state(), state({ commands: ["close", "quit"] })), true);
    assert.equal(menuStateChanged(state(), state({ sessionPinned: true })), true);
    assert.equal(menuStateChanged(state(), state({ sessionArchived: true })), true);
    assert.equal(menuStateChanged(null, state()), true);
    assert.equal(menuStateChanged(state(), null), true);
    assert.equal(menuStateChanged(null, null), false);
  });
});

describe("registerDesktopCommandStateReset", () => {
  it("resets on full navigations and renderer crashes only", () => {
    const webContents = new EventEmitter();
    let resets = 0;
    registerDesktopCommandStateReset(webContents, () => {
      resets += 1;
    });
    webContents.emit("did-navigate-in-page");
    assert.equal(resets, 0);
    webContents.emit("did-navigate");
    webContents.emit("render-process-gone");
    assert.equal(resets, 2);
  });
});

describe("registerPickListMenuYield", () => {
  /** A page whose menu-shortcut setting is recorded, and whose pick list focus the test sets. */
  function page() {
    const webContents = Object.assign(new EventEmitter(), {
      ignoresMenuShortcuts: false,
      setIgnoreMenuShortcuts(ignore) {
        this.ignoresMenuShortcuts = ignore;
      },
    });
    const focus = { pickList: false };
    registerPickListMenuYield(webContents, () => focus.pickList);
    const press = (input) => {
      webContents.emit("before-input-event", {}, { type: "keyDown", ...input });
      return webContents.ignoresMenuShortcuts;
    };
    return { focus, press };
  }
  const ctrl = (key, code = `Key${key.toUpperCase()}`) => ({ key, code, control: true });

  it("leaves Ctrl-N and Ctrl-P to a focused pick list, and only then", () => {
    const { focus, press } = page();
    assert.equal(press(ctrl("n")), false);
    focus.pickList = true;
    assert.equal(press(ctrl("n")), true);
    assert.equal(press(ctrl("p")), true);
    // A non-Latin layout steps on the physical key.
    assert.equal(press(ctrl("т", "KeyN")), true);
    focus.pickList = false;
    assert.equal(press(ctrl("n")), false);
  });

  it("keeps the menu's other chords, even with a pick list focused", () => {
    const { focus, press } = page();
    focus.pickList = true;
    assert.equal(press({ ...ctrl("n"), shift: true }), false);
    assert.equal(press({ ...ctrl("n"), alt: true }), false);
    assert.equal(press({ key: "n", code: "KeyN", meta: true }), false);
    assert.equal(press(ctrl("w")), false);
    // The setting follows each key down, so a step doesn't leave the menu ignored.
    assert.equal(press(ctrl("n")), true);
    assert.equal(press(ctrl("t")), false);
  });
});
