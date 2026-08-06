import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  isCommandPaletteHotkey,
  isSessionSearchHotkey,
  useCommandPaletteHotkey,
  useSessionSearchHotkey,
} from "./usePaletteHotkeys";

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

describe("isSessionSearchHotkey", () => {
  it("matches Cmd+Shift+F and Ctrl+Shift+F", () => {
    for (const mod of [{ metaKey: true }, { ctrlKey: true }]) {
      expect(
        isSessionSearchHotkey(new KeyboardEvent("keydown", { key: "f", shiftKey: true, ...mod })),
      ).toBe(true);
      // Shift+f reports as "F" on most layouts; accept both.
      expect(
        isSessionSearchHotkey(new KeyboardEvent("keydown", { key: "F", shiftKey: true, ...mod })),
      ).toBe(true);
    }
  });

  it("rejects the chord without Shift, with Alt, or on another key", () => {
    expect(isSessionSearchHotkey(new KeyboardEvent("keydown", { key: "f", metaKey: true }))).toBe(
      false,
    );
    expect(
      isSessionSearchHotkey(
        new KeyboardEvent("keydown", { key: "f", metaKey: true, shiftKey: true, altKey: true }),
      ),
    ).toBe(false);
    expect(
      isSessionSearchHotkey(
        new KeyboardEvent("keydown", { key: "g", metaKey: true, shiftKey: true }),
      ),
    ).toBe(false);
    expect(isSessionSearchHotkey(new KeyboardEvent("keydown", { key: "f", shiftKey: true }))).toBe(
      false,
    );
  });

  it("does not overlap the command-palette chord", () => {
    const palette = new KeyboardEvent("keydown", { key: "k", metaKey: true });
    const search = new KeyboardEvent("keydown", { key: "f", metaKey: true, shiftKey: true });
    expect(isSessionSearchHotkey(palette)).toBe(false);
    expect(isCommandPaletteHotkey(search)).toBe(false);
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

  it("leaves Ctrl+K to a terminal but claims Cmd+K, which xterm drops", () => {
    const onToggle = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle));

    const term = document.createElement("div");
    term.className = "xterm";
    const input = document.createElement("input");
    term.appendChild(input);
    document.body.appendChild(term);
    input.focus();
    expect(document.activeElement).toBe(input);

    press({ key: "k", ctrlKey: true });
    expect(onToggle).not.toHaveBeenCalled();

    press({ key: "k", metaKey: true });

    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("unbinds on unmount", () => {
    const onToggle = vi.fn();
    const { unmount } = renderHook(() => useCommandPaletteHotkey(onToggle));
    unmount();

    press({ key: "k", metaKey: true });

    expect(onToggle).not.toHaveBeenCalled();
  });
});

describe("useSessionSearchHotkey", () => {
  it("toggles on Cmd+Shift+F and prevents the browser default", () => {
    const onToggle = vi.fn();
    renderHook(() => useSessionSearchHotkey(onToggle));

    const e = press({ key: "f", metaKey: true, shiftKey: true });

    expect(onToggle).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(true);
  });

  it("ignores auto-repeat", () => {
    const onToggle = vi.fn();
    renderHook(() => useSessionSearchHotkey(onToggle));

    press({ key: "f", metaKey: true, shiftKey: true, repeat: true });

    expect(onToggle).not.toHaveBeenCalled();
  });

  it("does nothing when disabled", () => {
    const onToggle = vi.fn();
    renderHook(() => useSessionSearchHotkey(onToggle, false));

    const e = press({ key: "f", metaKey: true, shiftKey: true });

    expect(onToggle).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("bails when focus sits inside a terminal or code editor", () => {
    const onToggle = vi.fn();
    renderHook(() => useSessionSearchHotkey(onToggle));

    const editor = document.createElement("div");
    editor.className = "monaco-editor";
    const input = document.createElement("input");
    editor.appendChild(input);
    document.body.appendChild(editor);
    input.focus();
    expect(document.activeElement).toBe(input);

    press({ key: "f", metaKey: true, shiftKey: true });

    expect(onToggle).not.toHaveBeenCalled();
  });

  it("unbinds on unmount", () => {
    const onToggle = vi.fn();
    const { unmount } = renderHook(() => useSessionSearchHotkey(onToggle));
    unmount();

    press({ key: "f", metaKey: true, shiftKey: true });

    expect(onToggle).not.toHaveBeenCalled();
  });
});
