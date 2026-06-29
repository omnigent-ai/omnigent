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

function Harness({ value }: { value: string }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useAutoGrowTextarea(ref, value);
  // Inline line-height/padding so getComputedStyle returns real numbers
  // (jsdom reflects inline styles), letting the maxHeight math run.
  return (
    <textarea
      ref={ref}
      data-testid="ta"
      defaultValue={value}
      style={{ lineHeight: "20px", paddingTop: "0px", paddingBottom: "0px" }}
    />
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
});
