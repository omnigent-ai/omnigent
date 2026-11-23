import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { writeSubmitWithModEnter } from "@/lib/composerSendShortcutPreferences";
import type { TerminalSplitState } from "@/lib/paneSplitLayout";
import { TERMINAL_SURFACE_ATTRIBUTE } from "@/lib/paneSurfaces";
import { paneShortcutChords } from "@/shell/paneShortcuts";
import type { PaneCommands } from "@/shell/terminalPaneActions";
import { toggleZoomedPane, zoomedPaneKey } from "@/shell/terminalPaneZoom";
import type { SplitTarget } from "@/shell/terminalSplitTarget";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

import {
  PANE_COMMANDS_HEADING,
  resetPanePaletteOwnerForTests,
  usePaneCommandsListed,
  useTerminalPaneHotkeys,
} from "./useTerminalPaneHotkeys";

const SPLIT: TerminalSplitState = {
  layout: { axis: "columns", ratio: 0.5, children: ["a", "b"] },
  focusedKey: "a",
};

let nextTarget = 0;

function memoryTarget(initial: TerminalSplitState | undefined = SPLIT) {
  let state: TerminalSplitState | undefined = initial;
  const target: SplitTarget = {
    id: `keys-${++nextTarget}`,
    read: () => state,
    update: (update) => {
      state = update(state);
    },
  };
  return { target, read: () => state };
}

function commandsDouble(overrides: Partial<PaneCommands> = {}): PaneCommands {
  return {
    canSplit: true,
    splitting: false,
    closingSplit: false,
    splitPane: vi.fn(),
    canClosePane: (key) => key !== "a",
    closePane: vi.fn(),
    ...overrides,
  };
}

/** Panes a and b, each with a terminal, a chat pane body in a, and a field outside the panes. */
function mountPanes() {
  const panes = document.createElement("div");
  panes.innerHTML = `
    <div data-terminal-pane="a">
      <div ${TERMINAL_SURFACE_ATTRIBUTE}><textarea data-testid="terminal-a"></textarea></div>
      <div data-chat-pane-body="session-a"><textarea data-testid="composer"></textarea></div>
    </div>
    <div data-terminal-pane="b">
      <div ${TERMINAL_SURFACE_ATTRIBUTE}><textarea data-testid="terminal-b"></textarea></div>
      <div data-testid="editor" contenteditable="true"></div>
    </div>
    <div data-chat-pane-body="session-b"><textarea data-testid="other-composer"></textarea></div>`;
  const outside = document.createElement("input");
  document.body.append(panes, outside);
  const byTestId = (id: string) => panes.querySelector<HTMLElement>(`[data-testid='${id}']`)!;
  return { panes, outside, byTestId };
}

function setup({
  lone = false,
  split: initialSplit = SPLIT,
  actions = commandsDouble(),
  chatPane = null as { key: string; sessionId: string } | null,
  defaultForPalette = true,
} = {}) {
  const split = lone ? undefined : initialSplit;
  const dom = mountPanes();
  const memory = memoryTarget(split);
  const panesRef = { current: dom.panes };
  const hook = renderHook(
    (props: { split: TerminalSplitState | undefined }) =>
      useTerminalPaneHotkeys({
        target: memory.target,
        enabled: true,
        split: props.split,
        singlePaneKey: "a",
        chatPane,
        defaultForPalette,
        actions,
        panesRef,
      }),
    { initialProps: { split } },
  );
  return { ...dom, ...memory, actions, hook };
}

/** Presses pane key chord `chord` in `element`; returns whether a pane claimed it. */
function press(
  element: HTMLElement,
  init: KeyboardEventInit & { key: string },
): { claimed: boolean } {
  element.focus();
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  act(() => void element.dispatchEvent(event));
  return { claimed: event.defaultPrevented };
}

const SPLIT_RIGHT = { metaKey: true, code: "KeyD", key: "d" };
const MAXIMIZE = { metaKey: true, shiftKey: true, key: "Enter" };

const paneRows = () =>
  Object.values(usePaletteCommandsStore.getState().groups).filter(
    (group) => group.heading === PANE_COMMANDS_HEADING,
  );

beforeEach(() => {
  vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
});
afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  document.body.replaceChildren();
  resetPanePaletteOwnerForTests();
});

describe("useTerminalPaneHotkeys", () => {
  it("splits the pane whose terminal has the keyboard, claiming the chord from its shell", () => {
    const { byTestId, actions } = setup();
    expect(press(byTestId("terminal-b"), SPLIT_RIGHT).claimed).toBe(true);
    expect(actions.splitPane).toHaveBeenCalledExactlyOnceWith("b", "columns");
  });

  it("leaves the chord to fields, editors and everything outside the panes", () => {
    const { byTestId, outside, actions } = setup();
    expect(press(byTestId("editor"), SPLIT_RIGHT).claimed).toBe(false);
    expect(press(outside, SPLIT_RIGHT).claimed).toBe(false);
    // The composer keeps its keys while the chat isn't a pane.
    expect(press(byTestId("composer"), SPLIT_RIGHT).claimed).toBe(false);
    expect(actions.splitPane).not.toHaveBeenCalled();
  });

  it("acts on the chat pane from its composer, for the keys the chat lends", () => {
    const { byTestId, actions } = setup({ chatPane: { key: "a", sessionId: "session-a" } });
    expect(press(byTestId("composer"), SPLIT_RIGHT).claimed).toBe(true);
    expect(actions.splitPane).toHaveBeenCalledWith("a", "columns");
    // Another session's chat, as in a grid, keeps its keys for its own panes.
    expect(press(byTestId("other-composer"), SPLIT_RIGHT).claimed).toBe(false);
    expect(actions.splitPane).toHaveBeenCalledTimes(1);
    // Divider keys stay the composer's.
    expect(
      press(byTestId("composer"), { metaKey: true, ctrlKey: true, key: "ArrowRight" }),
    ).toEqual({ claimed: false });
  });

  it("leaves ⇧⌘↩ to the composer where it steers the queue, as it does once ⌘↩ sends", () => {
    const { byTestId, target } = setup({ chatPane: { key: "a", sessionId: "session-a" } });
    expect(press(byTestId("composer"), MAXIMIZE).claimed).toBe(true);
    expect(zoomedPaneKey(target)).toBe("a");
    act(() => toggleZoomedPane(target));
    writeSubmitWithModEnter(true);
    expect(press(byTestId("composer"), MAXIMIZE).claimed).toBe(false);
    expect(zoomedPaneKey(target)).toBeNull();
    // A terminal pane still maximizes with it.
    expect(press(byTestId("terminal-b"), MAXIMIZE).claimed).toBe(true);
  });

  it("claims a split key while a shell starts, but opens nothing more", () => {
    const { byTestId, actions } = setup({ actions: commandsDouble({ splitting: true }) });
    expect(press(byTestId("terminal-a"), SPLIT_RIGHT).claimed).toBe(true);
    expect(actions.splitPane).not.toHaveBeenCalled();
  });

  it("maximizes a split's pane, and leaves ⇧⌘↩ to a lone pane's terminal", () => {
    const split = setup();
    expect(press(split.byTestId("terminal-b"), MAXIMIZE).claimed).toBe(true);
    expect(zoomedPaneKey(split.target)).toBe("b");
    document.body.replaceChildren();

    const lone = setup({ lone: true });
    expect(press(lone.byTestId("terminal-a"), MAXIMIZE).claimed).toBe(false);
  });

  it("moves the keyboard to the pane beside, and equalizes the panes", () => {
    const { byTestId, read, target } = setup({
      split: { focusedKey: "a", layout: { axis: "columns", ratio: 0.7, children: ["a", "b"] } },
    });
    press(byTestId("terminal-a"), { metaKey: true, altKey: true, key: "ArrowRight" });
    expect(read()?.focusedKey).toBe("b");
    press(byTestId("terminal-b"), { metaKey: true, ctrlKey: true, code: "Equal", key: "=" });
    expect(target.read()?.layout).toEqual(SPLIT.layout);
  });

  it("lists ⌘K's pane commands with their keys, acting on the split's focused pane", () => {
    const { actions } = setup();
    const [group] = paneRows();
    const row = (label: string) => group!.commands.find((command) => command.label === label);
    expect(row("Split Right")?.nativeShortcut).toEqual(
      paneShortcutChords("terminal.splitRight")[0],
    );
    // a is focused and is the agent's, which can't close.
    expect(row("Close Pane")?.availability).toEqual({ available: false });
    act(() => row("Split Right")!.run());
    expect(actions.splitPane).toHaveBeenCalledWith("a", "columns");
    expect(row("Swap with Pane Right")?.availability).toEqual({ available: true });
    expect(row("Move Pane…")).toBeDefined();
  });

  it("lists only the commands of the view whose panes last had the keyboard", () => {
    const main = setup();
    const sidebar = setup({ defaultForPalette: false });
    expect(paneRows()).toHaveLength(1);
    act(() => sidebar.byTestId("terminal-b").focus());
    const [group] = paneRows();
    expect(paneRows()).toHaveLength(1);
    expect(group!.commands[0]!.id).toContain(sidebar.target.id);
    expect(main.target.id).not.toBe(sidebar.target.id);
  });

  it("gives each pane's row its swaps, Move Pane and Close, through the same actions", () => {
    const { hook, actions, read } = setup();
    const menu = hook.result.current("b");
    expect(menu?.swaps?.map((choice) => choice.label)).toEqual(["Swap with Pane Left"]);
    expect(menu?.moveOptions?.().length).toBeGreaterThan(0);
    act(() => menu!.close!());
    expect(actions.closePane).toHaveBeenCalledWith("b");
    // The agent's pane has no Close, and a lone pane no menu commands.
    expect(hook.result.current("a")?.close).toBeUndefined();
    act(() => menu!.swaps![0]!.run());
    expect(read()?.layout).toEqual({ axis: "columns", ratio: 0.5, children: ["b", "a"] });
    hook.rerender({ split: undefined });
    expect(hook.result.current("a")).toBeNull();
  });
});

describe("usePaneCommandsListed", () => {
  it("lists only the default view's commands before any panes have had the keyboard", () => {
    expect(renderHook(() => usePaneCommandsListed("main", true, true)).result.current).toBe(true);
    expect(renderHook(() => usePaneCommandsListed("side", true, false)).result.current).toBe(false);
  });

  it("lists nothing for a view that's turned off, even the default one", () => {
    expect(renderHook(() => usePaneCommandsListed("main", false, true)).result.current).toBe(false);
  });
});
