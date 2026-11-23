// A divider drag that ends without saving logs why, so a report of a resize that snapped back
// names the cause: the layout hiding the split, maximizing a pane, losing the held divider or
// leaving the screen, or the pointer being taken away. A release logs nothing.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, type MockInstance, vi } from "vitest";

import type { TerminalSplitState } from "@/lib/terminalSplitLayout";
import { SplitPanesLayout } from "@/shell/SplitPanesLayout";

import { formatDividerDragEnd } from "./paneDividerDragEnd";

const SPLIT: TerminalSplitState = {
  layout: { axis: "columns", ratio: 0.5, children: ["a", "b"] },
  focusedKey: "a",
};

function View({
  split,
  zoomedKey = null,
}: {
  split: TerminalSplitState | undefined;
  zoomedKey?: string | null;
}) {
  const panesRef = useRef<HTMLDivElement | null>(null);
  return (
    <SplitPanesLayout
      conversationId="c1"
      split={split}
      singlePaneKey="a"
      paneIdOf={(key) => key}
      renderPane={(key) => <div data-testid={`content-${key}`} />}
      onResize={() => {}}
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
const ended = (reason: string) =>
  formatDividerDragEnd("c1", "divider", reason as Parameters<typeof formatDividerDragEnd>[2]);

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
  it.each<[string, TerminalSplitState | undefined, string | null]>([
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
    ["pointer-cancelled", "pointerCancel"],
    ["pointer-lost", "lostPointerCapture"],
  ] as const)("names %s when the pointer is taken away", (reason, event) => {
    render(<View split={SPLIT} />);
    const divider = pressDivider();
    fireEvent[event](divider, { pointerId: 1 });
    // The pointer's own end, once: the layout dropping the drag after it adds nothing.
    expect(logged()).toEqual([ended(reason)]);
  });

  it("logs the layout's reason once, though the pointer is let go after it", () => {
    const { rerender } = render(<View split={SPLIT} />);
    const divider = pressDivider();
    rerender(<View split={undefined} />);
    fireEvent.lostPointerCapture(divider, { pointerId: 1 });
    expect(logged()).toEqual([ended("split-hidden")]);
  });
});
