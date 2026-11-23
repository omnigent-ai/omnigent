import { afterEach, describe, expect, it, vi } from "vitest";

import type * as NativeBridge from "@/lib/nativeBridge";
import { inModifierOrder } from "@/lib/shortcuts/chord";

// The key arrays are built when the modules load, so each platform loads them afresh.
async function loadKeyArrays(mac: boolean) {
  vi.resetModules();
  vi.stubGlobal("navigator", { platform: mac ? "MacIntel" : "Win32", userAgent: "" });
  return {
    shortcut: await import("./KeyboardShortcut"),
    modelPicker: await import("@/hooks/useModelPickerHotkey"),
    dictation: await import("@/hooks/useVoiceDictationHotkey"),
    approve: await import("@/hooks/useApproveHotkey"),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe.each([
  { platform: "macOS", mac: true, mod: "⌘", ctrl: "⌃", alt: "⌥" },
  { platform: "other platforms", mac: false, mod: "Ctrl", ctrl: "Ctrl", alt: "Alt" },
])("key arrays from the registry on $platform", ({ mac, mod, ctrl, alt }) => {
  // Key caps as drawn here: ⌃⌥⇧⌘ order on macOS.
  const caps = (...keys: string[]) => inModifierOrder(keys, mac);
  it("match the chords' fixed key caps", async () => {
    const { shortcut, modelPicker, dictation, approve } = await loadKeyArrays(mac);
    expect(shortcut.VIEW_MODE_TOGGLE_KEYS).toEqual(caps(mod, alt, "\\"));
    expect(shortcut.SPLIT_TERMINAL_KEYS).toEqual({
      columns: caps(mod, alt, "⇧", "\\"),
      rows: caps(mod, alt, "-"),
    });
    expect(shortcut.NEW_TERMINAL_KEYS).toEqual(caps(ctrl, "`"));
    expect(shortcut.BACK_KEYS).toEqual(caps(mod, "["));
    expect(shortcut.FORWARD_KEYS).toEqual(caps(mod, "]"));
    expect(shortcut.COMMAND_PALETTE_KEYS).toEqual(caps(mod, "K"));
    expect(modelPicker.MODEL_PICKER_KEYS).toEqual(caps(ctrl, "⇧", "M"));
    expect(dictation.VOICE_DICTATION_KEYS).toEqual(caps(mod, alt, "V"));
    expect(approve.APPROVE_KEYS).toEqual(caps(mod, "↵"));
    expect(approve.DECLINE_KEYS).toEqual(caps("Esc"));
  });

  it("keep the browser chords for the fixed sidebar and Settings arrays in any shell", async () => {
    vi.doMock("@/lib/nativeBridge", async (importOriginal) => ({
      ...(await importOriginal<typeof NativeBridge>()),
      isNativeShell: () => true,
      supportsDesktopCommands: () => true,
    }));
    const { shortcut } = await loadKeyArrays(mac);
    vi.doUnmock("@/lib/nativeBridge");
    expect(shortcut.SIDEBAR_TOGGLE_KEYS).toEqual(caps(mod, alt, "["));
    expect(shortcut.RIGHT_SIDEBAR_TOGGLE_KEYS).toEqual(caps(mod, alt, "]"));
    expect(shortcut.SETTINGS_KEYS).toEqual(caps(mod, alt, ","));
    // The runtime-aware helpers still default to this shell's chords.
    expect(shortcut.sidebarToggleShortcutKeys()).toEqual(caps(mod, "B"));
    expect(shortcut.rightSidebarToggleShortcutKeys()).toEqual(caps(mod, "⇧", "B"));
    expect(shortcut.settingsShortcutKeys()).toEqual(caps(mod, ","));
  });

  it("switch the sidebar and Settings helpers between native and browser chords", async () => {
    const { shortcut } = await loadKeyArrays(mac);
    expect(shortcut.sidebarToggleShortcutKeys(true)).toEqual(caps(mod, "B"));
    expect(shortcut.sidebarToggleShortcutKeys(false)).toEqual(caps(mod, alt, "["));
    expect(shortcut.rightSidebarToggleShortcutKeys(true)).toEqual(caps(mod, "⇧", "B"));
    expect(shortcut.rightSidebarToggleShortcutKeys(false)).toEqual(caps(mod, alt, "]"));
    expect(shortcut.settingsShortcutKeys(true)).toEqual(caps(mod, ","));
    expect(shortcut.settingsShortcutKeys(false)).toEqual(caps(mod, alt, ","));
  });
});
