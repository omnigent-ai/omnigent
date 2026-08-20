import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { isCommandPaletteHotkey, useCommandPaletteHotkey } from "./useCommandPaletteHotkey";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

function press(init: KeyboardEventInit): KeyboardEvent {
  const e = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  window.dispatchEvent(e);
  return e;
}

describe("isCommandPaletteHotkey", () => {
  it("matches Cmd+K and Ctrl+K", () => {
    expect(isCommandPaletteHotkey(new KeyboardEvent("keydown", { key: "k", metaKey: true }))).toBe(
      true,
    );
    expect(isCommandPaletteHotkey(new KeyboardEvent("keydown", { key: "k", ctrlKey: true }))).toBe(
      true,
    );
    // Uppercase (some layouts report "K" with the modifier).
    expect(isCommandPaletteHotkey(new KeyboardEvent("keydown", { key: "K", metaKey: true }))).toBe(
      true,
    );
  });

  it("rejects plain k, and k with Alt or Shift held", () => {
    expect(isCommandPaletteHotkey(new KeyboardEvent("keydown", { key: "k" }))).toBe(false);
    expect(
      isCommandPaletteHotkey(
        new KeyboardEvent("keydown", { key: "k", metaKey: true, altKey: true }),
      ),
    ).toBe(false);
    expect(
      isCommandPaletteHotkey(
        new KeyboardEvent("keydown", { key: "k", ctrlKey: true, shiftKey: true }),
      ),
    ).toBe(false);
  });

  it("rejects other keys with the modifier", () => {
    expect(isCommandPaletteHotkey(new KeyboardEvent("keydown", { key: "j", metaKey: true }))).toBe(
      false,
    );
  });
});

describe("useCommandPaletteHotkey", () => {
  it("toggles on Cmd+K and prevents the browser default", () => {
    const onToggle = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle));

    const e = press({ key: "k", metaKey: true });

    expect(onToggle).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(true);
  });

  it("ignores auto-repeat", () => {
    const onToggle = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle));

    press({ key: "k", metaKey: true, repeat: true });

    expect(onToggle).not.toHaveBeenCalled();
  });

  it("does nothing when disabled", () => {
    const onToggle = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle, false));

    const e = press({ key: "k", metaKey: true });

    expect(onToggle).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("bails when focus sits inside a terminal or code editor", () => {
    const onToggle = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle));

    const term = document.createElement("div");
    term.className = "xterm";
    const input = document.createElement("input");
    term.appendChild(input);
    document.body.appendChild(term);
    input.focus();
    expect(document.activeElement).toBe(input);

    press({ key: "k", metaKey: true });

    expect(onToggle).not.toHaveBeenCalled();
  });

  it("unbinds on unmount", () => {
    const onToggle = vi.fn();
    const { unmount } = renderHook(() => useCommandPaletteHotkey(onToggle));
    unmount();

    press({ key: "k", metaKey: true });

    expect(onToggle).not.toHaveBeenCalled();
  });
});
