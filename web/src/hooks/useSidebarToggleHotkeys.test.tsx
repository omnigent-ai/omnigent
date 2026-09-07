// ⌘⌥[ toggles the left sidebar, ⌘⌥] the right (Ctrl+Alt on Win/Linux); matches
// the physical bracket keys (not the glyph ⌥ produces), is platform-aware (only
// ⌘ fires on macOS, only Ctrl on Win/Linux), ignores the bare keys / missing-Alt
// / Shift variants / auto-repeat / AltGraph, fully claims the event, and unbinds
// on unmount.

import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useSidebarToggleHotkeys } from "./useSidebarToggleHotkeys";

/** Dispatch a keydown that reaches window from body (default: Ctrl+Alt+[). */
function press(
  mods: Partial<Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey" | "repeat">> = {
    ctrlKey: true,
    altKey: true,
  },
  code = "BracketLeft",
): void {
  document.body.dispatchEvent(
    new KeyboardEvent("keydown", { code, bubbles: true, cancelable: true, ...mods }),
  );
}

afterEach(() => vi.restoreAllMocks());

/** Render for the Ctrl (Win/Linux) path by default; pass isMac=true for ⌘. */
function setup(isMac = false) {
  const onToggleLeft = vi.fn();
  const onToggleRight = vi.fn();
  const utils = renderHook(() => useSidebarToggleHotkeys({ onToggleLeft, onToggleRight }, isMac));
  return { onToggleLeft, onToggleRight, ...utils };
}

describe("useSidebarToggleHotkeys", () => {
  it("Ctrl+Alt+[ toggles only the left sidebar (Win/Linux)", () => {
    const { onToggleLeft, onToggleRight } = setup(false);
    press({ ctrlKey: true, altKey: true }, "BracketLeft");
    expect(onToggleLeft).toHaveBeenCalledTimes(1);
    expect(onToggleRight).not.toHaveBeenCalled();
  });

  it("Ctrl+Alt+] toggles only the right sidebar (Win/Linux)", () => {
    const { onToggleLeft, onToggleRight } = setup(false);
    press({ ctrlKey: true, altKey: true }, "BracketRight");
    expect(onToggleRight).toHaveBeenCalledTimes(1);
    expect(onToggleLeft).not.toHaveBeenCalled();
  });

  it("Cmd+Alt+[ / ] fire on macOS", () => {
    const { onToggleLeft, onToggleRight } = setup(true);
    press({ metaKey: true, altKey: true }, "BracketLeft");
    press({ metaKey: true, altKey: true }, "BracketRight");
    expect(onToggleLeft).toHaveBeenCalledTimes(1);
    expect(onToggleRight).toHaveBeenCalledTimes(1);
  });

  it("ignores Ctrl+Alt on macOS and Cmd+Alt on Win/Linux (wrong modifier)", () => {
    const mac = setup(true);
    press({ ctrlKey: true, altKey: true }, "BracketLeft");
    expect(mac.onToggleLeft).not.toHaveBeenCalled();

    const other = setup(false);
    press({ metaKey: true, altKey: true }, "BracketLeft");
    expect(other.onToggleLeft).not.toHaveBeenCalled();
  });

  it("ignores the bare keys, missing-Alt, and Shift variants", () => {
    const { onToggleLeft, onToggleRight } = setup(false);
    press({}, "BracketLeft"); // bare [
    press({ ctrlKey: true }, "BracketLeft"); // Ctrl+[ alone = browser Back, not ours
    press({ ctrlKey: true, altKey: true, shiftKey: true }, "BracketRight");
    expect(onToggleLeft).not.toHaveBeenCalled();
    expect(onToggleRight).not.toHaveBeenCalled();
  });

  it("ignores other keys held with the modifiers", () => {
    const { onToggleLeft, onToggleRight } = setup(false);
    press({ ctrlKey: true, altKey: true }, "Backslash");
    press({ ctrlKey: true, altKey: true }, "Period");
    expect(onToggleLeft).not.toHaveBeenCalled();
    expect(onToggleRight).not.toHaveBeenCalled();
  });

  it("ignores auto-repeat (holding the chord doesn't flap the panel)", () => {
    const { onToggleLeft } = setup(false);
    press({ ctrlKey: true, altKey: true, repeat: true }, "BracketLeft");
    expect(onToggleLeft).not.toHaveBeenCalled();
  });

  it("ignores AltGraph chords (Ctrl+Alt produced by intl layouts)", () => {
    const { onToggleLeft, onToggleRight } = setup(false);
    const altGraph = vi
      .spyOn(KeyboardEvent.prototype, "getModifierState")
      .mockImplementation((keyArg) => keyArg === "AltGraph");
    press({ ctrlKey: true, altKey: true }, "BracketLeft");
    press({ ctrlKey: true, altKey: true }, "BracketRight");
    expect(onToggleLeft).not.toHaveBeenCalled();
    expect(onToggleRight).not.toHaveBeenCalled();
    altGraph.mockRestore();
  });

  it("still fires when getModifierState is unavailable (no throw)", () => {
    const { onToggleLeft } = setup(false);
    const ev = new KeyboardEvent("keydown", {
      code: "BracketLeft",
      ctrlKey: true,
      altKey: true,
      bubbles: true,
      cancelable: true,
    });
    // Some environments / synthetic events lack getModifierState; the handler
    // must guard the call rather than throw on every keydown.
    Object.defineProperty(ev, "getModifierState", { value: undefined, configurable: true });
    expect(() => document.body.dispatchEvent(ev)).not.toThrow();
    expect(onToggleLeft).toHaveBeenCalledTimes(1);
  });

  it("claims the event (preventDefault + stopPropagation)", () => {
    setup(false);
    const ev = new KeyboardEvent("keydown", {
      code: "BracketLeft",
      ctrlKey: true,
      altKey: true,
      bubbles: true,
      cancelable: true,
    });
    const stopSpy = vi.spyOn(ev, "stopPropagation");
    document.body.dispatchEvent(ev);
    expect(ev.defaultPrevented).toBe(true);
    expect(stopSpy).toHaveBeenCalledTimes(1);
  });

  it("unbinds on unmount", () => {
    const { onToggleLeft, unmount } = setup(false);
    unmount();
    press({ ctrlKey: true, altKey: true }, "BracketLeft");
    expect(onToggleLeft).not.toHaveBeenCalled();
  });
});
