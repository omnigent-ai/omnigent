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
        onAddServer={vi.fn()}
        onJoinManaged={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /get started locally/i }));
    expect(onGetStarted).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: /join your team/i }));
    expect(onJoinServer).toHaveBeenCalledOnce();
  });

  it("makes the first preset's split button the only CTA", () => {
    const onJoinManaged = vi.fn();
    render(
      <LandingStep
        managedServers={["https://team.example.com/omnigent?o=1"]}
        onGetStarted={vi.fn()}
        onJoinServer={vi.fn()}
        onAddServer={vi.fn()}
        onJoinManaged={onJoinManaged}
      />,
    );

    // Primary button names the first preset's capitalized host label.
    fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
    expect(onJoinManaged).toHaveBeenCalledWith("https://team.example.com/omnigent?o=1");
    expect(screen.queryByRole("button", { name: /get started locally/i })).not.toBeInTheDocument();
  });

  it("lists the other presets and a URL entry in the dropdown", () => {
    const onJoinManaged = vi.fn();
    const onAddServer = vi.fn();
    render(
      <LandingStep
        managedServers={["https://team.example.com", "https://other.example.com/"]}
        onGetStarted={vi.fn()}
        onJoinServer={vi.fn()}
        onAddServer={onAddServer}
        onJoinManaged={onJoinManaged}
      />,
    );
    fireEvent.pointerDown(screen.getByRole("button", { name: /choose team url/i }), { button: 0 });
    // The first preset is the main button, so it isn't repeated here.
    expect(screen.getAllByRole("menuitem").map((i) => i.textContent)).toEqual([
      "other.example.com",
      "Enter Omnigent server URL…",
    ]);
    fireEvent.click(screen.getByRole("menuitem", { name: "other.example.com" }));
    expect(onJoinManaged).toHaveBeenCalledWith("https://other.example.com/");

    fireEvent.pointerDown(screen.getByRole("button", { name: /choose team url/i }), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: /enter omnigent server url/i }));
    expect(onAddServer).toHaveBeenCalledOnce();
  });
});
