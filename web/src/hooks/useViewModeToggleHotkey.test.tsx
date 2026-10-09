import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useViewModeToggleHotkey } from "./useViewModeToggleHotkey";

function keydown(init: KeyboardEventInit = { ctrlKey: true, altKey: true }): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    code: "Backslash",
    bubbles: true,
    cancelable: true,
    ...init,
  });
  document.body.dispatchEvent(event);
  return event;
}

function setup(view: "chat" | "terminal" = "chat", enabled = true, isMac = false) {
  const setView = vi.fn();
  const result = renderHook(() => useViewModeToggleHotkey({ enabled, view, setView }, isMac));
  return { setView, ...result };
}

afterEach(() => vi.restoreAllMocks());

describe("useViewModeToggleHotkey", () => {
  it("switches from Chat to Terminal with Ctrl+Alt+\\", () => {
    const { setView } = setup("chat");
    keydown();
    expect(setView).toHaveBeenCalledWith("terminal");
  });

  it("switches from Terminal to Chat with Ctrl+Alt+\\", () => {
    const { setView } = setup("terminal");
    keydown();
    expect(setView).toHaveBeenCalledWith("chat");
  });

  it("uses Cmd instead of Ctrl on macOS", () => {
    const { setView } = setup("chat", true, true);
    keydown({ metaKey: true, altKey: true });
    expect(setView).toHaveBeenCalledWith("terminal");
  });

  it("ignores disabled, incomplete, shifted, repeated, and AltGraph chords", () => {
    const disabled = setup("chat", false);
    keydown();
    expect(disabled.setView).not.toHaveBeenCalled();

    const active = setup();
    keydown({ ctrlKey: true });
    keydown({ ctrlKey: true, altKey: true, shiftKey: true });
    keydown({ ctrlKey: true, altKey: true, repeat: true });
    const altGraph = vi
      .spyOn(KeyboardEvent.prototype, "getModifierState")
      .mockImplementation((key) => key === "AltGraph");
    keydown();
    expect(active.setView).not.toHaveBeenCalled();
    altGraph.mockRestore();
  });

  it("claims the chord before the focused terminal can consume it", () => {
    setup();
    const event = new KeyboardEvent("keydown", {
      code: "Backslash",
      ctrlKey: true,
      altKey: true,
      bubbles: true,
      cancelable: true,
    });
    const stop = vi.spyOn(event, "stopPropagation");
    document.body.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(stop).toHaveBeenCalledOnce();
  });

  it("unbinds on unmount", () => {
    const { setView, unmount } = setup();
    unmount();
    keydown();
    expect(setView).not.toHaveBeenCalled();
  });
});
