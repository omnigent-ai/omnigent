import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  isClearAllNotificationsHotkey,
  useClearAllNotificationsHotkey,
} from "./useClearAllNotificationsHotkey";

afterEach(() => vi.restoreAllMocks());

describe("isClearAllNotificationsHotkey", () => {
  it("matches only unmodified Shift+Escape", () => {
    expect(
      isClearAllNotificationsHotkey(
        new KeyboardEvent("keydown", { key: "Escape", shiftKey: true }),
      ),
    ).toBe(true);
    expect(isClearAllNotificationsHotkey(new KeyboardEvent("keydown", { key: "Escape" }))).toBe(
      false,
    );
    for (const modifier of ["metaKey", "ctrlKey", "altKey"] as const) {
      expect(
        isClearAllNotificationsHotkey(
          new KeyboardEvent("keydown", { key: "Escape", shiftKey: true, [modifier]: true }),
        ),
      ).toBe(false);
    }
  });
});

describe("useClearAllNotificationsHotkey", () => {
  it("claims Shift+Escape before plain-Escape handlers and clears once", () => {
    const onClear = vi.fn();
    const descendantEscapeHandler = vi.fn();
    document.addEventListener("keydown", descendantEscapeHandler);
    renderHook(() => useClearAllNotificationsHotkey(onClear));

    const event = new KeyboardEvent("keydown", {
      key: "Escape",
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    });
    document.body.dispatchEvent(event);

    expect(onClear).toHaveBeenCalledOnce();
    expect(descendantEscapeHandler).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(true);
    document.removeEventListener("keydown", descendantEscapeHandler);
  });

  it("leaves plain Escape available to existing handlers", () => {
    const onClear = vi.fn();
    renderHook(() => useClearAllNotificationsHotkey(onClear));

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));

    expect(onClear).not.toHaveBeenCalled();
  });

  it("ignores auto-repeat and already-handled Shift+Escape", () => {
    const onClear = vi.fn();
    renderHook(() => useClearAllNotificationsHotkey(onClear));

    window.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", shiftKey: true, repeat: true }),
    );
    const handled = new KeyboardEvent("keydown", {
      key: "Escape",
      shiftKey: true,
      cancelable: true,
    });
    handled.preventDefault();
    window.dispatchEvent(handled);

    expect(onClear).not.toHaveBeenCalled();
  });

  it("does nothing when disabled", () => {
    const onClear = vi.fn();
    renderHook(() => useClearAllNotificationsHotkey(onClear, false));

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", shiftKey: true }));

    expect(onClear).not.toHaveBeenCalled();
  });
});
