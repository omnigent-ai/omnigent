import { act, cleanup, render } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useElementWidth } from "./useElementWidth";

const painted: number[] = [];

/** An element whose `clientWidth` is the width the render gave it. */
function Measured({ width }: { width: number }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const measured = useElementWidth(ref);
  painted.push(measured);
  return (
    <div
      ref={(element) => {
        ref.current = element;
        if (element)
          Object.defineProperty(element, "clientWidth", { configurable: true, value: width });
      }}
    />
  );
}

/**
 * A layout that changes its own width with the width it measured, as a table that drops
 * columns when narrow and so becomes wider: each measurement asks for the other width.
 */
function SelfResizing() {
  const ref = useRef<HTMLDivElement | null>(null);
  const measured = useElementWidth(ref);
  painted.push(measured);
  const width = measured === 400 ? 600 : 400;
  return (
    <div
      ref={(element) => {
        ref.current = element;
        if (element)
          Object.defineProperty(element, "clientWidth", { configurable: true, value: width });
      }}
    />
  );
}

/** An element the test resizes itself, as the browser would between renders. */
function Observed() {
  const ref = useRef<HTMLDivElement | null>(null);
  painted.push(useElementWidth(ref));
  return <div data-testid="observed" ref={ref} />;
}

function setClientWidth(element: Element, width: number) {
  Object.defineProperty(element, "clientWidth", { configurable: true, value: width });
}

/** Shows one of two elements, as a view that swaps its measured container. */
function Swapping({ which }: { which: "first" | "second" }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useElementWidth(ref);
  return which === "first" ? (
    <div key="first" data-which="first" ref={ref} />
  ) : (
    <div key="second" data-which="second" ref={ref} />
  );
}

/** A ResizeObserver stand-in that records what it watches and can report a resize. */
function stubResizeObserver() {
  const watched = new Set<Element>();
  const callbacks = new Map<Element, () => void>();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      private element: Element | null = null;
      private readonly callback: () => void;
      constructor(callback: () => void) {
        this.callback = callback;
      }
      observe(element: Element) {
        this.element = element;
        watched.add(element);
        callbacks.set(element, this.callback);
      }
      disconnect() {
        if (this.element) watched.delete(this.element);
      }
    },
  );
  return { watched, resize: (element: Element) => act(() => callbacks.get(element)?.()) };
}

afterEach(() => {
  cleanup();
  painted.length = 0;
  vi.unstubAllGlobals();
});

describe("useElementWidth", () => {
  it("reads a width the same commit gave the element, before the observer reports it", () => {
    // An observer that never reports, as before the browser's next frame.
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
      },
    );
    const view = render(<Measured width={520} />);
    expect(painted.at(-1)).toBe(520);

    view.rerender(<Measured width={1280} />);
    expect(painted.at(-1)).toBe(1280);
  });

  it("settles when a layout's width follows the width it measured", () => {
    stubResizeObserver();
    expect(() => render(<SelfResizing />)).not.toThrow();
    // The first measurement re-renders once; the observer reports any further change.
    expect(painted.length).toBeLessThanOrEqual(3);
  });

  it("takes a resize the observer reports after its own render, and settles again", () => {
    const observer = stubResizeObserver();
    const view = render(<Observed />);
    const element = view.getByTestId("observed");
    setClientWidth(element, 520);
    observer.resize(element);
    expect(painted.at(-1)).toBe(520);

    // A resize between renders reaches the layout only through the observer.
    setClientWidth(element, 900);
    const before = painted.length;
    observer.resize(element);
    expect(painted.at(-1)).toBe(900);
    expect(painted.length).toBe(before + 1);
  });

  it("keeps settling when the observer reports a self-resizing layout", () => {
    const observer = stubResizeObserver();
    const view = render(<SelfResizing />);
    const element = view.container.firstElementChild!;
    const before = painted.length;
    expect(() => observer.resize(element)).not.toThrow();
    // One render for the report, at most one more for its measurement.
    expect(painted.length - before).toBeLessThanOrEqual(2);
  });

  it("measures a later resize after a render that changed nothing", () => {
    stubResizeObserver();
    const view = render(<Measured width={520} />);
    view.rerender(<Measured width={520} />);
    view.rerender(<Measured width={700} />);
    expect(painted.at(-1)).toBe(700);
  });

  it("follows the element when the ref moves to another one", () => {
    const observer = stubResizeObserver();
    const view = render(<Swapping which="first" />);
    const first = view.container.querySelector('[data-which="first"]')!;
    expect([...observer.watched]).toEqual([first]);

    view.rerender(<Swapping which="second" />);
    const second = view.container.querySelector('[data-which="second"]')!;
    expect([...observer.watched]).toEqual([second]);

    view.unmount();
    expect(observer.watched.size).toBe(0);
  });

  it("doesn't render again when the width is unchanged", () => {
    const view = render(<Measured width={520} />);
    const renders = painted.length;
    view.rerender(<Measured width={520} />);
    expect(painted.length).toBe(renders + 1);
  });
});
