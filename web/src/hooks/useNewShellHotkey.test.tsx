import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { isNewShellHotkey, useNewShellHotkey } from "./useNewShellHotkey";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

function event(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
}

/** An event whose getModifierState reports AltGraph active (jsdom can't set it via init). */
function altGraphEvent(init: KeyboardEventInit): KeyboardEvent {
  const e = event(init);
  Object.defineProperty(e, "getModifierState", { value: (k: string) => k === "AltGraph" });
  return e;
}

function press(init: KeyboardEventInit, target: HTMLElement = document.body): KeyboardEvent {
  const e = event(init);
  target.dispatchEvent(e);
  return e;
}

describe("isNewShellHotkey", () => {
  it("uses Cmd+Alt on macOS and Ctrl+Alt on other platforms", () => {
    expect(isNewShellHotkey(event({ code: "KeyT", metaKey: true, altKey: true }), true)).toBe(true);
    expect(isNewShellHotkey(event({ code: "KeyT", ctrlKey: true, altKey: true }), true)).toBe(
      false,
    );
    expect(isNewShellHotkey(event({ code: "KeyT", ctrlKey: true, altKey: true }), false)).toBe(
      true,
    );
    expect(isNewShellHotkey(event({ code: "KeyT", metaKey: true, altKey: true }), false)).toBe(
      false,
    );
  });

  it("requires Alt and rejects Shift or a missing modifier", () => {
    // No Alt → not the chord (that's the address-bar / other bindings).
    expect(isNewShellHotkey(event({ code: "KeyT", metaKey: true }), true)).toBe(false);
    // Shift added → reserved (browser reopen-tab); not ours.
    expect(
      isNewShellHotkey(event({ code: "KeyT", metaKey: true, altKey: true, shiftKey: true }), true),
    ).toBe(false);
  });

  it("rejects AltGr+T — reported as Ctrl+Alt on intl layouts, must not be the chord", () => {
    // A bare AltGr+T (no real Ctrl) on Windows/Linux would otherwise match the
    // Ctrl+Alt predicate and swallow the character; the AltGraph guard bails.
    expect(
      isNewShellHotkey(altGraphEvent({ code: "KeyT", ctrlKey: true, altKey: true }), false),
    ).toBe(false);
  });

  it("matches the physical key so Alt's remapped character doesn't matter", () => {
    // ⌥T yields "†" on macOS; keying off e.code (not e.key) still matches.
    expect(
      isNewShellHotkey(event({ code: "KeyT", key: "†", metaKey: true, altKey: true }), true),
    ).toBe(true);
    expect(isNewShellHotkey(event({ code: "KeyG", metaKey: true, altKey: true }), true)).toBe(
      false,
    );
  });
});

describe("useNewShellHotkey", () => {
  it("launches the default shell and claims the chord", () => {
    const onLaunch = vi.fn();
    renderHook(() => useNewShellHotkey(onLaunch, true, false));

    const e = press({ code: "KeyT", ctrlKey: true, altKey: true });

    expect(onLaunch).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(true);
  });

  it("defers to a focused terminal (xterm) — that surface owns its keystrokes", () => {
    const onLaunch = vi.fn();
    renderHook(() => useNewShellHotkey(onLaunch, true, false));
    const term = document.createElement("div");
    term.className = "xterm";
    const inner = document.createElement("textarea");
    term.appendChild(inner);
    document.body.appendChild(term);
    inner.focus();

    const e = press({ code: "KeyT", ctrlKey: true, altKey: true }, inner);

    expect(onLaunch).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("defers to a focused Monaco editor", () => {
    const onLaunch = vi.fn();
    renderHook(() => useNewShellHotkey(onLaunch, true, false));
    const editor = document.createElement("div");
    editor.className = "monaco-editor";
    const inner = document.createElement("textarea");
    editor.appendChild(inner);
    document.body.appendChild(editor);
    inner.focus();

    press({ code: "KeyT", ctrlKey: true, altKey: true }, inner);

    expect(onLaunch).not.toHaveBeenCalled();
  });

  it("still fires from a plain input (only editor/terminal surfaces defer)", () => {
    const onLaunch = vi.fn();
    renderHook(() => useNewShellHotkey(onLaunch, true, false));
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();

    press({ code: "KeyT", ctrlKey: true, altKey: true }, input);

    expect(onLaunch).toHaveBeenCalledTimes(1);
  });

  it("does not fire or swallow an AltGr+T keystroke (intl layouts type into the composer)", () => {
    const onLaunch = vi.fn();
    renderHook(() => useNewShellHotkey(onLaunch, true, false));
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();

    const e = altGraphEvent({ code: "KeyT", ctrlKey: true, altKey: true });
    input.dispatchEvent(e);

    expect(onLaunch).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("ignores auto-repeat", () => {
    const onLaunch = vi.fn();
    renderHook(() => useNewShellHotkey(onLaunch, true, false));

    press({ code: "KeyT", ctrlKey: true, altKey: true, repeat: true });

    expect(onLaunch).not.toHaveBeenCalled();
  });

  it("does nothing when disabled (no shell access / offline session)", () => {
    const onLaunch = vi.fn();
    renderHook(() => useNewShellHotkey(onLaunch, false, false));

    const e = press({ code: "KeyT", ctrlKey: true, altKey: true });

    expect(onLaunch).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });
});
