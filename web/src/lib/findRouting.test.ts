import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { isMacPlatform } from "./hotkeys";
import {
  findTarget,
  isFindShortcut,
  openFindForActiveSurface,
  openFindFromCommand,
  registerFindSurface,
  useCanOpenFindFromCommand,
} from "./findRouting";

const releases: (() => void)[] = [];

function register(surface: Parameters<typeof registerFindSurface>[0]) {
  releases.push(registerFindSurface(surface));
  return surface;
}

function pressFind(target: EventTarget = window): KeyboardEvent {
  const modifier = isMacPlatform() ? { metaKey: true } : { ctrlKey: true };
  const event = new KeyboardEvent("keydown", {
    key: "f",
    ...modifier,
    bubbles: true,
    cancelable: true,
  });
  target.dispatchEvent(event);
  return event;
}

afterEach(() => {
  for (const release of releases.splice(0)) release();
  document.body.innerHTML = "";
});

describe("isFindShortcut", () => {
  const keys = {
    key: "f",
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
    defaultPrevented: false,
  };

  it("accepts the platform's find chord only when nothing else handled it", () => {
    expect(isFindShortcut({ ...keys, metaKey: true }, true)).toBe(true);
    expect(isFindShortcut({ ...keys, key: "F", ctrlKey: true }, false)).toBe(true);
    expect(isFindShortcut({ ...keys, metaKey: true, defaultPrevented: true }, true)).toBe(false);
    expect(isFindShortcut({ ...keys, metaKey: true, altKey: true }, true)).toBe(false);
    expect(isFindShortcut({ ...keys, metaKey: true, shiftKey: true }, true)).toBe(false);
    expect(isFindShortcut(keys, true)).toBe(false);
  });

  it("leaves Ctrl+F to text fields on macOS, where it moves the caret", () => {
    expect(isFindShortcut({ ...keys, ctrlKey: true }, true)).toBe(false);
    expect(isFindShortcut({ ...keys, metaKey: true }, false)).toBe(false);
  });
});

describe("find routing", () => {
  it("sends find to the surface the user is working in, else to the fallback", () => {
    let fileActive = true;
    const chat = register({ fallback: true, isActive: () => true, openFind: vi.fn() });
    const file = register({ isActive: () => fileActive, openFind: vi.fn() });
    expect(findTarget()).toBe(file);
    fileActive = false;
    expect(findTarget()).toBe(chat);
  });

  it("opens the target from the shortcut and keeps the browser's find out of the way", () => {
    const chat = register({ fallback: true, isActive: () => true, openFind: vi.fn() });
    const event = pressFind();
    expect(event.defaultPrevented).toBe(true);
    expect(chat.openFind).toHaveBeenCalledOnce();
  });

  it("leaves Ctrl+F to a focused terminal's shell off macOS, and takes ⌘F there on macOS", () => {
    const terminal = register({ isActive: () => true, openFind: vi.fn() });
    document.body.innerHTML = `<div data-terminal-id="term_1"><textarea></textarea></div>`;
    const input = document.querySelector("textarea")!;
    input.focus();
    const offMac = pressFind(input);
    expect(offMac.defaultPrevented).toBe(false);
    expect(terminal.openFind).not.toHaveBeenCalled();

    vi.stubGlobal("navigator", { ...navigator, platform: "MacIntel", userAgentData: undefined });
    try {
      const onMac = pressFind(input);
      expect(onMac.defaultPrevented).toBe(true);
      expect(terminal.openFind).toHaveBeenCalledOnce();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("leaves the shortcut to a dialog that has focus", () => {
    const chat = register({ fallback: true, isActive: () => true, openFind: vi.fn() });
    document.body.innerHTML = `<div role="dialog"><input /></div>`;
    document.querySelector("input")!.focus();
    const event = pressFind(document.activeElement!);
    expect(event.defaultPrevented).toBe(false);
    expect(chat.openFind).not.toHaveBeenCalled();
  });
});

describe("a second ⌘F", () => {
  /** A component the user is in, with a find of its own. */
  const component = () => register({ isActive: () => true, openFind: vi.fn() });
  /** Focus in a find bar's field, as after the first ⌘F; or on one of its buttons. */
  function focusFindBar(on: "field" | "button" = "field") {
    document.body.innerHTML = `<div data-find-bar><input aria-label="Find" /><button>Next</button></div><div tabindex="0" id="content">content</div>`;
    document.querySelector<HTMLElement>(on === "field" ? "input" : "button")!.focus();
  }

  it("goes to the app's find from inside the component's find bar, field or buttons", () => {
    const chat = register({ fallback: true, isActive: () => true, openFind: vi.fn() });
    const file = component();
    pressFind();
    expect(file.openFind).toHaveBeenCalledOnce();
    focusFindBar();
    expect(pressFind(document.activeElement!).defaultPrevented).toBe(true);
    expect(chat.openFind).toHaveBeenCalledOnce();
    focusFindBar("button");
    pressFind(document.activeElement!);
    expect(chat.openFind).toHaveBeenCalledTimes(2);
    expect(file.openFind).toHaveBeenCalledOnce();
  });

  it("refocuses the component's open find from its content, as before", () => {
    const chat = register({ fallback: true, isActive: () => true, openFind: vi.fn() });
    const file = component();
    focusFindBar();
    document.querySelector<HTMLElement>("#content")!.focus();
    pressFind(document.activeElement!);
    expect(file.openFind).toHaveBeenCalledOnce();
    expect(chat.openFind).not.toHaveBeenCalled();
  });

  it("finds in the code editor's own find widget too", () => {
    const chat = register({ fallback: true, isActive: () => true, openFind: vi.fn() });
    const file = component();
    document.body.innerHTML = `<div class="monaco-editor"><div class="find-widget"><textarea aria-label="Find"></textarea></div></div>`;
    document.querySelector("textarea")!.focus();
    pressFind(document.activeElement!);
    expect(chat.openFind).toHaveBeenCalledOnce();
    expect(file.openFind).not.toHaveBeenCalled();
  });

  it("never goes to another component's find, even one that is also active", () => {
    const chat = register({ fallback: true, isActive: () => true, openFind: vi.fn() });
    const older = component();
    const newer = component();
    focusFindBar();
    pressFind(document.activeElement!);
    expect(older.openFind).not.toHaveBeenCalled();
    expect(newer.openFind).not.toHaveBeenCalled();
    expect(chat.openFind).toHaveBeenCalledOnce();
  });

  it("does nothing with no app find to go to, and keeps the browser's find out of the way", () => {
    const file = component();
    focusFindBar();
    const repeat = pressFind(document.activeElement!);
    expect(repeat.defaultPrevented).toBe(true);
    expect(file.openFind).not.toHaveBeenCalled();
  });

  it("sends an app request past the component to the app's find", () => {
    const chat = register({ fallback: true, isActive: () => true, openFind: vi.fn() });
    const file = component();
    openFindForActiveSurface({ to: "app" });
    expect(chat.openFind).toHaveBeenCalledOnce();
    expect(file.openFind).not.toHaveBeenCalled();
    openFindForActiveSurface();
    expect(file.openFind).toHaveBeenCalledOnce();
  });

  it("leaves ⌘K's Find on the component, even from its find bar", () => {
    const chat = register({ fallback: true, isActive: () => true, openFind: vi.fn() });
    const file = component();
    focusFindBar();
    openFindFromCommand();
    expect(file.openFind).toHaveBeenCalledOnce();
    expect(chat.openFind).not.toHaveBeenCalled();
  });
});

describe("the ⌘K Find command", () => {
  it("opens the active component's find, else the app's, else the newest file's", () => {
    const file = register({ isActive: () => false, openFind: vi.fn() });
    openFindFromCommand();
    expect(file.openFind).toHaveBeenCalledOnce();
    const chat = register({ fallback: true, isActive: () => true, openFind: vi.fn() });
    openFindFromCommand();
    expect(chat.openFind).toHaveBeenCalledOnce();
    file.isActive = () => true;
    openFindFromCommand();
    expect(file.openFind).toHaveBeenCalledTimes(2);
  });

  it("opens the app's find when it's the only one offered, though not active", () => {
    const { result, rerender } = renderHook(() => useCanOpenFindFromCommand());
    const chat = register({ fallback: true, isActive: () => false, openFind: vi.fn() });
    rerender();
    expect(result.current).toBe(true);
    openFindFromCommand();
    expect(chat.openFind).toHaveBeenCalledOnce();
  });

  it("is offered while something can take find", () => {
    const { result, rerender } = renderHook(() => useCanOpenFindFromCommand());
    expect(result.current).toBe(false);
    register({ fallback: true, isActive: () => true, openFind: vi.fn() });
    rerender();
    expect(result.current).toBe(true);
  });
});
