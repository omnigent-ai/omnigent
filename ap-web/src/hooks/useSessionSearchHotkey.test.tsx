// Cmd/Ctrl+Shift+F focuses sidebar session search. If the sidebar is closed,
// the hook opens it first; it ignores bare/Cmd-only/Alt-modified keys.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isSessionSearchHotkey, useSessionSearchHotkey } from "./useSessionSearchHotkey";

function press(
  key: string,
  mods: Partial<Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey">> = {
    metaKey: true,
    shiftKey: true,
  },
): KeyboardEvent {
  const e = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...mods });
  window.dispatchEvent(e);
  return e;
}

beforeEach(() => {
  document.body.innerHTML = "";
});
afterEach(() => {
  document.body.innerHTML = "";
});

describe("isSessionSearchHotkey", () => {
  it("matches Cmd/Ctrl+Shift+F without Alt", () => {
    expect(isSessionSearchHotkey(press("F", { metaKey: true, shiftKey: true }))).toBe(true);
    expect(isSessionSearchHotkey(press("f", { ctrlKey: true, shiftKey: true }))).toBe(true);
  });

  it("rejects incomplete or conflicting chords", () => {
    expect(isSessionSearchHotkey(press("f", { metaKey: true }))).toBe(false);
    expect(isSessionSearchHotkey(press("f", { shiftKey: true }))).toBe(false);
    expect(isSessionSearchHotkey(press("f", { metaKey: true, shiftKey: true, altKey: true }))).toBe(
      false,
    );
  });
});

describe("useSessionSearchHotkey", () => {
  it("focuses search without opening when the sidebar is already open", () => {
    const onOpenSidebar = vi.fn();
    const onFocusSearch = vi.fn();
    renderHook(() => useSessionSearchHotkey({ sidebarOpen: true, onOpenSidebar, onFocusSearch }));

    const e = press("F");

    expect(e.defaultPrevented).toBe(true);
    expect(onOpenSidebar).not.toHaveBeenCalled();
    expect(onFocusSearch).toHaveBeenCalledTimes(1);
  });

  it("opens the sidebar before focusing search when closed", () => {
    const onOpenSidebar = vi.fn();
    const onFocusSearch = vi.fn();
    renderHook(() => useSessionSearchHotkey({ sidebarOpen: false, onOpenSidebar, onFocusSearch }));

    press("F");

    expect(onOpenSidebar).toHaveBeenCalledTimes(1);
    expect(onFocusSearch).toHaveBeenCalledTimes(1);
  });

  it("supports Ctrl+Shift+F for Windows/Linux", () => {
    const onOpenSidebar = vi.fn();
    const onFocusSearch = vi.fn();
    renderHook(() => useSessionSearchHotkey({ sidebarOpen: true, onOpenSidebar, onFocusSearch }));

    press("F", { ctrlKey: true, shiftKey: true });

    expect(onFocusSearch).toHaveBeenCalledTimes(1);
  });

  it("ignores non-matching chords", () => {
    const onOpenSidebar = vi.fn();
    const onFocusSearch = vi.fn();
    renderHook(() => useSessionSearchHotkey({ sidebarOpen: false, onOpenSidebar, onFocusSearch }));

    press("F", { metaKey: true });
    press("F", { metaKey: true, shiftKey: true, altKey: true });

    expect(onOpenSidebar).not.toHaveBeenCalled();
    expect(onFocusSearch).not.toHaveBeenCalled();
  });
});
