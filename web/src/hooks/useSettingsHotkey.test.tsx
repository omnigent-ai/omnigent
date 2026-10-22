import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { isSettingsHotkey, useSettingsHotkey } from "./useSettingsHotkey";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));

afterEach(() => {
  cleanup();
  navigate.mockReset();
});

function event(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
}

describe("isSettingsHotkey", () => {
  it("uses Cmd on macOS and Ctrl on other platforms", () => {
    expect(isSettingsHotkey(event({ code: "Comma", metaKey: true, altKey: true }), true)).toBe(
      true,
    );
    expect(isSettingsHotkey(event({ code: "Comma", ctrlKey: true, altKey: true }), true)).toBe(
      false,
    );
    expect(isSettingsHotkey(event({ code: "Comma", ctrlKey: true, altKey: true }), false)).toBe(
      true,
    );
  });

  it("requires Alt and rejects Shift, AltGraph, and unrelated keys", () => {
    expect(isSettingsHotkey(event({ code: "Comma", metaKey: true }), true)).toBe(false);
    expect(
      isSettingsHotkey(event({ code: "Comma", metaKey: true, altKey: true, shiftKey: true }), true),
    ).toBe(false);
    const altGraph = event({ code: "Comma", ctrlKey: true, altKey: true });
    vi.spyOn(altGraph, "getModifierState").mockReturnValue(true);
    expect(isSettingsHotkey(altGraph, false)).toBe(false);
    expect(isSettingsHotkey(event({ code: "Period", metaKey: true, altKey: true }), true)).toBe(
      false,
    );
  });
});

describe("useSettingsHotkey", () => {
  it("navigates to Settings and claims Ctrl+Alt+,", () => {
    renderHook(() => useSettingsHotkey(true, false));
    const keyboardEvent = event({ code: "Comma", ctrlKey: true, altKey: true });

    window.dispatchEvent(keyboardEvent);

    expect(navigate).toHaveBeenCalledWith("/settings");
    expect(keyboardEvent.defaultPrevented).toBe(true);
  });

  it("ignores repeat and does nothing when disabled", () => {
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useSettingsHotkey(enabled, false),
      { initialProps: { enabled: true } },
    );
    window.dispatchEvent(event({ code: "Comma", ctrlKey: true, altKey: true, repeat: true }));
    rerender({ enabled: false });
    const disabledEvent = event({ code: "Comma", ctrlKey: true, altKey: true });
    window.dispatchEvent(disabledEvent);

    expect(navigate).not.toHaveBeenCalled();
    expect(disabledEvent.defaultPrevented).toBe(false);
  });
});
