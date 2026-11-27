// Every pane's strip row: its name always, actions on the active and hovered pane, its menu and
// keyboard, and no remount of the terminal under it.

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within as inside,
} from "@testing-library/react";
import { useEffect, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { TerminalSplitState } from "@/lib/paneSplitLayout";
import { PANE_ZOOM_LABELS } from "@/shell/paneCommandTable";
import { SplitPanesLayout, WINDOW_TOP_ROW_HEIGHT } from "@/shell/SplitPanesLayout";

import { PANE_ROW_PX } from "./paneGeometry";
import { HOVER_GRACE_MS } from "./paneHover";
import { focusPaneToolbar } from "./paneToolbarFocus";
import type { PaneStripCommands } from "./PaneTitleStrip";
import type { PaneStripSource } from "./usePaneStrips";

const SPLIT: TerminalSplitState = {
  layout: { axis: "columns", ratio: 0.5, children: ["a", "b"] },
  focusedKey: "a",
};

const mounts = new Map<string, number>();
function Pane({ paneKey }: { paneKey: string }) {
  const counted = useRef(false);
  useEffect(() => {
    if (counted.current) return;
    counted.current = true;
    mounts.set(paneKey, (mounts.get(paneKey) ?? 0) + 1);
  }, [paneKey]);
  return (
    <div className="relative flex-1" data-testid={`content-${paneKey}`}>
      <textarea data-testid={`input-${paneKey}`} />
    </div>
  );
}

interface Next {
  split?: TerminalSplitState;
  zoomedKey?: string | null;
  alsoMounted?: string[];
}

function setup({
  single = false,
  zoomedKey = null as string | null,
  withStrip = true,
  paneOf = null as PaneStripSource["paneOf"] | null,
  bodyPaneKey = null as string | null,
} = {}) {
  const commands = new Map<string, PaneStripCommands>();
  const focus = new Map<string, () => void>();
  for (const key of ["a", "b"]) {
    commands.set(key, {
      splitRight: vi.fn(),
      splitDown: vi.fn(),
      toggleZoom: vi.fn(),
      close: vi.fn(),
    });
    focus.set(key, vi.fn());
  }
  const strip: PaneStripSource = {
    paneOf:
      paneOf ??
      ((key) => ({
        name: `shell ${key}`,
        commands: commands.get(key) ?? null,
        focus: focus.get(key),
      })),
  };
  const panesRef = { current: null as HTMLDivElement | null };
  const onUnzoom = vi.fn();
  const refocusPane = vi.fn();
  const onFocusPane = vi.fn();
  const view = (next: Next) => (
    <TooltipProvider>
      <SplitPanesLayout
        conversationId="c1"
        split={next.split}
        singlePaneKey="a"
        alsoMounted={next.alsoMounted}
        paneIdOf={(key) => key}
        renderPane={(key) => <Pane paneKey={key} />}
        onResize={() => {}}
        panesRef={panesRef}
        zoomedKey={next.zoomedKey ?? null}
        onUnzoom={onUnzoom}
        refocusPane={refocusPane}
        onFocusPane={onFocusPane}
        strip={withStrip ? strip : undefined}
        bodySlot={bodyPaneKey === null ? undefined : { paneKey: bodyPaneKey, ref: () => {} }}
      />
    </TooltipProvider>
  );
  const rendered = render(view({ split: single ? undefined : SPLIT, zoomedKey }));
  return {
    commands,
    focus,
    onUnzoom,
    refocusPane,
    onFocusPane,
    rerender: (next: Next) => rendered.rerender(view(next)),
  };
}

const shownStrip = (key: string) =>
  document.querySelector(`[data-pane-strip="${key}"][data-pane-strip-shown]`);
const row = (key: string) => document.querySelector<HTMLElement>(`[data-pane-strip="${key}"]`)!;
const buttonsOf = (key: string) =>
  [...row(key).querySelectorAll("button")].map((button) => button.getAttribute("data-testid"));

/** Every pane's width: the mocked view's 800 px, shared by a split's two panes. */
function viewWidth(width: number) {
  vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockReturnValue({
    left: 0,
    // Below the window's title bar, away from its top-left corner.
    top: 48,
    right: width,
    bottom: 600,
    width,
    height: 600,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect);
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  mounts.clear();
  // The split is 800 px wide, so each pane is 400.
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0,
    // Below the window's title bar, away from its top-left corner.
    top: 48,
    right: 800,
    bottom: 600,
    width: 800,
    height: 600,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  // A title bar a failed case left would put every later case's rows under it.
  for (const strip of document.querySelectorAll(".electron-drag-strip")) strip.remove();
});

describe("SplitPanesLayout: every pane's strip row", () => {
  it("names every pane always, and shows actions on the active and the hovered pane only", () => {
    setup();
    expect(row("a").textContent).toContain("shell a");
    expect(row("b").textContent).toContain("shell b");
    expect(shownStrip("a")).not.toBeNull();
    expect(shownStrip("b")).toBeNull();
    expect(buttonsOf("b")).toEqual([]);
    fireEvent.pointerMove(screen.getByTestId("content-b"));
    expect(shownStrip("a")).not.toBeNull();
    expect(shownStrip("b")).not.toBeNull();
    fireEvent.pointerMove(document.body);
    act(() => vi.advanceTimersByTime(HOVER_GRACE_MS));
    expect(shownStrip("b")).toBeNull();
    expect(shownStrip("a")).not.toBeNull();
  });

  it("keeps the hovered pane's actions while the user types in it", () => {
    setup();
    fireEvent.pointerMove(screen.getByTestId("content-b"));
    fireEvent.keyDown(screen.getByTestId("input-b"), { key: "l" });
    expect(shownStrip("b")).not.toBeNull();
  });

  it("runs the hovered pane's own commands, though the keyboard is in the other", () => {
    const { commands } = setup();
    fireEvent.pointerMove(screen.getByTestId("content-b"));
    const within = row("b");
    for (const id of ["split-right", "split-down", "maximize", "close"])
      fireEvent.click(within.querySelector(`[data-testid="pane-strip-${id}"]`)!);
    const b = commands.get("b")!;
    expect(b.splitRight).toHaveBeenCalledTimes(1);
    expect(b.splitDown).toHaveBeenCalledTimes(1);
    expect(b.toggleZoom).toHaveBeenCalledTimes(1);
    expect(b.close).toHaveBeenCalledTimes(1);
    expect(commands.get("a")!.splitRight).not.toHaveBeenCalled();
  });

  it("orders the actions Split Right, Split Down, Maximize, Close, with no ⋯", () => {
    setup();
    expect(buttonsOf("a")).toEqual([
      "pane-strip-split-right",
      "pane-strip-split-down",
      "pane-strip-maximize",
      "pane-strip-close",
    ]);
    expect(document.querySelector("[data-testid=pane-strip-more]")).toBeNull();
  });

  it("gives an app pane Move to Sidebar before Close, which names the tab it closes", () => {
    const moveToSidebar = vi.fn();
    const close = vi.fn();
    setup({
      paneOf: (key) => ({
        name: `tab ${key}`,
        icon: <span data-testid={`icon-${key}`} />,
        closeLabel: "Close Tab",
        commands: { toggleZoom: vi.fn(), moveToSidebar, close },
      }),
    });
    expect(buttonsOf("a")).toEqual([
      "pane-strip-maximize",
      "pane-strip-move-to-sidebar",
      "pane-strip-close",
    ]);
    // The row shows the tab's own icon, not a terminal's.
    expect(row("a").querySelector("[data-testid=icon-a]")).not.toBeNull();
    const button = (testId: string) =>
      row("a").querySelector<HTMLButtonElement>(`[data-testid=${testId}]`)!;
    expect(button("pane-strip-close").getAttribute("aria-label")).toBe("Close Tab");
    expect(button("pane-strip-move-to-sidebar").getAttribute("aria-label")).toBe("Move to Sidebar");
    // Not the sidebar toggle's panel icon, which a pane at the top-right corner shows beside it.
    expect(button("pane-strip-move-to-sidebar").querySelector("svg")).toHaveClass(
      "lucide-arrow-right-to-line",
    );
    fireEvent.click(button("pane-strip-move-to-sidebar"));
    expect(moveToSidebar).toHaveBeenCalledTimes(1);
    expect(close).not.toHaveBeenCalled();
  });

  it("keeps the keyboard in the terminal when a row button is clicked", () => {
    setup();
    const pressed = fireEvent.mouseDown(
      row("a").querySelector("[data-testid=pane-strip-split-right]")!,
    );
    expect(pressed).toBe(false);
  });

  it("gives the pane's terminal the keyboard on a click in the row's blank space only", () => {
    const { focus } = setup();
    fireEvent.mouseDown(row("b").querySelector("[data-testid=pane-strip-name]")!);
    expect(focus.get("b")).toHaveBeenCalledTimes(1);
    fireEvent.mouseDown(row("a").querySelector("[data-testid=pane-strip-close]")!);
    expect(focus.get("a")).not.toHaveBeenCalled();
    fireEvent.mouseDown(row("b"), { button: 2 });
    expect(focus.get("b")).toHaveBeenCalledTimes(1);
  });

  describe("the keyboard in the row, as Show Pane Toolbar moves it there", () => {
    const button = (key: string, testId: string) =>
      row(key).querySelector<HTMLButtonElement>(`[data-testid=pane-strip-${testId}]`)!;
    const focused = () => document.activeElement?.getAttribute("data-testid");
    // The toolbar moves focus on the arrow's next tick.
    const press = (key: string) => {
      fireEvent.keyDown(document.activeElement!, { key });
      act(() => vi.advanceTimersByTime(0));
    };

    it("gives the pane's first button the keyboard, and the arrows, Home and End move along", () => {
      setup();
      expect(focusPaneToolbar(document, "a")).toBe(true);
      expect(focused()).toBe("pane-strip-split-right");
      press("ArrowRight");
      expect(focused()).toBe("pane-strip-split-down");
      press("End");
      expect(focused()).toBe("pane-strip-close");
      press("ArrowRight");
      expect(focused()).toBe("pane-strip-split-right");
      press("ArrowLeft");
      expect(focused()).toBe("pane-strip-close");
      press("Home");
      expect(focused()).toBe("pane-strip-split-right");
      // One tab stop for the row: the button the arrows left the keyboard on.
      expect(
        [...row("a").querySelectorAll("button")].map((each) => each.getAttribute("tabindex")),
      ).toEqual(["0", "-1", "-1", "-1"]);
    });

    it("gives the keyboard back to the pane on Escape", () => {
      const { focus } = setup();
      focusPaneToolbar(document, "a");
      press("ArrowRight");
      fireEvent.keyDown(document.activeElement!, { key: "Escape" });
      expect(focus.get("a")).toHaveBeenCalledTimes(1);
      expect(focus.get("b")).not.toHaveBeenCalled();
    });

    it("hands the pane the keyboard as a pressed button acts: before a split, after Maximize", () => {
      const calls: string[] = [];
      setup({
        paneOf: (key) => ({
          name: `shell ${key}`,
          commands: {
            splitRight: () => calls.push("split"),
            toggleZoom: () => calls.push("zoom"),
            close: () => calls.push("close"),
          },
          focus: () => calls.push(`focus ${key}`),
        }),
      });
      button("a", "split-right").focus();
      fireEvent.click(button("a", "split-right"));
      button("a", "maximize").focus();
      fireEvent.click(button("a", "maximize"));
      button("a", "close").focus();
      fireEvent.click(button("a", "close"));
      expect(calls).toEqual(["focus a", "split", "zoom", "focus a", "focus a", "close"]);
      // A click, with the keyboard elsewhere, runs the command alone.
      calls.length = 0;
      screen.getByTestId("input-b").focus();
      fireEvent.click(button("a", "maximize"));
      expect(calls).toEqual(["zoom"]);
    });

    it("finds no button in a row with no room for them", () => {
      viewWidth(60);
      setup();
      // The active pane's row, too narrow for any of its actions.
      expect(shownStrip("a")).not.toBeNull();
      expect(buttonsOf("a")).toEqual([]);
      expect(focusPaneToolbar(document, "a")).toBe(false);
      expect(focusPaneToolbar(document, "missing")).toBe(false);
    });
  });

  describe("the row's menu and double-click", () => {
    const menuItems = () =>
      screen.getAllByRole("menuitem").map((item) => item.textContent?.replace(/\s+/g, " ").trim());
    const openMenu = (key: string) =>
      fireEvent.contextMenu(row(key), { clientX: 120, clientY: 10 });

    it("offers the pane's actions with their shortcuts on a right-click, though the row hides them", () => {
      // Too narrow for any button: the menu still has every action.
      viewWidth(200);
      const { commands } = setup();
      expect(buttonsOf("b")).toEqual([]);
      openMenu("b");
      const menu = screen.getByRole("menu");
      expect(menu).toHaveAttribute("data-pane-strip-menu");
      const labels = ["Split Right", "Split Down", PANE_ZOOM_LABELS.maximize, "Close Pane"];
      const items = menuItems();
      expect(items).toHaveLength(labels.length);
      // Each label, then the pane key ⌘K shows for it: Maximize has one on every platform.
      labels.forEach((label, index) => expect(items[index]?.startsWith(label)).toBe(true));
      expect(items[2]).not.toBe(PANE_ZOOM_LABELS.maximize);
      expect(inside(menu).getByRole("separator")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("menuitem", { name: /^Split Down/ }));
      expect(commands.get("b")!.splitDown).toHaveBeenCalledTimes(1);
      expect(commands.get("a")!.splitDown).not.toHaveBeenCalled();
    });

    it("gives an app pane's menu Move to Sidebar, and Close named for its tab", () => {
      const moveToSidebar = vi.fn();
      setup({
        paneOf: (key) => ({
          name: `tab ${key}`,
          closeLabel: "Close Tab",
          commands: { toggleZoom: vi.fn(), moveToSidebar, close: vi.fn() },
        }),
      });
      openMenu("a");
      expect(screen.getByRole("menuitem", { name: /^Close Tab/ })).toBeInTheDocument();
      fireEvent.click(screen.getByRole("menuitem", { name: "Move to Sidebar" }));
      expect(moveToSidebar).toHaveBeenCalledTimes(1);
    });

    it("lists Move Pane and the swaps a pane has between its sizing actions and Close", async () => {
      const swap = vi.fn();
      const focus = vi.fn();
      setup({
        paneOf: (key) => ({
          name: `shell ${key}`,
          focus,
          commands: {
            splitRight: vi.fn(),
            toggleZoom: vi.fn(),
            close: vi.fn(),
            swaps: [{ id: "terminal.swapLeft", label: "Swap with Pane Left", run: swap }],
            moveOptions: () => [],
          },
        }),
      });
      openMenu("b");
      const labels = [
        "Split Right",
        PANE_ZOOM_LABELS.maximize,
        "Move Pane",
        "Swap with Pane Left",
        "Close Pane",
      ];
      const items = menuItems();
      expect(items).toHaveLength(labels.length);
      labels.forEach((label, index) => expect(items[index]?.startsWith(label)).toBe(true));
      expect(inside(screen.getByRole("menu")).getAllByRole("separator")).toHaveLength(2);
      fireEvent.click(screen.getByRole("menuitem", { name: /^Swap with Pane Left/ }));
      expect(swap).toHaveBeenCalledTimes(1);
      // The pane that moved keeps the keyboard.
      await waitFor(() => expect(focus).toHaveBeenCalledTimes(1));
    });

    it("opens Move Pane's spots as a submenu, runs the one chosen, and says when none has room", async () => {
      const move = vi.fn();
      const spots = { list: [{ id: "b>a:left", label: "Left of shell a", run: move }] };
      setup({
        paneOf: (key) => ({
          name: `shell ${key}`,
          commands: { toggleZoom: vi.fn(), moveOptions: () => spots.list },
        }),
      });
      const openSpots = async () => {
        openMenu("b");
        const trigger = screen.getByRole("menuitem", { name: /^Move Pane/ });
        trigger.focus();
        fireEvent.keyDown(trigger, { key: "ArrowRight" });
        return waitFor(() => screen.getByRole("menuitem", { name: /^Left of shell a/ }));
      };
      fireEvent.click(await openSpots());
      expect(move).toHaveBeenCalledTimes(1);
      await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());

      spots.list = [];
      openMenu("b");
      const trigger = screen.getByRole("menuitem", { name: /^Move Pane/ });
      trigger.focus();
      fireEvent.keyDown(trigger, { key: "ArrowRight" });
      const empty = await waitFor(() =>
        screen.getByRole("menuitem", { name: "Not enough room to move this pane." }),
      );
      expect(empty).toHaveAttribute("aria-disabled", "true");
    });

    it("offers Restore for a maximized pane, as its row's Restore runs", () => {
      const { onUnzoom } = setup({ zoomedKey: "a" });
      openMenu("a");
      fireEvent.click(
        screen.getByRole("menuitem", { name: new RegExp(`^${PANE_ZOOM_LABELS.restore}`) }),
      );
      expect(onUnzoom).toHaveBeenCalledWith("a");
      expect(screen.queryByRole("menuitem", { name: /^Split/ })).toBeNull();
    });

    it("hands the keyboard back to the terminal when the menu closes without a command", async () => {
      setup();
      const terminal = screen.getByTestId("input-a");
      terminal.focus();
      openMenu("a");
      expect(document.activeElement).not.toBe(terminal);
      fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
      await waitFor(() => expect(document.activeElement).toBe(terminal));
    });

    afterEach(() => {
      for (const stray of document.querySelectorAll("body > textarea")) stray.remove();
    });
    const settled = () =>
      new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
    // A pane's terminal: its strip's focus gives it the keyboard.
    const terminalOf = (focus: Map<string, () => void>, key: string) => {
      const terminal = document.body.appendChild(document.createElement("textarea"));
      vi.mocked(focus.get(key)!).mockImplementation(() => terminal.focus());
      return terminal;
    };
    // The fading menu's item takes focus back, as a pointer resting on it does.
    const fadingMenuTakesFocus = () => screen.getAllByRole("menuitem")[0]!.focus();

    it("gives the keyboard to the pane Maximize acts on as the menu closes, not back to where it was", async () => {
      const { commands, focus } = setup();
      const composer = document.body.appendChild(document.createElement("textarea"));
      const terminalB = terminalOf(focus, "b");
      vi.mocked(commands.get("b")!.toggleZoom!).mockImplementation(fadingMenuTakesFocus);
      composer.focus();
      openMenu("b");
      fireEvent.click(
        screen.getByRole("menuitem", { name: new RegExp(`^${PANE_ZOOM_LABELS.maximize}`) }),
      );
      expect(commands.get("b")!.toggleZoom).toHaveBeenCalledTimes(1);
      await waitFor(() => expect(document.activeElement).toBe(terminalB));
      await settled();
      expect(document.activeElement).toBe(terminalB);
    });

    it("chooses the pane Split acts on first, then gives the split's chosen pane the keyboard", async () => {
      const { commands, focus, refocusPane } = setup();
      const composer = document.body.appendChild(document.createElement("textarea"));
      const newPane = document.body.appendChild(document.createElement("textarea"));
      const order: string[] = [];
      vi.mocked(focus.get("b")!).mockImplementation(() => void order.push("choose b"));
      vi.mocked(commands.get("b")!.splitDown!).mockImplementation(() => {
        order.push("split");
        fadingMenuTakesFocus();
      });
      refocusPane.mockImplementation((key: string) => {
        order.push(`refocus ${key}`);
        newPane.focus();
      });
      composer.focus();
      openMenu("b");
      fireEvent.click(screen.getByRole("menuitem", { name: /^Split Down/ }));
      await waitFor(() => expect(document.activeElement).toBe(newPane));
      expect(order).toEqual(["choose b", "split", "refocus b"]);
      await settled();
      expect(document.activeElement).toBe(newPane);
    });

    it("leaves the split's chosen pane alone as focus moves into a pane's menu", () => {
      const { onFocusPane } = setup();
      fireEvent.focus(screen.getByTestId("input-a"));
      expect(onFocusPane).toHaveBeenLastCalledWith("a");
      onFocusPane.mockClear();
      openMenu("b");
      fadingMenuTakesFocus();
      expect(onFocusPane).not.toHaveBeenCalled();
    });

    it("keeps another pane's menu opened straight from this one; Escape gives that pane the keyboard", async () => {
      const { focus } = setup();
      const terminalA = terminalOf(focus, "a");
      const terminalB = terminalOf(focus, "b");
      terminalA.focus();
      openMenu("a");
      const first = screen.getByRole("menu");
      openMenu("b");
      await waitFor(() => expect(first).not.toBeInTheDocument());
      await settled();
      const second = screen.getByRole("menu");
      expect(second.contains(document.activeElement)).toBe(true);
      fireEvent.keyDown(second, { key: "Escape" });
      await waitFor(() => expect(document.activeElement).toBe(terminalB));
    });

    // Shell a's menu, then pane b's straight from it, past the first one's fade.
    const switchFromMenuAToMenuB = async () => {
      openMenu("a");
      const first = screen.getByRole("menu");
      openMenu("b");
      await waitFor(() => expect(first).not.toBeInTheDocument());
      await settled();
      return screen.getByRole("menu");
    };

    it("leaves the keyboard where a click put it, as a menu opened from another closes", async () => {
      const { focus } = setup();
      const terminalA = terminalOf(focus, "a");
      terminalOf(focus, "b");
      const composer = document.body.appendChild(document.createElement("textarea"));
      terminalA.focus();
      await switchFromMenuAToMenuB();
      fireEvent.pointerDown(composer);
      composer.focus();
      await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
      await settled();
      expect(document.activeElement).toBe(composer);
      expect(focus.get("b")).not.toHaveBeenCalled();
    });

    it("keeps a third menu opened straight from the second, as the second closes", async () => {
      const { focus } = setup();
      const terminalA = terminalOf(focus, "a");
      terminalOf(focus, "b");
      terminalA.focus();
      const second = await switchFromMenuAToMenuB();
      openMenu("a");
      await waitFor(() => expect(second).not.toBeInTheDocument());
      await settled();
      const third = screen.getByRole("menu");
      expect(third.contains(document.activeElement)).toBe(true);
      expect(screen.getByRole("menuitem", { name: /^Close Pane/ })).toBeInTheDocument();
      expect(focus.get("b")).not.toHaveBeenCalled();
    });

    it("gives the keyboard to the pane a command acts on in a menu opened from another", async () => {
      const { commands, focus } = setup();
      const terminalA = terminalOf(focus, "a");
      const terminalB = terminalOf(focus, "b");
      vi.mocked(commands.get("b")!.toggleZoom!).mockImplementation(fadingMenuTakesFocus);
      terminalA.focus();
      openMenu("a");
      const first = screen.getByRole("menu");
      openMenu("b");
      await waitFor(() => expect(first).not.toBeInTheDocument());
      await settled();
      fireEvent.click(
        screen.getByRole("menuitem", { name: new RegExp(`^${PANE_ZOOM_LABELS.maximize}`) }),
      );
      await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
      await waitFor(() => expect(document.activeElement).toBe(terminalB));
      await settled();
      expect(document.activeElement).toBe(terminalB);
    });

    it("maximizes or restores on a double-click in the row's own space, not on its controls", () => {
      const { commands, focus } = setup();
      fireEvent.doubleClick(row("b").querySelector("[data-testid=pane-strip-name]")!);
      expect(commands.get("b")!.toggleZoom).toHaveBeenCalledTimes(1);
      // The pane keeps the keyboard as its size changes.
      expect(focus.get("b")).toHaveBeenCalledTimes(1);
      // The active pane shows its buttons: a double-click on one is that button's, not the row's.
      fireEvent.doubleClick(row("a").querySelector("[data-testid=pane-strip-close]")!);
      expect(commands.get("a")!.toggleZoom).not.toHaveBeenCalled();
      expect(row("b").hasAttribute("data-pane-strip-zoomable")).toBe(true);
    });

    it("keeps the keyboard where it is on a right-press or Ctrl-press, for the menu to hand back", () => {
      const { focus } = setup();
      // Not prevented, the press would move the keyboard to the page before the menu opens.
      expect(fireEvent.mouseDown(row("b"), { button: 0, ctrlKey: true })).toBe(false);
      expect(fireEvent.mouseDown(row("b"), { button: 2 })).toBe(false);
      expect(focus.get("b")).not.toHaveBeenCalled();
    });
  });

  it("lets actions give way as a pane narrows, keeping its name", () => {
    // Two 170 px panes: 82 px of icon and title, room for three 24 px buttons.
    viewWidth(340);
    setup();
    expect(buttonsOf("a")).toEqual([
      "pane-strip-split-right",
      "pane-strip-split-down",
      "pane-strip-maximize",
    ]);
    cleanup();
    viewWidth(200);
    setup();
    expect(buttonsOf("a")).toEqual([]);
    expect(row("a").querySelector("[data-testid=pane-strip-name]")?.className).not.toContain(
      "sr-only",
    );
  });

  it("lays out a split's panes as square tiles side by side, each row at its tile's width", () => {
    // Two 202 px tiles fit all four actions, as a lone 202 px pane does.
    viewWidth(404);
    setup();
    expect(buttonsOf("a")).toEqual([
      "pane-strip-split-right",
      "pane-strip-split-down",
      "pane-strip-maximize",
      "pane-strip-close",
    ]);
    // No card around a tile or its row; only the divider between them.
    for (const key of ["a", "b"]) {
      const tile = document.querySelector<HTMLElement>(`[data-terminal-pane="${key}"]`)!;
      const surface = row(key).parentElement!;
      for (const element of [tile, surface]) {
        expect(element.className).not.toMatch(/\brounded|\bp-0\.5\b|\bborder\b/);
      }
    }
    expect(document.querySelectorAll("[data-testid=terminal-split-divider]")).toHaveLength(1);
  });

  it("keeps only Restore below the row's floor, the name left for screen readers", () => {
    viewWidth(200);
    setup({ zoomedKey: "b" });
    expect(row("b").hasAttribute("data-pane-strip-compact")).toBe(false);
    cleanup();
    viewWidth(100);
    setup({ zoomedKey: "b" });
    expect(row("b").hasAttribute("data-pane-strip-compact")).toBe(true);
    expect(row("b").querySelector("[data-testid=pane-strip-name]")?.className).toContain("sr-only");
    expect(buttonsOf("b")).toEqual(["pane-strip-restore"]);
  });

  it("shows a maximized pane only Restore and Close, with no label and no pill", () => {
    viewWidth(2000);
    const { onUnzoom } = setup({ zoomedKey: "b" });
    expect(row("b").textContent).not.toContain(PANE_ZOOM_LABELS.maximized);
    expect(document.querySelector("[data-terminal-zoom-pill]")).toBeNull();
    // Splitting a maximized pane would show nothing of it, so the row leaves the splits out.
    expect(buttonsOf("b")).toEqual(["pane-strip-restore", "pane-strip-close"]);
    // The view's own Restore, as the pill's was, which hands the pane the keyboard.
    fireEvent.click(screen.getByTestId("pane-strip-restore"));
    expect(onUnzoom).toHaveBeenCalledWith("b");
  });

  it("names Maximize and Restore with the shared pane zoom labels", () => {
    setup({ zoomedKey: "b" });
    expect(screen.getByTestId("pane-strip-restore").getAttribute("aria-label")).toBe(
      PANE_ZOOM_LABELS.restore,
    );
    // Restore says the pane is maximized, as a pressed toggle.
    expect(screen.getByTestId("pane-strip-restore").getAttribute("aria-pressed")).toBe("true");
    cleanup();
    setup();
    expect(
      row("a").querySelector("[data-testid=pane-strip-maximize]")?.getAttribute("aria-label"),
    ).toBe(PANE_ZOOM_LABELS.maximize);
  });

  it("keeps the old view, with no rows and the Maximized pill, where the pane kind gives none", () => {
    setup({ zoomedKey: "b", withStrip: false });
    expect(document.querySelector("[data-terminal-zoom-pill]")).not.toBeNull();
    expect(document.querySelector("[data-pane-strip]")).toBeNull();
    expect(document.querySelector("[data-pane-card]")).toBeNull();
  });

  it("marks the focused pane of a split with an accent line on its toolbar, and a lone pane not", () => {
    const { rerender } = setup();
    expect(row("a").hasAttribute("data-pane-strip-focused")).toBe(true);
    expect(row("b").hasAttribute("data-pane-strip-focused")).toBe(false);
    rerender({ split: { ...SPLIT, focusedKey: "b" } });
    expect(row("a").hasAttribute("data-pane-strip-focused")).toBe(false);
    expect(row("b").hasAttribute("data-pane-strip-focused")).toBe(true);
    // The line is its own layer, so it can be drawn over the session header's glass.
    expect(row("a").querySelector("[data-pane-strip-focus-line]")).toBeNull();
    expect(row("b").querySelector("[data-pane-strip-focus-line]")).not.toBeNull();
    rerender({ split: SPLIT, zoomedKey: "a" });
    expect(row("a").hasAttribute("data-pane-strip-focused")).toBe(false);
    rerender({ split: undefined });
    expect(row("a").hasAttribute("data-pane-strip-focused")).toBe(false);
    expect(mounts.get("a")).toBe(1);
  });

  it.each([true, false])(
    "keeps a pane's content and state as it hides and shows again (rows %s)",
    (withStrip) => {
      const { rerender } = setup({ withStrip });
      const input = () => document.querySelector<HTMLTextAreaElement>("[data-testid=input-b]")!;
      fireEvent.change(input(), { target: { value: "typed in b" } });
      // a closes to one pane; b stays mounted, hidden, then shows in the split again.
      rerender({ split: undefined, alsoMounted: ["b"] });
      expect(document.querySelector('[data-terminal-pane="b"]')).toHaveAttribute(
        "data-pane-hidden",
      );
      rerender({ split: SPLIT, alsoMounted: ["b"] });
      expect(input().value).toBe("typed in b");
      // Another pane maximized hides b; restoring shows it again.
      rerender({ split: SPLIT, zoomedKey: "a" });
      rerender({ split: { ...SPLIT, focusedKey: "b" } });
      expect(input().value).toBe("typed in b");
      expect(mounts.get("a")).toBe(1);
      expect(mounts.get("b")).toBe(1);
    },
  );

  it("keeps a hidden pane's row, and a kept pane's room for one, as tall as the title bar's", () => {
    const titleBar = document.body.appendChild(document.createElement("div"));
    titleBar.className = "electron-drag-strip";
    // The view starts under the title bar, edge to edge.
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockReturnValue({
      left: 0,
      top: 0,
      right: 800,
      bottom: 600,
      width: 800,
      height: 600,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);
    const { rerender } = setup();
    const tall = (key: string) => row(key).hasAttribute("data-pane-strip-title-bar-row");
    expect(tall("a") && tall("b")).toBe(true);
    // Behind a maximized pane, b's row keeps its height, so its terminal keeps its size.
    rerender({ split: SPLIT, zoomedKey: "a" });
    expect(row("b").hasAttribute("data-pane-strip-window-top")).toBe(false);
    expect(tall("b")).toBe(true);
    // Kept mounted off the split, b reserves the row it has when it shows alone.
    rerender({ split: undefined, alsoMounted: ["b"] });
    const kept = document.querySelector<HTMLElement>('[data-terminal-pane="b"]')!;
    expect(kept.style.paddingTop).toContain(WINDOW_TOP_ROW_HEIGHT);
    titleBar.remove();
  });

  it("reserves a plain row for a kept pane below the title bar", () => {
    const { rerender } = setup();
    rerender({ split: undefined, alsoMounted: ["b"] });
    const kept = document.querySelector<HTMLElement>('[data-terminal-pane="b"]')!;
    expect(kept.style.paddingTop).toContain(`${PANE_ROW_PX}px`);
    expect(kept.style.paddingTop).not.toContain("--pane-window-top-row");
  });

  it("keeps a row mounted, and what it measures, as its pane's commands appear", () => {
    let ready = false;
    const commands: PaneStripCommands = { splitRight: vi.fn(), toggleZoom: vi.fn() };
    const { rerender } = setup({
      single: true,
      paneOf: () => ({ name: "zsh", commands: ready ? commands : null }),
    });
    const before = row("a");
    // The shell finishes starting, and its commands appear.
    ready = true;
    rerender({});
    expect(row("a")).toBe(before);
    expect(buttonsOf("a")).toContain("pane-strip-split-right");
  });

  it("shows only the name of a pane with no commands", () => {
    setup({ single: true, paneOf: () => ({ name: "read-only", commands: null }) });
    expect(row("a").textContent).toContain("read-only");
    expect(buttonsOf("a")).toEqual([]);
  });

  it("names a lone shell with its session and status, and Show Chat goes back", () => {
    const showChat = vi.fn();
    setup({
      single: true,
      paneOf: () => ({
        name: "zsh",
        detail: "s2",
        status: <span data-testid="status">Running</span>,
        showChat,
        commands: null,
      }),
    });
    expect(row("a").textContent).toContain("zsh · s2");
    expect(screen.getByTestId("status")).toBeTruthy();
    expect(buttonsOf("a")).toEqual(["pane-strip-show-chat"]);
    fireEvent.click(screen.getByTestId("pane-strip-show-chat"));
    expect(showChat).toHaveBeenCalledTimes(1);
  });

  describe("at the window's top-left corner", () => {
    /** The view at the window's corner, `width` px wide; the controls' room there `inset` px. */
    function atCorner(width: number, inset: number) {
      vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (
        this: HTMLElement,
      ) {
        const own = this.hasAttribute("data-pane-strip-window-inset");
        return {
          left: 0,
          top: 0,
          right: own ? inset : width,
          bottom: 600,
          width: own ? inset : width,
          height: own ? 28 : 600,
          x: 0,
          y: 0,
          toJSON: () => ({}),
        } as DOMRect;
      });
    }
    const insetOf = (key: string) => row(key).querySelector("[data-pane-strip-window-inset]");

    it("starts only the top-left pane's row after the window's controls, and budgets for them", () => {
      atCorner(400, 120);
      setup();
      expect(insetOf("a")).not.toBeNull();
      expect(row("a").firstElementChild).toBe(insetOf("a"));
      expect(insetOf("b")).toBeNull();
      // Each pane is 200 px: the corner's 120 px leave pane a too little for its actions.
      expect(row("a").hasAttribute("data-pane-strip-compact")).toBe(true);
      expect(buttonsOf("b")).toEqual([]);
      fireEvent.pointerMove(screen.getByTestId("content-b"));
      expect(buttonsOf("b")).toEqual([
        "pane-strip-split-right",
        "pane-strip-split-down",
        "pane-strip-maximize",
        "pane-strip-close",
      ]);
    });

    it("moves the room to a maximized pane, which fills the corner", () => {
      atCorner(800, 120);
      setup({ zoomedKey: "b" });
      expect(insetOf("b")).not.toBeNull();
      expect(insetOf("a")).toBeNull();
    });

    it("keeps no room on a view below the title bar", () => {
      setup({ zoomedKey: "a" });
      expect(document.querySelector("[data-pane-strip-window-inset]")).toBeNull();
      expect(document.querySelector("[data-pane-strip-window-top]")).toBeNull();
    });

    const bodyTop = () =>
      document.querySelector<HTMLElement>("[data-chat-pane-body-host]")!.style.top;

    it("marks the rows along the window's top, and a maximized pane's, and starts the chat below them", () => {
      const strip = document.body.appendChild(document.createElement("div"));
      strip.className = "electron-drag-strip";
      atCorner(800, 120);
      setup({ bodyPaneKey: "a" });
      for (const key of ["a", "b"]) {
        expect(row(key).hasAttribute("data-pane-strip-window-top")).toBe(true);
      }
      // The Mac makes these rows as tall as the title bar, so the chat starts below that height.
      expect(bodyTop()).toBe(WINDOW_TOP_ROW_HEIGHT);
      cleanup();
      atCorner(800, 120);
      setup({ zoomedKey: "b", bodyPaneKey: "b" });
      expect(row("b").hasAttribute("data-pane-strip-window-top")).toBe(true);
      expect(bodyTop()).toBe(WINDOW_TOP_ROW_HEIGHT);
      strip.remove();
    });

    it("starts the chat one plain row down on a view below the title bar", () => {
      setup({ bodyPaneKey: "a" });
      expect(row("a").hasAttribute("data-pane-strip-window-top")).toBe(false);
      expect(bodyTop()).toBe("28px");
    });
  });

  it("draws the row and pane in the terminal's own colors", () => {
    setup();
    const card = row("a").parentElement!;
    expect(card.style.getPropertyValue("--background")).not.toBe("");
    expect(card.style.getPropertyValue("--muted-foreground")).not.toBe("");
    expect(card.style.getPropertyValue("--border")).not.toBe("");
  });
});
