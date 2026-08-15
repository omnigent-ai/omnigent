import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TerminalFirstContextValue } from "@/shell/TerminalFirstContext";
import { canToggleTerminalFirstView, useViewModeToggleHotkey } from "./useViewModeToggleHotkey";

const isIOSShellMock = vi.fn(() => false);
vi.mock("@/lib/nativeBridge", () => ({
  isIOSShell: () => isIOSShellMock(),
}));

function makeCtx(overrides: Partial<TerminalFirstContextValue> = {}): TerminalFirstContextValue {
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

function press(
  target: Element = document.body,
  mods: Partial<Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey" | "repeat">> = {
    ctrlKey: true,
    altKey: true,
  },
  code = "KeyT",
): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    code,
    key: "t",
    bubbles: true,
    cancelable: true,
    ...mods,
  });
  target.dispatchEvent(event);
  return event;
}

beforeEach(() => {
  isIOSShellMock.mockReturnValue(false);
});

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe("canToggleTerminalFirstView", () => {
  it("requires a live terminal-first view toggle", () => {
    expect(canToggleTerminalFirstView(makeCtx())).toBe(true);
    expect(canToggleTerminalFirstView(makeCtx({ isTerminalFirst: false }))).toBe(false);
    expect(canToggleTerminalFirstView(makeCtx({ isShellView: true }))).toBe(false);
    expect(canToggleTerminalFirstView(makeCtx({ view: "chat", terminalsAvailable: false }))).toBe(
      false,
    );
    expect(
      canToggleTerminalFirstView(makeCtx({ view: "terminal", terminalsAvailable: false })),
    ).toBe(true);
  });

  it("is unavailable in the iOS shell where the native bar owns switching", () => {
    isIOSShellMock.mockReturnValue(true);
    expect(canToggleTerminalFirstView(makeCtx())).toBe(false);
  });
});

describe("useViewModeToggleHotkey", () => {
  it("toggles Chat to Terminal with Ctrl+Alt+T and Cmd+Option+T", () => {
    const chatCtx = makeCtx();
    const { rerender } = renderHook(({ ctx }) => useViewModeToggleHotkey(ctx), {
      initialProps: { ctx: chatCtx },
    });

    press(document.body, { ctrlKey: true, altKey: true });
    expect(chatCtx.setView).toHaveBeenCalledWith("terminal");

    const terminalCtx = makeCtx({ view: "terminal" });
    rerender({ ctx: terminalCtx });
    press(document.body, { metaKey: true, altKey: true });
    expect(terminalCtx.setView).toHaveBeenCalledWith("chat");
  });

  it("fires from editable fields without inserting text", () => {
    const ctx = makeCtx();
    renderHook(() => useViewModeToggleHotkey(ctx));
    const input = document.createElement("input");
    document.body.append(input);
    input.focus();

    const event = press(input);

    expect(ctx.setView).toHaveBeenCalledWith("terminal");
    expect(event.defaultPrevented).toBe(true);
    expect(input.value).toBe("");
  });

  it("deliberately fires while xterm owns focus", () => {
    const ctx = makeCtx({ view: "terminal" });
    renderHook(() => useViewModeToggleHotkey(ctx));
    const xterm = document.createElement("div");
    xterm.className = "xterm";
    const textarea = document.createElement("textarea");
    xterm.append(textarea);
    document.body.append(xterm);
    textarea.focus();

    press(textarea);

    expect(ctx.setView).toHaveBeenCalledWith("chat");
    expect(textarea.value).toBe("");
  });

  it("does not switch to an unavailable or starting terminal", () => {
    const ctx = makeCtx({ terminalsAvailable: false, terminalStartingUp: true });
    renderHook(() => useViewModeToggleHotkey(ctx));

    press();

    expect(ctx.setView).not.toHaveBeenCalled();
  });

  it("is inert outside terminal-first mode, in shell view, on iOS, and when disabled", () => {
    const contexts = [makeCtx({ isTerminalFirst: false }), makeCtx({ isShellView: true })];
    for (const ctx of contexts) {
      const { unmount } = renderHook(() => useViewModeToggleHotkey(ctx));
      press();
      expect(ctx.setView).not.toHaveBeenCalled();
      unmount();
    }

    const iosCtx = makeCtx();
    isIOSShellMock.mockReturnValue(true);
    const { unmount } = renderHook(() => useViewModeToggleHotkey(iosCtx));
    press();
    expect(iosCtx.setView).not.toHaveBeenCalled();
    unmount();

    isIOSShellMock.mockReturnValue(false);
    const embeddedCtx = makeCtx();
    renderHook(() => useViewModeToggleHotkey(embeddedCtx, false));
    press();
    expect(embeddedCtx.setView).not.toHaveBeenCalled();
  });

  it("ignores repeat, Shift, AltGraph, and unrelated chords", () => {
    const ctx = makeCtx();
    renderHook(() => useViewModeToggleHotkey(ctx));
    press(document.body, { ctrlKey: true, altKey: true, repeat: true });
    press(document.body, { ctrlKey: true, altKey: true, shiftKey: true });
    press(document.body, { ctrlKey: true }, "KeyT");
    press(document.body, { ctrlKey: true, altKey: true }, "KeyY");
    vi.spyOn(KeyboardEvent.prototype, "getModifierState").mockImplementation(
      (modifier) => modifier === "AltGraph",
    );
    press();

    expect(ctx.setView).not.toHaveBeenCalled();
  });

  it("claims the chord and unbinds on unmount", () => {
    const ctx = makeCtx();
    const { unmount } = renderHook(() => useViewModeToggleHotkey(ctx));
    const event = press();
    expect(event.defaultPrevented).toBe(true);
    expect(ctx.setView).toHaveBeenCalledTimes(1);

    unmount();
    press();
    expect(ctx.setView).toHaveBeenCalledTimes(1);
  });
});
