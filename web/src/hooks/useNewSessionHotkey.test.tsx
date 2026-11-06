import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EmbeddedProvider } from "@/lib/embedded";
import { setEmbedRoot } from "@/lib/host";

import { isNewSessionHotkey, useNewSessionHotkey } from "./useNewSessionHotkey";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));

afterEach(() => {
  cleanup();
  setEmbedRoot(null);
  navigate.mockReset();
  document.body.innerHTML = "";
});

function event(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
}

function press(init: KeyboardEventInit, target: HTMLElement = document.body): KeyboardEvent {
  const e = event(init);
  target.dispatchEvent(e);
  return e;
}

describe("isNewSessionHotkey", () => {
  it("uses Cmd on macOS and Ctrl on other platforms", () => {
    expect(
      isNewSessionHotkey(event({ key: "O", code: "KeyO", metaKey: true, shiftKey: true }), true),
    ).toBe(true);
    expect(
      isNewSessionHotkey(event({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true }), true),
    ).toBe(false);
    expect(
      isNewSessionHotkey(event({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true }), false),
    ).toBe(true);
    expect(
      isNewSessionHotkey(event({ key: "O", code: "KeyO", metaKey: true, shiftKey: true }), false),
    ).toBe(false);
  });

  it("matches the displayed O across keyboard layouts", () => {
    expect(
      isNewSessionHotkey(event({ key: "O", code: "KeyS", ctrlKey: true, shiftKey: true }), false),
    ).toBe(true);
    expect(
      isNewSessionHotkey(event({ key: "o", code: "KeyS", metaKey: true, shiftKey: true }), true),
    ).toBe(true);
    expect(
      isNewSessionHotkey(event({ key: "R", code: "KeyO", ctrlKey: true, shiftKey: true }), false),
    ).toBe(false);
  });

  it.each([true, false])("rejects modified chords and unrelated keys with isMac=%s", (isMac) => {
    const command = { metaKey: isMac, ctrlKey: !isMac };
    for (const init of [
      { key: "O", code: "KeyO" },
      { key: "O", code: "KeyO", altKey: true, shiftKey: true },
      { key: "M", code: "KeyM", shiftKey: true },
      { key: "N", code: "KeyN", altKey: true, shiftKey: true },
      { key: "O", code: "KeyO", ctrlKey: true, metaKey: true, shiftKey: true },
    ]) {
      expect(isNewSessionHotkey(event({ ...command, ...init }), isMac)).toBe(false);
    }
    const altGraph = event({ ...command, key: "O", code: "KeyO", shiftKey: true });
    vi.spyOn(altGraph, "getModifierState").mockImplementation((key) => key === "AltGraph");
    expect(isNewSessionHotkey(altGraph, isMac)).toBe(false);
  });

  it("uses Cmd+N in native shells, keeps the older Alt chord, and not in browsers", () => {
    const native = event({ key: "n", code: "KeyN", metaKey: true });
    expect(isNewSessionHotkey(native, true, true)).toBe(true);
    expect(isNewSessionHotkey(native, true, false)).toBe(false);
    const old = event({ key: "˜", code: "KeyN", metaKey: true, altKey: true });
    expect(isNewSessionHotkey(old, true, true)).toBe(true);
    expect(isNewSessionHotkey(old, true, false)).toBe(false);
    expect(
      isNewSessionHotkey(
        event({ key: "O", code: "KeyO", metaKey: true, shiftKey: true }),
        true,
        true,
      ),
    ).toBe(false);
    expect(
      isNewSessionHotkey(event({ code: "KeyN", ctrlKey: true, altKey: true }), false, true),
    ).toBe(true);
  });

  it.each([
    ["⌘", { metaKey: true }, true],
    ["Ctrl", { ctrlKey: true }, false],
  ])("on another layout, %s+N follows the key that types N", (_name, modifier, isMac) => {
    // Dvorak types B at the physical N key, and N at the physical L key.
    expect(isNewSessionHotkey(event({ key: "n", code: "KeyL", ...modifier }), isMac, true)).toBe(
      true,
    );
    expect(isNewSessionHotkey(event({ key: "b", code: "KeyN", ...modifier }), isMac, true)).toBe(
      false,
    );
  });
});

describe("useNewSessionHotkey", () => {
  it("navigates to the shared new-session route and claims Ctrl+Shift+O", () => {
    renderHook(() => useNewSessionHotkey(true, false));

    const e = press({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true });

    expect(navigate).toHaveBeenCalledWith("/");
    expect(e.defaultPrevented).toBe(true);
  });

  it("claims the displayed O from a different physical key", () => {
    renderHook(() => useNewSessionHotkey(true, false));

    const e = press({ key: "O", code: "KeyS", ctrlKey: true, shiftKey: true });

    expect(e.defaultPrevented).toBe(true);
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/");
  });

  it("works from editable fields so the global action is focus-independent", () => {
    renderHook(() => useNewSessionHotkey(true, false));
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();

    press({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true }, input);

    expect(navigate).toHaveBeenCalledWith("/");
  });

  it("handles embedded input without claiming shortcuts in the host page", () => {
    const root = document.createElement("div");
    const input = document.createElement("input");
    root.appendChild(input);
    document.body.appendChild(root);
    setEmbedRoot(root);
    renderHook(() => useNewSessionHotkey(true, false), { wrapper: EmbeddedProvider });

    const outside = press({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true });
    expect(outside.defaultPrevented).toBe(false);
    expect(navigate).not.toHaveBeenCalled();

    const inside = press({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true }, input);
    expect(inside.defaultPrevented).toBe(true);
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/");
  });

  it("ignores auto-repeat", () => {
    renderHook(() => useNewSessionHotkey(true, false));

    press({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true, repeat: true });

    expect(navigate).not.toHaveBeenCalled();
  });

  it("leaves the shortcut to the host page when disabled", () => {
    renderHook(() => useNewSessionHotkey(false, false));

    const e = press({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true });

    expect(navigate).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });
});
