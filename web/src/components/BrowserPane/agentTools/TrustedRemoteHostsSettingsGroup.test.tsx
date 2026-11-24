import type { ReactElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TrustedRemoteHostsSettingsGroup } from "./TrustedRemoteHostsSettingsGroup";
import { UPDATE_DESKTOP_NOTE } from "./BrowserAgentToolsSettingsGroup";

function renderInRouter(ui: ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

function installBridge(
  hosts: { server_url: string; host_id: string }[] = [
    { server_url: "https://server.example.com/", host_id: "abc123" },
  ],
) {
  const bridge = {
    kind: "electron",
    browserOpenOrNavigate: vi.fn(),
    getDesktopFeatures: vi.fn().mockResolvedValue({ trustedRemoteLocalhostPreview: true }),
    getTrustedRemoteHosts: vi.fn().mockResolvedValue({ ok: true, hosts }),
    addTrustedRemoteHost: vi.fn().mockImplementation(async (serverUrl: string, hostId: string) => ({
      ok: true,
      added: true,
      hosts: [...hosts, { server_url: serverUrl, host_id: hostId }],
    })),
    removeTrustedRemoteHost: vi.fn().mockResolvedValue({ ok: true, removed: true, hosts: [] }),
  };
  (window as unknown as Record<string, unknown>).omnigentDesktop = bridge;
  return bridge;
}

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("TrustedRemoteHostsSettingsGroup", () => {
  it("lists trusted hosts and removes one", async () => {
    const bridge = installBridge();
    renderInRouter(<TrustedRemoteHostsSettingsGroup />);
    expect(await screen.findByText("https://server.example.com/")).toBeInTheDocument();
    expect(screen.getByText("abc123")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", {
        name: "Remove trusted host https://server.example.com/ abc123",
      }),
    );
    await waitFor(() =>
      expect(bridge.removeTrustedRemoteHost).toHaveBeenCalledWith(
        "https://server.example.com/",
        "abc123",
      ),
    );
    await waitFor(() =>
      expect(screen.queryByText("https://server.example.com/")).not.toBeInTheDocument(),
    );
  });

  it("adds a host from the form", async () => {
    const bridge = installBridge([]);
    renderInRouter(<TrustedRemoteHostsSettingsGroup />);
    await screen.findByText(/No remote hosts trusted yet/);
    fireEvent.change(screen.getByLabelText("Server URL"), {
      target: { value: "https://other.example.com" },
    });
    fireEvent.change(screen.getByLabelText("Host ID"), { target: { value: "def456" } });
    fireEvent.click(screen.getByRole("button", { name: "Trust host" }));
    await waitFor(() =>
      expect(bridge.addTrustedRemoteHost).toHaveBeenCalledWith(
        "https://other.example.com",
        "def456",
      ),
    );
    await waitFor(() => expect(screen.getByText("https://other.example.com")).toBeInTheDocument());
  });

  it("renders nothing outside the desktop app", () => {
    const { container } = renderInRouter(<TrustedRemoteHostsSettingsGroup />);
    expect(container).toBeEmptyDOMElement();
  });

  it("says to update on a desktop app without the feature", async () => {
    installBridge();
    (
      window as unknown as { omnigentDesktop: { getDesktopFeatures: unknown } }
    ).omnigentDesktop.getDesktopFeatures = vi.fn().mockResolvedValue({});
    renderInRouter(<TrustedRemoteHostsSettingsGroup />);
    expect(await screen.findByText(UPDATE_DESKTOP_NOTE)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Trust host" })).not.toBeInTheDocument();
  });
});
