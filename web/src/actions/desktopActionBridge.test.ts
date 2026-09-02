import { describe, expect, it } from "vitest";
import { parseKeybinding } from "./keybindingParser";
import {
  DESKTOP_ACTION_BINDING_VERSION,
  desktopActionBindingSnapshot,
  isDesktopMenuAction,
  keyStrokeToElectronAccelerator,
} from "./desktopActionBridge";
import type { ActionId, KeybindingRule } from "./types";

function stroke(binding: string) {
  return parseKeybinding(binding)[0];
}

function rule<A extends ActionId>(id: string, action: A, binding: string): KeybindingRule<A> {
  return { id, action, mode: "global", sequence: parseKeybinding(binding) } as KeybindingRule<A>;
}

describe("desktop action bridge model", () => {
  it.each([
    ["mod+n", "CmdOrCtrl+N"],
    ["primary+,", "CmdOrCtrl+,"],
    ["ctrl+alt+[KeyJ]", "Ctrl+Alt+J"],
    ["meta+shift+[Digit1]", "Cmd+Shift+1"],
    ["primary+[BracketLeft]", "CmdOrCtrl+["],
  ])("converts %s to %s", (binding, accelerator) => {
    expect(keyStrokeToElectronAccelerator(stroke(binding))).toBe(accelerator);
  });

  it("rejects keys Electron cannot safely own globally", () => {
    expect(keyStrokeToElectronAccelerator(stroke("ctrl+[Numpad1]"))).toBeNull();
    expect(keyStrokeToElectronAccelerator(stroke("f"))).toBeNull();
    expect(keyStrokeToElectronAccelerator(stroke("shift+a"))).toBeNull();
    expect(keyStrokeToElectronAccelerator(stroke("primary+c"))).toBeNull();
    expect(keyStrokeToElectronAccelerator(stroke("meta+q"))).toBeNull();
    expect(keyStrokeToElectronAccelerator(stroke("f12"))).toBeNull();
  });

  it("publishes every menu action and marks missing bindings unbound", () => {
    const snapshot = desktopActionBindingSnapshot(
      [
        rule("session.new", "session.action.new", "mod+n"),
        rule("file.find", "file.action.find", "primary+f"),
      ],
      true,
    );
    expect(snapshot).toEqual({
      version: DESKTOP_ACTION_BINDING_VERSION,
      bindings: [
        { action: "session.action.new", accelerator: "CmdOrCtrl+N" },
        { action: "workbench.action.navigateSettings", accelerator: null },
        { action: "file.action.find", accelerator: "CmdOrCtrl+F" },
      ],
    });
  });

  it("prefers a user binding over a sibling default", () => {
    const snapshot = desktopActionBindingSnapshot(
      [
        { ...rule("default", "session.action.new", "mod+n"), origin: "default" },
        { ...rule("alternate", "session.action.new", "ctrl+j"), origin: "user" },
      ],
      false,
    );
    expect(snapshot.bindings[0]).toEqual({
      action: "session.action.new",
      accelerator: "Ctrl+J",
    });
  });

  it("uses the first effective binding and ignores web-only rules", () => {
    const snapshot = desktopActionBindingSnapshot(
      [
        {
          ...rule("web", "session.action.new", "ctrl+w"),
          when: { type: "equals", key: "isNativeShell", value: false },
        },
        rule("user", "session.action.new", "ctrl+j"),
      ],
      false,
    );
    expect(snapshot.bindings[0]).toEqual({
      action: "session.action.new",
      accelerator: "Ctrl+J",
    });
  });

  it("leaves duplicate native accelerators to contextual renderer dispatch", () => {
    const snapshot = desktopActionBindingSnapshot(
      [
        rule("session", "session.action.new", "ctrl+j"),
        rule("settings", "workbench.action.navigateSettings", "ctrl+j"),
      ],
      false,
    );
    expect(snapshot.bindings.slice(0, 2)).toEqual([
      { action: "session.action.new", accelerator: null },
      { action: "workbench.action.navigateSettings", accelerator: null },
    ]);
  });

  it("recognizes only menu-owned action identifiers", () => {
    expect(isDesktopMenuAction("file.action.find")).toBe(true);
    expect(isDesktopMenuAction("composer.action.send")).toBe(false);
  });
});
