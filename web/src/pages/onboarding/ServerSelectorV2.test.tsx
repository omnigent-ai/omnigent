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

  it("Get started advances to the deployment-mode step", () => {
    render(<ServerSelectorV2 setup={makeSetup()} />);
    fireEvent.click(screen.getByRole("button", { name: /get started/i }));
    expect(
      screen.getByRole("heading", { name: /where do you want your sessions/i }),
    ).toBeInTheDocument();
  });

  it("Join a server advances to the server-select step", () => {
    render(<ServerSelectorV2 setup={makeSetup()} />);
    fireEvent.click(screen.getByRole("button", { name: /join a server/i }));
    expect(screen.getByRole("heading", { name: /join an existing server/i })).toBeInTheDocument();
  });

  it("opens directly on the server step when a connect error is present", () => {
    render(<ServerSelectorV2 setup={makeSetup({ error: "Could not load http://dead/" })} />);
    // The error banner is only reachable on the server step — so being able to
    // see it proves the flow opened there rather than on the landing hero.
    expect(screen.getByRole("heading", { name: /join an existing server/i })).toBeInTheDocument();
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
