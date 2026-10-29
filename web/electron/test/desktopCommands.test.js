const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const {
  DESKTOP_COMMANDS,
  registerDesktopCommandStateReset,
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
  });

  it("reports nothing for a non-object and no commands for a missing list", () => {
    assert.equal(sanitizeDesktopCommandState(null), null);
    assert.equal(sanitizeDesktopCommandState("close"), null);
    assert.deepEqual([...sanitizeDesktopCommandState({}).commands], []);
  });

  it("covers every command the menu dispatches", () => {
    assert.deepEqual([...DESKTOP_COMMANDS].sort(), [
      "archive-session",
      "close",
      "new-browser-tab",
      "new-terminal",
      "toggle-pin-session",
    ]);
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
