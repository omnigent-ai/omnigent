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

/** Dispatch a touch event with `fingers` touches down at `time` ms; returns the event. */
function touch(
  target: Element,
  type: string,
  x: number,
  y: number,
  time: number,
  fingers = 1,
): Event {
  const event = new Event(type, { bubbles: true, cancelable: true });
  const lifted = type === "touchend" || type === "touchcancel";
  const touches = lifted ? [] : Array.from({ length: fingers }, () => ({ clientX: x, clientY: y }));
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

    // Slow enough (~0.19 px/ms) that only the distance can close it.
    const { moves, end } = swipe(getByTestId("row"), { dx: -150, steps: 10, stepMs: 80 });

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

    const { end } = swipe(getByTestId("row"), { dx: -60, steps: 4, stepMs: 80 });

    expect(onSettle).toHaveBeenCalledExactlyOnceWith(false);
    // The lift is still claimed, so the scrim's click can't close the drawer.
    expect(end.defaultPrevented).toBe(true);
  });

  // Each pair sits either side of one cutoff; the moves before it stay slow,
  // short, or early enough that no other rule decides the release.
  it.each([
    ["past a third of the width", { dx: -100, steps: 5, stepMs: 80 }, true],
    ["just short of a third", { dx: -98, steps: 5, stepMs: 80 }, false],
  ])("closes by distance only %s", (_label, gesture, close) => {
    const { getByTestId, onSettle } = setup();

    swipe(getByTestId("row"), gesture);

    expect(onSettle).toHaveBeenCalledExactlyOnceWith(close);
  });

  it.each([
    ["at 0.62 px/ms, lifted 100 ms after the last move", 31, 200, true],
    ["below 0.6 px/ms", 29, 200, false],
    ["lifted 101 ms after the last move", 31, 201, false],
  ])("treats a short drag as a flick only %s", (_label, stepPx, liftAt, close) => {
    const { getByTestId, onSettle } = setup();
    const row = getByTestId("row");

    // Two even moves, so the speed over the 100 ms window is stepPx / 50.
    touch(row, "touchstart", 200, 100, 0);
    touch(row, "touchmove", 200 - stepPx, 100, 50);
    touch(row, "touchmove", 200 - 2 * stepPx, 100, 100);
    touch(row, "touchend", 0, 0, liftAt);

    expect(onSettle).toHaveBeenCalledExactlyOnceWith(close);
  });

  it("does not read one jittery move at the end of a slow drag as a flick", () => {
    const { getByTestId, onSettle } = setup();
    const row = getByTestId("row");

    touch(row, "touchstart", 200, 100, 0);
    touch(row, "touchmove", 186, 100, 60);
    touch(row, "touchmove", 172, 100, 120);
    touch(row, "touchmove", 158, 100, 180);
    // 3 px in 2 ms is 1.5 px/ms on its own, but ~0.3 px/ms over the window.
    touch(row, "touchmove", 155, 100, 182);
    touch(row, "touchend", 0, 0, 190);

    expect(onSettle).toHaveBeenCalledExactlyOnceWith(false);
  });

  // Each is a steady drag below flick speed whose window opens between samples.
  it.each([
    [
      "0.5 px/ms",
      [
        [170, 60],
        [125, 150],
      ],
      160,
    ],
    [
      "0.375 px/ms",
      [
        [170, 80],
        [140, 160],
      ],
      176,
    ],
  ])("does not overstate a steady %s drag reported by sparse moves", (_label, moves, liftAt) => {
    const { getByTestId, onSettle } = setup();
    const row = getByTestId("row");

    touch(row, "touchstart", 200, 100, 0);
    for (const [x, time] of moves) touch(row, "touchmove", x, 100, time);
    touch(row, "touchend", 0, 0, liftAt);

    expect(onSettle).toHaveBeenCalledExactlyOnceWith(false);
  });

  it("still closes on a flick that ends a slow drag", () => {
    const { getByTestId, onSettle } = setup();
    const row = getByTestId("row");

    touch(row, "touchstart", 200, 100, 0);
    touch(row, "touchmove", 186, 100, 100);
    touch(row, "touchmove", 180, 100, 200);
    touch(row, "touchmove", 160, 100, 220);
    touch(row, "touchmove", 138, 100, 240);
    touch(row, "touchmove", 116, 100, 260);
    touch(row, "touchend", 0, 0, 270);

    expect(onSettle).toHaveBeenCalledExactlyOnceWith(true);
  });

  it.each([
    [HOLD_MS - 1, true],
    [HOLD_MS, false],
  ])("claims a drag whose first decisive move comes at %i ms: %s", (moveAt, claimed) => {
    const { getByTestId, onDragProgress } = setup();
    const row = getByTestId("row");

    touch(row, "touchstart", 200, 100, 0);
    const move = touch(row, "touchmove", 180, 100, moveAt);

    expect(onDragProgress).toHaveBeenCalledTimes(claimed ? 1 : 0);
    expect(move.defaultPrevented).toBe(claimed);
  });

  it("starts from the scrim over the exposed chat strip", () => {
    const { getByTestId, onSettle } = setup();

    swipe(getByTestId("scrim"), { x: 350, dx: -200, steps: 10, stepMs: 80 });

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
    const { getByTestId, onDragProgress, onSettle } = setup();

    const scroller = swipe(getByTestId("scroller-content"), { dx: -150 });
    const outside = swipe(getByTestId("outside"), { dx: -150 });

    expect(onDragProgress).not.toHaveBeenCalled();
    expect(onSettle).not.toHaveBeenCalled();
    for (const { moves, end } of [scroller, outside]) {
      expect(moves.some((move) => move.defaultPrevented)).toBe(false);
      expect(end.defaultPrevented).toBe(false);
    }
  });

  it("does nothing while disabled", () => {
    const { getByTestId, onDragProgress, onSettle } = setup(false);

    swipe(getByTestId("row"), { dx: -150 });

    expect(onDragProgress).not.toHaveBeenCalled();
    expect(onSettle).not.toHaveBeenCalled();
  });

  it("settles by distance and stops tracking when a second finger lands mid-drag", () => {
    const { getByTestId, onDragProgress, onSettle } = setup();
    const row = getByTestId("row");

    touch(row, "touchstart", 200, 100, 0);
    touch(row, "touchmove", 170, 100, 80);
    touch(row, "touchmove", 140, 100, 160);
    touch(row, "touchstart", 140, 100, 200, 2);

    expect(onDragProgress).toHaveBeenLastCalledWith(null);
    expect(onSettle).toHaveBeenCalledExactlyOnceWith(false);

    touch(row, "touchmove", 60, 100, 240, 2);
    touch(row, "touchend", 0, 0, 300);

    expect(onDragProgress).toHaveBeenLastCalledWith(null);
    expect(onSettle).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["a second finger lands", (row: Element) => touch(row, "touchstart", 150, 100, 50, 2)],
    ["the system cancels the touch", (row: Element) => touch(row, "touchcancel", 0, 0, 50)],
  ])("does not count a fast drag as a flick when %s", (_label, interrupt) => {
    const { getByTestId, onSettle } = setup();
    const row = getByTestId("row");

    touch(row, "touchstart", 200, 100, 0);
    touch(row, "touchmove", 175, 100, 20);
    touch(row, "touchmove", 150, 100, 40);
    interrupt(row);
    touch(row, "touchend", 0, 0, 60);

    expect(onSettle).toHaveBeenCalledExactlyOnceWith(false);
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
