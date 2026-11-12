"use strict";

const assert = require("node:assert/strict");
const { describe, it } = require("node:test");
const { parseKeyCombo, keyboardKey } = require("../src/browserKeys");

describe("parseKeyCombo", () => {
  it("accepts case-insensitive key names and aliases", () => {
    for (const [input, key] of [
      ["Enter", "Enter"],
      ["tAB", "Tab"],
      ["arrowdown", "ArrowDown"],
      ["PAGEUP", "PageUp"],
      ["f12", "F12"],
      ["ESC", "Escape"],
      ["return", "Enter"],
      ["Space", " "],
      ["Del", "Delete"],
    ]) {
      assert.deepEqual(parseKeyCombo(input), { modifiers: [], key }, input);
    }
  });

  it("normalizes modifier aliases, preserves order and removes duplicates", () => {
    assert.deepEqual(parseKeyCombo("Cmd+Ctrl+Option+x"), {
      modifiers: ["Meta", "Control", "Alt"],
      key: "x",
    });
    assert.deepEqual(parseKeyCombo("command+META+control+CTRL+ alt +SHIFT+Tab"), {
      modifiers: ["Meta", "Control", "Alt", "Shift"],
      key: "Tab",
    });
  });

  it("resolves ControlOrMeta for the requested platform", () => {
    for (const [platform, modifier] of [
      ["darwin", "Meta"],
      ["linux", "Control"],
      ["win32", "Control"],
    ]) {
      assert.deepEqual(parseKeyCombo("cONTROLorMETA+a", platform), {
        modifiers: [modifier],
        key: "a",
      });
    }
  });

  it("leaves characters and Shift mapping to Puppeteer", () => {
    for (const key of ["a", "A", "7", " ", "+", "é", "🙂"]) {
      assert.deepEqual(parseKeyCombo(key), { modifiers: [], key });
    }
    for (const key of ["a", "1", "/", ";", "+"]) {
      assert.deepEqual(parseKeyCombo(`Shift+${key}`), { modifiers: ["Shift"], key });
    }
    assert.deepEqual(parseKeyCombo("Ctrl++"), { modifiers: ["Control"], key: "+" });
  });

  it("rejects missing keys, unknown names and invalid modifiers", () => {
    for (const combo of ["", "Hyper+a", "NotAKey", "Shift+", "Ctrl++a", "__proto__+a"]) {
      assert.throws(() => parseKeyCombo(combo), undefined, combo);
    }
    for (const combo of [undefined, null, 5, "x".repeat(65)]) {
      assert.throws(() => parseKeyCombo(combo), /non-empty string of at most 64 characters/);
    }
    for (const combo of ["Control+é", "Shift+🙂"]) {
      assert.throws(() => parseKeyCombo(combo), /Unicode characters cannot be used in shortcuts/);
    }
    assert.deepEqual(parseKeyCombo(`${"Shift+".repeat(10)}Home`), {
      modifiers: ["Shift"],
      key: "Home",
    });
  });
});

describe("keyboardKey", () => {
  it("lets Puppeteer produce shifted letters, digits and punctuation", async () => {
    const { CdpKeyboard } = await import("puppeteer-core/internal/cdp/Input.js");
    for (const [input, expectedKey, expectedText] of [
      ["Shift+b", "B", "B"],
      ["Shift+1", "!", "!"],
      ["Shift+/", "?", "?"],
      ["Shift+;", ":", ":"],
      ["Shift++", "+", "+"],
      ["Control+Shift+a", "A", ""],
      ["1", "1", "1"],
      ["Escape", "Escape", ""],
    ]) {
      const events = [];
      const keyboard = new CdpKeyboard({
        async send(_method, event) {
          events.push(event);
        },
      });
      const combo = parseKeyCombo(input);
      for (const modifier of combo.modifiers) {
        // oxlint-disable-next-line no-await-in-loop -- Modifiers precede the key.
        await keyboard.down(modifier);
      }
      // oxlint-disable-next-line no-await-in-loop -- Inspect each shortcut's emitted input.
      await keyboard.press(keyboardKey(combo));
      const pressed = events[combo.modifiers.length];
      assert.deepEqual(
        { key: pressed.key, text: pressed.text },
        {
          key: expectedKey,
          text: expectedText,
        },
        input,
      );
    }
  });
});
