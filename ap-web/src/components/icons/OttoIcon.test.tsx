import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { OttoIcon } from "./OttoIcon";

afterEach(cleanup);

describe("OttoIcon", () => {
  it("exposes two otto-eye groups for the blink animation", () => {
    const { container } = render(<OttoIcon />);
    // The blink keyframes target `.otto-working .otto-eye` in index.css; CSS
    // selectors fail silently, so renaming/flattening these groups would
    // freeze the eyes with no other signal.
    const eyes = container.querySelectorAll("svg > g.otto-eye");
    expect(eyes).toHaveLength(2);
    // 3 paths per eye = sclera + pupil + highlight; losing one shifts the
    // group's fill-box bounds and the blink no longer collapses on center.
    for (const eye of eyes) {
      expect(eye.querySelectorAll("path")).toHaveLength(3);
    }
  });

  it("spreads props onto the root svg and stays hidden from screen readers", () => {
    const { container } = render(<OttoIcon className="otto-working h-4" />);
    const svg = container.querySelector("svg");
    // The animation is opt-in via className, so the spread must reach the root.
    expect(svg).toHaveClass("otto-working");
    // The art's coordinate space; consumers size via className so a viewBox
    // change silently distorts the mascot everywhere.
    expect(svg).toHaveAttribute("viewBox", "0 0 1024 1024");
    // Decorative in both render sites; the pin's aria-live region must only
    // ever announce the "Working…" text.
    expect(svg).toHaveAttribute("aria-hidden", "true");
  });
});
