import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { OttoIcon } from "./OttoIcon";

afterEach(cleanup);

describe("OttoIcon", () => {
  it("exposes four otto-eye groups for the blink animation", () => {
    const { container } = render(<OttoIcon />);
    // The blink keyframes target `.otto-working .otto-eye` in index.css; CSS
    // selectors fail silently, so renaming/flattening these groups would
    // freeze the eyes with no other signal.
    const eyes = container.querySelectorAll("svg > g.otto-eye");
    // 4 = Otto's two eyes + the buddy starfish's two; dropping the buddy's
    // groups would leave it staring unblinking next to a blinking Otto.
    expect(eyes).toHaveLength(4);
    // 3 paths per eye = sclera + pupil + highlight; losing one shifts the
    // group's fill-box bounds and the blink no longer collapses on center.
    for (const eye of eyes) {
      expect(eye.querySelectorAll("path")).toHaveLength(3);
    }
  });

  it("wraps only Otto's two pupils in otto-pupil groups for cursor tracking", () => {
    const { container } = render(<OttoIcon />);
    // OttoEyes finds these groups by class through the forwarded ref;
    // querySelectorAll fails silently, so renaming the class (or adding
    // groups to the buddy's eyes) would break or skew tracking with no
    // other signal. 2 = Otto's eyes only — the buddy stays still.
    const pupils = container.querySelectorAll("svg g.otto-eye > g.otto-pupil");
    expect(pupils).toHaveLength(2);
    // 2 paths per group = black disc + glint. The sclera must stay outside
    // the group, or it would slide along with the pupil instead of framing it.
    for (const pupil of pupils) {
      expect(pupil.querySelectorAll("path")).toHaveLength(2);
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
    // Decorative by default; the pin's aria-live region must only
    // ever announce the "Working…" text.
    expect(svg).toHaveAttribute("aria-hidden", "true");
  });

  it("lets callers override aria-hidden for the new-chat hero render", () => {
    const { container } = render(<OttoIcon role="img" aria-label="Omnigent" aria-hidden={false} />);
    const svg = container.querySelector("svg");
    // NewChatDialog renders the mascot as a meaningful image; the override
    // only works while the spread stays after the aria-hidden default.
    expect(svg).toHaveAttribute("aria-hidden", "false");
    expect(svg).toHaveAttribute("aria-label", "Omnigent");
  });
});
