import { describe, expect, it } from "vitest";
import { SHIFT_ENTER_CSI_U } from "./TerminalSession";
import {
  EXTRA_KEY_ROWS,
  MODIFIERS_OFF,
  NO_MODIFIERS,
  activeModifiers,
  encodeExtraKey,
  encodeModifiedInput,
  hasActiveModifier,
  reduceModifiers,
  type Modifiers,
  type ModifierStates,
} from "./terminalExtraKeysModel";

const normal = { applicationCursor: false };
const app = { applicationCursor: true };
const mods = (over: Partial<Modifiers>): Modifiers => ({ ...NO_MODIFIERS, ...over });

describe("EXTRA_KEY_ROWS", () => {
  it("is the fixed Termux-style 2×7 grid with the three modifiers on row two", () => {
    expect(EXTRA_KEY_ROWS).toHaveLength(2);
    expect(EXTRA_KEY_ROWS.map((row) => row.map((k) => k.label))).toEqual([
      ["Esc", "⇧Tab", "Tab", "Home", "↑", "End", "PgUp"],
      ["Ctrl", "Alt", "Shift", "←", "↓", "→", "PgDn"],
    ]);
    const modifiers = EXTRA_KEY_ROWS.flat().filter((k) => k.kind === "modifier");
    expect(modifiers.map((k) => k.id)).toEqual(["ctrl", "alt", "shift"]);
  });
});

describe("encodeExtraKey", () => {
  it("sends Esc as a bare 0x1b and ⇧Tab as legacy CSI Z, ignoring modifiers", () => {
    // WHY: Claude Code gates Kitty CSI-u on a terminal allow-list, so CSI Z is
    // the only Shift+Tab form every harness accepts; Esc must never grow a
    // modifier prefix or it stops being Esc.
    expect(encodeExtraKey("esc", NO_MODIFIERS, normal)).toBe("\x1b");
    expect(encodeExtraKey("esc", mods({ ctrl: true, alt: true }), normal)).toBe("\x1b");
    expect(encodeExtraKey("shift-tab", NO_MODIFIERS, normal)).toBe("\x1b[Z");
    expect(encodeExtraKey("shift-tab", mods({ shift: true, alt: true }), app)).toBe("\x1b[Z");
    expect(encodeExtraKey("shift-tab", NO_MODIFIERS, normal)).not.toContain(";2u");
  });

  it("encodes Tab plainly, as CSI Z under Shift, and Alt-prefixed under Alt", () => {
    expect(encodeExtraKey("tab", NO_MODIFIERS, normal)).toBe("\t");
    expect(encodeExtraKey("tab", mods({ shift: true }), normal)).toBe("\x1b[Z");
    expect(encodeExtraKey("tab", mods({ alt: true }), normal)).toBe("\x1b\t");
    expect(encodeExtraKey("tab", mods({ ctrl: true }), normal)).toBe("\t");
  });

  it("honors DECCKM for arrows and Home/End", () => {
    expect(encodeExtraKey("up", NO_MODIFIERS, normal)).toBe("\x1b[A");
    expect(encodeExtraKey("down", NO_MODIFIERS, normal)).toBe("\x1b[B");
    expect(encodeExtraKey("right", NO_MODIFIERS, normal)).toBe("\x1b[C");
    expect(encodeExtraKey("left", NO_MODIFIERS, normal)).toBe("\x1b[D");
    expect(encodeExtraKey("home", NO_MODIFIERS, normal)).toBe("\x1b[H");
    expect(encodeExtraKey("end", NO_MODIFIERS, normal)).toBe("\x1b[F");

    expect(encodeExtraKey("up", NO_MODIFIERS, app)).toBe("\x1bOA");
    expect(encodeExtraKey("down", NO_MODIFIERS, app)).toBe("\x1bOB");
    expect(encodeExtraKey("right", NO_MODIFIERS, app)).toBe("\x1bOC");
    expect(encodeExtraKey("left", NO_MODIFIERS, app)).toBe("\x1bOD");
    expect(encodeExtraKey("home", NO_MODIFIERS, app)).toBe("\x1bOH");
    expect(encodeExtraKey("end", NO_MODIFIERS, app)).toBe("\x1bOF");
  });

  it("encodes modified cursor keys as CSI 1;m X with the xterm bit sum, ignoring DECCKM", () => {
    // m = 1 + Shift(1) + Alt(2) + Ctrl(4)
    expect(encodeExtraKey("up", mods({ shift: true }), normal)).toBe("\x1b[1;2A");
    expect(encodeExtraKey("left", mods({ alt: true }), normal)).toBe("\x1b[1;3D");
    expect(encodeExtraKey("right", mods({ ctrl: true }), normal)).toBe("\x1b[1;5C");
    expect(encodeExtraKey("down", mods({ ctrl: true, shift: true }), normal)).toBe("\x1b[1;6B");
    expect(encodeExtraKey("home", mods({ ctrl: true, alt: true, shift: true }), normal)).toBe(
      "\x1b[1;8H",
    );
    // Application cursor mode does not change the modified form.
    expect(encodeExtraKey("end", mods({ ctrl: true }), app)).toBe("\x1b[1;5F");
  });

  it("encodes PgUp/PgDn with an optional modifier parameter", () => {
    expect(encodeExtraKey("pgup", NO_MODIFIERS, normal)).toBe("\x1b[5~");
    expect(encodeExtraKey("pgdn", NO_MODIFIERS, normal)).toBe("\x1b[6~");
    expect(encodeExtraKey("pgup", NO_MODIFIERS, app)).toBe("\x1b[5~");
    expect(encodeExtraKey("pgup", mods({ shift: true }), normal)).toBe("\x1b[5;2~");
    expect(encodeExtraKey("pgdn", mods({ ctrl: true, alt: true }), normal)).toBe("\x1b[6;7~");
  });
});

describe("encodeModifiedInput", () => {
  it("maps Ctrl + letter to the control code regardless of case", () => {
    expect(encodeModifiedInput("c", mods({ ctrl: true }))).toBe("\x03");
    expect(encodeModifiedInput("C", mods({ ctrl: true }))).toBe("\x03");
    expect(encodeModifiedInput("r", mods({ ctrl: true }))).toBe("\x12");
    expect(encodeModifiedInput("a", mods({ ctrl: true }))).toBe("\x01");
    expect(encodeModifiedInput("z", mods({ ctrl: true }))).toBe("\x1a");
  });

  it("maps the Ctrl edge keys @ [ \\ ] ^ _ ? (and their digit aliases)", () => {
    const ctrl = mods({ ctrl: true });
    expect(encodeModifiedInput("@", ctrl)).toBe("\x00");
    expect(encodeModifiedInput(" ", ctrl)).toBe("\x00");
    expect(encodeModifiedInput("2", ctrl)).toBe("\x00");
    expect(encodeModifiedInput("[", ctrl)).toBe("\x1b");
    expect(encodeModifiedInput("3", ctrl)).toBe("\x1b");
    expect(encodeModifiedInput("\\", ctrl)).toBe("\x1c");
    expect(encodeModifiedInput("4", ctrl)).toBe("\x1c");
    expect(encodeModifiedInput("]", ctrl)).toBe("\x1d");
    expect(encodeModifiedInput("5", ctrl)).toBe("\x1d");
    expect(encodeModifiedInput("^", ctrl)).toBe("\x1e");
    expect(encodeModifiedInput("6", ctrl)).toBe("\x1e");
    expect(encodeModifiedInput("_", ctrl)).toBe("\x1f");
    expect(encodeModifiedInput("7", ctrl)).toBe("\x1f");
    expect(encodeModifiedInput("?", ctrl)).toBe("\x7f");
    expect(encodeModifiedInput("8", ctrl)).toBe("\x7f");
  });

  it("passes a non-transformable character through unchanged under Ctrl", () => {
    expect(encodeModifiedInput("1", mods({ ctrl: true }))).toBe("1");
    expect(encodeModifiedInput("é", mods({ ctrl: true }))).toBe("é");
  });

  it("prefixes Alt + char with Esc in a single string", () => {
    expect(encodeModifiedInput("p", mods({ alt: true }))).toBe("\x1bp");
    expect(encodeModifiedInput("c", mods({ alt: true, ctrl: true }))).toBe("\x1b\x03");
  });

  it("encodes Shift + Enter as the shared CSI-u sequence and leaves other Shift chars alone", () => {
    expect(encodeModifiedInput("\r", mods({ shift: true }))).toBe(SHIFT_ENTER_CSI_U);
    expect(encodeModifiedInput("\r", mods({ shift: true, ctrl: true }))).toBe("\r");
    expect(encodeModifiedInput("a", mods({ shift: true }))).toBe("a");
    expect(encodeModifiedInput("\r", NO_MODIFIERS)).toBe("\r");
  });

  it("passes multi-character chunks (IME commit, paste) through untouched", () => {
    expect(encodeModifiedInput("ls -la", mods({ ctrl: true, alt: true }))).toBe("ls -la");
    expect(encodeModifiedInput("ab", mods({ shift: true }))).toBe("ab");
    expect(encodeModifiedInput("", mods({ ctrl: true }))).toBe("");
  });

  it("is the identity with no modifiers", () => {
    expect(encodeModifiedInput("c", NO_MODIFIERS)).toBe("c");
    expect(encodeModifiedInput("\x1b", NO_MODIFIERS)).toBe("\x1b");
  });
});

describe("reduceModifiers", () => {
  const armedCtrl: ModifierStates = { ...MODIFIERS_OFF, ctrl: "armed" };
  const lockedCtrl: ModifierStates = { ...MODIFIERS_OFF, ctrl: "locked" };

  it("arms on tap, disarms on a second tap", () => {
    expect(reduceModifiers(MODIFIERS_OFF, { type: "tap", mod: "ctrl" })).toEqual(armedCtrl);
    expect(reduceModifiers(armedCtrl, { type: "tap", mod: "ctrl" })).toEqual(MODIFIERS_OFF);
  });

  it("locks on long-press from off or armed, and a tap on locked turns it off", () => {
    expect(reduceModifiers(MODIFIERS_OFF, { type: "longPress", mod: "ctrl" })).toEqual(lockedCtrl);
    expect(reduceModifiers(armedCtrl, { type: "longPress", mod: "ctrl" })).toEqual(lockedCtrl);
    expect(reduceModifiers(lockedCtrl, { type: "tap", mod: "ctrl" })).toEqual(MODIFIERS_OFF);
  });

  it("consume clears armed modifiers but a lock survives", () => {
    const both: ModifierStates = { ctrl: "locked", alt: "armed", shift: "armed" };
    expect(reduceModifiers(both, { type: "consume" })).toEqual({
      ctrl: "locked",
      alt: "off",
      shift: "off",
    });
    expect(reduceModifiers(lockedCtrl, { type: "consume" })).toEqual(lockedCtrl);
  });

  it("returns the same object when nothing changes so the hot path can skip React", () => {
    // WHY: the onData transform dispatches consume on every chunk while a lock
    // is held; a fresh object there would re-render the row per keystroke.
    expect(reduceModifiers(MODIFIERS_OFF, { type: "consume" })).toBe(MODIFIERS_OFF);
    expect(reduceModifiers(lockedCtrl, { type: "consume" })).toBe(lockedCtrl);
    expect(reduceModifiers(lockedCtrl, { type: "longPress", mod: "ctrl" })).toBe(lockedCtrl);
  });

  it("exposes armed and locked alike as active modifiers", () => {
    expect(activeModifiers({ ctrl: "armed", alt: "locked", shift: "off" })).toEqual({
      ctrl: true,
      alt: true,
      shift: false,
    });
    expect(hasActiveModifier(MODIFIERS_OFF)).toBe(false);
    expect(hasActiveModifier(armedCtrl)).toBe(true);
    expect(hasActiveModifier(lockedCtrl)).toBe(true);
  });
});
