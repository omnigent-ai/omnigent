import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { PinIcon } from "lucide-react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSidebarSessionOrderStore } from "@/hooks/useSidebarSessionOrder";
import type * as HotkeysModule from "@/lib/hotkeys";
import type * as NativeBridgeModule from "@/lib/nativeBridge";
import type { BrowserRecentSessionInput } from "@/lib/nativeBridge";
import { AVAILABLE, type PaletteCommand, usePaletteCommandsStore } from "@/store/paletteCommands";

import { QuickPickShortcutHost } from "./QuickPickShortcutHost";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));
const shell = vi.hoisted(() => ({ openSession: null as string | null, mac: true, menu: false }));
vi.mock("../commandContext", () => ({
  useCommandContext: () => ({ session: shell.openSession ? { id: shell.openSession } : null }),
}));
vi.mock("@/lib/hotkeys", async (importOriginal) => ({
  ...(await importOriginal<typeof HotkeysModule>()),
  isMacPlatform: () => shell.mac,
}));
const bridge = vi.hoisted(() => ({
  forward: null as ((input: BrowserRecentSessionInput) => void) | null,
  supported: [] as boolean[],
  cancelled: 0,
}));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridgeModule>()),
  supportsDesktopCommands: () => shell.menu,
  onBrowserRecentSessionInput: (callback: (input: BrowserRecentSessionInput) => void) => {
    bridge.forward = callback;
    return () => {
      bridge.forward = null;
    };
  },
  setBrowserRecentSessionSwitchSupported: async (supported: boolean) => {
    bridge.supported.push(supported);
  },
  cancelBrowserRecentSessionSwitch: async () => {
    bridge.cancelled += 1;
  },
}));

/** Register the open session's header row `id`, as its header does. */
function headerRow(id: string, availability: PaletteCommand["availability"] = AVAILABLE) {
  const run = vi.fn();
  usePaletteCommandsStore.setState({
    groups: {
      "open-session:c1": {
        heading: "Session",
        commands: [
          {
            id: `open-session:c1:${id}`,
            label: id,
            icon: PinIcon,
            keywords: [],
            availability,
            run,
          },
        ],
      },
    },
  });
  return run;
}

const PIN = { key: "π", code: "KeyP", metaKey: true, altKey: true };
const ARCHIVE = { key: "A", code: "KeyA", metaKey: true, shiftKey: true };

const NEXT = { key: "ArrowDown", code: "ArrowDown", ctrlKey: true, altKey: true, shiftKey: true };
const PREVIOUS = { key: "ArrowUp", code: "ArrowUp", ctrlKey: true, altKey: true, shiftKey: true };
const press = (init: KeyboardEventInit) => fireEvent.keyDown(window, init);
const CTRL_TAB = { key: "Tab", code: "Tab", ctrlKey: true };
const CTRL_SHIFT_TAB = { ...CTRL_TAB, shiftKey: true };

/** Focus a field inside a new element matching `markup`, such as a terminal. */
function focusInside(attributes: Record<string, string>): () => void {
  const surface = document.createElement("div");
  for (const [name, value] of Object.entries(attributes)) surface.setAttribute(name, value);
  const field = document.createElement("textarea");
  surface.append(field);
  document.body.append(surface);
  act(() => field.focus());
  return () => surface.remove();
}

/** A key the desktop shell forwards from an embedded Browser page. */
function forwarded(input: Partial<BrowserRecentSessionInput>): void {
  act(() =>
    bridge.forward?.({
      type: "keydown",
      key: "Tab",
      code: "Tab",
      ctrlKey: true,
      shiftKey: false,
      altKey: false,
      metaKey: false,
      repeat: false,
      ...input,
    }),
  );
}

beforeEach(() => {
  useSidebarSessionOrderStore.setState({
    orderedSessionIds: ["a", "b", "c"],
    activeSessionId: "b",
  });
});
afterEach(() => {
  cleanup();
  navigate.mockClear();
  usePaletteCommandsStore.setState({ groups: {} });
  Object.assign(shell, { openSession: null, mac: true, menu: false });
  Object.assign(bridge, { forward: null, supported: [], cancelled: 0 });
});

describe("QuickPickShortcutHost", () => {
  it("steps through the sidebar's sessions as ⌘K's Next and Previous session do", () => {
    render(<QuickPickShortcutHost />);
    press(NEXT);
    press(PREVIOUS);
    expect(navigate.mock.calls).toEqual([["/c/c"], ["/c/a"]]);
  });

  it("leaves the key alone when no sidebar lists sessions, or a dialog has focus", () => {
    render(<QuickPickShortcutHost />);
    act(() =>
      useSidebarSessionOrderStore.setState({ orderedSessionIds: [], activeSessionId: null }),
    );
    expect(press(NEXT)).toBe(true);
    expect(navigate).not.toHaveBeenCalled();

    act(() =>
      useSidebarSessionOrderStore.setState({ orderedSessionIds: ["a", "b"], activeSessionId: "a" }),
    );
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    const field = document.createElement("input");
    dialog.append(field);
    document.body.append(dialog);
    act(() => field.focus());
    press(NEXT);
    expect(navigate).not.toHaveBeenCalled();
    dialog.remove();
  });

  it("claims its chord before a page listener that would read it too", () => {
    render(<QuickPickShortcutHost />);
    // As the transcript's message steps do, which don't check Shift.
    const pageListener = vi.fn((event: KeyboardEvent) => event.defaultPrevented);
    window.addEventListener("keydown", pageListener);
    press(NEXT);
    window.removeEventListener("keydown", pageListener);
    expect(navigate).toHaveBeenCalledWith("/c/c");
    expect(pageListener).not.toHaveBeenCalled();
  });

  it("keeps a held chord's repeats from that listener, stepping only once", () => {
    shell.mac = false;
    render(<QuickPickShortcutHost />);
    const pageListener = vi.fn();
    window.addEventListener("keydown", pageListener);
    press(NEXT);
    press({ ...NEXT, repeat: true });
    press({ ...NEXT, repeat: true });
    window.removeEventListener("keydown", pageListener);
    expect(navigate.mock.calls).toEqual([["/c/c"]]);
    expect(pageListener).not.toHaveBeenCalled();
  });

  it("stops listening once unmounted", () => {
    const { unmount } = render(<QuickPickShortcutHost />);
    unmount();
    press(NEXT);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("steps with Ctrl+Tab and Ctrl+Shift+Tab, with no switcher", () => {
    render(<QuickPickShortcutHost />);
    expect(press(CTRL_TAB)).toBe(false);
    expect(press(CTRL_SHIFT_TAB)).toBe(false);
    expect(navigate.mock.calls).toEqual([["/c/c"], ["/c/a"]]);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it("takes Ctrl+Tab from a terminal, but leaves it to a dialog", () => {
    render(<QuickPickShortcutHost />);
    const removeTerminal = focusInside({ class: "xterm" });
    press(NEXT);
    expect(navigate).not.toHaveBeenCalled();
    press(CTRL_TAB);
    expect(navigate).toHaveBeenCalledWith("/c/c");
    removeTerminal();

    navigate.mockClear();
    const removeDialog = focusInside({ role: "dialog" });
    expect(press(CTRL_TAB)).toBe(true);
    expect(navigate).not.toHaveBeenCalled();
    removeDialog();
  });

  it("steps once for a held Ctrl+Tab and swallows its repeats", () => {
    render(<QuickPickShortcutHost />);
    press(CTRL_TAB);
    expect(press({ ...CTRL_TAB, repeat: true })).toBe(false);
    expect(press({ ...CTRL_TAB, repeat: true })).toBe(false);
    expect(navigate).toHaveBeenCalledTimes(1);
  });

  it("steps for Ctrl+Tab forwarded from a Browser page and clears the shell's latch", () => {
    const { unmount } = render(<QuickPickShortcutHost />);
    expect(bridge.supported).toEqual([true]);
    forwarded({});
    forwarded({ shiftKey: true });
    forwarded({ repeat: true });
    forwarded({ type: "keyup", key: "Control" });
    expect(navigate.mock.calls).toEqual([["/c/c"], ["/c/a"]]);
    expect(bridge.cancelled).toBe(3);
    unmount();
    expect(bridge.supported).toEqual([true, false]);
    expect(bridge.forward).toBeNull();
  });

  it("runs the open session's header rows from the menu's keys, in their current state", () => {
    shell.openSession = "c1";
    render(<QuickPickShortcutHost />);
    const unpin = headerRow("unpin");
    press(PIN);
    expect(unpin).toHaveBeenCalledTimes(1);
    const archive = headerRow("archive");
    press(ARCHIVE);
    expect(archive).toHaveBeenCalledTimes(1);
    const greyed = headerRow("pin", { available: false, reason: "A pin change is still saving." });
    expect(press(PIN)).toBe(true);
    expect(greyed).not.toHaveBeenCalled();
  });

  it("leaves Pin to the desktop menu, and to Go to a file off macOS", () => {
    shell.openSession = "c1";
    shell.menu = true;
    const { unmount } = render(<QuickPickShortcutHost />);
    const pin = headerRow("pin");
    press(PIN);
    unmount();
    shell.menu = false;
    shell.mac = false;
    render(<QuickPickShortcutHost />);
    press({ key: "p", code: "KeyP", ctrlKey: true, altKey: true });
    expect(pin).not.toHaveBeenCalled();
  });
});
