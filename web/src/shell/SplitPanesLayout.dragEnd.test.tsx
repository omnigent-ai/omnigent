// A divider drag that ends without saving logs why, so a report of a resize that snapped back
// names the cause: the layout hiding the split, maximizing a pane, losing the held divider or
// leaving the screen, or the pointer being taken away. A release logs nothing.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { type ComponentProps, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, type MockInstance, vi } from "vitest";

import type { TerminalSplitState } from "@/lib/terminalSplitLayout";
import { SplitPanesLayout } from "@/shell/SplitPanesLayout";

import { type DividerDragEnd, formatDividerDragEnd } from "./paneDividerDragEnd";

const SPLIT: TerminalSplitState = {
  layout: { axis: "columns", ratio: 0.5, children: ["a", "b"] },
  focusedKey: "a",
};

const ignoreResize = () => {};

function View({
  split,
  zoomedKey = null,
  onResize = ignoreResize,
}: {
  split: TerminalSplitState | undefined;
  zoomedKey?: string | null;
  onResize?: ComponentProps<typeof SplitPanesLayout>["onResize"];
}) {
  const panesRef = useRef<HTMLDivElement | null>(null);
  return (
    <SplitPanesLayout
      conversationId="c1"
      split={split}
      singlePaneKey="a"
      paneIdOf={(key) => key}
      renderPane={(key) => <div data-testid={`content-${key}`} />}
      onResize={onResize}
      panesRef={panesRef}
      zoomedKey={zoomedKey}
      onUnzoom={() => {}}
      sizing={() => null}
    />
  );
}

/** Presses the divider and moves it to 30% of a 1000 px wide view. */
function pressDivider() {
  const divider = screen.getByTestId("terminal-split-divider");
  vi.spyOn(divider.parentElement!, "getBoundingClientRect").mockReturnValue({
    left: 0,
    top: 0,
    width: 1000,
    height: 500,
  } as DOMRect);
  let captured = false;
  divider.setPointerCapture = () => {
    captured = true;
  };
  divider.hasPointerCapture = () => captured;
  fireEvent.pointerDown(divider, { pointerId: 1 });
  fireEvent.pointerMove(divider, { pointerId: 1, clientX: 300, clientY: 250 });
  return divider;
}

let info: MockInstance<typeof console.info>;
const logged = () => info.mock.calls.map(([line]) => line);
const ended = (reason: DividerDragEnd) => formatDividerDragEnd("c1", "divider", reason);

beforeEach(() => {
  info = vi.spyOn(console, "info").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("a divider drag that ends unsaved", () => {
  it("logs nothing when it's released", () => {
    render(<View split={SPLIT} />);
    fireEvent.pointerUp(pressDivider(), { pointerId: 1 });
    expect(logged()).toEqual([]);
  });

  const ROWS: TerminalSplitState = {
    layout: { axis: "rows", ratio: 0.5, children: ["a", "b"] },
    focusedKey: "a",
  };
  it.each<[DividerDragEnd, TerminalSplitState | undefined, string | null]>([
    ["split-hidden", undefined, null],
    ["pane-maximized", SPLIT, "b"],
    ["divider-gone", ROWS, null],
  ])("names %s when the layout ends it", (reason, split, zoomedKey) => {
    const { rerender } = render(<View split={SPLIT} />);
    pressDivider();
    rerender(<View split={split} zoomedKey={zoomedKey} />);
    expect(logged()).toEqual([ended(reason)]);
  });

  it("names panes-unmounted when the panes leave the screen mid-drag", () => {
    const { unmount } = render(<View split={SPLIT} />);
    pressDivider();
    unmount();
    expect(logged()).toEqual([ended("panes-unmounted")]);
  });

  it.each([
    [
      "pointer-cancelled",
      (divider: HTMLElement) => fireEvent.pointerCancel(divider, { pointerId: 1 }),
    ],
    [
      "pointer-lost",
      (divider: HTMLElement) => {
        // Its capture ends, and the pointer it follows then moves with the button up.
        fireEvent.lostPointerCapture(divider, { pointerId: 1 });
        fireEvent.pointerMove(document.body, { pointerId: 1, buttons: 0 });
      },
    ],
    ["window-blurred", () => fireEvent.blur(window)],
  ] as const)("names %s when the pointer is taken away", (reason, takeAway) => {
    render(<View split={SPLIT} />);
    const divider = pressDivider();
    takeAway(divider);
    // The pointer's own end, once: the layout dropping the drag after it adds nothing.
    expect(logged()).toEqual([ended(reason)]);
  });

  it("logs nothing when its capture ends just before a release elsewhere", () => {
    const onResize = vi.fn();
    render(<View split={SPLIT} onResize={onResize} />);
    const divider = pressDivider();
    fireEvent.lostPointerCapture(divider, { pointerId: 1 });
    fireEvent.pointerUp(screen.getByTestId("content-a"), { pointerId: 1 });
    expect(logged()).toEqual([]);
    expect(onResize).toHaveBeenCalledWith([{ path: [], ratio: 0.3 }]);
  });

  it("ends a drag that follows its pointer once the layout drops it, logging only the layout's reason", () => {
    const onResize = vi.fn();
    const { rerender } = render(<View split={SPLIT} onResize={onResize} />);
    const divider = pressDivider();
    // The capture ends while the divider is still shown, so the drag follows the pointer.
    fireEvent.lostPointerCapture(divider, { pointerId: 1 });
    rerender(<View split={undefined} onResize={onResize} />);
    // The document no longer follows it: a cancel or a release there does nothing more.
    fireEvent.pointerCancel(document.body, { pointerId: 1 });
    fireEvent.pointerUp(document.body, { pointerId: 1 });
    expect(logged()).toEqual([ended("split-hidden")]);
    expect(onResize).not.toHaveBeenCalled();
  });
});
