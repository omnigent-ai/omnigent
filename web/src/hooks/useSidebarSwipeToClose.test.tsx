import { cleanup, render } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useSidebarSwipeToClose } from "./useSidebarSwipeToClose";

const DRAWER_WIDTH = 300;
const HOLD_MS = 250;

function Harness(props: {
  enabled?: boolean;
  onDragProgress: (progress: number | null) => void;
  onSettle: (close: boolean) => void;
}) {
  const drawerRef = useRef<HTMLElement>(null);
  const scrimRef = useRef<HTMLButtonElement>(null);
  useSidebarSwipeToClose({
    enabled: props.enabled ?? true,
    drawerRef,
    scrimRef,
    holdMs: HOLD_MS,
    onDragProgress: props.onDragProgress,
    onSettle: props.onSettle,
  });
  return (
    <>
      <button type="button" ref={scrimRef} data-testid="scrim" />
      <aside ref={drawerRef} data-testid="drawer">
        <div data-testid="row">row</div>
        <div data-testid="scroller" style={{ overflowX: "auto" }}>
          <span data-testid="scroller-content">wide</span>
        </div>
      </aside>
      <div data-testid="outside">outside</div>
    </>
  );
}

function setup(enabled = true) {
  const onDragProgress = vi.fn();
  const onSettle = vi.fn();
  const view = render(
    <Harness enabled={enabled} onDragProgress={onDragProgress} onSettle={onSettle} />,
  );
  const drawer = view.getByTestId("drawer");
  drawer.getBoundingClientRect = () => ({ width: DRAWER_WIDTH }) as DOMRect;
  const scroller = view.getByTestId("scroller");
  Object.defineProperty(scroller, "scrollWidth", { value: 600 });
  Object.defineProperty(scroller, "clientWidth", { value: 200 });
  return { ...view, onDragProgress, onSettle };
}

/** Dispatch a single-finger touch event at `time` ms; returns the event. */
function touch(target: Element, type: string, x: number, y: number, time: number): Event {
  const event = new Event(type, { bubbles: true, cancelable: true });
  const touches = type === "touchend" || type === "touchcancel" ? [] : [{ clientX: x, clientY: y }];
  Object.defineProperty(event, "touches", { value: touches });
  Object.defineProperty(event, "timeStamp", { value: time });
  target.dispatchEvent(event);
  return event;
}

/** Drag from (x, y) by (dx, dy) over `steps` moves `stepMs` apart, then lift. */
function swipe(
  target: Element,
  { x = 200, y = 100, dx = 0, dy = 0, steps = 5, stepMs = 40, holdMs = 0, liftDelayMs = 16 },
) {
  touch(target, "touchstart", x, y, 0);
  let time = holdMs;
  const moves: Event[] = [];
  for (let step = 1; step <= steps; step++) {
    time += stepMs;
    moves.push(touch(target, "touchmove", x + (dx * step) / steps, y + (dy * step) / steps, time));
  }
  const end = touch(target, "touchend", 0, 0, time + liftDelayMs);
  return { moves, end };
}

afterEach(() => cleanup());

describe("useSidebarSwipeToClose", () => {
  it("tracks the finger and closes once the drag passes a third of the drawer", () => {
    const { getByTestId, onDragProgress, onSettle } = setup();

    const { moves, end } = swipe(getByTestId("row"), { dx: -150 });

    expect(onDragProgress).toHaveBeenCalledWith(1 - 60 / DRAWER_WIDTH);
    expect(onDragProgress).toHaveBeenCalledWith(1 - 150 / DRAWER_WIDTH);
    expect(onDragProgress).toHaveBeenLastCalledWith(null);
    expect(onSettle).toHaveBeenCalledExactlyOnceWith(true);
    // The claimed drag stops the list scrolling and the row activating.
    expect(moves.at(-1)?.defaultPrevented).toBe(true);
    expect(end.defaultPrevented).toBe(true);
  });

  it("springs back open after a short, slow drag", () => {
    const { getByTestId, onSettle } = setup();

    swipe(getByTestId("row"), { dx: -60, steps: 4, stepMs: 80 });

    expect(onSettle).toHaveBeenCalledExactlyOnceWith(false);
  });

  it("closes on a quick leftward flick even over a short distance", () => {
    const { getByTestId, onSettle } = setup();

    swipe(getByTestId("row"), { dx: -60, steps: 3, stepMs: 16 });

    expect(onSettle).toHaveBeenCalledExactlyOnceWith(true);
  });

  it("does not treat a flick as closing when the finger rested before lifting", () => {
    const { getByTestId, onSettle } = setup();

    swipe(getByTestId("row"), { dx: -60, steps: 3, stepMs: 16, liftDelayMs: 300 });

    expect(onSettle).toHaveBeenCalledExactlyOnceWith(false);
  });

  it("starts from the scrim over the exposed chat strip", () => {
    const { getByTestId, onSettle } = setup();

    swipe(getByTestId("scrim"), { x: 350, dx: -200 });

    expect(onSettle).toHaveBeenCalledExactlyOnceWith(true);
  });

  it.each([
    ["a vertical scroll of the list", { dy: -150, dx: -20 }],
    ["a rightward swipe", { dx: 150 }],
    ["a hold before dragging (row long-press)", { dx: -150, holdMs: HOLD_MS }],
  ])("leaves %s alone", (_label, gesture) => {
    const { getByTestId, onDragProgress, onSettle } = setup();

    const { moves } = swipe(getByTestId("row"), gesture);

    expect(onDragProgress).not.toHaveBeenCalled();
    expect(onSettle).not.toHaveBeenCalled();
    expect(moves.some((move) => move.defaultPrevented)).toBe(false);
  });

  it("leaves horizontally scrollable content and touches outside the drawer alone", () => {
    const { getByTestId, onSettle } = setup();

    swipe(getByTestId("scroller-content"), { dx: -150 });
    swipe(getByTestId("outside"), { dx: -150 });

    expect(onSettle).not.toHaveBeenCalled();
  });

  it("does nothing while disabled", () => {
    const { getByTestId, onDragProgress, onSettle } = setup(false);

    swipe(getByTestId("row"), { dx: -150 });

    expect(onDragProgress).not.toHaveBeenCalled();
    expect(onSettle).not.toHaveBeenCalled();
  });

  it("settles by distance when the system cancels the touch mid-drag", () => {
    const { getByTestId, onDragProgress, onSettle } = setup();
    const row = getByTestId("row");

    touch(row, "touchstart", 200, 100, 0);
    touch(row, "touchmove", 100, 100, 40);
    touch(row, "touchmove", 50, 100, 80);
    touch(row, "touchcancel", 0, 0, 90);

    expect(onDragProgress).toHaveBeenLastCalledWith(null);
    expect(onSettle).toHaveBeenCalledExactlyOnceWith(true);
  });
});
