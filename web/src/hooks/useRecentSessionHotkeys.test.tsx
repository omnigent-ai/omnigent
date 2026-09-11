import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useRecentSessionHotkeys } from "./useRecentSessionHotkeys";

const navigate = vi.fn();
const native = vi.hoisted(() => ({ value: true, server: "first" }));
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));
vi.mock("@/lib/nativeBridge", () => ({ isNativeShell: () => native.value }));
vi.mock("@/lib/host", () => ({ getOmnigentServerIdentity: () => native.server }));

function press(options: KeyboardEventInit = {}) {
  const event = new KeyboardEvent("keydown", {
    key: "Tab",
    ctrlKey: true,
    bubbles: true,
    cancelable: true,
    ...options,
  });
  document.activeElement?.dispatchEvent(event);
  return event;
}

function release() {
  window.dispatchEvent(new KeyboardEvent("keyup", { key: "Control" }));
}

function visitSessions() {
  const hook = renderHook(({ active, ids }) => useRecentSessionHotkeys(ids, active), {
    initialProps: { active: "a" as string | undefined, ids: ["a", "b", "c"] },
  });
  const visit = (active: string | undefined, ids = ["a", "b", "c"]) =>
    hook.rerender({ active, ids });
  visit("b");
  visit("c");
  return { ...hook, visit };
}

beforeEach(() => {
  navigate.mockClear();
  native.value = true;
  native.server = "first";
});
afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

describe("recent session hotkeys", () => {
  it("does not carry history across servers", () => {
    const { visit } = visitSessions();
    native.server = "second";
    visit("other", ["other"]);
    expect(press().defaultPrevented).toBe(false);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("can return to the only visited session from a non-session route", () => {
    const { rerender } = renderHook(({ active }) => useRecentSessionHotkeys(["a"], active), {
      initialProps: { active: "a" as string | undefined },
    });
    rerender({ active: undefined });
    press({ shiftKey: true });
    expect(navigate).toHaveBeenCalledWith("/c/a");
  });

  it("does not restore a removed cycling target on modifier release", () => {
    const { visit } = visitSessions();
    press();
    visit("b");
    visit(undefined, ["a", "c"]);
    release();
    press();
    expect(navigate).toHaveBeenLastCalledWith("/c/c");
  });

  it("cycles a frozen MRU list while Control stays held, then commits on release", () => {
    const { visit } = visitSessions();
    expect(press().defaultPrevented).toBe(true);
    expect(navigate).toHaveBeenLastCalledWith("/c/b");
    visit("b");
    press();
    expect(navigate).toHaveBeenLastCalledWith("/c/a");
    visit("a");
    release();
    press();
    expect(navigate).toHaveBeenLastCalledWith("/c/c");
  });

  it("toggles back to the last used session after a separate chord", () => {
    const { visit } = visitSessions();
    press();
    visit("b");
    release();
    press();
    expect(navigate).toHaveBeenLastCalledWith("/c/c");
  });

  it("reverses with Shift and wraps without shuffling history", () => {
    const { visit } = visitSessions();
    press({ shiftKey: true });
    expect(navigate).toHaveBeenLastCalledWith("/c/a");
    visit("a");
    press();
    expect(navigate).toHaveBeenLastCalledWith("/c/c");
  });

  it("handles rapid presses before the router renders", () => {
    visitSessions();
    press();
    press();
    expect(navigate).toHaveBeenLastCalledWith("/c/a");
  });

  it("does not include unvisited sessions or duplicate visits", () => {
    const { visit } = visitSessions();
    visit("b");
    visit("b");
    press();
    expect(navigate).toHaveBeenLastCalledWith("/c/c");
    press();
    expect(navigate).toHaveBeenLastCalledWith("/c/a");
  });

  it("drops removed sessions and retains visited sessions outside the loaded page", () => {
    const { visit } = visitSessions();
    visit("older");
    visit("c", ["a", "c"]);
    press();
    expect(navigate).toHaveBeenLastCalledWith("/c/older");
    press();
    expect(navigate).toHaveBeenLastCalledWith("/c/a");
  });

  it("can return from a non-session route", () => {
    const { visit } = visitSessions();
    visit(undefined);
    press();
    expect(navigate).toHaveBeenLastCalledWith("/c/c");
  });

  it("finishes cycling on blur", () => {
    const { visit } = visitSessions();
    press();
    visit("b");
    window.dispatchEvent(new Event("blur"));
    press();
    expect(navigate).toHaveBeenLastCalledWith("/c/c");
  });

  it("uses Ctrl+backtick in browsers without taking browser or OS tab switching", () => {
    native.value = false;
    visitSessions();
    expect(press().defaultPrevented).toBe(false);
    expect(navigate).not.toHaveBeenCalled();
    expect(press({ altKey: true }).defaultPrevented).toBe(false);
    expect(press({ key: "`", code: "Backquote" }).defaultPrevented).toBe(true);
    expect(navigate).toHaveBeenLastCalledWith("/c/b");
    press({ key: "~", code: "Backquote", shiftKey: true });
    expect(navigate).toHaveBeenLastCalledWith("/c/c");
  });

  it.each([".xterm", ".monaco-editor", "[role=dialog]"])("yields inside %s", (selector) => {
    visitSessions();
    const surface = document.createElement("div");
    if (selector.startsWith(".")) surface.className = selector.slice(1);
    else surface.setAttribute("role", "dialog");
    const input = document.createElement("input");
    surface.append(input);
    document.body.append(surface);
    input.focus();
    expect(press().defaultPrevented).toBe(false);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("works from the composer but ignores repeat and unrelated modifiers", () => {
    visitSessions();
    const input = document.createElement("textarea");
    document.body.append(input);
    input.focus();
    for (const options of [
      { repeat: true },
      { ctrlKey: false },
      { metaKey: true },
      { altKey: true },
    ]) {
      expect(press(options).defaultPrevented).toBe(false);
    }
    expect(navigate).not.toHaveBeenCalled();
    press();
    expect(navigate).toHaveBeenCalledWith("/c/b");
  });

  it("leaves Tab alone with fewer than two visited sessions and after unmount", () => {
    const { unmount } = renderHook(() => useRecentSessionHotkeys(["a", "b"], "a"));
    expect(press().defaultPrevented).toBe(false);
    unmount();
    press();
    expect(navigate).not.toHaveBeenCalled();
  });
});
