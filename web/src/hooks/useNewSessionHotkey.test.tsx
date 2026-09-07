import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { isNewSessionHotkey, useNewSessionHotkey } from "./useNewSessionHotkey";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));

afterEach(() => {
  cleanup();
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
    expect(isNewSessionHotkey(event({ key: "n", metaKey: true }), true)).toBe(true);
    expect(isNewSessionHotkey(event({ key: "n", ctrlKey: true }), true)).toBe(false);
    expect(isNewSessionHotkey(event({ key: "n", ctrlKey: true }), false)).toBe(true);
    expect(isNewSessionHotkey(event({ key: "n", metaKey: true }), false)).toBe(false);
  });

  it("rejects modified chords and unrelated keys", () => {
    expect(isNewSessionHotkey(event({ key: "n", metaKey: true, shiftKey: true }), true)).toBe(
      false,
    );
    expect(isNewSessionHotkey(event({ key: "n", ctrlKey: true, altKey: true }), false)).toBe(false);
    expect(isNewSessionHotkey(event({ key: "m", metaKey: true }), true)).toBe(false);
  });
});

describe("useNewSessionHotkey", () => {
  it("navigates to the shared new-session route and claims Ctrl+N", () => {
    renderHook(() => useNewSessionHotkey(true, false));

    const e = press({ key: "n", ctrlKey: true });

    expect(navigate).toHaveBeenCalledWith("/");
    expect(e.defaultPrevented).toBe(true);
  });

  it("works from editable fields so the global action is focus-independent", () => {
    renderHook(() => useNewSessionHotkey(true, false));
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();

    press({ key: "N", ctrlKey: true }, input);

    expect(navigate).toHaveBeenCalledWith("/");
  });

  it("ignores auto-repeat", () => {
    renderHook(() => useNewSessionHotkey(true, false));

    press({ key: "n", ctrlKey: true, repeat: true });

    expect(navigate).not.toHaveBeenCalled();
  });

  it("leaves the shortcut to the host page when disabled", () => {
    renderHook(() => useNewSessionHotkey(false, false));

    const e = press({ key: "n", ctrlKey: true });

    expect(navigate).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });
});
