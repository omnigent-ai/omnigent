import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LandingStep } from "./LandingStep";

afterEach(cleanup);

describe("LandingStep", () => {
  it("fires onGetStarted / onJoinServer without presets", () => {
    const onGetStarted = vi.fn();
    const onJoinServer = vi.fn();
    render(
      <LandingStep
        managedServers={[]}
        onGetStarted={onGetStarted}
        onJoinServer={onJoinServer}
        onJoinManaged={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /get started locally/i }));
    expect(onGetStarted).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: /join your team/i }));
    expect(onJoinServer).toHaveBeenCalledOnce();
  });

  it("shows the preset split button with presets", () => {
    const onJoinManaged = vi.fn();
    render(
      <LandingStep
        managedServers={["https://field-eng-omni.aws.databricksapps.com"]}
        onGetStarted={vi.fn()}
        onJoinServer={vi.fn()}
        onJoinManaged={onJoinManaged}
      />,
    );

    // Primary button names the first preset's short name.
    fireEvent.click(screen.getByRole("button", { name: /join your team \(field-eng-omni\)/i }));
    expect(onJoinManaged).toHaveBeenCalledWith("https://field-eng-omni.aws.databricksapps.com");
  });

  it("offers 'Show all servers…' in the preset dropdown → onJoinServer", () => {
    const onJoinServer = vi.fn();
    render(
      <LandingStep
        managedServers={["https://field-eng-omni.aws.databricksapps.com"]}
        onGetStarted={vi.fn()}
        onJoinServer={onJoinServer}
        onJoinManaged={vi.fn()}
      />,
    );
    fireEvent.pointerDown(screen.getByRole("button", { name: /choose team url/i }), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: /show all servers/i }));
    expect(onJoinServer).toHaveBeenCalledOnce();
  });
});
