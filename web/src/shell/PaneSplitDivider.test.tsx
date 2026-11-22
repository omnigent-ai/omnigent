import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { paneDividerDrag } from "@/shell/paneDividerDrag";
import { PaneSplitDivider } from "@/shell/PaneSplitDivider";

describe("PaneSplitDivider drags", () => {
  const divider = {
    axis: "columns" as const,
    area: { left: 0, top: 0, width: 100, height: 100 },
    ratio: 0.5,
    path: [],
  };

  function renderDivider() {
    const onCommit = vi.fn();
    const onAbort = vi.fn();
    const view = render(
      <PaneSplitDivider
        divider={divider}
        containerRef={{ current: document.body }}
        onResize={vi.fn()}
        onCommit={onCommit}
        onAbort={onAbort}
      />,
    );
    const element = screen.getByRole("separator");
    element.setPointerCapture = vi.fn();
    element.hasPointerCapture = () => true;
    element.releasePointerCapture = vi.fn();
    const rerender = (dragging: boolean) =>
      view.rerender(
        <PaneSplitDivider
          divider={divider}
          containerRef={{ current: document.body }}
          onResize={vi.fn()}
          onCommit={onCommit}
          onAbort={onAbort}
          dragging={dragging}
        />,
      );
    return { ...view, rerender, element, onCommit, onAbort };
  }

  it.each([
    ["pointerUp", "saves"],
    ["pointerCancel", "drops"],
    ["lostPointerCapture", "drops"],
  ] as const)("is a window drag from pointer down until %s, which %s its sizes", (end, outcome) => {
    const { element, onCommit, onAbort } = renderDivider();
    fireEvent.pointerDown(element, { pointerId: 1 });
    expect(paneDividerDrag.dragging()).toBe(true);
    fireEvent[end](element, { pointerId: 1 });
    expect(paneDividerDrag.dragging()).toBe(false);
    expect(onCommit).toHaveBeenCalledTimes(outcome === "saves" ? 1 : 0);
    expect(onAbort).toHaveBeenCalledTimes(outcome === "drops" ? 1 : 0);
  });

  it("saves once on a release, though the pointer's capture ends right after", () => {
    const { element, onCommit, onAbort } = renderDivider();
    fireEvent.pointerDown(element, { pointerId: 1 });
    fireEvent.pointerUp(element, { pointerId: 1 });
    fireEvent.lostPointerCapture(element, { pointerId: 1 });
    expect(onCommit).toHaveBeenCalledOnce();
    expect(onAbort).not.toHaveBeenCalled();
  });

  it("keeps its drag through another finger's press, lifting or cancelling", () => {
    const { element, onCommit, onAbort } = renderDivider();
    fireEvent.pointerDown(element, { pointerId: 1, button: 0 });
    fireEvent.pointerDown(element, { pointerId: 2, button: 0 });
    expect(element.setPointerCapture).toHaveBeenCalledTimes(1);
    fireEvent.pointerUp(element, { pointerId: 2 });
    fireEvent.pointerCancel(element, { pointerId: 2 });
    expect(paneDividerDrag.dragging()).toBe(true);
    expect(onCommit).not.toHaveBeenCalled();
    expect(onAbort).not.toHaveBeenCalled();
    fireEvent.pointerUp(element, { pointerId: 1 });
    expect(onCommit).toHaveBeenCalledOnce();
    expect(paneDividerDrag.dragging()).toBe(false);
  });

  it.each([
    [1, "middle"],
    [2, "right"],
  ])("isn't a drag from button %i, the %s button", (button) => {
    const { element, onCommit, onAbort } = renderDivider();
    fireEvent.pointerDown(element, { pointerId: 1, button });
    expect(paneDividerDrag.dragging()).toBe(false);
    expect(element.setPointerCapture).not.toHaveBeenCalled();
    fireEvent.pointerUp(element, { pointerId: 1, button });
    expect(onCommit).not.toHaveBeenCalled();
    expect(onAbort).not.toHaveBeenCalled();
  });

  it("lets the pointer go once the layout ends its drag", () => {
    const { element, rerender, onCommit, onAbort } = renderDivider();
    fireEvent.pointerDown(element, { pointerId: 1 });
    rerender(true);
    expect(element.releasePointerCapture).not.toHaveBeenCalled();
    rerender(false);
    expect(element.releasePointerCapture).toHaveBeenCalledWith(1);
    expect(paneDividerDrag.dragging()).toBe(false);
    // Letting it go ends its capture, which ends nothing more.
    fireEvent.lostPointerCapture(element, { pointerId: 1 });
    expect(onCommit).not.toHaveBeenCalled();
    expect(onAbort).not.toHaveBeenCalled();
  });

  it("ends its drag when it goes mid-drag", () => {
    const { element, unmount } = renderDivider();
    fireEvent.pointerDown(element, { pointerId: 1 });
    unmount();
    expect(paneDividerDrag.dragging()).toBe(false);
  });

  it("isn't a drag from the keyboard", () => {
    const { element } = renderDivider();
    fireEvent.keyDown(element, { key: "ArrowRight" });
    expect(paneDividerDrag.dragging()).toBe(false);
  });
});

describe("PaneSplitDivider keys", () => {
  const render50 = (ratio: number, onResize = vi.fn()) => {
    render(
      <PaneSplitDivider
        divider={{
          axis: "columns",
          area: { left: 0, top: 0, width: 100, height: 100 },
          ratio,
          path: [],
        }}
        containerRef={{ current: document.body }}
        onResize={onResize}
        onCommit={vi.fn()}
      />,
    );
    return { onResize, element: screen.getByRole("separator") };
  };

  afterEach(cleanup);

  it("steps the divider along its axis, a final resize each time", () => {
    const { onResize, element } = render50(0.5);
    fireEvent.keyDown(element, { key: "ArrowRight" });
    expect(onResize).toHaveBeenLastCalledWith([], 0.53, true);
    fireEvent.keyDown(element, { key: "ArrowUp" });
    expect(onResize).toHaveBeenCalledTimes(1);
  });

  it("lets a proportional split past the limits step back in, never further out", () => {
    const { onResize, element } = render50(0.9);
    expect(element).toHaveAttribute("aria-valuemax", "90");
    fireEvent.keyDown(element, { key: "ArrowRight" });
    expect(onResize).not.toHaveBeenCalled();
    fireEvent.keyDown(element, { key: "ArrowLeft" });
    expect(onResize).toHaveBeenLastCalledWith([], 0.85, true);
  });
});
