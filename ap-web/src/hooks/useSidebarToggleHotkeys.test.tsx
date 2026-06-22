// ⌘⌥[ toggles the left sidebar, ⌘⌥] the right; matches the physical bracket
// keys (not the glyph ⌥ produces), ignores the bare keys / missing-Alt / Shift
// variants / auto-repeat / AltGraph, fully claims the event, and unbinds on
// unmount.

import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useSidebarToggleHotkeys } from "./useSidebarToggleHotkeys";

/** Dispatch a keydown that reaches window from body (default: Ctrl+Alt+[). */
function press(
  mods: Partial<
    Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey" | "repeat">
  > = { ctrlKey: true, altKey: true },
  code = "BracketLeft",
): void {
  document.body.dispatchEvent(
    new KeyboardEvent("keydown", { code, bubbles: true, cancelable: true, ...mods }),
  );
}

afterEach(() => vi.restoreAllMocks());

function setup() {
  const onToggleLeft = vi.fn();
  const onToggleRight = vi.fn();
  const utils = renderHook(() => useSidebarToggleHotkeys({ onToggleLeft, onToggleRight }));
  return { onToggleLeft, onToggleRight, ...utils };
}

describe("useSidebarToggleHotkeys", () => {
  it("Ctrl+Alt+[ toggles only the left sidebar", () => {
    const { onToggleLeft, onToggleRight } = setup();
    press({ ctrlKey: true, altKey: true }, "BracketLeft");
    expect(onToggleLeft).toHaveBeenCalledTimes(1);
    expect(onToggleRight).not.toHaveBeenCalled();
  });

  it("Ctrl+Alt+] toggles only the right sidebar", () => {
    const { onToggleLeft, onToggleRight } = setup();
    press({ ctrlKey: true, altKey: true }, "BracketRight");
    expect(onToggleRight).toHaveBeenCalledTimes(1);
    expect(onToggleLeft).not.toHaveBeenCalled();
  });

  it("Cmd variants also fire (macOS)", () => {
    const { onToggleLeft, onToggleRight } = setup();
    press({ metaKey: true, altKey: true }, "BracketLeft");
    press({ metaKey: true, altKey: true }, "BracketRight");
    expect(onToggleLeft).toHaveBeenCalledTimes(1);
    expect(onToggleRight).toHaveBeenCalledTimes(1);
  });

  it("ignores the bare keys, missing-Alt, and Shift variants", () => {
    const { onToggleLeft, onToggleRight } = setup();
    press({}, "BracketLeft"); // bare [
    press({ ctrlKey: true }, "BracketLeft"); // ⌘[ alone = browser Back, not ours
    press({ metaKey: true, altKey: true, shiftKey: true }, "BracketRight");
    expect(onToggleLeft).not.toHaveBeenCalled();
    expect(onToggleRight).not.toHaveBeenCalled();
  });

  it("ignores other keys held with the modifiers", () => {
    const { onToggleLeft, onToggleRight } = setup();
    press({ ctrlKey: true, altKey: true }, "Backslash");
    press({ metaKey: true, altKey: true }, "Period");
    expect(onToggleLeft).not.toHaveBeenCalled();
    expect(onToggleRight).not.toHaveBeenCalled();
  });

  it("ignores auto-repeat (holding the chord doesn't flap the panel)", () => {
    const { onToggleLeft } = setup();
    press({ ctrlKey: true, altKey: true, repeat: true }, "BracketLeft");
    expect(onToggleLeft).not.toHaveBeenCalled();
  });

  it("ignores AltGraph chords (Ctrl+Alt produced by intl layouts)", () => {
    const { onToggleLeft, onToggleRight } = setup();
    const altGraph = vi
      .spyOn(KeyboardEvent.prototype, "getModifierState")
      .mockImplementation((keyArg) => keyArg === "AltGraph");
    press({ ctrlKey: true, altKey: true }, "BracketLeft");
    press({ ctrlKey: true, altKey: true }, "BracketRight");
    expect(onToggleLeft).not.toHaveBeenCalled();
    expect(onToggleRight).not.toHaveBeenCalled();
    altGraph.mockRestore();
  });

  it("claims the event (preventDefault + stopPropagation)", () => {
    setup();
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
    const { onToggleLeft, unmount } = setup();
    unmount();
    press({ ctrlKey: true, altKey: true }, "BracketLeft");
    expect(onToggleLeft).not.toHaveBeenCalled();
  });
});
