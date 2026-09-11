// Cmd/Ctrl+] / [ steps next/prev with wrap; off-list ] enters at top, [ at
// bottom; platform-aware (only ⌘ on macOS, only Ctrl elsewhere); fires while the
// composer is focused but bails inside terminals / the code editor; ignores
// Alt/Shift/bare brackets; a no-op step (same id) doesn't navigate.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSessionSwitchHotkey } from "./useSessionSwitchHotkey";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
}));

/** Dispatch a bracket keydown that bubbles to window from `target` (default:
 *  body, ⌘ held → macOS). */
function press(
  code: "BracketLeft" | "BracketRight",
  mods: Partial<Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey">> = {
    metaKey: true,
  },
  target: HTMLElement = document.body,
): KeyboardEvent {
  const e = new KeyboardEvent("keydown", { code, bubbles: true, cancelable: true, ...mods });
  target.dispatchEvent(e);
  return e;
}

/** Render on macOS (⌘) by default; pass isMac=false for the Ctrl (Win/Linux) path. */
function render(ids: readonly string[], activeId: string | undefined, isMac = true) {
  return renderHook(() => useSessionSwitchHotkey(ids, activeId, isMac));
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

  it("Cmd+] opens the next conversation", () => {
    render(ids, "b");
    press("BracketRight");
    expect(navigate).toHaveBeenCalledWith("/c/c");
  });

  it("Cmd+[ opens the previous conversation", () => {
    render(ids, "b");
    press("BracketLeft");
    expect(navigate).toHaveBeenCalledWith("/c/a");
  });

  it("wraps: ] from the last goes to the first, [ from the first to the last", () => {
    const { rerender } = renderHook(({ active }) => useSessionSwitchHotkey(ids, active, true), {
      initialProps: { active: "c" },
    });
    press("BracketRight");
    expect(navigate).toHaveBeenLastCalledWith("/c/a");

    rerender({ active: "a" });
    press("BracketLeft");
    expect(navigate).toHaveBeenLastCalledWith("/c/c");
  });

  it("off-list: ] enters at the top, [ at the bottom", () => {
    const { rerender } = renderHook(({ active }) => useSessionSwitchHotkey(ids, active, true), {
      initialProps: { active: undefined as string | undefined },
    });
    press("BracketRight");
    expect(navigate).toHaveBeenLastCalledWith("/c/a");

    rerender({ active: undefined });
    press("BracketLeft");
    expect(navigate).toHaveBeenLastCalledWith("/c/c");
  });

  it("Ctrl+] also works (Windows/Linux)", () => {
    render(ids, "a", false);
    press("BracketRight", { ctrlKey: true });
    expect(navigate).toHaveBeenCalledWith("/c/b");
  });

  it("ignores Ctrl+] on macOS (only ⌘ fires there)", () => {
    render(ids, "a", true);
    press("BracketRight", { ctrlKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("ignores Alt+chord (reserved for the sidebar-toggle hotkey)", () => {
    render(ids, "a");
    press("BracketRight", { metaKey: true, altKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("ignores Shift+chord", () => {
    render(ids, "a");
    press("BracketRight", { metaKey: true, shiftKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("ignores a bare bracket with no Cmd/Ctrl", () => {
    render(ids, "a");
    press("BracketRight", {});
    expect(navigate).not.toHaveBeenCalled();
  });

  it("switches while a textarea is focused (brackets carry no caret command)", () => {
    render(ids, "a");
    const ta = document.createElement("textarea");
    document.body.appendChild(ta);
    press("BracketRight", { metaKey: true }, ta);
    expect(navigate).toHaveBeenCalledWith("/c/b");
  });

  it("switches while an input is focused", () => {
    render(ids, "a");
    const input = document.createElement("input");
    document.body.appendChild(input);
    press("BracketRight", { metaKey: true }, input);
    expect(navigate).toHaveBeenCalledWith("/c/b");
  });

  it.each(["xterm", "monaco-editor"])("bails inside a %s surface", (className) => {
    render(ids, "a");
    const surface = document.createElement("div");
    surface.className = className;
    const inner = document.createElement("textarea");
    surface.appendChild(inner);
    document.body.appendChild(surface);
    inner.focus();
    press("BracketRight", { metaKey: true }, inner);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("does not navigate behind an open command palette", () => {
    render(ids, "a");
    const input = document.createElement("input");
    input.setAttribute("cmdk-input", "");
    document.body.appendChild(input);
    input.focus();
    press("BracketRight", { metaKey: true }, input);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("yields when a focused widget already claimed the chord", () => {
    render(ids, "a");
    const menu = document.createElement("div");
    document.body.appendChild(menu);
    menu.addEventListener("keydown", (e) => e.preventDefault());
    press("BracketRight", { metaKey: true }, menu);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("claims the event (suppresses the browser Back/Forward gesture)", () => {
    render(ids, "a");
    const e = press("BracketRight");
    expect(e.defaultPrevented).toBe(true);
  });

  it("does nothing when the list is empty", () => {
    render([], "a");
    press("BracketRight");
    expect(navigate).not.toHaveBeenCalled();
  });

  it("does not navigate when the step lands on the already-active id", () => {
    render(["only"], "only");
    press("BracketRight");
    expect(navigate).not.toHaveBeenCalled();
  });
});
