import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { composerInputOverflows, useComposerInputOverflow } from "./useComposerInputOverflow";

function sized(element: HTMLElement, scrollHeight: number, clientHeight: number): HTMLElement {
  Object.defineProperty(element, "scrollHeight", { configurable: true, value: scrollHeight });
  Object.defineProperty(element, "clientHeight", { configurable: true, value: clientHeight });
  return element;
}

afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("composerInputOverflows", () => {
  it("is false while the area and its input show the whole draft", () => {
    const area = sized(document.createElement("div"), 100, 100);
    area.append(sized(document.createElement("textarea"), 60, 60));
    expect(composerInputOverflows(area)).toBe(false);
  });

  it("ignores a sub-pixel rounding difference", () => {
    const area = sized(document.createElement("div"), 101, 100);
    expect(composerInputOverflows(area)).toBe(false);
  });

  it("is true once the textarea scrolls", () => {
    const area = sized(document.createElement("div"), 200, 200);
    area.append(sized(document.createElement("textarea"), 400, 180));
    expect(composerInputOverflows(area)).toBe(true);
  });

  it("is true once the CodeMirror scroller scrolls", () => {
    const area = sized(document.createElement("div"), 200, 200);
    const scroller = sized(document.createElement("div"), 400, 180);
    scroller.className = "cm-scroller";
    area.append(scroller);
    expect(composerInputOverflows(area)).toBe(true);
  });

  it("is true once the area itself scrolls, as it does with quote blocks", () => {
    const area = sized(document.createElement("div"), 500, 320);
    expect(composerInputOverflows(area)).toBe(true);
  });
});

/** A ResizeObserver that reports a resize only to the observers watching that element. */
class FakeResizeObserver {
  static all: FakeResizeObserver[] = [];
  readonly watched = new Set<Element>();
  private readonly callback: ResizeObserverCallback;
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    FakeResizeObserver.all.push(this);
  }
  observe(element: Element) {
    this.watched.add(element);
  }
  unobserve(element: Element) {
    this.watched.delete(element);
  }
  disconnect() {
    this.watched.clear();
  }
  static resize(element: Element) {
    for (const observer of FakeResizeObserver.all) {
      if (observer.watched.has(element)) {
        observer.callback([], observer as unknown as ResizeObserver);
      }
    }
  }
}

describe("useComposerInputOverflow", () => {
  function watchArea(...inputs: HTMLElement[]) {
    FakeResizeObserver.all = [];
    vi.stubGlobal("ResizeObserver", FakeResizeObserver);
    vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame"] });
    const area = sized(document.createElement("div"), 200, 200);
    area.append(...inputs);
    document.body.append(area);
    const view = renderHook(() => useComposerInputOverflow({ current: area }, true));
    return { area, view };
  }
  /** The input's box shrinks to `clientHeight`, while the area around it keeps its size. */
  const shrink = (input: HTMLElement, clientHeight: number) => {
    sized(input, input.scrollHeight, clientHeight);
    act(() => {
      FakeResizeObserver.resize(input);
      vi.advanceTimersByTime(16);
    });
  };

  it("sees a textarea that shrinks inside an area that doesn't", () => {
    const textarea = sized(document.createElement("textarea"), 120, 120);
    const { view } = watchArea(textarea);
    expect(view.result.current).toBe(false);

    shrink(textarea, 60);

    expect(view.result.current).toBe(true);
  });

  it("watches the CodeMirror scroller that replaces the textarea, and stops watching the textarea", async () => {
    const textarea = sized(document.createElement("textarea"), 120, 120);
    const { area, view } = watchArea(textarea);
    const scroller = sized(document.createElement("div"), 120, 120);
    scroller.className = "cm-scroller";
    textarea.replaceWith(scroller);
    // The area's mutation is heard on a microtask, and measured on the next frame.
    await act(async () => {});
    act(() => vi.advanceTimersByTime(16));
    expect(view.result.current).toBe(false);
    expect(FakeResizeObserver.all.some((observer) => observer.watched.has(textarea))).toBe(false);
    expect(area.contains(scroller)).toBe(true);

    shrink(scroller, 60);

    expect(view.result.current).toBe(true);
  });
});
