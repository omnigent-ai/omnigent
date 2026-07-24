import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { isVoiceDictationHotkey, useVoiceDictationHotkey } from "./useVoiceDictationHotkey";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

function press(init: KeyboardEventInit): KeyboardEvent {
  const e = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  window.dispatchEvent(e);
  return e;
}

describe("isVoiceDictationHotkey", () => {
  it("matches Cmd+Alt+V and Ctrl+Alt+V by physical code", () => {
    expect(
      isVoiceDictationHotkey(
        new KeyboardEvent("keydown", { code: "KeyV", metaKey: true, altKey: true }),
      ),
    ).toBe(true);
    expect(
      isVoiceDictationHotkey(
        new KeyboardEvent("keydown", { code: "KeyV", ctrlKey: true, altKey: true }),
      ),
    ).toBe(true);
    // ⌥ rewrites the character to "√" on macOS, but e.code stays "KeyV".
    expect(
      isVoiceDictationHotkey(
        new KeyboardEvent("keydown", { code: "KeyV", key: "√", metaKey: true, altKey: true }),
      ),
    ).toBe(true);
  });

  it("rejects the chord without Alt, and with Shift held", () => {
    expect(
      isVoiceDictationHotkey(new KeyboardEvent("keydown", { code: "KeyV", metaKey: true })),
    ).toBe(false);
    expect(
      isVoiceDictationHotkey(
        new KeyboardEvent("keydown", { code: "KeyV", metaKey: true, altKey: true, shiftKey: true }),
      ),
    ).toBe(false);
    // Alt+V without a Cmd/Ctrl modifier.
    expect(
      isVoiceDictationHotkey(new KeyboardEvent("keydown", { code: "KeyV", altKey: true })),
    ).toBe(false);
  });

  it("rejects other keys with the modifier chord", () => {
    expect(
      isVoiceDictationHotkey(
        new KeyboardEvent("keydown", { code: "KeyK", metaKey: true, altKey: true }),
      ),
    ).toBe(false);
  });
});

describe("useVoiceDictationHotkey", () => {
  it("toggles on Cmd+Alt+V and prevents the browser default", () => {
    const onToggle = vi.fn();
    renderHook(() => useVoiceDictationHotkey(onToggle));

    const e = press({ code: "KeyV", metaKey: true, altKey: true });

    expect(onToggle).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(true);
  });

  it("ignores auto-repeat", () => {
    const onToggle = vi.fn();
    renderHook(() => useVoiceDictationHotkey(onToggle));

    press({ code: "KeyV", metaKey: true, altKey: true, repeat: true });

    expect(onToggle).not.toHaveBeenCalled();
  });

  it("does nothing when disabled", () => {
    const onToggle = vi.fn();
    renderHook(() => useVoiceDictationHotkey(onToggle, false));

    const e = press({ code: "KeyV", metaKey: true, altKey: true });

    expect(onToggle).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("bails when focus sits inside a terminal or code editor", () => {
    const onToggle = vi.fn();
    renderHook(() => useVoiceDictationHotkey(onToggle));

    const term = document.createElement("div");
    term.className = "xterm";
    const input = document.createElement("input");
    term.appendChild(input);
    document.body.appendChild(term);
    input.focus();
    expect(document.activeElement).toBe(input);

    press({ code: "KeyV", metaKey: true, altKey: true });

    expect(onToggle).not.toHaveBeenCalled();
  });

  it("unbinds on unmount", () => {
    const onToggle = vi.fn();
    const { unmount } = renderHook(() => useVoiceDictationHotkey(onToggle));
    unmount();

    press({ code: "KeyV", metaKey: true, altKey: true });

    expect(onToggle).not.toHaveBeenCalled();
  });
});
