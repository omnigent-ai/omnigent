import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  DISMISS_DISTANCE_PX,
  DISMISS_HOLD_MS,
  useSwipeDownToDismissKeyboard,
} from "./useSwipeDownToDismissKeyboard";

function Harness({ enabled = true }: { enabled?: boolean }) {
  useSwipeDownToDismissKeyboard({ enabled, surfaceSelector: ".surface" });
  return (
    <>
      <div className="surface">
        <p data-testid="page">transcript</p>
        <textarea data-testid="composer" />
        <div data-testid="terminal" className="xterm" />
        <div data-testid="popover" style={{ position: "fixed" }}>
          <span data-testid="popover-row">menu row</span>
        </div>
        <div data-testid="drawer" style={{ position: "fixed" }}>
          <span data-testid="drawer-page">side chat</span>
          <textarea data-testid="drawer-composer" />
        </div>
        <button type="button" data-testid="button">
          send
        </button>
      </div>
      <div data-testid="outside">portal</div>
    </>
  );
}

function setup(enabled = true) {
  const view = render(<Harness enabled={enabled} />);
  const composer = view.getByTestId("composer");
  composer.focus();
  return { ...view, composer };
}

function touch(target: Element, type: string, x: number, y: number, time: number, fingers = 1) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  const lifted = type === "touchend" || type === "touchcancel";
  const touches = lifted ? [] : Array.from({ length: fingers }, () => ({ clientX: x, clientY: y }));
  Object.defineProperty(event, "touches", { value: touches });
  Object.defineProperty(event, "timeStamp", { value: time });
  target.dispatchEvent(event);
  return event;
}

/** Drag one finger from (x, y) by (dx, dy) over `steps` moves `stepMs` apart. */
function swipe(
  target: Element,
  { x = 100, y = 200, dx = 0, dy = 0, steps = 5, stepMs = 16, holdMs = 0, lift = true },
) {
  touch(target, "touchstart", x, y, 0);
  let time = holdMs;
  const moves: Event[] = [];
  for (let step = 1; step <= steps; step++) {
    time += stepMs;
    moves.push(touch(target, "touchmove", x + (dx * step) / steps, y + (dy * step) / steps, time));
  }
  if (lift) touch(target, "touchend", 0, 0, time + 16);
  return moves;
}

afterEach(() => cleanup());

describe("useSwipeDownToDismissKeyboard", () => {
  it("blurs the focused composer once a downward swipe crosses the threshold", () => {
    const { getByTestId, composer } = setup();
    const page = getByTestId("page");

    touch(page, "touchstart", 100, 200, 0);
    touch(page, "touchmove", 102, 200 + DISMISS_DISTANCE_PX - 1, 16);
    expect(composer).toHaveFocus();
    const crossing = touch(page, "touchmove", 103, 200 + DISMISS_DISTANCE_PX, 32);
    // Dismisses mid-drag, before the finger lifts, and leaves the scroll alone.
    expect(composer).not.toHaveFocus();
    expect(crossing.defaultPrevented).toBe(false);
  });

  it("dismisses from a swipe that starts on composer chrome", () => {
    const { getByTestId, composer } = setup();
    swipe(getByTestId("button"), { dy: 120 });
    expect(composer).not.toHaveFocus();
  });

  it("dismisses a slow swipe that starts after a brief rest", () => {
    const { getByTestId, composer } = setup();
    swipe(getByTestId("page"), { dy: 200, steps: 24, stepMs: 30, holdMs: 200 });
    expect(composer).not.toHaveFocus();
  });

  it.each([
    ["an upward swipe", { dy: -120 }],
    ["a sideways swipe", { dx: 160, dy: 60 }],
    ["a short drag", { dy: DISMISS_DISTANCE_PX - 1 }],
    ["a long press before dragging", { dy: 120, holdMs: DISMISS_HOLD_MS }],
  ])("keeps focus for %s", (_name, gesture) => {
    const { getByTestId, composer } = setup();
    swipe(getByTestId("page"), gesture);
    expect(composer).toHaveFocus();
  });

  it("does not dismiss once an undecided sideways drag turns downward", () => {
    const { getByTestId, composer } = setup();
    const page = getByTestId("page");
    touch(page, "touchstart", 100, 200, 0);
    touch(page, "touchmove", 130, 205, 16);
    touch(page, "touchmove", 130, 300, 32);
    expect(composer).toHaveFocus();
  });

  it.each([
    ["the text field itself", "composer"],
    ["a terminal", "terminal"],
    ["a popover", "popover-row"],
    ["content outside the surface", "outside"],
  ])("ignores swipes that start on %s", (_name, testId) => {
    const { getByTestId, composer } = setup();
    swipe(getByTestId(testId), { dy: 120 });
    expect(composer).toHaveFocus();
  });

  it("dismisses inside a fixed drawer that hosts the focused composer", () => {
    const { getByTestId } = setup();
    const drawerComposer = getByTestId("drawer-composer");
    drawerComposer.focus();
    swipe(getByTestId("drawer-page"), { dy: 120 });
    expect(drawerComposer).not.toHaveFocus();
  });

  it("ignores a second finger", () => {
    const { getByTestId, composer } = setup();
    const page = getByTestId("page");
    touch(page, "touchstart", 100, 200, 0, 2);
    touch(page, "touchmove", 100, 320, 16, 2);
    expect(composer).toHaveFocus();

    touch(page, "touchstart", 100, 200, 0);
    touch(page, "touchmove", 100, 220, 16);
    touch(page, "touchmove", 100, 320, 32, 2);
    touch(page, "touchmove", 100, 330, 48);
    expect(composer).toHaveFocus();
  });

  it("does nothing while disabled or without a focused text field", () => {
    const disabled = setup(false);
    swipe(disabled.getByTestId("page"), { dy: 120 });
    expect(disabled.composer).toHaveFocus();
    cleanup();

    const { getByTestId } = setup();
    const button = getByTestId("button");
    button.focus();
    swipe(getByTestId("page"), { dy: 120 });
    expect(button).toHaveFocus();
  });
});
