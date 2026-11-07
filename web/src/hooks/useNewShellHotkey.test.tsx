import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { isTerminalHotkey, useNewShellHotkey } from "./useNewShellHotkey";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

function event(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
}

function press(init: KeyboardEventInit, target: HTMLElement = document.body): KeyboardEvent {
  const e = event(init);
  target.dispatchEvent(e);
  return e;
}

describe("⌃` terminal chord", () => {
  it("matches literal Control plus the backquote key only", () => {
    expect(isTerminalHotkey(event({ code: "Backquote", ctrlKey: true }))).toBe(true);
    expect(isTerminalHotkey(event({ code: "Backquote", metaKey: true }))).toBe(false);
    expect(isTerminalHotkey(event({ code: "Backquote", ctrlKey: true, shiftKey: true }))).toBe(
      false,
    );
    // AltGr arrives as Ctrl+Alt on Windows/Linux layouts.
    expect(isTerminalHotkey(event({ code: "Backquote", ctrlKey: true, altKey: true }))).toBe(false);
    expect(isTerminalHotkey(event({ code: "KeyT", ctrlKey: true }))).toBe(false);
  });

  it("opens a shell even from a focused terminal, and claims the chord", () => {
    const onLaunch = vi.fn();
    renderHook(() => useNewShellHotkey(onLaunch, true));
    const terminal = document.createElement("div");
    terminal.className = "xterm";
    const input = document.createElement("textarea");
    terminal.append(input);
    document.body.append(terminal);
    input.focus();

    const pressed = press({ code: "Backquote", ctrlKey: true }, input);
    expect(onLaunch).toHaveBeenCalledTimes(1);
    expect(pressed.defaultPrevented).toBe(true);
  });

  it("leaves Cmd/Ctrl+Alt+T alone, since ⌃` is the one shell chord", () => {
    const onLaunch = vi.fn();
    renderHook(() => useNewShellHotkey(onLaunch, true));
    for (const modifiers of [{ metaKey: true }, { ctrlKey: true }]) {
      expect(press({ code: "KeyT", altKey: true, ...modifiers }).defaultPrevented).toBe(false);
    }
    expect(onLaunch).not.toHaveBeenCalled();
  });

  it("ignores auto-repeat and a disabled hook", () => {
    const onLaunch = vi.fn();
    const { rerender } = renderHook(({ enabled }) => useNewShellHotkey(onLaunch, enabled), {
      initialProps: { enabled: true },
    });
    press({ code: "Backquote", ctrlKey: true, repeat: true });
    expect(onLaunch).not.toHaveBeenCalled();
    rerender({ enabled: false });
    press({ code: "Backquote", ctrlKey: true });
    expect(onLaunch).not.toHaveBeenCalled();
  });
});
