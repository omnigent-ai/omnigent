import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AppShellSkeleton } from "./AppShellSkeleton";

afterEach(cleanup);

describe("AppShellSkeleton", () => {
  it("exposes a loading status for assistive tech", () => {
    render(<AppShellSkeleton />);
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("paints the shell silhouette (rail + header + central placeholder)", () => {
    const { container } = render(<AppShellSkeleton />);
    // The outer node carries the app-shell canvas class so the gradient
    // matches the real shell and the swap doesn't flash a different bg.
    expect(container.querySelector(".app-shell")).not.toBeNull();
    // Desktop conversations rail silhouette.
    expect(container.querySelector("aside")).not.toBeNull();
  });

  it("only pulses when motion is allowed (prefers-reduced-motion safe)", () => {
    const { container } = render(<AppShellSkeleton />);
    const animated = container.querySelectorAll(".motion-safe\\:animate-pulse");
    // Several placeholder blocks shimmer; none use a bare animate-pulse that
    // would ignore prefers-reduced-motion.
    expect(animated.length).toBeGreaterThan(0);
    expect(container.querySelector(".animate-pulse:not(.motion-safe\\:animate-pulse)")).toBeNull();
  });
});
