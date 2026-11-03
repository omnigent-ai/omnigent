import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EmbeddedProvider } from "@/lib/embedded";
import { setEmbedRoot } from "@/lib/host";

import { isRenameSessionHotkey, useRenameSessionHotkey } from "./useRenameSessionHotkey";

afterEach(() => {
  cleanup();
  setEmbedRoot(null);
  document.body.innerHTML = "";
});

function event(init: KeyboardEventInit = {}): KeyboardEvent {
  return new KeyboardEvent("keydown", {
    bubbles: true,
    cancelable: true,
    key: "r",
    code: "KeyR",
    ctrlKey: true,
    altKey: true,
    ...init,
  });
}

describe("isRenameSessionHotkey", () => {
  it.each([true, false])("requires the platform command modifier (isMac=%s)", (isMac) => {
    const command = { metaKey: isMac, ctrlKey: !isMac };
    expect(isRenameSessionHotkey(event(command), isMac)).toBe(true);
    for (const init of [
      { metaKey: !isMac, ctrlKey: isMac },
      { metaKey: true, ctrlKey: true },
      { altKey: false },
      { shiftKey: true },
      { code: "KeyT" },
    ]) {
      expect(isRenameSessionHotkey(event({ ...command, ...init }), isMac)).toBe(false);
    }
  });

  it("recognizes the Option-remapped Mac key and leaves AltGr typing alone", () => {
    expect(isRenameSessionHotkey(event({ metaKey: true, ctrlKey: false, key: "®" }), true)).toBe(
      true,
    );
    const altGraph = event();
    vi.spyOn(altGraph, "getModifierState").mockImplementation((key) => key === "AltGraph");
    expect(isRenameSessionHotkey(altGraph, false)).toBe(false);
  });
});

describe("useRenameSessionHotkey", () => {
  it("renames from a focused composer and removes its listener on unmount", () => {
    const rename = vi.fn();
    const { unmount } = renderHook(() => useRenameSessionHotkey(rename, true, false));
    const composer = document.createElement("textarea");
    document.body.appendChild(composer);
    composer.focus();
    const pressed = event();
    composer.dispatchEvent(pressed);
    expect(pressed.defaultPrevented).toBe(true);
    expect(rename).toHaveBeenCalledOnce();

    unmount();
    const afterUnmount = event();
    composer.dispatchEvent(afterUnmount);
    expect(afterUnmount.defaultPrevented).toBe(false);
    expect(rename).toHaveBeenCalledOnce();
  });

  it("uses the current callback and leaves unavailable sessions untouched", () => {
    const first = vi.fn();
    const current = vi.fn();
    const { rerender } = renderHook(
      ({ rename, enabled }) => useRenameSessionHotkey(rename, enabled, false),
      { initialProps: { rename: first, enabled: true } },
    );
    rerender({ rename: current, enabled: true });
    window.dispatchEvent(event());
    expect(current).toHaveBeenCalledOnce();
    expect(first).not.toHaveBeenCalled();

    rerender({ rename: current, enabled: false });
    const disabled = event();
    window.dispatchEvent(disabled);
    expect(disabled.defaultPrevented).toBe(false);
    expect(current).toHaveBeenCalledOnce();
  });

  it("ignores held, composing, and already handled keys", () => {
    const rename = vi.fn();
    renderHook(() => useRenameSessionHotkey(rename, true, false));
    window.dispatchEvent(event({ repeat: true }));
    window.dispatchEvent(event({ isComposing: true }));
    const handled = event();
    handled.preventDefault();
    window.dispatchEvent(handled);
    expect(rename).not.toHaveBeenCalled();
  });

  it.each(["xterm", "monaco-editor", "dialog", "alertdialog", "menu"])(
    "leaves focused %s controls alone",
    (surface) => {
      const rename = vi.fn();
      renderHook(() => useRenameSessionHotkey(rename, true, false));
      const owner = document.createElement("div");
      if (surface === "xterm" || surface === "monaco-editor") owner.className = surface;
      else owner.setAttribute("role", surface);
      const input = document.createElement("input");
      owner.appendChild(input);
      document.body.appendChild(owner);
      input.focus();
      const pressed = event();
      input.dispatchEvent(pressed);
      expect(pressed.defaultPrevented).toBe(false);
      expect(rename).not.toHaveBeenCalled();
    },
  );

  it("only claims embedded shortcuts from inside the app", () => {
    const rename = vi.fn();
    const root = document.createElement("div");
    const composer = document.createElement("textarea");
    root.appendChild(composer);
    document.body.appendChild(root);
    setEmbedRoot(root);
    renderHook(() => useRenameSessionHotkey(rename, true, false), { wrapper: EmbeddedProvider });

    const outside = event();
    document.body.dispatchEvent(outside);
    expect(outside.defaultPrevented).toBe(false);
    expect(rename).not.toHaveBeenCalled();

    const inside = event();
    composer.dispatchEvent(inside);
    expect(inside.defaultPrevented).toBe(true);
    expect(rename).toHaveBeenCalledOnce();
  });
});
