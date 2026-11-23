// Dragging a split's pane by its strip row: a press past the threshold drags, the half it would
// take shows in the accent, a drop there moves it once however long the drag took, and every
// other ending moves nothing and gives the frames the pointer back.

import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { TerminalSplitState } from "@/lib/paneSplitLayout";
import type * as PaneDividerDragModule from "@/shell/paneDividerDrag";
import { createPaneDividerDrag } from "@/shell/paneDividerDrag";
import { SplitPanesLayout } from "@/shell/SplitPanesLayout";

import { resetAppPanes, setPendingAppPaneDrop } from "../appPanes";
import { PANE_DRAG_THRESHOLD_PX, type PaneDragOutside } from "./usePaneMoveDrag";

const SPLIT: TerminalSplitState = {
  layout: { axis: "columns", ratio: 0.5, children: ["a", "b"] },
  focusedKey: "a",
};

// The view is 800 × 600 px from the window's origin: a is x 0–400, b is x 400–800.
const VIEW = { left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600 };

const dividerDrag = createPaneDividerDrag();
vi.mock("@/shell/paneDividerDrag", async (importOriginal) => {
  const actual = await importOriginal<typeof PaneDividerDragModule>();
  return {
    ...actual,
    get paneDividerDrag() {
      return dividerDrag;
    },
  };
});

function setup({
  single = false,
  zoomedKey = null as string | null,
  movable = true,
  outside = undefined as PaneDragOutside | undefined,
} = {}) {
  const onMovePane = vi.fn();
  const refocusPane = vi.fn();
  const focus = { a: vi.fn(), b: vi.fn() };
  const panesRef = { current: null as HTMLDivElement | null };
  const view = (next: { split?: TerminalSplitState; zoomedKey?: string | null }) => (
    <TooltipProvider>
      <SplitPanesLayout
        conversationId="c1"
        split={next.split}
        singlePaneKey="a"
        paneIdOf={(key) => key}
        renderPane={(key) => <div data-testid={`content-${key}`} />}
        onResize={() => {}}
        panesRef={panesRef}
        zoomedKey={next.zoomedKey ?? null}
        onUnzoom={() => {}}
        sizing={() => null}
        strip={{
          paneOf: (key) => ({
            name: `shell ${key}`,
            commands: null,
            focus: focus[key as "a" | "b"],
          }),
        }}
        onMovePane={movable ? onMovePane : undefined}
        refocusPane={refocusPane}
        paneDragOutside={outside}
      />
    </TooltipProvider>
  );
  const rendered = render(view({ split: single ? undefined : SPLIT, zoomedKey }));
  return {
    onMovePane,
    refocusPane,
    focus,
    rerender: (next: { split?: TerminalSplitState; zoomedKey?: string | null }) =>
      rendered.rerender(view(next)),
  };
}

const row = (key: string) => document.querySelector<HTMLElement>(`[data-pane-strip="${key}"]`)!;
const dropShown = () => document.querySelector("[data-pane-drop]")?.getAttribute("data-pane-drop");

const press = (key: string, x: number, y = 10) =>
  fireEvent.pointerDown(row(key), { button: 0, pointerId: 1, clientX: x, clientY: y });
const moveTo = (x: number, y: number) =>
  act(() => void fireEvent.pointerMove(window, { pointerId: 1, clientX: x, clientY: y }));
const release = (x: number, y: number) =>
  act(() => void fireEvent.pointerUp(window, { pointerId: 1, clientX: x, clientY: y }));

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    ...VIEW,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect);
});

afterEach(() => {
  resetAppPanes();
  cleanup();
  // A title bar a failed case left would shift every later case's rows.
  for (const strip of document.querySelectorAll(".electron-drag-strip")) strip.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("SplitPanesLayout: dragging a pane by its row", () => {
  it("marks a split's rows movable with a drag handle, and not a lone or maximized pane's, nor without a mover", () => {
    const handle = (key: string) => row(key).querySelector("[data-pane-strip-drag-handle]");
    setup();
    for (const key of ["a", "b"]) {
      expect(row(key).hasAttribute("data-pane-strip-movable")).toBe(true);
      expect(handle(key)).not.toBeNull();
    }
    // Only a hint, shown while the pointer is over the row: presses go through it to the row.
    expect(handle("a")!.className).toMatch(/pointer-events-none/);
    expect(handle("a")!.className).toMatch(/(^|\s)opacity-0(\s|$)/);
    expect(handle("a")!.className).toMatch(/group-hover\/pane-strip:opacity-100/);
    expect(row("a").className).toMatch(/group\/pane-strip/);
    // Movable, it looks and points as any row: no grip dots, no grab cursor.
    expect(row("a").querySelector("[data-pane-strip-grip]")).toBeNull();
    expect(row("a").className).not.toMatch(/cursor-grab/);
    // A lone pane, a maximized one, and a shell that can't move panes: the row as it was.
    for (const options of [{ single: true }, { zoomedKey: "a" }, { movable: false }]) {
      cleanup();
      setup(options);
      expect(row("a").hasAttribute("data-pane-strip-movable")).toBe(false);
      expect(handle("a")).toBeNull();
      // Nor does a press lift it.
      press("a", 200);
      expect(row("a").hasAttribute("data-pane-strip-held")).toBe(false);
      release(200, 10);
    }
  });

  it("lifts the pane from the press, before it drags, until the drag ends", () => {
    const { onMovePane } = setup();
    const card = (key: string) => row(key).parentElement!;
    const handle = () => row("a").querySelector<HTMLElement>("[data-pane-strip-drag-handle]")!;
    const lifted = (key: string) =>
      card(key).hasAttribute("data-pane-held") && row(key).hasAttribute("data-pane-strip-held");
    expect(lifted("a")).toBe(false);
    expect(card("a").className).not.toMatch(/shadow-lg|ring-primary/);

    // Pressed and not yet moved: lifted, with the handle shown, stretched in the accent.
    press("a", 200);
    expect(lifted("a")).toBe(true);
    expect(lifted("b")).toBe(false);
    expect(row("a").hasAttribute("data-pane-strip-dragging")).toBe(false);
    expect(card("a").className).toMatch(/-translate-y-px/);
    expect(card("a").className).toMatch(/shadow-lg/);
    expect(card("a").className).toMatch(/ring-primary/);
    expect(card("a").parentElement!.className).toMatch(/z-\[5\]/);
    expect(handle().className).toMatch(/bg-primary/);
    expect(handle().className).toMatch(/w-12/);
    expect(handle().className).not.toMatch(/opacity-0/);

    // Still lifted through the drag, and set down where it lands.
    moveTo(780, 300);
    expect(lifted("a")).toBe(true);
    release(780, 300);
    expect(onMovePane).toHaveBeenCalledTimes(1);
    expect(lifted("a")).toBe(false);
    expect(handle().className).toMatch(/opacity-0/);

    // A plain click sets it down as it lets go.
    press("a", 200);
    expect(lifted("a")).toBe(true);
    release(200, 10);
    expect(lifted("a")).toBe(false);

    // Escape mid-drag, or a cancelled pointer, sets it down unmoved.
    press("a", 200);
    moveTo(780, 300);
    act(() => void fireEvent.keyDown(window, { key: "Escape" }));
    expect(lifted("a")).toBe(false);
    press("a", 200);
    act(() => void fireEvent.pointerCancel(window, { pointerId: 1 }));
    expect(lifted("a")).toBe(false);
    expect(onMovePane).toHaveBeenCalledTimes(1);
  });

  it("shows the drag handle only while the pointer is over the row", () => {
    setup();
    const handle = row("a").querySelector<HTMLElement>("[data-pane-strip-drag-handle]")!;
    expect(handle.className).toMatch(/(^|\s)opacity-0(\s|$)/);
    expect(handle.className).toMatch(/group-hover\/pane-strip:opacity-100/);
    expect(row("a").className).toMatch(/group\/pane-strip/);
  });

  it("lifts the pane from the press, before it drags, until the drag ends", () => {
    const { onMovePane } = setup();
    const card = (key: string) => row(key).parentElement!;
    const handle = () => row("a").querySelector<HTMLElement>("[data-pane-strip-drag-handle]")!;
    const lifted = (key: string) =>
      card(key).hasAttribute("data-pane-held") && row(key).hasAttribute("data-pane-strip-held");
    expect(lifted("a")).toBe(false);
    expect(card("a").className).not.toMatch(/shadow-lg|ring-primary/);

    // Pressed and not yet moved: lifted, with the handle shown, stretched in the accent.
    press("a", 200);
    expect(lifted("a")).toBe(true);
    expect(lifted("b")).toBe(false);
    expect(row("a").hasAttribute("data-pane-strip-dragging")).toBe(false);
    expect(card("a").className).toMatch(/-translate-y-px/);
    expect(card("a").className).toMatch(/shadow-lg/);
    expect(card("a").className).toMatch(/ring-primary/);
    expect(card("a").parentElement!.className).toMatch(/z-\[5\]/);
    expect(handle().className).toMatch(/bg-primary/);
    expect(handle().className).toMatch(/w-12/);
    expect(handle().className).not.toMatch(/opacity-0/);

    // Still lifted through the drag, and set down where it lands.
    moveTo(780, 300);
    expect(lifted("a")).toBe(true);
    release(780, 300);
    expect(onMovePane).toHaveBeenCalledTimes(1);
    expect(lifted("a")).toBe(false);
    expect(handle().className).toMatch(/opacity-0/);

    // A plain click sets it down as it lets go.
    press("a", 200);
    expect(lifted("a")).toBe(true);
    release(200, 10);
    expect(lifted("a")).toBe(false);

    // Escape mid-drag, or a cancelled pointer, sets it down unmoved.
    press("a", 200);
    moveTo(780, 300);
    act(() => void fireEvent.keyDown(window, { key: "Escape" }));
    expect(lifted("a")).toBe(false);
    press("a", 200);
    act(() => void fireEvent.pointerCancel(window, { pointerId: 1 }));
    expect(lifted("a")).toBe(false);
    expect(onMovePane).toHaveBeenCalledTimes(1);
  });

  it("never lifts a pane that can't move, nor on a Ctrl-press", () => {
    setup({ movable: false });
    press("a", 200);
    expect(row("a").hasAttribute("data-pane-strip-held")).toBe(false);
    cleanup();
    setup();
    fireEvent.pointerDown(row("a"), { button: 0, ctrlKey: true, pointerId: 1, clientX: 200 });
    expect(row("a").hasAttribute("data-pane-strip-held")).toBe(false);
  });

  it("never lifts or drags on a Ctrl-press, the Mac's right-click, which opens the row's menu", () => {
    const { onMovePane } = setup();
    fireEvent.pointerDown(row("a"), { button: 0, ctrlKey: true, pointerId: 1, clientX: 200 });
    expect(row("a").hasAttribute("data-pane-strip-held")).toBe(false);
    moveTo(780, 300);
    expect(dropShown()).toBeUndefined();
    release(780, 300);
    expect(onMovePane).not.toHaveBeenCalled();
  });

  it("never drags from a double-click's second press, though it moves past the threshold", () => {
    const now = vi.spyOn(performance, "now").mockReturnValue(1000);
    const { onMovePane } = setup();
    press("a", 100);
    release(100, 10);
    now.mockReturnValue(1200);
    press("a", 101);
    moveTo(104, 13);
    expect(row("a").hasAttribute("data-pane-strip-dragging")).toBe(false);
    expect(dividerDrag.dragging()).toBe(false);
    moveTo(780, 300);
    expect(dropShown()).toBeUndefined();
    release(780, 300);
    expect(onMovePane).not.toHaveBeenCalled();
    // A press well after the click is a fresh one, and drags.
    now.mockReturnValue(3000);
    press("a", 100);
    moveTo(780, 300);
    expect(dropShown()).toBe("b:right");
    release(780, 300);
    expect(onMovePane).toHaveBeenCalledTimes(1);
  });

  it("drags from a press on another pane's row right after a click on this one's, however near", () => {
    const now = vi.spyOn(performance, "now").mockReturnValue(1000);
    const { onMovePane } = setup();
    // A click just left of the divider, then a press just right of it, on b's row.
    press("a", 398);
    release(398, 10);
    now.mockReturnValue(1100);
    press("b", 402);
    moveTo(20, 300);
    expect(dropShown()).toBe("a:left");
    release(20, 300);
    expect(onMovePane).toHaveBeenCalledExactlyOnceWith({ key: "b", targetKey: "a", side: "left" });
  });

  it("starts a drag from a press on the drag handle", () => {
    const { onMovePane } = setup();
    const handle = row("a").querySelector<HTMLElement>("[data-pane-strip-drag-handle]")!;
    fireEvent.pointerDown(handle, { button: 0, pointerId: 1, clientX: 200, clientY: 4 });
    moveTo(780, 300);
    expect(dropShown()).toBe("b:right");
    release(780, 300);
    expect(onMovePane).toHaveBeenCalledWith({ key: "a", targetKey: "b", side: "right" });
  });

  /** The Mac title bar's drag strip, `bottom` px tall; the view starts at `viewLeft`, `viewTop`. */
  function underTitleBar(bottom: number, viewTop: number, viewLeft = 0) {
    const strip = document.body.appendChild(document.createElement("div"));
    strip.className = "electron-drag-strip";
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (
      this: HTMLElement,
    ) {
      const top = this === strip ? 0 : viewTop;
      const left = this === strip ? 0 : viewLeft;
      const height = this === strip ? bottom : VIEW.height;
      return {
        ...VIEW,
        top,
        y: top,
        bottom: top + height,
        height,
        left,
        x: left,
        right: left + VIEW.width,
        toJSON: () => ({}),
      };
    });
    return strip;
  }

  it("marks no row as under the title bar where the window draws no drag strip", () => {
    setup();
    for (const key of ["a", "b"]) {
      expect(row(key).hasAttribute("data-pane-strip-window-top")).toBe(false);
    }
  });

  it("marks the rows of floating panes inset below the window's top, under its drag strip", () => {
    const strip = underTitleBar(48, 9);
    setup();
    for (const key of ["a", "b"]) {
      expect(row(key).hasAttribute("data-pane-strip-window-top")).toBe(true);
    }
    cleanup();
    strip.remove();
    // Panes that start where the drag strip ends sit below the title bar.
    const below = underTitleBar(48, 48);
    setup();
    expect(row("a").hasAttribute("data-pane-strip-window-top")).toBe(false);
    below.remove();
  });

  it("keeps the traffic-light room in the top-left row of floating panes inset from the corner", () => {
    const strip = underTitleBar(48, 9, 9);
    const { rerender } = setup();
    const inset = (key: string) => row(key).querySelector("[data-pane-strip-window-inset]");
    // The movable top-left row, a pane drag elsewhere, still leaves the window's controls room.
    expect(row("a").hasAttribute("data-pane-strip-movable")).toBe(true);
    expect(row("a").hasAttribute("data-pane-strip-window-top")).toBe(true);
    expect(inset("a")).not.toBeNull();
    expect(inset("b")).toBeNull();
    // A pane moved into the corner takes the room with it.
    rerender({
      split: { ...SPLIT, layout: { axis: "columns", ratio: 0.5, children: ["b", "a"] } },
    });
    expect(inset("b")).not.toBeNull();
    expect(inset("a")).toBeNull();
    cleanup();
    strip.remove();
    // A view starting right of the title bar's corner, beside an open sidebar, keeps none.
    const beside = underTitleBar(48, 9, 320);
    setup();
    expect(inset("a")).toBeNull();
    beside.remove();
  });

  it("keeps the rows along the window's top movable, and marks only those", () => {
    const strip = underTitleBar(48, 0);
    const { rerender } = setup();
    for (const key of ["a", "b"]) {
      expect(row(key).hasAttribute("data-pane-strip-window-top")).toBe(true);
      expect(row(key).hasAttribute("data-pane-strip-movable")).toBe(true);
    }
    // Stacked, the lower pane's row sits below the top edge.
    rerender({ split: { ...SPLIT, layout: { axis: "rows", ratio: 0.5, children: ["a", "b"] } } });
    expect(row("a").hasAttribute("data-pane-strip-window-top")).toBe(true);
    expect(row("b").hasAttribute("data-pane-strip-window-top")).toBe(false);
    expect(row("b").hasAttribute("data-pane-strip-movable")).toBe(true);
    strip.remove();
  });

  it("stays a click below the threshold: the pane gets the keyboard and nothing drags", () => {
    const { onMovePane, focus } = setup();
    press("a", 100);
    fireEvent.mouseDown(row("a"), { button: 0 });
    moveTo(100 + PANE_DRAG_THRESHOLD_PX - 1, 10);
    expect(row("a").hasAttribute("data-pane-strip-dragging")).toBe(false);
    expect(dividerDrag.dragging()).toBe(false);
    release(100 + PANE_DRAG_THRESHOLD_PX - 1, 10);
    expect(focus.a).toHaveBeenCalled();
    expect(onMovePane).not.toHaveBeenCalled();
  });

  it("shows where the pane would land, and moves it there once on the drop", () => {
    vi.useFakeTimers();
    const { onMovePane, refocusPane } = setup();
    press("a", 100);
    moveTo(120, 10);
    expect(row("a").hasAttribute("data-pane-strip-dragging")).toBe(true);
    // Frames stop taking the pointer meanwhile, as for a divider drag.
    expect(dividerDrag.dragging()).toBe(true);
    // Over a itself: nowhere to drop.
    expect(dropShown()).toBeUndefined();
    // Near b's right edge: a would leave its place and take the right half, edged where it
    // meets b.
    moveTo(780, 300);
    expect(dropShown()).toBe("b:right");
    const drop = document.querySelector<HTMLElement>("[data-pane-drop]")!;
    expect(drop.style.left).toBe("50%");
    expect(drop.style.width).toBe("50%");
    expect(drop.className).toContain("border-l-2");
    expect(drop.className).toContain("bg-primary/15");
    // A slow drag still drops on release.
    act(() => void vi.advanceTimersByTime(2_000));
    release(780, 300);
    expect(onMovePane).toHaveBeenCalledTimes(1);
    expect(onMovePane).toHaveBeenCalledWith({ key: "a", targetKey: "b", side: "right" });
    expect(dropShown()).toBeUndefined();
    expect(dividerDrag.dragging()).toBe(false);
    expect(refocusPane).not.toHaveBeenCalled();
    release(780, 300);
    expect(onMovePane).toHaveBeenCalledTimes(1);
  });

  it("moves nothing on a drop over the pane itself or outside the panes", () => {
    const { onMovePane, refocusPane } = setup();
    press("a", 100);
    moveTo(200, 300);
    release(200, 300);
    press("a", 100);
    moveTo(900, 300);
    expect(dropShown()).toBeUndefined();
    release(900, 300);
    expect(onMovePane).not.toHaveBeenCalled();
    expect(refocusPane).toHaveBeenCalledTimes(2);
    expect(dividerDrag.dragging()).toBe(false);
  });

  it("ignores a press on the row's controls", () => {
    setup();
    const button = document.createElement("button");
    row("a").append(button);
    fireEvent.pointerDown(button, { button: 0, pointerId: 1, clientX: 100, clientY: 10 });
    moveTo(700, 300);
    expect(row("a").hasAttribute("data-pane-strip-dragging")).toBe(false);
    expect(dividerDrag.dragging()).toBe(false);
  });

  /** The window goes inactive, or, with `focused`, blurs as a frame takes the keyboard. */
  const blurWindow = async ({ focused = false } = {}) => {
    vi.spyOn(document, "hasFocus").mockReturnValue(focused);
    await act(async () => {
      window.dispatchEvent(new Event("blur"));
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
    });
  };

  const endings: [string, () => void | Promise<void>][] = [
    ["Escape", () => act(() => void fireEvent.keyDown(window, { key: "Escape" }))],
    [
      "a cancelled pointer",
      () => act(() => void fireEvent.pointerCancel(window, { pointerId: 1 })),
    ],
    [
      "a lost pointer",
      () => act(() => void fireEvent(row("a"), withPointerId(new Event("lostpointercapture")))),
    ],
    [
      "a hidden page",
      () => {
        vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
        act(() => void document.dispatchEvent(new Event("visibilitychange")));
      },
    ],
    ["an inactive window", () => blurWindow()],
  ];

  it.each(endings)(
    "ends on %s, moving nothing and giving the pane back its keyboard",
    async (_name, endIt) => {
      const { onMovePane, refocusPane } = setup();
      press("a", 100);
      moveTo(780, 300);
      expect(dropShown()).toBe("b:right");
      await endIt();
      expect(row("a").hasAttribute("data-pane-strip-held")).toBe(false);
      expect(dropShown()).toBeUndefined();
      expect(row("a").hasAttribute("data-pane-strip-dragging")).toBe(false);
      expect(dividerDrag.dragging()).toBe(false);
      expect(refocusPane).toHaveBeenCalledWith("a");
      release(780, 300);
      expect(onMovePane).not.toHaveBeenCalled();
    },
  );

  it.each(endings)(
    "sets a held pane down on %s before it drags, and it won't drag after",
    async (_name, endIt) => {
      const { onMovePane } = setup();
      press("a", 100);
      expect(row("a").hasAttribute("data-pane-strip-held")).toBe(true);
      await endIt();
      expect(row("a").hasAttribute("data-pane-strip-held")).toBe(false);
      expect(row("a").parentElement!.hasAttribute("data-pane-held")).toBe(false);
      moveTo(780, 300);
      expect(row("a").hasAttribute("data-pane-strip-dragging")).toBe(false);
      expect(dividerDrag.dragging()).toBe(false);
      expect(dropShown()).toBeUndefined();
      release(780, 300);
      expect(onMovePane).not.toHaveBeenCalled();
    },
  );

  it("lets Escape before a drag through to the pane, and keeps a drag's Escape to itself", () => {
    setup();
    const reached = vi.fn();
    document.addEventListener("keydown", reached);
    try {
      press("a", 100);
      act(() => void fireEvent.keyDown(document.body, { key: "Escape" }));
      expect(reached).toHaveBeenCalledTimes(1);
      press("a", 100);
      moveTo(780, 300);
      act(() => void fireEvent.keyDown(document.body, { key: "Escape" }));
      expect(reached).toHaveBeenCalledTimes(1);
    } finally {
      document.removeEventListener("keydown", reached);
      release(780, 300);
    }
  });

  it("stays held when a frame takes the keyboard from the press, and still drags", async () => {
    const { onMovePane } = setup();
    press("a", 100);
    await blurWindow({ focused: true });
    expect(row("a").hasAttribute("data-pane-strip-held")).toBe(true);
    moveTo(780, 300);
    expect(dropShown()).toBe("b:right");
    await blurWindow({ focused: true });
    expect(dropShown()).toBe("b:right");
    release(780, 300);
    expect(onMovePane).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["another pane is maximized", { split: SPLIT, zoomedKey: "b" }],
    ["the split goes", { split: undefined }],
    [
      "the dragged pane goes",
      {
        split: {
          layout: { axis: "columns", ratio: 0.5, children: ["c", "b"] },
          focusedKey: "b",
        } satisfies TerminalSplitState,
      },
    ],
  ])("ends when %s, giving no pane the keyboard back", (_case, next) => {
    const { onMovePane, refocusPane, rerender } = setup();
    press("a", 100);
    moveTo(780, 300);
    rerender(next);
    expect(dividerDrag.dragging()).toBe(false);
    release(780, 300);
    expect(onMovePane).not.toHaveBeenCalled();
    expect(refocusPane).not.toHaveBeenCalled();
  });

  it("ends as the view unmounts, giving the frames the pointer back and no pane the keyboard", () => {
    const { refocusPane } = setup();
    press("a", 100);
    moveTo(780, 300);
    expect(dividerDrag.dragging()).toBe(true);
    cleanup();
    expect(dividerDrag.dragging()).toBe(false);
    expect(refocusPane).not.toHaveBeenCalled();
  });
});

function withPointerId(event: Event): Event {
  Object.defineProperty(event, "pointerId", { value: 1 });
  return event;
}

describe("SplitPanesLayout: off the split", () => {
  it("follows a pane dragged off every spot, and lets the place it's dropped on take it", () => {
    const outside = { over: vi.fn(), drop: vi.fn(() => true) };
    const { onMovePane, refocusPane } = setup({ outside });
    press("a", 100);
    moveTo(120, 10);
    // Over its own pane: no spot, so the place off the split hears where it is.
    expect(outside.over).toHaveBeenLastCalledWith("a", { x: 120, y: 10 });
    moveTo(780, 300);
    expect(outside.over).toHaveBeenLastCalledWith("a", null);
    moveTo(900, 300);
    release(900, 300);
    expect(outside.drop).toHaveBeenCalledWith("a", { x: 900, y: 300 });
    expect(outside.over).toHaveBeenLastCalledWith("a", null);
    expect(onMovePane).not.toHaveBeenCalled();
    expect(refocusPane).not.toHaveBeenCalled();
    expect(dividerDrag.dragging()).toBe(false);
  });

  it("gives the pane the keyboard back when the place off the split refuses it", () => {
    const outside = { over: vi.fn(), drop: vi.fn(() => false) };
    const { refocusPane } = setup({ outside });
    press("a", 100);
    moveTo(900, 300);
    release(900, 300);
    expect(refocusPane).toHaveBeenCalledWith("a");
  });

  it("ends the drag as the split changes under it, though the pointer stays put, landing nothing", () => {
    const columnOverRows = (ratio: number): TerminalSplitState => ({
      layout: {
        axis: "columns",
        ratio: 0.5,
        children: ["a", { axis: "rows", ratio, children: ["b", "c"] }],
      },
      focusedKey: "a",
    });
    const { onMovePane, refocusPane, rerender } = setup();
    rerender({ split: columnOverRows(0.5) });
    press("a", 100);
    moveTo(120, 10);
    // b is the right column's top half: its right side is the right quarter of that half.
    moveTo(780, 100);
    const preview = () => document.querySelector<HTMLElement>("[data-pane-drop]");
    expect(dropShown()).toBe("b:right");
    expect(preview()!.style.height).toBe("50%");
    // The saved split changes to 70/30 with no pointer move: the drag ends, as on Escape.
    rerender({ split: columnOverRows(0.7) });
    expect(preview()).toBeNull();
    expect(row("a").hasAttribute("data-pane-strip-held")).toBe(false);
    expect(refocusPane).toHaveBeenCalledWith("a");
    release(780, 100);
    expect(onMovePane).not.toHaveBeenCalled();
  });

  it("keeps a held press, not yet a drag, through a change to the split", () => {
    const { onMovePane, rerender } = setup();
    press("a", 100);
    rerender({
      split: { ...SPLIT, layout: { axis: "columns", ratio: 0.6, children: ["a", "b"] } },
    });
    expect(row("a").hasAttribute("data-pane-strip-held")).toBe(true);
    moveTo(120, 10);
    moveTo(780, 300);
    release(780, 300);
    expect(onMovePane).toHaveBeenCalledOnce();
  });

  it("offers Bottom below a tall pane's middle, and keeps it near the diagonal as it lands", () => {
    const { onMovePane } = setup();
    press("a", 100);
    moveTo(120, 10);
    // b is 400 wide and 600 tall: two thirds of the way down its middle is its bottom zone.
    moveTo(600, 400);
    expect(dropShown()).toBe("b:bottom");
    // Just past the diagonal toward the right, Bottom stays: the right isn't clearly nearer.
    moveTo(680, 408);
    expect(dropShown()).toBe("b:bottom");
    release(680, 408);
    expect(onMovePane).toHaveBeenCalledWith({ key: "a", targetKey: "b", side: "bottom" });
  });

  it("shows where a sidebar tab dragged over the panes would open", () => {
    setup();
    expect(dropShown()).toBeUndefined();
    act(() =>
      setPendingAppPaneDrop("c1", {
        targetKey: "b",
        side: "bottom",
        area: { left: 50, top: 50, width: 50, height: 50 },
      }),
    );
    expect(dropShown()).toBe("b:bottom");
    act(() => setPendingAppPaneDrop("c1", null));
    expect(dropShown()).toBeUndefined();
  });
});
