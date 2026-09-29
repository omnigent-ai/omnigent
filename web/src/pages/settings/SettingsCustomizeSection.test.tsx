// Integration tests for the Harnesses subsection: real per-host readiness
// derivation (harnessReadinessOnHost runs unmocked) driving the card status +
// Set-up affordance, host selection, the no-host state, and search. The host
// query and the heavy composer/dialog imports are mocked so the grid renders
// fast without a live backend.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Host } from "@/hooks/useHosts";
import { SettingsCustomizeSection } from "./SettingsCustomizeSection";

let hosts: Host[] = [];
vi.mock("@/hooks/useHosts", async (importActual) => ({
  ...(await importActual()),
  useHosts: () => ({ data: hosts }),
}));

// The "Set up" button is gated on the harness_install feature (like New Chat),
// so drive that flag through the server-info mock.
let harnessInstall = true;
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ features: { harness_install: harnessInstall } }),
}));

// The real ComposerAgentIcon pulls in the whole composer; stub it to a marker.
vi.mock("@/shell/NewChatDialog", () => ({
  ComposerAgentIcon: () => <span data-testid="agent-icon" />,
}));

// Assert the setup dialog is opened with the right harness/host, without
// rendering its full install/auth flow.
const setupDialogProps = vi.fn();
vi.mock("@/shell/HarnessSetupDialog", () => ({
  HarnessSetupDialog: (props: { open: boolean; harness: string | null; host: Host | null }) => {
    setupDialogProps(props);
    return props.open ? (
      <div
        data-testid="setup-dialog"
        data-harness={props.harness}
        data-host={props.host?.host_id ?? ""}
      />
    ) : null;
  },
}));

function renderHarnesses() {
  render(
    <MemoryRouter initialEntries={["/settings/customize/harnesses"]}>
      <SettingsCustomizeSection subSection="harnesses" />
    </MemoryRouter>,
  );
}

const ONLINE: Host = {
  host_id: "h1",
  name: "my-laptop",
  owner: "me",
  status: "online",
  configured_harnesses: { "claude-native": true, "codex-native": "needs-auth" },
};

afterEach(() => {
  cleanup();
  hosts = [];
  harnessInstall = true;
  setupDialogProps.mockReset();
});

describe("Harnesses subsection", () => {
  it("shows Installed (no Set-up) for a ready harness and a badge + Set-up for a not-ready one", () => {
    hosts = [ONLINE];
    renderHarnesses();

    // claude-native is ready → "Installed", no action button.
    expect(screen.getByText("Installed")).toBeTruthy();
    expect(screen.queryByTestId("harness-action-claude-native")).toBeNull();

    // codex-native reports needs-auth → warning badge + a Set-up button.
    expect(screen.getByText("needs auth")).toBeTruthy();
    expect(screen.getByTestId("harness-action-codex-native")).toBeTruthy();
  });

  it("opens the setup dialog for the clicked harness", () => {
    hosts = [ONLINE];
    renderHarnesses();

    fireEvent.click(screen.getByTestId("harness-action-codex-native"));

    const dialog = screen.getByTestId("setup-dialog");
    expect(dialog.getAttribute("data-harness")).toBe("codex-native");
    // The dialog is bound to the host chosen when setup opened, not the live
    // selection — a later host switch can't redirect the credential/install.
    expect(dialog.getAttribute("data-host")).toBe("h1");
  });

  it("treats unknown readiness (host reports nothing) as neutral, not needs-setup", () => {
    // Older host: configured_harnesses null → every harness is readiness-unknown.
    hosts = [{ ...ONLINE, configured_harnesses: null }];
    renderHarnesses();

    // No false "needs setup": no badge, no Set-up button, no bogus "Installed".
    expect(screen.queryByTestId("harness-action-codex-native")).toBeNull();
    expect(screen.queryByTestId("harness-action-claude-native")).toBeNull();
    expect(screen.queryByText("Installed")).toBeNull();
    expect(screen.queryByText("needs setup")).toBeNull();
  });

  it("hides Set-up (keeps the badge) when harness_install is disabled", () => {
    // Flag off + binary-missing: the setup dialog would be a dead end (no
    // runnable install step), so we show status only — matching New Chat.
    harnessInstall = false;
    hosts = [{ ...ONLINE, configured_harnesses: { "codex-native": "binary-missing" } }];
    renderHarnesses();

    expect(screen.getByText("binary missing")).toBeTruthy();
    expect(screen.queryByTestId("harness-action-codex-native")).toBeNull();
  });

  it("shows the no-host notice and no Set-up buttons when no host is online", () => {
    hosts = [{ ...ONLINE, status: "offline" }];
    renderHarnesses();

    expect(screen.getByTestId("harness-no-host")).toBeTruthy();
    // No host → no readiness, so no Set-up affordance on any card.
    expect(screen.queryByTestId("harness-action-codex-native")).toBeNull();
  });

  it("filters harnesses by the search query", () => {
    hosts = [ONLINE];
    renderHarnesses();

    fireEvent.change(screen.getByTestId("harness-search"), { target: { value: "codex" } });

    expect(screen.getByText("Codex")).toBeTruthy();
    expect(screen.queryByText("Claude Code")).toBeNull();
  });
});
