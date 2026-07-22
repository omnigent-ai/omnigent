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

  it("keeps both pupils centered when the pointer moves", () => {
    const { container } = render(<OttoEyes />);
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 1000, clientY: 50.84 }));

    const pupils = Array.from(container.querySelectorAll<SVGGElement>("g.otto-pupil"));
    expect(pupils).toHaveLength(2);
    for (const pupil of pupils) {
      expect(pupil.style.transform).toBe("");
    }
  });
});
