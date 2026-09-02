import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SetupTerminalStep } from "./SetupTerminalStep";

afterEach(cleanup);

describe("SetupTerminalStep", () => {
  it("starts the local server on mount and shows Ready on success", async () => {
    const onStartLocal = vi.fn().mockResolvedValue({ ok: true });
    render(<SetupTerminalStep onStartLocal={onStartLocal} onBack={vi.fn()} />);

    expect(onStartLocal).toHaveBeenCalledOnce();
    expect(await screen.findByText("Server ready")).toBeInTheDocument();
    // No failure controls on success.
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("shows the error and a Retry that starts the server again on failure", async () => {
    const onStartLocal = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, error: "omnigent CLI not found" })
      .mockResolvedValueOnce({ ok: true });
    render(<SetupTerminalStep onStartLocal={onStartLocal} onBack={vi.fn()} />);

    expect(await screen.findByText("omnigent CLI not found")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onStartLocal).toHaveBeenCalledTimes(2);
    expect(await screen.findByText("Server ready")).toBeInTheDocument();
  });

  it("fires onBack from Back on the failure screen", async () => {
    const onBack = vi.fn();
    render(
      <SetupTerminalStep onStartLocal={vi.fn().mockResolvedValue({ ok: false })} onBack={onBack} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(onBack).toHaveBeenCalledOnce();
  });
});
