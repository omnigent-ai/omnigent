import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";

vi.mock("@/shell/AppShell", () => ({
  AppShell: () => <main data-testid="app-shell" />,
}));

vi.mock("@/pages/SetupPage", () => ({
  SetupPage: () => <main data-testid="setup-page" />,
}));

const serverInfo: ServerInfo = {
  accounts_enabled: false,
  single_user: false,
  login_url: null,
  needs_setup: false,
  databricks_features: false,
  managed_sandboxes_enabled: false,
  sandbox_provider: null,
  sharing_mode: "on",
  public_sharing_enabled: true,
  server_version: null,
  smart_routing_enabled: false,
};

describe("App launch animation", () => {
  it("renders the exact animated announcement on load", () => {
    render(
      <CapabilitiesProvider info={serverInfo}>
        <MemoryRouter initialEntries={["/"]}>
          <App />
        </MemoryRouter>
      </CapabilitiesProvider>,
    );

    const announcement = screen.getByText("AGI is here");
    expect(announcement).toBeInTheDocument();
    expect(announcement).toHaveClass("agi-launch-text");
    expect(announcement.closest("[aria-label='AGI is here']")).toHaveClass("agi-launch");
  });

  it("also renders the animated announcement on the first-run setup route", () => {
    render(
      <CapabilitiesProvider
        info={{
          ...serverInfo,
          accounts_enabled: true,
          needs_setup: true,
        }}
      >
        <MemoryRouter initialEntries={["/setup"]}>
          <App />
        </MemoryRouter>
      </CapabilitiesProvider>,
    );

    const announcement = screen.getByText("AGI is here");
    expect(announcement).toHaveClass("agi-launch-text");
    expect(announcement.closest("[aria-label='AGI is here']")).toHaveClass("agi-launch");
  });
});
