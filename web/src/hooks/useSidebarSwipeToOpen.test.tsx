import { cleanup, render } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EDGE_EXCLUSION_PX, useSidebarSwipeToOpen } from "./useSidebarSwipeToOpen";

const DRAWER_WIDTH = 300;
const HOLD_MS = 250;

function Harness(props: {
  enabled?: boolean;
  onDragProgress: (progress: number | null) => void;
  onSettle: (open: boolean) => void;
}) {
  const drawerRef = useRef<HTMLElement>(null);
  useSidebarSwipeToOpen({
    enabled: props.enabled ?? true,
    drawerRef,
    surfaceSelector: ".surface",
    holdMs: HOLD_MS,
    onDragProgress: props.onDragProgress,
    onSettle: props.onSettle,
  });
  return (
    <>
      <div className="surface">
        <aside ref={drawerRef} data-testid="drawer" />
        <main>
          <p data-testid="chat">message</p>
          <div data-testid="scroller" style={{ overflowX: "auto" }}>
            <span data-testid="scroller-content">wide</span>
          </div>
          <textarea data-testid="composer" />
          <div data-testid="overlay" style={{ position: "fixed" }}>
            <span data-testid="overlay-content">drawer</span>
          </div>
        </main>
      </div>
      <div data-testid="outside">portal</div>
    </>
  );
}

function setup(enabled = true) {
  const onDragProgress = vi.fn();
  const onSettle = vi.fn();
  const view = render(
    <Harness enabled={enabled} onDragProgress={onDragProgress} onSettle={onSettle} />,
  );
  view.getByTestId("drawer").getBoundingClientRect = () => ({ width: DRAWER_WIDTH }) as DOMRect;
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
  { x = 60, y = 100, dx = 0, dy = 0, steps = 5, stepMs = 40, holdMs = 0, liftDelayMs = 16 },
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

describe("useSidebarSwipeToOpen", () => {
  it("tracks the finger and opens once the drag passes a third of the drawer", () => {
    const { getByTestId, onDragProgress, onSettle } = setup();

    // Slow enough (~0.19 px/ms) that only the distance can open it.
    const { moves, end } = swipe(getByTestId("chat"), { dx: 150, steps: 10, stepMs: 80 });

    expect(onDragProgress).toHaveBeenCalledWith(60 / DRAWER_WIDTH);
    expect(onDragProgress).toHaveBeenCalledWith(150 / DRAWER_WIDTH);
    expect(onDragProgress).toHaveBeenLastCalledWith(null);
    expect(onSettle).toHaveBeenCalledExactlyOnceWith(true);
    // The claimed drag stops the transcript scrolling and the tap activating.
    expect(moves.at(-1)?.defaultPrevented).toBe(true);
    expect(end.defaultPrevented).toBe(true);
  });

  it.each([
    ["past a third of the width", { dx: 100, steps: 5, stepMs: 80 }, true],
    ["just short of a third", { dx: 98, steps: 5, stepMs: 80 }, false],
  ])("opens by distance only %s", (_label, gesture, open) => {
    const { getByTestId, onSettle } = setup();

    swipe(getByTestId("chat"), gesture);

    expect(onSettle).toHaveBeenCalledExactlyOnceWith(open);
  });

  it.each([
    ["at 0.62 px/ms, lifted 100 ms after the last move", 31, 200, true],
    ["below 0.6 px/ms", 29, 200, false],
    ["lifted 101 ms after the last move", 31, 201, false],
  ])("treats a short drag as a flick only %s", (_label, stepPx, liftAt, open) => {
    const { getByTestId, onSettle } = setup();
    const chat = getByTestId("chat");

    touch(chat, "touchstart", 60, 100, 0);
    touch(chat, "touchmove", 60 + stepPx, 100, 50);
    touch(chat, "touchmove", 60 + 2 * stepPx, 100, 100);
    touch(chat, "touchend", 0, 0, liftAt);

    expect(onSettle).toHaveBeenCalledExactlyOnceWith(open);
  });

  it.each([
    ["a vertical scroll of the transcript", { dy: -150, dx: 20 }],
    ["a leftward swipe", { x: 250, dx: -150 }],
    ["a hold before dragging (long-press)", { dx: 150, holdMs: HOLD_MS }],
    ["a swipe from the screen edge (OS back gesture)", { x: EDGE_EXCLUSION_PX - 1, dx: 150 }],
  ])("leaves %s alone", (_label, gesture) => {
    const { getByTestId, onDragProgress, onSettle } = setup();

    const { moves } = swipe(getByTestId("chat"), gesture);

    expect(onDragProgress).not.toHaveBeenCalled();
    expect(onSettle).not.toHaveBeenCalled();
    expect(moves.some((move) => move.defaultPrevented)).toBe(false);
  });

  it.each([
    ["horizontally scrollable content", "scroller-content"],
    ["a text field", "composer"],
    ["a floating overlay", "overlay-content"],
    ["content outside the shell", "outside"],
  ])("leaves swipes on %s alone", (_label, testId) => {
    const { getByTestId, onDragProgress, onSettle } = setup();

    const { moves, end } = swipe(getByTestId(testId), { dx: 150 });

    expect(onDragProgress).not.toHaveBeenCalled();
    expect(onSettle).not.toHaveBeenCalled();
    expect(moves.some((move) => move.defaultPrevented)).toBe(false);
    expect(end.defaultPrevented).toBe(false);
  });

  it("does nothing while disabled", () => {
    const { getByTestId, onDragProgress, onSettle } = setup(false);

    swipe(getByTestId("chat"), { dx: 150 });

    expect(onDragProgress).not.toHaveBeenCalled();
    expect(onSettle).not.toHaveBeenCalled();
  });

  it("settles by distance and stops tracking when a second finger lands mid-drag", () => {
    const { getByTestId, onDragProgress, onSettle } = setup();
    const chat = getByTestId("chat");

    touch(chat, "touchstart", 60, 100, 0);
    touch(chat, "touchmove", 90, 100, 80);
    touch(chat, "touchmove", 120, 100, 160);
    touch(chat, "touchstart", 120, 100, 200, 2);

    expect(onDragProgress).toHaveBeenLastCalledWith(null);
    expect(onSettle).toHaveBeenCalledExactlyOnceWith(false);

    touch(chat, "touchmove", 250, 100, 240, 2);
    touch(chat, "touchend", 0, 0, 300);

    expect(onSettle).toHaveBeenCalledTimes(1);
  });
});
