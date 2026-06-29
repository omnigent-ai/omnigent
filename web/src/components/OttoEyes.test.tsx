import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OttoEyes } from "./OttoEyes";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("OttoEyes", () => {
  it("renders the mascot with image semantics", () => {
    const { container } = render(<OttoEyes className="h-18" />);
    const svg = container.querySelector("svg");
    // The new-chat hero is a meaningful image, so the wrapper must override
    // OttoIcon's decorative aria-hidden default; losing the override would
    // silently hide the brand image from screen readers.
    expect(svg).toHaveAttribute("role", "img");
    expect(svg).toHaveAttribute("aria-label", "Omnigent");
    expect(svg).toHaveAttribute("aria-hidden", "false");
    expect(svg).toHaveClass("h-18");
  });

  it("slides both pupils toward the pointer", async () => {
    const { container } = render(<OttoEyes />);
    const svg = container.querySelector("svg");
    if (!svg) throw new Error("OttoEyes did not render an svg");
    // jsdom layout is zero-sized, which the effect treats as "not rendered";
    // give the svg a real box so the screen-space math runs.
    vi.spyOn(svg, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 100,
      bottom: 100,
      width: 100,
      height: 100,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);
    // The handler only reads clientX/clientY; jsdom has no PointerEvent
    // constructor, and a MouseEvent dispatched as "pointermove" reaches the
    // same listener. y=50.84 ≈ the eye-center row (520.6/1024 × 100).
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 1000, clientY: 50.84 }));
    // The transforms are written in a rAF callback scheduled by the handler;
    // awaiting our own, later-scheduled frame sequences the test behind it.
    await new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));

    const pupils = Array.from(container.querySelectorAll<SVGGElement>("g.otto-pupil"));
    // Both of Otto's pupils must move; 0 or 1 means the otto-pupil class
    // contract with OttoIcon (or the ref forwarding) broke.
    expect(pupils).toHaveLength(2);
    for (const pupil of pupils) {
      const match = pupil.style.transform.match(/^translate\((-?[\d.]+)px, (-?[\d.]+)px\)$/) ?? [];
      // The pointer sits far to the right of both eye centers on their shared
      // row, so each pupil rides the rim right by the full MAX_OFFSET
      // (~9.3 viewBox units) with ~zero vertical drift. 3 = full regex match
      // + the tx/ty capture groups; an empty array (length 0) means the
      // pointermove → rAF pipeline never wrote a transform to this pupil.
      expect(match).toHaveLength(3);
      expect(Number(match[1])).toBeCloseTo(9.3, 1);
      expect(Math.abs(Number(match[2]))).toBeLessThan(0.1);
    }
  });
});
