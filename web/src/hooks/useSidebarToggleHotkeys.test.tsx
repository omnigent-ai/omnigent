// ⌘⌥[ toggles the left sidebar; ⌘B and ⌘⌥] toggle the right. Matches physical
// keys, ignores modifier variants / auto-repeat / AltGraph, fully claims the
// event from editable surfaces, and unbinds on unmount.

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
  target: HTMLElement = document.body,
): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    code,
    bubbles: true,
    cancelable: true,
    ...mods,
  });
  target.dispatchEvent(event);
  return event;
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

  it("Cmd/Ctrl+B toggles only the right sidebar", () => {
    const { onToggleLeft, onToggleRight } = setup();
    press({ metaKey: true }, "KeyB");
    press({ ctrlKey: true }, "KeyB");
    expect(onToggleRight).toHaveBeenCalledTimes(2);
    expect(onToggleLeft).not.toHaveBeenCalled();
  });

  it("Cmd variants of the legacy bracket chords still fire (macOS)", () => {
    const { onToggleLeft, onToggleRight } = setup();
    press({ metaKey: true, altKey: true }, "BracketLeft");
    press({ metaKey: true, altKey: true }, "BracketRight");
    expect(onToggleLeft).toHaveBeenCalledTimes(1);
    expect(onToggleRight).toHaveBeenCalledTimes(1);
  });

  it("leaves bare B and its Shift/Alt variants available", () => {
    const { onToggleLeft, onToggleRight } = setup();
    press({}, "KeyB");
    press({ ctrlKey: true, shiftKey: true }, "KeyB");
    press({ metaKey: true, altKey: true }, "KeyB");
    expect(onToggleLeft).not.toHaveBeenCalled();
    expect(onToggleRight).not.toHaveBeenCalled();
  });

  it("ignores the bare brackets, missing-Alt, and Shift bracket variants", () => {
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
    const { onToggleLeft, onToggleRight } = setup();
    press({ ctrlKey: true, altKey: true, repeat: true }, "BracketLeft");
    press({ ctrlKey: true, repeat: true }, "KeyB");
    expect(onToggleLeft).not.toHaveBeenCalled();
    expect(onToggleRight).not.toHaveBeenCalled();
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

  it("still fires when getModifierState is unavailable (no throw)", () => {
    const { onToggleLeft } = setup();
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

  it.each([
    ["composer", "textarea", ""],
    ["terminal", "textarea", "xterm-helper-textarea"],
    ["file editor", "textarea", "inputarea"],
  ])("claims Cmd+B before the focused %s handles it", (_name, tag, className) => {
    const { onToggleRight } = setup();
    const target = document.createElement(tag);
    target.className = className;
    document.body.appendChild(target);
    target.focus();
    const targetHandler = vi.fn();
    target.addEventListener("keydown", targetHandler);

    const event = press({ metaKey: true }, "KeyB", target);

    expect(onToggleRight).toHaveBeenCalledTimes(1);
    expect(event.defaultPrevented).toBe(true);
    expect(targetHandler).not.toHaveBeenCalled();
    target.remove();
  });

  it("unbinds on unmount", () => {
    const { onToggleLeft, unmount } = setup();
    unmount();
    press({ ctrlKey: true, altKey: true }, "BracketLeft");
    expect(onToggleLeft).not.toHaveBeenCalled();
  });
});
