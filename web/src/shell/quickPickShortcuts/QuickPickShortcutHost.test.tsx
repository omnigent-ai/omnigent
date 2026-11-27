import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { PinIcon } from "lucide-react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSidebarSessionOrderStore } from "@/hooks/useSidebarSessionOrder";
import type * as HotkeysModule from "@/lib/hotkeys";
import type * as NativeBridgeModule from "@/lib/nativeBridge";
import type { BrowserRecentSessionInput } from "@/lib/nativeBridge";
import { AVAILABLE, type PaletteCommand, usePaletteCommandsStore } from "@/store/paletteCommands";

import { QuickPickShortcutHost } from "./QuickPickShortcutHost";

/** Inside a query client, as in the app shell: ⌘K's recent-session row reads its cache. */
const renderHost = () =>
  render(
    <QueryClientProvider client={new QueryClient()}>
      <QuickPickShortcutHost />
    </QueryClientProvider>,
  );

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
const FORK = { key: "ƒ", code: "KeyF", metaKey: true, altKey: true };

const press = (init: KeyboardEventInit) => fireEvent.keyDown(window, init);
const CTRL_TAB = { key: "Tab", code: "Tab", ctrlKey: true };
const CTRL_SHIFT_TAB = { ...CTRL_TAB, shiftKey: true };
const NEXT = CTRL_TAB;
const PREVIOUS = CTRL_SHIFT_TAB;

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
    pendingSessionId: null,
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
  it("steps through the sidebar's sessions with Ctrl+Tab and Ctrl+Shift+Tab, with no switcher", () => {
    renderHost();
    expect(press(NEXT)).toBe(false);
    expect(press(PREVIOUS)).toBe(false);
    expect(navigate.mock.calls).toEqual([["/c/c"], ["/c/b"]]);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it("steps sessions with Ctrl+Alt+Shift+arrows too, the chord a browser tab reaches", () => {
    renderHost();
    const arrow = { ctrlKey: true, altKey: true, shiftKey: true };
    expect(press({ key: "ArrowDown", code: "ArrowDown", ...arrow })).toBe(false);
    expect(press({ key: "ArrowUp", code: "ArrowUp", ...arrow })).toBe(false);
    expect(navigate.mock.calls).toEqual([["/c/c"], ["/c/b"]]);
  });

  it("leaves the key alone when no sidebar lists sessions, or a dialog has focus", () => {
    renderHost();
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
    renderHost();
    // As a page listener that reads Control+Tab would.
    const pageListener = vi.fn((event: KeyboardEvent) => event.defaultPrevented);
    window.addEventListener("keydown", pageListener);
    press(NEXT);
    window.removeEventListener("keydown", pageListener);
    expect(navigate).toHaveBeenCalledWith("/c/c");
    expect(pageListener).not.toHaveBeenCalled();
  });

  it("stops listening once unmounted", () => {
    const { unmount } = renderHost();
    unmount();
    press(NEXT);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("takes Ctrl+Tab from a terminal, but leaves it to a dialog", () => {
    renderHost();
    const removeTerminal = focusInside({ class: "xterm" });
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
    renderHost();
    press(CTRL_TAB);
    expect(press({ ...CTRL_TAB, repeat: true })).toBe(false);
    expect(press({ ...CTRL_TAB, repeat: true })).toBe(false);
    expect(navigate).toHaveBeenCalledTimes(1);
  });

  it("steps for Ctrl+Tab forwarded from a Browser page and clears the shell's latch", () => {
    const { unmount } = renderHost();
    expect(bridge.supported).toEqual([true]);
    forwarded({});
    forwarded({ shiftKey: true });
    forwarded({ repeat: true });
    forwarded({ type: "keyup", key: "Control" });
    expect(navigate.mock.calls).toEqual([["/c/c"], ["/c/b"]]);
    expect(bridge.cancelled).toBe(3);
    unmount();
    expect(bridge.supported).toEqual([true, false]);
    expect(bridge.forward).toBeNull();
  });

  it("runs the open session's header rows from the menu's keys, in their current state", () => {
    shell.openSession = "c1";
    renderHost();
    const unpin = headerRow("unpin");
    press(PIN);
    expect(unpin).toHaveBeenCalledTimes(1);
    const archive = headerRow("archive");
    press(ARCHIVE);
    expect(archive).toHaveBeenCalledTimes(1);
    const fork = headerRow("fork");
    expect(press(FORK)).toBe(false);
    expect(fork).toHaveBeenCalledTimes(1);
    const greyed = headerRow("pin", { available: false, reason: "A pin change is still saving." });
    expect(press(PIN)).toBe(true);
    expect(greyed).not.toHaveBeenCalled();
  });

  it("leaves Pin to the desktop menu, and to Go to a file off macOS", () => {
    shell.openSession = "c1";
    shell.menu = true;
    const { unmount } = renderHost();
    const pin = headerRow("pin");
    press(PIN);
    unmount();
    shell.menu = false;
    shell.mac = false;
    renderHost();
    press({ key: "p", code: "KeyP", ctrlKey: true, altKey: true });
    expect(pin).not.toHaveBeenCalled();
  });
});
