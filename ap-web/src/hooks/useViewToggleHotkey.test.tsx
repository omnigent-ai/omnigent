// Cmd/Ctrl+P flips a terminal-first session's center surface (chat <->
// terminal). Requires Cmd/Ctrl, no Alt/Shift; desktop-only; inert (and leaves
// the native event alone) outside the Electron shell, off terminal-first
// sessions, and while a rail shell owns the view.

import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TerminalFirstContextValue } from "@/shell/TerminalFirstContext";
import { useViewToggleHotkey } from "./useViewToggleHotkey";

// Desktop-only (Cmd+P is Print in a browser); default the mock to "native"
// and flip it per-test for the browser case.
const isNativeShell = vi.fn(() => true);
vi.mock("@/lib/nativeBridge", () => ({
  isNativeShell: () => isNativeShell(),
}));

/** Build a context value, overriding only the fields a test cares about. */
function ctx(overrides: Partial<TerminalFirstContextValue> = {}): TerminalFirstContextValue {
  return {
    isClaudeNative: false,
    isNativeWrapper: false,
    isTerminalFirst: true,
    isShellView: false,
    view: "chat",
    terminalViewKey: null,
    setView: vi.fn(),
    terminalsAvailable: true,
    terminalStartingUp: false,
    ...overrides,
  };
}

/** Dispatch a "p" keydown bubbling to window; returns the event for
 *  preventDefault assertions. */
function press(
  mods: Partial<Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey">> = {
    metaKey: true,
  },
  key = "p",
): KeyboardEvent {
  const e = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...mods });
  document.body.dispatchEvent(e);
  return e;
}

beforeEach(() => {
  isNativeShell.mockReturnValue(true);
});

describe("useViewToggleHotkey", () => {
  it("Cmd+P flips chat -> terminal", () => {
    const c = ctx({ view: "chat" });
    renderHook(() => useViewToggleHotkey(c));
    const e = press();
    expect(c.setView).toHaveBeenCalledWith("terminal");
    expect(e.defaultPrevented).toBe(true);
  });

  it("Cmd+P flips terminal -> chat", () => {
    const c = ctx({ view: "terminal" });
    renderHook(() => useViewToggleHotkey(c));
    press();
    expect(c.setView).toHaveBeenCalledWith("chat");
  });

  it("Ctrl+P works the same as Cmd+P", () => {
    const c = ctx({ view: "chat" });
    renderHook(() => useViewToggleHotkey(c));
    press({ ctrlKey: true });
    expect(c.setView).toHaveBeenCalledWith("terminal");
  });

  it("accepts an uppercase P (defensive against layout/case)", () => {
    const c = ctx({ view: "chat" });
    renderHook(() => useViewToggleHotkey(c));
    press({ metaKey: true }, "P");
    expect(c.setView).toHaveBeenCalledWith("terminal");
  });

  it("does nothing without a modifier", () => {
    const c = ctx();
    renderHook(() => useViewToggleHotkey(c));
    const e = press({});
    expect(c.setView).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("ignores Alt+P and Shift+P", () => {
    const c = ctx();
    renderHook(() => useViewToggleHotkey(c));
    press({ metaKey: true, altKey: true });
    press({ metaKey: true, shiftKey: true });
    expect(c.setView).not.toHaveBeenCalled();
  });

  it("is inert in a plain browser, leaving Print untouched", () => {
    isNativeShell.mockReturnValue(false);
    const c = ctx();
    renderHook(() => useViewToggleHotkey(c));
    const e = press();
    expect(c.setView).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("is inert on a non-terminal-first session, leaving Print untouched", () => {
    const c = ctx({ isTerminalFirst: false });
    renderHook(() => useViewToggleHotkey(c));
    const e = press();
    expect(c.setView).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("is inert while a rail shell owns the view", () => {
    const c = ctx({ isShellView: true });
    renderHook(() => useViewToggleHotkey(c));
    const e = press();
    expect(c.setView).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("is inert (no throw) when the context is null", () => {
    renderHook(() => useViewToggleHotkey(null));
    const e = press();
    expect(e.defaultPrevented).toBe(false);
  });

  it("reads the live context after a re-render without rebinding", () => {
    const first = ctx({ view: "chat" });
    const { rerender } = renderHook((c: TerminalFirstContextValue) => useViewToggleHotkey(c), {
      initialProps: first,
    });
    const second = ctx({ view: "terminal" });
    rerender(second);
    press();
    expect(first.setView).not.toHaveBeenCalled();
    expect(second.setView).toHaveBeenCalledWith("chat");
  });
});
