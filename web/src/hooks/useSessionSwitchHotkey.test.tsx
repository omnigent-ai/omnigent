// Cmd/Ctrl+↓/↑ steps next/prev with wrap; off-list ↓ enters at top, ↑ at
// bottom; fires inside text fields but ignores Alt/Shift/bare arrows; a no-op
// step (same id) doesn't navigate.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSessionSwitchHotkey } from "./useSessionSwitchHotkey";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
}));

/** Dispatch a keydown that bubbles to window from `target` (default: body). */
function press(
  key: "ArrowUp" | "ArrowDown",
  mods: Partial<Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey">> = {
    metaKey: true,
  },
  target: HTMLElement = document.body,
): void {
  target.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, ...mods }));
}

beforeEach(() => {
  navigate.mockClear();
  document.body.innerHTML = "";
});
afterEach(() => {
  document.body.innerHTML = "";
});

describe("useSessionSwitchHotkey", () => {
  const ids = ["a", "b", "c"];

  it("Cmd+↓ opens the next conversation", () => {
    renderHook(() => useSessionSwitchHotkey(ids, "b"));
    press("ArrowDown");
    expect(navigate).toHaveBeenCalledWith("/c/c");
  });

  it("Cmd+↑ opens the previous conversation", () => {
    renderHook(() => useSessionSwitchHotkey(ids, "b"));
    press("ArrowUp");
    expect(navigate).toHaveBeenCalledWith("/c/a");
  });

  it("wraps: ↓ from the last goes to the first, ↑ from the first to the last", () => {
    const { rerender } = renderHook(({ active }) => useSessionSwitchHotkey(ids, active), {
      initialProps: { active: "c" },
    });
    press("ArrowDown");
    expect(navigate).toHaveBeenLastCalledWith("/c/a");

    rerender({ active: "a" });
    press("ArrowUp");
    expect(navigate).toHaveBeenLastCalledWith("/c/c");
  });

  it("off-list: ↓ enters at the top, ↑ at the bottom", () => {
    const { rerender } = renderHook(({ active }) => useSessionSwitchHotkey(ids, active), {
      initialProps: { active: undefined as string | undefined },
    });
    press("ArrowDown");
    expect(navigate).toHaveBeenLastCalledWith("/c/a");

    rerender({ active: undefined });
    press("ArrowUp");
    expect(navigate).toHaveBeenLastCalledWith("/c/c");
  });

  it("Ctrl+↓ also works (Windows/Linux)", () => {
    renderHook(() => useSessionSwitchHotkey(ids, "a"));
    press("ArrowDown", { ctrlKey: true });
    expect(navigate).toHaveBeenCalledWith("/c/b");
  });

  it("ignores Alt+chord (reserved for message navigation)", () => {
    renderHook(() => useSessionSwitchHotkey(ids, "a"));
    press("ArrowDown", { metaKey: true, altKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("ignores Shift+chord", () => {
    renderHook(() => useSessionSwitchHotkey(ids, "a"));
    press("ArrowDown", { metaKey: true, shiftKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("ignores a bare arrow with no Cmd/Ctrl", () => {
    renderHook(() => useSessionSwitchHotkey(ids, "a"));
    press("ArrowDown", {});
    expect(navigate).not.toHaveBeenCalled();
  });

  it("still switches while a text field is focused (no click-out needed)", () => {
    renderHook(() => useSessionSwitchHotkey(ids, "a"));
    const ta = document.createElement("textarea");
    document.body.appendChild(ta);
    press("ArrowDown", { metaKey: true }, ta);
    expect(navigate).toHaveBeenCalledWith("/c/b");
  });

  it("does nothing when the list is empty", () => {
    renderHook(() => useSessionSwitchHotkey([], "a"));
    press("ArrowDown");
    expect(navigate).not.toHaveBeenCalled();
  });

  it("does not navigate when the step lands on the already-active id", () => {
    renderHook(() => useSessionSwitchHotkey(["only"], "only"));
    press("ArrowDown");
    expect(navigate).not.toHaveBeenCalled();
  });
});
