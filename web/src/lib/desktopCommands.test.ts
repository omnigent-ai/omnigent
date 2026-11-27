import { act, cleanup, renderHook } from "@testing-library/react";
import { createElement, type ReactNode, Suspense } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  type DesktopCommandHandler,
  resetDesktopCommandsForTests,
  useAvailableDesktopCommands,
  useDesktopCommand,
} from "./desktopCommands";
import { PICK_LIST_INPUT_ATTRIBUTE } from "./listNavigationKeys";
import type { DesktopCommandState } from "./nativeBridge";

type CommandListener = (event: { command: string; browserViewId: string | null }) => void;

let listener: CommandListener | null;
let unsubscribe: ReturnType<typeof vi.fn>;
let setDesktopCommandState: ReturnType<typeof vi.fn<(state: DesktopCommandState) => void>>;

function installShell(): void {
  unsubscribe = vi.fn();
  setDesktopCommandState = vi.fn<(state: DesktopCommandState) => void>();
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    setBadgeCount: () => {},
    notify: async () => false,
    onDesktopCommand: (callback: CommandListener) => {
      listener = callback;
      return unsubscribe;
    },
    setDesktopCommandState,
  };
}

function send(command: string, browserViewId: string | null = null): void {
  listener?.({ command, browserViewId });
}

/** Let the queued state report run. */
async function flushReports(): Promise<void> {
  await Promise.resolve();
}

/** The last reported pick-list focus, once a focus event or the list's attribute has reported. */
async function settledPickListFocused(): Promise<boolean | undefined> {
  // The event or attribute queues a report on one microtask, which runs on the next.
  await flushReports();
  await flushReports();
  return setDesktopCommandState.mock.lastCall?.[0].pickListFocused;
}

beforeEach(() => {
  listener = null;
  installShell();
});

afterEach(() => {
  cleanup();
  resetDesktopCommandsForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  document.body.innerHTML = "";
});

describe("useDesktopCommand", () => {
  it("runs the handler for its command with the focused browser view", () => {
    const handler = vi.fn();
    renderHook(() => useDesktopCommand("close", handler));

    send("close", "browser-tab:s:t");
    send("new-terminal");
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith({
      command: "close",
      browserViewId: "browser-tab:s:t",
      closingQuits: false,
      quitDetail: null,
    });
  });

  it("tries higher priorities first and falls through on false", () => {
    const calls: string[] = [];
    const rail: DesktopCommandHandler = () => {
      calls.push("rail");
      return false;
    };
    const shell: DesktopCommandHandler = () => {
      calls.push("shell");
    };
    renderHook(() => {
      useDesktopCommand("close", shell);
      useDesktopCommand("close", rail, { priority: 10 });
    });

    send("close");
    expect(calls).toEqual(["rail", "shell"]);
  });

  it("stops at the first handler that takes the command", () => {
    const shell = vi.fn();
    renderHook(() => {
      useDesktopCommand("close", shell);
      useDesktopCommand("close", () => true, { priority: 10 });
    });

    send("close");
    expect(shell).not.toHaveBeenCalled();
  });

  it("ignores commands while a dialog holds focus, except Quit", () => {
    const handler = vi.fn();
    const quit = vi.fn();
    renderHook(() => {
      useDesktopCommand("new-browser-tab", handler);
      useDesktopCommand("quit", quit);
    });
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    const input = document.createElement("input");
    dialog.append(input);
    document.body.append(dialog);
    input.focus();

    send("new-browser-tab");
    send("quit");
    expect(handler).not.toHaveBeenCalled();
    expect(quit).toHaveBeenCalledTimes(1);
  });

  it("still reloads while a dialog holds focus", () => {
    const reload = vi.fn();
    const newTab = vi.fn();
    renderHook(() => useDesktopCommand("reload", reload));
    renderHook(() => useDesktopCommand("new-browser-tab", newTab));
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    const input = document.createElement("input");
    dialog.append(input);
    document.body.append(dialog);
    input.focus();

    send("reload");
    send("new-browser-tab");
    expect(reload).toHaveBeenCalledTimes(1);
    expect(newTab).not.toHaveBeenCalled();
  });

  it("uses the latest handler without re-registering", async () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(({ handler }) => useDesktopCommand("new-terminal", handler), {
      initialProps: { handler: first },
    });
    await flushReports();
    rerender({ handler: second });
    await flushReports();

    send("new-terminal");
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
    expect(setDesktopCommandState).toHaveBeenCalledTimes(1);
  });

  it("reports the commands it can run and the session's pin and archive state", async () => {
    const { rerender, unmount } = renderHook(
      ({ pinned, browser }: { pinned: boolean; browser: boolean }) => {
        useDesktopCommand("close", () => {});
        useDesktopCommand("new-browser-tab", browser ? () => {} : null);
        useDesktopCommand("toggle-pin-session", () => {}, { active: pinned });
        useDesktopCommand("archive-session", () => {}, { active: false });
      },
      { initialProps: { pinned: false, browser: true } },
    );
    await flushReports();
    expect(setDesktopCommandState).toHaveBeenLastCalledWith({
      commands: ["archive-session", "close", "new-browser-tab", "toggle-pin-session"],
      sessionPinned: false,
      sessionArchived: false,
      pickListFocused: false,
    });

    rerender({ pinned: true, browser: false });
    await flushReports();
    expect(setDesktopCommandState).toHaveBeenLastCalledWith({
      commands: ["archive-session", "close", "toggle-pin-session"],
      sessionPinned: true,
      sessionArchived: false,
      pickListFocused: false,
    });

    unmount();
    await flushReports();
    expect(setDesktopCommandState).toHaveBeenLastCalledWith({
      commands: [],
      sessionPinned: false,
      sessionArchived: false,
      pickListFocused: false,
    });
    expect(setDesktopCommandState).toHaveBeenCalledTimes(3);
  });

  it("reports while an open pick list has the keyboard, so the menu leaves it Ctrl-N", async () => {
    renderHook(() => useDesktopCommand("close", () => {}));
    const field = document.createElement("input");
    const other = document.createElement("input");
    document.body.append(field, other);
    expect(await settledPickListFocused()).toBe(false);

    field.focus();
    field.setAttribute(PICK_LIST_INPUT_ATTRIBUTE, "");
    expect(await settledPickListFocused()).toBe(true);
    // The list closing drops its attribute, with the field still focused.
    field.removeAttribute(PICK_LIST_INPUT_ATTRIBUTE);
    expect(await settledPickListFocused()).toBe(false);
    field.setAttribute(PICK_LIST_INPUT_ATTRIBUTE, "");
    expect(await settledPickListFocused()).toBe(true);
    other.focus();
    expect(await settledPickListFocused()).toBe(false);
  });

  it("corrects a report left by a removed pick list on the next Ctrl-N or Ctrl-P", async () => {
    renderHook(() => useDesktopCommand("close", () => {}));
    const field = document.createElement("input");
    field.setAttribute(PICK_LIST_INPUT_ATTRIBUTE, "");
    document.body.append(field);
    field.focus();
    expect(await settledPickListFocused()).toBe(true);
    // Removing the focused field moves focus to the body without a focus event.
    field.remove();
    await settledPickListFocused();
    document.body.dispatchEvent(
      new KeyboardEvent("keydown", { key: "n", code: "KeyN", ctrlKey: true, bubbles: true }),
    );
    expect(await settledPickListFocused()).toBe(false);
  });

  it("coalesces a burst of registrations into one report", async () => {
    renderHook(() => {
      useDesktopCommand("close", () => {});
      useDesktopCommand("new-terminal", () => {});
      useDesktopCommand("new-browser-tab", () => {});
    });
    await flushReports();
    expect(setDesktopCommandState).toHaveBeenCalledTimes(1);
  });

  it("stays inert on shells without the command bridge", async () => {
    delete (window as unknown as Record<string, unknown>).omnigentDesktop;
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      setBadgeCount: () => {},
      notify: async () => false,
    };
    const handler = vi.fn();
    renderHook(() => useDesktopCommand("close", handler));
    await flushReports();

    expect(listener).toBeNull();
    expect(setDesktopCommandState).not.toHaveBeenCalled();
  });
});

describe("useDesktopCommand while a render suspends", () => {
  it("keeps running the committed handler", () => {
    const committed = vi.fn();
    const suspended = vi.fn();
    const pending = new Promise<void>(() => {});
    const { rerender } = renderHook(
      ({ handler, suspend }: { handler: DesktopCommandHandler; suspend: boolean }) => {
        useDesktopCommand("close", handler);
        if (suspend) throw pending;
      },
      {
        initialProps: { handler: committed, suspend: false },
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(Suspense, { fallback: null }, children),
      },
    );
    rerender({ handler: suspended, suspend: true });
    send("close");
    expect(committed).toHaveBeenCalledOnce();
    expect(suspended).not.toHaveBeenCalled();
  });
});

describe("useAvailableDesktopCommands", () => {
  it("follows the commands mounted surfaces handle", async () => {
    const available = renderHook(() => useAvailableDesktopCommands());
    expect([...available.result.current]).toEqual([]);

    const fork = renderHook(() => useDesktopCommand("fork-session", () => {}));
    await act(flushReports);
    expect([...available.result.current]).toEqual(["fork-session"]);

    fork.unmount();
    await act(flushReports);
    expect([...available.result.current]).toEqual([]);
  });
});
