import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { RunningDot } from "./RunningDot";

afterEach(cleanup);

describe("RunningDot", () => {
  it("spins an HTML wrapper, not the svg, so Chrome can composite it on HiDPI screens", () => {
    render(<RunningDot className="size-8" />);
    const dot = screen.getByTestId("running-dot");
    expect(dot.tagName).toBe("SPAN");
    expect(dot).toHaveClass("animate-spin", "size-8", "text-muted-foreground");
    expect(dot).toHaveAttribute("aria-hidden", "true");
    expect(dot).toHaveAttribute("role", "presentation");
    const svg = dot.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg).not.toHaveClass("animate-spin");
    expect(svg).toHaveClass("size-full");
  });

  it("defaults the wrapper to size-3 when no className is passed", () => {
    render(<RunningDot />);
    const dot = screen.getByTestId("running-dot");
    expect(dot).toHaveClass("size-3", "animate-spin");
    expect(dot.querySelector("svg")).toHaveClass("size-full");
  });
});
