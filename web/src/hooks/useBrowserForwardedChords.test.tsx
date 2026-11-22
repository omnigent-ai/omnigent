import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BrowserForwardedKeydown } from "@/lib/nativeBridge";

import { useCommandPaletteHotkey } from "./useCommandPaletteHotkey";
import {
  forwardedChordsFor,
  tabsSidebarForwardedShortcuts,
  useBrowserForwardedChords,
} from "./useBrowserForwardedChords";

function setBridge(bridge: Record<string, unknown> | undefined) {
  if (bridge) {
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      ...bridge,
    };
  } else {
    delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  }
}

function capableBridge() {
  let forward: ((input: BrowserForwardedKeydown) => void) | null = null;
  const unsubscribe = vi.fn();
  const browserSetForwardedChords = vi.fn().mockResolvedValue({ ok: true });
  setBridge({
    browserSetForwardedChords,
    onBrowserForwardedInput: (callback: (input: BrowserForwardedKeydown) => void) => {
      forward = callback;
      return unsubscribe;
    },
  });
  return {
    browserSetForwardedChords,
    unsubscribe,
    forward: (input: BrowserForwardedKeydown) => forward?.(input),
  };
}

const COMMAND_K: BrowserForwardedKeydown = {
  type: "keydown",
  key: "k",
  code: "KeyK",
  metaKey: true,
  ctrlKey: false,
  altKey: false,
  shiftKey: false,
  repeat: false,
};

const chord = (fields: Record<string, unknown>) => ({
  metaKey: false,
  ctrlKey: false,
  altKey: false,
  shiftKey: false,
  ...fields,
});

afterEach(() => setBridge(undefined));

describe("forwardedChordsFor", () => {
  it("lists the Mac's tab chords and ⌘K, and leaves ⌘← and ⌘→ to a page's text fields", () => {
    expect(forwardedChordsFor(tabsSidebarForwardedShortcuts(true), true)).toEqual([
      chord({ key: "k", metaKey: true }),
      chord({ key: "K", metaKey: true }),
      chord({ code: "KeyN", metaKey: true, altKey: true, shiftKey: true }),
      chord({ code: "KeyW", metaKey: true, altKey: true }),
      chord({ code: "KeyT", metaKey: true, altKey: true, shiftKey: true }),
    ]);
  });

  it("lists only Ctrl+K off the Mac, where the Ctrl+Alt tab chords are how AltGr types", () => {
    expect(forwardedChordsFor(tabsSidebarForwardedShortcuts(true), false)).toEqual([
      chord({ key: "k", ctrlKey: true }),
      chord({ key: "K", ctrlKey: true }),
    ]);
  });

  it("leaves Reopen's chord with the page while there's no closed tab", () => {
    expect(tabsSidebarForwardedShortcuts(false)).not.toContain("workspace.reopenTab");
    expect(tabsSidebarForwardedShortcuts(true)).toContain("workspace.reopenTab");
  });
});

describe("useBrowserForwardedChords", () => {
  it("lists the chords on a capable shell, and none on unmount", () => {
    const bridge = capableBridge();
    const { unmount } = renderHook(() => useBrowserForwardedChords(["palette.commands"], true));
    expect(bridge.browserSetForwardedChords).toHaveBeenCalledExactlyOnceWith([
      chord({ key: "k", metaKey: true }),
      chord({ key: "K", metaKey: true }),
    ]);

    unmount();
    expect(bridge.unsubscribe).toHaveBeenCalled();
    expect(bridge.browserSetForwardedChords).toHaveBeenLastCalledWith([]);
  });

  it("warns when the shell refuses the list", async () => {
    const bridge = capableBridge();
    bridge.browserSetForwardedChords.mockResolvedValue({ ok: false, error: "untrusted sender" });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    renderHook(() => useBrowserForwardedChords(["palette.commands"], true));
    await vi.waitFor(() =>
      expect(warn).toHaveBeenCalledWith(
        "[nativeBridge] browser forwarded chords update refused:",
        "untrusted sender",
      ),
    );
    warn.mockRestore();
  });

  it("replays a forwarded ⌘K where the palette hotkey hears it", () => {
    const bridge = capableBridge();
    const onToggle = vi.fn();
    renderHook(() => {
      useCommandPaletteHotkey(onToggle, true, true);
      useBrowserForwardedChords(["palette.commands"], true);
    });
    const stale = document.createElement("input");
    document.body.append(stale);
    stale.focus();

    act(() => bridge.forward(COMMAND_K));
    expect(onToggle).toHaveBeenCalledTimes(1);
    // The key came from the page, so the app's last focused field doesn't keep focus.
    expect(document.activeElement).not.toBe(stale);
    stale.remove();
  });

  it("asks for nothing without a desktop bridge, on a shell missing either method, or with an empty list", () => {
    setBridge(undefined);
    renderHook(() => useBrowserForwardedChords(["palette.commands"], true)).unmount();

    const browserSetForwardedChords = vi.fn().mockResolvedValue({ ok: true });
    setBridge({ browserSetForwardedChords });
    renderHook(() => useBrowserForwardedChords(["palette.commands"], true)).unmount();
    expect(browserSetForwardedChords).not.toHaveBeenCalled();

    const onBrowserForwardedInput = vi.fn(() => () => {});
    setBridge({ onBrowserForwardedInput });
    renderHook(() => useBrowserForwardedChords(["palette.commands"], true)).unmount();
    expect(onBrowserForwardedInput).not.toHaveBeenCalled();

    const bridge = capableBridge();
    renderHook(() => useBrowserForwardedChords([], true));
    expect(bridge.browserSetForwardedChords).not.toHaveBeenCalled();
  });
});
