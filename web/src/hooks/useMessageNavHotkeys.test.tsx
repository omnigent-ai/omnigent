import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as HotkeysModule from "@/lib/hotkeys";

import { useMessageNavHotkeys } from "./useMessageNavHotkeys";

const platform = vi.hoisted(() => ({ mac: false }));
vi.mock("@/lib/hotkeys", async (importOriginal) => ({
  ...(await importOriginal<typeof HotkeysModule>()),
  isMacPlatform: () => platform.mac,
}));

afterEach(() => {
  platform.mac = false;
});

function press(init: KeyboardEventInit & { altGraph?: boolean }): KeyboardEvent {
  const { altGraph = false, ...rest } = init;
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...rest });
  Object.defineProperty(event, "getModifierState", {
    value: (key: string) => key === "AltGraph" && altGraph,
  });
  window.dispatchEvent(event);
  return event;
}

function mount() {
  const nav = { goPrev: vi.fn(), goNext: vi.fn() };
  const hook = renderHook(() => useMessageNavHotkeys(nav));
  return { nav, ...hook };
}

describe("useMessageNavHotkeys", () => {
  it("steps with Ctrl+Alt and an arrow off macOS, Shift held or not", () => {
    const { nav } = mount();
    expect(press({ key: "ArrowUp", ctrlKey: true, altKey: true }).defaultPrevented).toBe(true);
    press({ key: "ArrowDown", ctrlKey: true, altKey: true, shiftKey: true });
    expect(nav.goPrev).toHaveBeenCalledTimes(1);
    expect(nav.goNext).toHaveBeenCalledTimes(1);
  });

  it("steps with ⌘⌥ and an arrow on macOS, but not with Control added", () => {
    platform.mac = true;
    const { nav } = mount();
    press({ key: "ArrowUp", metaKey: true, altKey: true, shiftKey: true });
    press({ key: "ArrowDown", metaKey: true, ctrlKey: true, altKey: true });
    press({ key: "ArrowDown", ctrlKey: true, altKey: true });
    expect(nav.goPrev).toHaveBeenCalledTimes(1);
    expect(nav.goNext).not.toHaveBeenCalled();
  });

  it("leaves AltGr arrows alone on Windows and Linux, which report AltGr as Ctrl+Alt", () => {
    const { nav } = mount();
    press({ key: "ArrowDown", ctrlKey: true, altKey: true, altGraph: true });
    press({ key: "ArrowUp", ctrlKey: true, altKey: true, altGraph: true });
    expect(nav.goNext).not.toHaveBeenCalled();
    expect(nav.goPrev).not.toHaveBeenCalled();
  });

  it("still steps with ⌘⌥ on macOS, where Firefox reports Option as AltGr", () => {
    platform.mac = true;
    const { nav } = mount();
    press({ key: "ArrowUp", metaKey: true, altKey: true, altGraph: true });
    press({ key: "ArrowDown", metaKey: true, altKey: true, altGraph: true });
    expect(nav.goPrev).toHaveBeenCalledTimes(1);
    expect(nav.goNext).toHaveBeenCalledTimes(1);
  });

  it("leaves other chords, a chord a focused editor took, and stops on unmount", () => {
    const { nav, unmount } = mount();
    press({ key: "ArrowUp", altKey: true });
    press({ key: "ArrowUp", ctrlKey: true });
    const taken = new KeyboardEvent("keydown", {
      key: "ArrowUp",
      ctrlKey: true,
      altKey: true,
      cancelable: true,
    });
    taken.preventDefault();
    window.dispatchEvent(taken);
    unmount();
    press({ key: "ArrowUp", ctrlKey: true, altKey: true });
    expect(nav.goPrev).not.toHaveBeenCalled();
  });
});
