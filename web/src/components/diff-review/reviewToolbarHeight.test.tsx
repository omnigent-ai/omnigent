import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { installResizableWidth } from "@/test/resizableWidth";

import { createReviewToolbarHeights, RecordedReviewToolbar } from "./reviewToolbarHeight";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("a review's remembered toolbar heights", () => {
  it("answers the height drawn at a width, to the pixel", () => {
    const heights = createReviewToolbarHeights();
    heights.remember(320.4, 62.6);
    expect(heights.at(320)).toBe(63);
    expect(heights.at(319.6)).toBe(63);
    expect(heights.at(321)).toBeNull();
  });

  it("knows nothing for an unmeasured width or a review that drew nothing", () => {
    const heights = createReviewToolbarHeights();
    heights.remember(0, 37);
    heights.remember(400, 0);
    expect(heights.at(null)).toBeNull();
    expect(heights.at(0)).toBeNull();
    expect(heights.at(400)).toBeNull();
  });

  it("keeps the latest height at a width, and forgets the widths drawn longest ago", () => {
    const heights = createReviewToolbarHeights();
    heights.remember(300, 63);
    heights.remember(300, 37);
    expect(heights.at(300)).toBe(37);
    for (let width = 400; width < 1200; width += 100) heights.remember(width, 37);
    expect(heights.at(300)).toBeNull();
    expect(heights.at(400)).toBe(37);
    expect(heights.at(1100)).toBe(37);
  });
});

describe("a recorded review toolbar", () => {
  it("records its height at the review's width as it draws and as the review resizes", () => {
    const resize = installResizableWidth(320);
    let height = 63;
    const measured = vi.mocked(HTMLElement.prototype.getBoundingClientRect);
    const widthOnly = measured.getMockImplementation()!;
    measured.mockImplementation(function (this: HTMLElement) {
      return { ...widthOnly.call(this), height } as DOMRect;
    });
    const heights = createReviewToolbarHeights();
    render(
      <RecordedReviewToolbar heights={heights}>
        <div role="toolbar" aria-label="Diff review" />
      </RecordedReviewToolbar>,
    );
    // A narrow review's toolbar wrapped to a second row.
    expect(heights.at(320)).toBe(63);
    height = 37;
    resize(640);
    expect(heights.at(640)).toBe(37);
    expect(heights.at(320)).toBe(63);
  });
});
