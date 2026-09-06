import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LandingStep } from "./LandingStep";

afterEach(cleanup);

describe("LandingStep", () => {
  it("fires onGetStarted / onJoinServer for the two actions", () => {
    const onGetStarted = vi.fn();
    const onJoinServer = vi.fn();
    render(<LandingStep onGetStarted={onGetStarted} onJoinServer={onJoinServer} />);

    fireEvent.click(screen.getByRole("button", { name: /get started/i }));
    expect(onGetStarted).toHaveBeenCalledOnce();
    expect(onJoinServer).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /join a server/i }));
    expect(onJoinServer).toHaveBeenCalledOnce();
  });
});
