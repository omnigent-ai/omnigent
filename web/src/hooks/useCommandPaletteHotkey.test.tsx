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

  /** Focus an input inside a container with `className`, e.g. "xterm". */
  function focusInside(className: string): void {
    const surface = document.createElement("div");
    surface.className = className;
    const input = document.createElement("input");
    surface.appendChild(input);
    document.body.appendChild(surface);
    input.focus();
    expect(document.activeElement).toBe(input);
  }

  it("claims the chord in the capture phase, ahead of host-page listeners", () => {
    // The desktop shell renders the embed build over a CSS-hidden host page
    // with its own ⌘K listener; the chord must reach us, not them. Dispatch
    // from a deep node so the event actually propagates (dispatching on
    // window would never reach document listeners regardless).
    const onToggle = vi.fn();
    const hostListener = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle));
    document.addEventListener("keydown", hostListener);
    const deep = document.createElement("div");
    document.body.appendChild(deep);

    deep.dispatchEvent(
      new KeyboardEvent("keydown", { key: "k", metaKey: true, bubbles: true, cancelable: true }),
    );

    expect(onToggle).toHaveBeenCalledTimes(1);
    expect(hostListener).not.toHaveBeenCalled();
    document.removeEventListener("keydown", hostListener);
  });

  it("lets the chord through to host-page listeners when a focused surface owns it", () => {
    const onToggle = vi.fn();
    const hostListener = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle));
    document.addEventListener("keydown", hostListener);
    focusInside("monaco-editor");

    // Dispatch from the focused input so the event propagates up to document.
    document.activeElement?.dispatchEvent(
      new KeyboardEvent("keydown", { key: "k", metaKey: true, bubbles: true, cancelable: true }),
    );

    expect(onToggle).not.toHaveBeenCalled();
    expect(hostListener).toHaveBeenCalledTimes(1);
    document.removeEventListener("keydown", hostListener);
  });

  it("bails on Ctrl+K in a terminal — xterm sends it to the PTY as ^K", () => {
    const onToggle = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle));
    focusInside("xterm");

    press({ key: "k", ctrlKey: true });

    expect(onToggle).not.toHaveBeenCalled();
  });

  it("claims Cmd+K in a terminal — xterm drops Cmd chords, so nothing owns it", () => {
    const onToggle = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle));
    focusInside("xterm");

    press({ key: "k", metaKey: true });

    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("bails on both variants inside the code editor (⌘K is a chord prefix)", () => {
    const onToggle = vi.fn();
    renderHook(() => useCommandPaletteHotkey(onToggle));
    focusInside("monaco-editor");

    press({ key: "k", metaKey: true });
    press({ key: "k", ctrlKey: true });

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
