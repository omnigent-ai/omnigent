// ⌘/Ctrl+[ toggles the left sidebar, ⌘/Ctrl+] the right; ignores the bare keys
// and Alt/Shift-modified variants, and unbinds on unmount.

import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useSidebarToggleHotkeys } from "./useSidebarToggleHotkeys";

/** Dispatch a keydown that reaches window from body (default: Ctrl+[). */
function press(
  mods: Partial<Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey" | "repeat">> = {
    ctrlKey: true,
  },
  key = "[",
): void {
  document.body.dispatchEvent(
    new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...mods }),
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
  it("Ctrl+[ toggles only the left sidebar", () => {
    const { onToggleLeft, onToggleRight } = setup();
    press({ ctrlKey: true }, "[");
    expect(onToggleLeft).toHaveBeenCalledTimes(1);
    expect(onToggleRight).not.toHaveBeenCalled();
  });

  it("Ctrl+] toggles only the right sidebar", () => {
    const { onToggleLeft, onToggleRight } = setup();
    press({ ctrlKey: true }, "]");
    expect(onToggleRight).toHaveBeenCalledTimes(1);
    expect(onToggleLeft).not.toHaveBeenCalled();
  });

  it("Cmd variants also fire (macOS)", () => {
    const { onToggleLeft, onToggleRight } = setup();
    press({ metaKey: true }, "[");
    press({ metaKey: true }, "]");
    expect(onToggleLeft).toHaveBeenCalledTimes(1);
    expect(onToggleRight).toHaveBeenCalledTimes(1);
  });

  it("ignores bare keys and Alt/Shift-modified variants", () => {
    const { onToggleLeft, onToggleRight } = setup();
    press({}, "["); // bare [
    press({ ctrlKey: true, shiftKey: true }, "[");
    press({ metaKey: true, altKey: true }, "]");
    expect(onToggleLeft).not.toHaveBeenCalled();
    expect(onToggleRight).not.toHaveBeenCalled();
  });

  it("ignores other keys held with the modifier", () => {
    const { onToggleLeft, onToggleRight } = setup();
    press({ ctrlKey: true }, "/");
    press({ metaKey: true }, ".");
    expect(onToggleLeft).not.toHaveBeenCalled();
    expect(onToggleRight).not.toHaveBeenCalled();
  });

  it("ignores auto-repeat (holding the chord doesn't flap the panel)", () => {
    const { onToggleLeft } = setup();
    press({ ctrlKey: true, repeat: true }, "[");
    expect(onToggleLeft).not.toHaveBeenCalled();
  });

  it("claims the event (suppresses the browser Back/Forward gesture)", () => {
    setup();
    const ev = new KeyboardEvent("keydown", {
      key: "[",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    document.body.dispatchEvent(ev);
    expect(ev.defaultPrevented).toBe(true);
  });

  it("unbinds on unmount", () => {
    const { onToggleLeft, unmount } = setup();
    unmount();
    press({ ctrlKey: true }, "[");
    expect(onToggleLeft).not.toHaveBeenCalled();
  });
});
