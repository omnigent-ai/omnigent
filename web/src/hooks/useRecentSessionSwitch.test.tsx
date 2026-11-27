// ⌃] / ⌃[ open a transient recency switcher: presses while Control is held
// cycle its selection, and releasing Control opens it.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EmbeddedProvider } from "@/lib/embedded";
import { setEmbedRoot } from "@/lib/host";
import { shortcutKeys } from "@/lib/shortcuts/registry";

import {
  cancelRecentSessionSwitcher,
  commitRecentSessionSwitcher,
  openRecentSessionSwitcher,
  selectRecentSession,
  useClaimRecentSessionChord,
  useRecentSessionHotkey,
  useRecentSessionSwitchStore,
  useRecordRecentSessionVisits,
  useRegisterLiveSessions,
} from "./useRecentSessionSwitch";
import { useSidebarSessionOrderStore } from "./useSidebarSessionOrder";
import { mountKeystrokeSurface } from "@/test/keystrokeSurfaces";

const navigate = vi.fn((path: string) => {
  // Opening a session makes it the sidebar's active row, as the route change would.
  const id = path.replace("/c/", "");
  useSidebarSessionOrderStore.setState({ activeSessionId: id });
});
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
}));

/** The sidebar's visible rows; unless told otherwise, exactly the loaded live sessions. */
function sidebar(
  orderedSessionIds: string[],
  activeSessionId: string | null,
  liveSessionIds: string[] = orderedSessionIds,
) {
  useSidebarSessionOrderStore.setState({
    orderedSessionIds,
    activeSessionId,
    pendingSessionId: null,
  });
  const live = new Set(liveSessionIds);
  useRecentSessionSwitchStore.setState({ readLiveSessionIds: () => live, sessionsKnown: true });
}

function history(ids: string[]) {
  useRecentSessionSwitchStore.setState({ history: ids, switcher: null, chordOwner: null });
}

const ctrlBracketRight = { key: "]", code: "BracketRight", ctrlKey: true };
const ctrlBracketLeft = { key: "[", code: "BracketLeft", ctrlKey: true };

function press(init: KeyboardEventInit, target: EventTarget = document.body): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  act(() => {
    target.dispatchEvent(event);
  });
  return event;
}

function releaseControl() {
  act(() => {
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "Control", bubbles: true }));
  });
}

const opened = () => navigate.mock.calls.map(([path]) => path);
const switcher = () => useRecentSessionSwitchStore.getState().switcher;
const selected = () => switcher()?.selectedId;

function pointer(type: "pointermove" | "pointerdown", ctrlKey: boolean) {
  act(() => {
    window.dispatchEvent(new PointerEvent(type, { ctrlKey, bubbles: true }));
  });
}

beforeEach(() => {
  navigate.mockClear();
  localStorage.clear();
  sidebar(["a", "b", "c", "d"], "a");
  history(["a", "b", "c"]);
});

afterEach(() => {
  setEmbedRoot(null);
  document.body.innerHTML = "";
  sidebar([], null);
  vi.restoreAllMocks();
});

describe("useRecentSessionHotkey", () => {
  it("leaves the host page's own ⌃] alone when embedded, and switches inside the embed", () => {
    const root = document.createElement("div");
    const inside = document.createElement("input");
    const hostInput = document.createElement("input");
    root.appendChild(inside);
    document.body.append(root, hostInput);
    setEmbedRoot(root);
    renderHook(() => useRecentSessionHotkey(true), { wrapper: EmbeddedProvider });

    hostInput.focus();
    expect(press(ctrlBracketRight, hostInput).defaultPrevented).toBe(false);
    expect(switcher()).toBeNull();

    inside.focus();
    expect(press(ctrlBracketRight, inside).defaultPrevented).toBe(true);
    // A press the host page sees doesn't step a switch started inside the embed.
    expect(press(ctrlBracketRight, hostInput).defaultPrevented).toBe(false);
    expect(selected()).toBe("b");
    releaseControl();
    expect(opened()).toEqual(["/c/b"]);
  });

  it("⌃] opens the switcher one step back, without leaving the open session", () => {
    renderHook(() => useRecentSessionHotkey(true));
    const event = press(ctrlBracketRight);
    expect(event.defaultPrevented).toBe(true);
    expect(switcher()).toMatchObject({ holding: true, order: ["a", "b", "c", "d"] });
    expect(selected()).toBe("b");
    expect(opened()).toEqual([]);
  });

  it("cycles while Control stays held, wrapping, then opens the selection on release", () => {
    renderHook(() => useRecentSessionHotkey(true));
    press(ctrlBracketRight);
    press(ctrlBracketRight);
    press(ctrlBracketRight);
    expect(selected()).toBe("d");
    press(ctrlBracketRight);
    expect(selected()).toBe("a");
    press(ctrlBracketLeft);
    expect(selected()).toBe("d");
    expect(opened()).toEqual([]);

    releaseControl();
    expect(opened()).toEqual(["/c/d"]);
    expect(switcher()).toBeNull();
    expect(useRecentSessionSwitchStore.getState().history).toEqual(["d", "a", "b", "c"]);
  });

  it("toggles between the last two sessions on separate presses, like ⌘Tab", () => {
    renderHook(() => useRecentSessionHotkey(true));
    press(ctrlBracketRight);
    releaseControl();
    press(ctrlBracketRight);
    releaseControl();
    expect(opened()).toEqual(["/c/b", "/c/a"]);
  });

  it("⌃[ starts the other way, at the never-visited and oldest sessions", () => {
    renderHook(() => useRecentSessionHotkey(true));
    press(ctrlBracketLeft);
    expect(selected()).toBe("d");
  });

  it("skips visits to sessions the sidebar no longer lists", () => {
    history(["a", "deleted", "archived", "c"]);
    renderHook(() => useRecentSessionHotkey(true));
    press(ctrlBracketRight);
    expect(selected()).toBe("c");
  });

  it("Esc cancels and stays on the open session, even with Control still held", () => {
    renderHook(() => useRecentSessionHotkey(true));
    press(ctrlBracketRight);
    // The dialog closes on Esc; the binding must not read it as a release.
    press({ key: "Escape", code: "Escape", ctrlKey: true });
    press({ key: "Escape", code: "Escape" });
    cancelRecentSessionSwitcher();
    releaseControl();
    expect(opened()).toEqual([]);
    expect(switcher()).toBeNull();
  });

  it("closes without switching when the window loses focus or is hidden", () => {
    renderHook(() => useRecentSessionHotkey(true));
    press(ctrlBracketRight);
    act(() => {
      window.dispatchEvent(new Event("blur"));
    });
    expect(switcher()).toBeNull();

    press(ctrlBracketRight);
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    Reflect.deleteProperty(document, "visibilityState");
    expect(switcher()).toBeNull();
    releaseControl();
    expect(opened()).toEqual([]);
  });

  it("opens the selection on a key or pointer event without Control, if the release was missed", () => {
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    renderHook(() => useRecentSessionHotkey(true));
    press(ctrlBracketRight);
    pointer("pointermove", true);
    expect(switcher()).not.toBeNull();
    pointer("pointermove", false);
    expect(opened()).toEqual(["/c/b"]);

    press(ctrlBracketRight);
    press({ key: "x", code: "KeyX" });
    expect(opened()).toEqual(["/c/b", "/c/a"]);
  });

  it("ignores pointer events while this window isn't focused", () => {
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    renderHook(() => useRecentSessionHotkey(true));
    press(ctrlBracketRight);
    pointer("pointermove", false);
    expect(opened()).toEqual([]);
    expect(switcher()).not.toBeNull();
  });

  it("steps once per press: key repeats are consumed without moving", () => {
    renderHook(() => useRecentSessionHotkey(true));
    press(ctrlBracketRight);
    const repeat = press({ ...ctrlBracketRight, repeat: true });
    expect(selected()).toBe("b");
    expect(repeat.defaultPrevented).toBe(true);
  });

  it.each([
    { surface: "xterm terminal", kind: "xterm" },
    { surface: "code editor", kind: "monaco-editor" },
  ] as const)("leaves ⌃[ and ⌃] to a focused $surface, where ⌃[ is Escape", ({ kind }) => {
    renderHook(() => useRecentSessionHotkey(true));
    const { input } = mountKeystrokeSurface(kind);

    const escape = press(ctrlBracketLeft, input);
    const groupSeparator = press(ctrlBracketRight, input);
    expect(switcher()).toBeNull();
    expect(escape.defaultPrevented).toBe(false);
    expect(groupSeparator.defaultPrevented).toBe(false);
  });

  it.each([
    { name: "⌃⇧]", init: { ...ctrlBracketRight, shiftKey: true } },
    { name: "⌃⌥]", init: { ...ctrlBracketRight, altKey: true } },
    { name: "⌘]", init: { key: "]", code: "BracketRight", metaKey: true } },
  ])("ignores $name", ({ init }) => {
    renderHook(() => useRecentSessionHotkey(true));
    const event = press(init);
    expect(switcher()).toBeNull();
    expect(event.defaultPrevented).toBe(false);
  });

  it("leaves the chord alone once the sessions are known to be none", () => {
    sidebar([], null);
    renderHook(() => useRecentSessionHotkey(true));
    const event = press(ctrlBracketRight);
    expect(switcher()).toBeNull();
    expect(event.defaultPrevented).toBe(false);
  });

  it("is not bound off macOS, where Ctrl+[ and Ctrl+] are taken", () => {
    renderHook(() => useRecentSessionHotkey(false));
    const event = press(ctrlBracketRight);
    expect(switcher()).toBeNull();
    expect(event.defaultPrevented).toBe(false);
    expect(shortcutKeys("session.recent", { mac: false })).toEqual([]);
    expect(shortcutKeys("session.recent", { mac: true })).toEqual(["⌃", "]"]);
  });

  it("hands the first press to a surface that claims it, wherever focus is", () => {
    const onSwitch = vi.fn();
    renderHook(() => {
      useRecentSessionHotkey(true);
      useClaimRecentSessionChord(true, onSwitch);
    });
    const button = document.createElement("button");
    document.body.appendChild(button);
    button.focus();
    const event = press(ctrlBracketLeft, button);
    expect(onSwitch.mock.calls).toEqual([[-1]]);
    expect(switcher()).toBeNull();
    expect(event.defaultPrevented).toBe(true);
  });
});

describe("a switcher opened before the sessions load", () => {
  function loadingSidebar() {
    const live = new Set(["a", "b", "c", "d"]);
    const readLiveSessionIds = () => live;
    const view = renderHook(
      ({ known }: { known: boolean }) => {
        useRegisterLiveSessions(readLiveSessionIds, known);
        useRecentSessionHotkey(true);
      },
      { initialProps: { known: false } },
    );
    return { arrive: () => view.rerender({ known: true }) };
  }

  it("shows loading, keeps the presses, and selects once the rows arrive", () => {
    const { arrive } = loadingSidebar();
    press(ctrlBracketRight);
    press(ctrlBracketRight);
    expect(switcher()).toMatchObject({ order: null, selectedId: null });
    arrive();
    expect(selected()).toBe("c");
    expect(opened()).toEqual([]);
  });

  it("opens the selection as soon as the rows arrive, after an early release", () => {
    const { arrive } = loadingSidebar();
    press(ctrlBracketRight);
    releaseControl();
    expect(opened()).toEqual([]);
    expect(switcher()).not.toBeNull();
    arrive();
    expect(opened()).toEqual(["/c/b"]);
    expect(switcher()).toBeNull();
  });

  it("doesn't open anything if the window lost focus while it waited", () => {
    const { arrive } = loadingSidebar();
    press(ctrlBracketRight);
    releaseControl();
    act(() => {
      window.dispatchEvent(new Event("blur"));
    });
    arrive();
    expect(opened()).toEqual([]);
  });
});

describe("openRecentSessionSwitcher from ⌘K", () => {
  it("stays open until Enter or a click, ignoring Control's release", () => {
    renderHook(() => useRecentSessionHotkey(true));
    expect(openRecentSessionSwitcher(1, { holding: false, navigate })).toBe(true);
    releaseControl();
    pointer("pointermove", false);
    expect(switcher()).toMatchObject({ holding: false });
    selectRecentSession("c");
    commitRecentSessionSwitcher();
    expect(opened()).toEqual(["/c/c"]);
    expect(useRecentSessionSwitchStore.getState().history).toEqual(["c", "a", "b"]);
  });

  it("opens a clicked row, and stays put on the open one", () => {
    openRecentSessionSwitcher(1, { holding: false, navigate });
    commitRecentSessionSwitcher("a");
    expect(opened()).toEqual([]);
    openRecentSessionSwitcher(1, { holding: false, navigate });
    commitRecentSessionSwitcher("d");
    expect(opened()).toEqual(["/c/d"]);
  });

  it("reports nothing to do on an empty sidebar", () => {
    sidebar([], null);
    expect(openRecentSessionSwitcher(1, { holding: false, navigate })).toBe(false);
    expect(switcher()).toBeNull();
  });

  it("selects the latest visit when no session is open", () => {
    sidebar(["a", "b", "c"], null);
    openRecentSessionSwitcher(1, { holding: false, navigate });
    expect(selected()).toBe("a");
  });

  it("reaches a live visit the sidebar hides, such as one in a collapsed folder", () => {
    sidebar(["a", "c", "d"], "a", ["a", "b", "c", "d"]);
    openRecentSessionSwitcher(1, { holding: false, navigate });
    expect(selected()).toBe("b");
  });

  it("never offers an archived session the Archived filter lists", () => {
    history(["a", "archived", "b"]);
    sidebar(["archived"], "a", ["a", "b"]);
    openRecentSessionSwitcher(1, { holding: true, navigate });
    expect(switcher()?.order).toEqual(["a", "b"]);
  });

  it("passes over a session deleted or archived while it is open", () => {
    history(["a", "b", "c", "d"]);
    openRecentSessionSwitcher(1, { holding: true, navigate });
    sidebar(["a", "b", "d"], "a");
    openRecentSessionSwitcher(1, { holding: true, navigate });
    expect(selected()).toBe("d");
  });

  it("persists the history across reloads", () => {
    openRecentSessionSwitcher(1, { holding: true, navigate });
    commitRecentSessionSwitcher();
    expect(JSON.parse(localStorage.getItem("omnigent:recent-session-history")!)).toEqual([
      "b",
      "a",
      "c",
    ]);
  });
});

describe("useRecordRecentSessionVisits", () => {
  it("records each session the user opens", () => {
    history([]);
    const { rerender } = renderHook(
      ({ activeId }: { activeId: string | undefined }) => useRecordRecentSessionVisits(activeId),
      { initialProps: { activeId: "a" as string | undefined } },
    );
    rerender({ activeId: "b" });
    rerender({ activeId: undefined });
    expect(useRecentSessionSwitchStore.getState().history).toEqual(["b", "a"]);
  });
});
