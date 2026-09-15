import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModeSelectStep } from "./ModeSelectStep";

afterEach(cleanup);

describe("ModeSelectStep", () => {
  it("defaults to Local with a Begin CTA that fires onBegin", () => {
    const onBegin = vi.fn();
    const onCloudSetup = vi.fn();
    render(<ModeSelectStep onBack={vi.fn()} onBegin={onBegin} onCloudSetup={onCloudSetup} />);

    // Local is the checked radio by default.
    const local = screen.getByRole("radio", { name: /^Local/ });
    expect(local).toHaveAttribute("aria-checked", "true");

    fireEvent.click(screen.getByRole("button", { name: "Begin" }));
    expect(onBegin).toHaveBeenCalledOnce();
    expect(onCloudSetup).not.toHaveBeenCalled();
  });

  it("selecting Cloud swaps the CTA to Server setup → onCloudSetup", () => {
    const onBegin = vi.fn();
    const onCloudSetup = vi.fn();
    render(<ModeSelectStep onBack={vi.fn()} onBegin={onBegin} onCloudSetup={onCloudSetup} />);

    fireEvent.click(screen.getByRole("radio", { name: /^Cloud/ }));
    expect(screen.getByRole("radio", { name: /^Cloud/ })).toHaveAttribute("aria-checked", "true");
    // Begin is gone; Server setup drives the cloud path.
    expect(screen.queryByRole("button", { name: "Begin" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /server setup/i }));
    expect(onCloudSetup).toHaveBeenCalledOnce();
    expect(onBegin).not.toHaveBeenCalled();
  });

  it("fires onBack from Back", () => {
    const onBack = vi.fn();
    render(<ModeSelectStep onBack={onBack} onBegin={vi.fn()} onCloudSetup={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(onBack).toHaveBeenCalledOnce();
  });
});
