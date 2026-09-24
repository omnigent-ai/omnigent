import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ServerSelectorV2, type ServerSelectorV2Setup } from "./ServerSelectorV2";

afterEach(cleanup);

function makeSetup(over: Partial<ServerSelectorV2Setup> = {}): ServerSelectorV2Setup {
  return {
    initialUrl: "http://localhost:6767",
    recentServers: [],
    managedServers: [],
    onConnect: vi.fn().mockResolvedValue({}),
    onStartLocal: vi.fn().mockResolvedValue({ ok: true }),
    onCopy: vi.fn(),
    onCheckServer: vi.fn().mockResolvedValue({ status: "ok" }),
    onCloudSetup: vi.fn(),
    onSwitchToLegacy: vi.fn(),
    ...over,
  };
}

describe("ServerSelectorV2", () => {
  it("starts on the landing step", () => {
    render(<ServerSelectorV2 setup={makeSetup()} />);
    expect(screen.getByRole("heading", { name: "Meet Omnigent" })).toBeInTheDocument();
  });

  it("Get started locally shows the local intro (not install yet)", () => {
    render(<ServerSelectorV2 setup={makeSetup()} />);
    fireEvent.click(screen.getByRole("button", { name: /get started locally/i }));
    // Local/Cloud switcher was replaced by a single local intro; install starts
    // only after clicking Install Omnigent.
    expect(screen.getByRole("heading", { name: /set up omnigent locally/i })).toBeInTheDocument();
    expect(screen.queryByText(/starting the local server/i)).not.toBeInTheDocument();
  });

  it("Install Omnigent from the local intro starts the install", () => {
    render(<ServerSelectorV2 setup={makeSetup()} />);
    fireEvent.click(screen.getByRole("button", { name: /get started locally/i }));
    fireEvent.click(screen.getByRole("button", { name: /install omnigent/i }));
    expect(screen.getByText(/starting the local server/i)).toBeInTheDocument();
  });

  it("a returning user (installed) starts on the server list, not the landing", () => {
    render(
      <ServerSelectorV2
        setup={makeSetup({ installed: true, recentServers: ["https://team.example.com/"] })}
      />,
    );
    expect(screen.getByText(/^Recents$/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Meet Omnigent" })).not.toBeInTheDocument();
  });

  it("Join your team advances to the server-select step", () => {
    render(
      <ServerSelectorV2 setup={makeSetup({ recentServers: ["https://team.example.com/"] })} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team/i }));
    expect(screen.getByText(/^Recents$/)).toBeInTheDocument();
  });

  it("picking a preset server from the landing shows its detail step", () => {
    render(
      <ServerSelectorV2
        setup={makeSetup({ managedServers: ["https://field-eng-omni.aws.databricksapps.com"] })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team \(field-eng-omni\)/i }));
    expect(screen.getByRole("heading", { name: /you.?re in/i })).toBeInTheDocument();
    // Single server, no radio list to select from.
    expect(screen.queryByRole("radiogroup")).not.toBeInTheDocument();
  });

  it("opens directly on the server step when a connect error is present", () => {
    render(
      <ServerSelectorV2
        setup={makeSetup({
          error: "Could not load http://dead/",
          recentServers: ["https://team.example.com/"],
        })}
      />,
    );
    // The error banner is only reachable on the server step — so being able to
    // see it proves the flow opened there rather than on the landing hero.
    expect(screen.getByText(/^Recents$/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load http://dead/");
  });

  it("the cog menu switches back to the legacy selector", () => {
    const onSwitchToLegacy = vi.fn();
    render(<ServerSelectorV2 setup={makeSetup({ onSwitchToLegacy })} />);
    // radix dropdown opens on pointerDown.
    fireEvent.pointerDown(screen.getByRole("button", { name: /server selector settings/i }), {
      button: 0,
    });
    fireEvent.click(
      screen.getByRole("menuitem", { name: /switch to legacy selector experience/i }),
    );
    expect(onSwitchToLegacy).toHaveBeenCalledOnce();
  });
});
