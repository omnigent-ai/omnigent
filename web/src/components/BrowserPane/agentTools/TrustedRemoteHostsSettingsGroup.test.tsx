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
    getTrustedRemoteHosts: vi
      .fn()
      .mockResolvedValue({ ok: true, serverUrl: "https://server.example.com/", hosts }),
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
    expect(screen.getByLabelText("Server URL")).toHaveValue("https://server.example.com/");
    expect(screen.getByLabelText("Server URL")).toHaveAttribute("readonly");
    fireEvent.change(screen.getByLabelText("Host ID"), { target: { value: "def456" } });
    fireEvent.click(screen.getByRole("button", { name: "Trust host" }));
    await waitFor(() =>
      expect(bridge.addTrustedRemoteHost).toHaveBeenCalledWith(
        "https://server.example.com/",
        "def456",
      ),
    );
    await waitFor(() =>
      expect(screen.getByText("https://server.example.com/")).toBeInTheDocument(),
    );
    expect(screen.getByText("def456")).toBeInTheDocument();
    expect(screen.getByLabelText("Host ID")).toHaveValue("");
  });

  it.each(["list", "add", "remove"] as const)(
    "shows a rejected %s request and keeps the form usable",
    async (operation) => {
      const bridge = installBridge();
      const method = {
        list: bridge.getTrustedRemoteHosts,
        add: bridge.addTrustedRemoteHost,
        remove: bridge.removeTrustedRemoteHost,
      }[operation];
      method.mockRejectedValueOnce(new Error("IPC failed"));
      renderInRouter(<TrustedRemoteHostsSettingsGroup />);
      if (operation !== "list") {
        await screen.findByText("abc123");
        if (operation === "add") {
          fireEvent.change(screen.getByLabelText("Host ID"), { target: { value: "def456" } });
          fireEvent.click(screen.getByRole("button", { name: "Trust host" }));
        } else {
          fireEvent.click(screen.getByRole("button", { name: /Remove trusted host/ }));
        }
      }
      expect(await screen.findByRole("alert")).toHaveTextContent(/Could not/);
      if (operation === "list") {
        expect(screen.queryByText(/No remote hosts trusted yet/)).not.toBeInTheDocument();
        expect(screen.queryByRole("button", { name: "Trust host" })).not.toBeInTheDocument();
      }
      if (operation === "add")
        expect(screen.getByRole("button", { name: "Trust host" })).toBeEnabled();
      if (operation === "remove")
        expect(screen.getByRole("button", { name: /Remove trusted host/ })).toBeEnabled();
      if (operation !== "list") {
        method.mockResolvedValueOnce({ ok: false, error: "Request refused" });
        fireEvent.click(
          screen.getByRole("button", {
            name: operation === "add" ? "Trust host" : /Remove trusted host/,
          }),
        );
        await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Request refused"));
        expect(screen.getByText("abc123")).toBeInTheDocument();
        if (operation === "add") expect(screen.getByLabelText("Host ID")).toHaveValue("def456");
      }
    },
  );

  it("hides the empty list and form when listing is refused", async () => {
    const bridge = installBridge();
    bridge.getTrustedRemoteHosts.mockResolvedValueOnce({ ok: false, error: "Not connected" });
    renderInRouter(<TrustedRemoteHostsSettingsGroup />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Not connected");
    expect(screen.queryByText(/No remote hosts trusted yet/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Trust host" })).not.toBeInTheDocument();
  });

  it.each(["add", "remove"] as const)(
    "refreshes stale rows from a refused %s result",
    async (operation) => {
      const bridge = installBridge();
      const method = { add: bridge.addTrustedRemoteHost, remove: bridge.removeTrustedRemoteHost }[
        operation
      ];
      method.mockResolvedValueOnce({ ok: false, hosts: [], error: "Host list changed" });
      renderInRouter(<TrustedRemoteHostsSettingsGroup />);
      await screen.findByText("abc123");
      if (operation === "add") {
        fireEvent.change(screen.getByLabelText("Host ID"), { target: { value: "def456" } });
        fireEvent.click(screen.getByRole("button", { name: "Trust host" }));
      } else {
        fireEvent.click(screen.getByRole("button", { name: /Remove trusted host/ }));
      }
      expect(await screen.findByRole("alert")).toHaveTextContent("Host list changed");
      expect(screen.queryByText("abc123")).not.toBeInTheDocument();
      expect(screen.getByText(/No remote hosts trusted yet/)).toBeInTheDocument();
    },
  );

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
