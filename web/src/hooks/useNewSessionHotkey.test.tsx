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
    expect(isNewSessionHotkey(event({ code: "KeyN", metaKey: true, altKey: true }), true)).toBe(
      true,
    );
    expect(isNewSessionHotkey(event({ code: "KeyN", ctrlKey: true, altKey: true }), true)).toBe(
      false,
    );
    expect(isNewSessionHotkey(event({ code: "KeyN", ctrlKey: true, altKey: true }), false)).toBe(
      true,
    );
    expect(isNewSessionHotkey(event({ code: "KeyN", metaKey: true, altKey: true }), false)).toBe(
      false,
    );
  });

  it("rejects modified chords and unrelated keys", () => {
    expect(
      isNewSessionHotkey(
        event({ code: "KeyN", metaKey: true, altKey: true, shiftKey: true }),
        true,
      ),
    ).toBe(false);
    expect(isNewSessionHotkey(event({ code: "KeyN", ctrlKey: true }), false)).toBe(false);
    expect(isNewSessionHotkey(event({ code: "KeyM", metaKey: true, altKey: true }), true)).toBe(
      false,
    );
  });

  it("matches the physical N key when Alt remaps its character", () => {
    expect(
      isNewSessionHotkey(event({ key: "˜", code: "KeyN", metaKey: true, altKey: true }), true),
    ).toBe(true);
  });
});

describe("useNewSessionHotkey", () => {
  it("navigates to the shared new-session route and claims Ctrl+Alt+N", () => {
    renderHook(() => useNewSessionHotkey(true, false));

    const e = press({ code: "KeyN", ctrlKey: true, altKey: true });

    expect(navigate).toHaveBeenCalledWith("/");
    expect(e.defaultPrevented).toBe(true);
  });

  it("works from editable fields so the global action is focus-independent", () => {
    renderHook(() => useNewSessionHotkey(true, false));
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();

    press({ code: "KeyN", ctrlKey: true, altKey: true }, input);

    expect(navigate).toHaveBeenCalledWith("/");
  });

  it("ignores auto-repeat", () => {
    renderHook(() => useNewSessionHotkey(true, false));

    press({ code: "KeyN", ctrlKey: true, altKey: true, repeat: true });

    expect(navigate).not.toHaveBeenCalled();
  });

  it("leaves the shortcut to the host page when disabled", () => {
    renderHook(() => useNewSessionHotkey(false, false));

    const e = press({ code: "KeyN", ctrlKey: true, altKey: true });

    expect(navigate).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });
});
