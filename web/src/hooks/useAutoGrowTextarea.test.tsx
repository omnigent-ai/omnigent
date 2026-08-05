import { render } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAutoGrowTextarea } from "./useAutoGrowTextarea";

// A controllable ResizeObserver stand-in. jsdom has none, so the hook's
// re-measure path is dead unless we provide one. We capture the callback so
// the test can fire it to simulate "layout settled".
let roCallback: ResizeObserverCallback | null = null;
class FakeResizeObserver {
  constructor(cb: ResizeObserverCallback) {
    roCallback = cb;
  }
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

/**
 * Override the element's ``scrollHeight`` (jsdom reports 0 — no layout).
 * Set ``px`` to simulate a laid-out element of that content height.
 */
function stubScrollHeight(el: HTMLElement, px: number): void {
  Object.defineProperty(el, "scrollHeight", {
    configurable: true,
    get: () => px,
  });
}

function Harness({ value, onGrowth }: { value: string; onGrowth?: (px: number) => void }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useAutoGrowTextarea(ref, value, 10, onGrowth);
  // Inline line-height/padding so getComputedStyle returns real numbers
  // (jsdom reflects inline styles), letting the maxHeight math run. The
  // wrapper mirrors both composers, where the textarea sits in a box of its
  // own that the hook pins while it collapses to measure.
  return (
    <div data-testid="wrapper">
      <textarea
        ref={ref}
        data-testid="ta"
        defaultValue={value}
        style={{ lineHeight: "20px", paddingTop: "0px", paddingBottom: "0px" }}
      />
    </div>
  );
}

beforeEach(() => {
  roCallback = null;
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useAutoGrowTextarea", () => {
  it("does not collapse the height when the element is not laid out yet", () => {
    // Reproduces the post-delete route swap: ChatPage stays mounted and
    // swaps in the landing composer, so the fresh textarea mounts before
    // layout settles and scrollHeight reads 0. The hook must NOT lock in a
    // collapsed/NaN height here — that clipped the placeholder (the bug).
    const { getByTestId } = render(<Harness value="" />);
    const ta = getByTestId("ta") as HTMLTextAreaElement;
    // scrollHeight defaults to 0 in jsdom (no layout). The guard leaves the
    // height at "auto" rather than setting "0px"/"NaNpx". A failure here
    // (e.g. "0px") means the guard regressed and the placeholder would clip.
    expect(ta.style.height).toBe("auto");
  });

  it("re-measures once layout settles and the ResizeObserver fires", () => {
    const { getByTestId } = render(<Harness value="" />);
    const ta = getByTestId("ta") as HTMLTextAreaElement;
    // Layout has now settled: the content box is one 20px line tall.
    stubScrollHeight(ta, 20);
    expect(roCallback).not.toBeNull();
    // Fire the observer the way the browser would after the box gains size.
    roCallback?.([], {} as ResizeObserver);
    // 20px content fits under the 10-row cap, so height tracks scrollHeight
    // exactly. A failure (still "auto") means the observer isn't wired up and
    // the box would never recover from the mid-swap 0-height measurement.
    expect(ta.style.height).toBe("20px");
  });

  it("caps the height at maxRows and then lets the textarea scroll", () => {
    const { getByTestId } = render(<Harness value="x" />);
    const ta = getByTestId("ta") as HTMLTextAreaElement;
    // 40 lines of 20px = 800px content, far past the 10-row cap.
    stubScrollHeight(ta, 800);
    roCallback?.([], {} as ResizeObserver);
    // Capped at 10 rows * 20px lineHeight + 0 padding = 200px, so a long
    // prompt scrolls instead of growing without bound. A larger value means
    // the maxRows clamp regressed.
    expect(ta.style.height).toBe("200px");
  });

  it("holds the wrapper's height while the textarea collapses to measure", () => {
    // The collapse to "auto" is a one-row box. Left free to reach the page it
    // briefly shortens the composer, and the browser clamps the transcript's
    // scrollTop against the taller viewport that opens up — a clamp that
    // sticks, shunting the chat down a line on every keystroke. jsdom can't
    // reproduce the clamp (no layout), so assert the invariant that prevents
    // it: the wrapper's box is fixed for as long as the textarea is collapsed.
    const { getByTestId } = render(<Harness value="two\nlines" />);
    const ta = getByTestId("ta") as HTMLTextAreaElement;
    const wrapper = getByTestId("wrapper") as HTMLDivElement;
    wrapper.getBoundingClientRect = () => ({ height: 100 }) as DOMRect;

    // scrollHeight is read while the textarea is collapsed, so it's the one
    // moment that sees the layout the rest of the page would have seen.
    let wrapperHeightWhileCollapsed: string | null = null;
    Object.defineProperty(ta, "scrollHeight", {
      configurable: true,
      get: () => {
        wrapperHeightWhileCollapsed = wrapper.style.height;
        return 40;
      },
    });
    roCallback?.([], {} as ResizeObserver);

    expect(wrapperHeightWhileCollapsed).toBe("100px");
    // Released again once the real height is on, so the wrapper goes back to
    // tracking its content instead of freezing at the pinned value.
    expect(wrapper.style.height).toBe("");
    expect(ta.style.height).toBe("40px");
  });

  it("reports no growth while the element has no layout", () => {
    // Mid route swap scrollHeight reads 0 and the height is left alone. The
    // growth has to be published anyway: a caller offsetting its layout by the
    // last value would otherwise hold that offset until the next measure.
    const growth: number[] = [];
    render(<Harness value="two rows" onGrowth={(px) => growth.push(px)} />);
    expect(growth).toEqual([0]);
  });

  it("reports how far past one row the box has grown", () => {
    // The in-session composer subtracts this from its own top margin, keeping
    // the extra rows out of the flex column so the transcript's scroll
    // viewport — and the scrollbar and turn rail drawn from it — hold still.
    const growth: number[] = [];
    const { getByTestId } = render(<Harness value="x" onGrowth={(px) => growth.push(px)} />);
    const ta = getByTestId("ta") as HTMLTextAreaElement;

    // One 20px row: resting height, nothing to float.
    stubScrollHeight(ta, 20);
    roCallback?.([], {} as ResizeObserver);
    expect(growth.at(-1)).toBe(0);

    // Four rows: three of them past resting.
    stubScrollHeight(ta, 80);
    roCallback?.([], {} as ResizeObserver);
    expect(growth.at(-1)).toBe(60);

    // Past the 10-row cap the box scrolls instead of growing, so the reported
    // growth caps with it rather than tracking the content.
    stubScrollHeight(ta, 800);
    roCallback?.([], {} as ResizeObserver);
    expect(growth.at(-1)).toBe(180);
  });
});
