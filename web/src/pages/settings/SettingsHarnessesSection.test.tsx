// Integration tests for Settings → Harnesses: real per-host readiness
// derivation (harnessReadinessOnHost runs unmocked) driving the card status,
// Set-up affordance, Installed filter, and the details page. The host query, the
// host inventory, and the heavy composer/dialog imports are mocked so it renders
// without a backend.

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { HarnessInventory } from "@/hooks/useHarnessInventory";
import type { Host } from "@/hooks/useHosts";
import { SettingsHarnessesSection } from "./SettingsHarnessesSection";

let hosts: Host[] = [];
vi.mock("@/hooks/useHosts", async (importActual) => ({
  ...(await importActual()),
  useHosts: () => ({ data: hosts }),
}));

// Claude has two MCP servers (one from the toolkit plugin), a skill, and that
// plugin; the Codex entries must not leak into Claude's tabs.
const INVENTORY: HarnessInventory = {
  status: "ready",
  unavailable: [],
  isEmpty: false,
  context: {
    credentials: [],
    mcps: [
      { id: "claude:github", name: "github", harness: "claude" },
      {
        id: "claude:plugin:toolkit:linear",
        name: "linear",
        harness: "claude",
        detail: "toolkit plugin",
        plugin: "toolkit",
      },
      { id: "codex:docs", name: "docs", harness: "codex" },
    ],
    skills: [
      { id: "claude:review", name: "review", harness: "claude", description: "Review diffs." },
      { id: "codex:fix", name: "fix", harness: "codex", description: "" },
    ],
    plugins: [
      { id: "claude:toolkit", name: "toolkit", harness: "claude", skills: ["lint", "ship"] },
    ],
  },
};
let inventory: HarnessInventory = INVENTORY;
vi.mock("@/hooks/useHarnessInventory", async (importActual) => ({
  ...(await importActual()),
  useHarnessInventory: () => inventory,
}));

// The "Set up" button is gated on the harness_install feature (like New Chat),
// so drive that flag through the server-info mock.
let harnessInstall = true;
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    features: { harness_install: harnessInstall, harness_settings_ui: true },
  }),
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

function renderHarnesses(harness?: string) {
  render(
    <MemoryRouter initialEntries={[`/settings/harnesses${harness ? `/${harness}` : ""}`]}>
      <SettingsHarnessesSection />
    </MemoryRouter>,
  );
}

// Radix Tabs activate on focus; jsdom's synthetic click doesn't move focus.
function selectTab(name: string) {
  const tab = screen.getByRole("tab", { name });
  fireEvent.focus(tab);
  fireEvent.click(tab);
}

const card = (harness: string) => screen.getByTestId(`harness-card-${harness}`);

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
  inventory = INVENTORY;
  harnessInstall = true;
  setupDialogProps.mockReset();
});

describe("Harnesses grid", () => {
  it("shows Installed (no Set-up) for a ready harness and a badge + Set-up for a not-ready one", () => {
    hosts = [ONLINE];
    renderHarnesses();

    // claude-native is ready → "Installed", no action button.
    expect(within(card("claude-native")).getByText("Configured")).toBeTruthy();
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
    expect(within(card("claude-native")).queryByText("Installed")).toBeNull();
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

  it("shows only ready harnesses under the Installed filter", () => {
    hosts = [ONLINE];
    renderHarnesses();

    selectTab("Configured");

    expect(card("claude-native")).toBeTruthy();
    expect(screen.queryByTestId("harness-card-codex-native")).toBeNull();
  });

  it("links an installed harness's card to its details page, but not a not-ready one", () => {
    hosts = [ONLINE];
    renderHarnesses();

    expect(card("claude-native").getAttribute("href")).toBe("/settings/harnesses/claude-native");
    expect(card("codex-native").getAttribute("href")).toBeNull();
  });
});

describe("Harness card navigation", () => {
  const selected = (name: string) =>
    screen.getByRole("tab", { name }).getAttribute("aria-selected");

  it("opens the details on MCP servers from the card", () => {
    hosts = [ONLINE];
    renderHarnesses();

    fireEvent.click(card("claude-native"));
    expect(selected("MCP servers · 2")).toBe("true");
  });

  it("opens the details on Settings from the card's gear", () => {
    hosts = [ONLINE];
    renderHarnesses();

    fireEvent.click(screen.getByTestId("harness-settings-claude-native"));
    expect(selected("Settings")).toBe("true");
  });
});

describe("Harness details", () => {
  it("shows the header, gateway credential, and catalog tabs for an installed harness", () => {
    hosts = [{ ...ONLINE, gateway_inference: { "claude-native": true } }];
    renderHarnesses("claude-native");

    expect(screen.getByRole("heading", { name: "Claude Code" })).toBeTruthy();
    // Counts and rows cover this harness's family only.
    expect(screen.getByRole("tab", { name: "MCP servers · 2" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Skills · 1" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Plugins · 1" })).toBeTruthy();
    expect(screen.getByTestId("catalog-row-github")).toBeTruthy();
    expect(screen.queryByTestId("catalog-row-docs")).toBeNull();

    // The credential lives under the Settings tab.
    selectTab("Settings");
    expect(screen.getByText("AI Gateway")).toBeTruthy();
  });

  it("lists MCP servers and skills as plain rows with host-reported details only", () => {
    hosts = [ONLINE];
    renderHarnesses("claude-native");

    // No tool list from the host: no count, nothing to open or expand.
    const linear = screen.getByTestId("catalog-row-linear");
    expect(within(linear).getByText("toolkit plugin")).toBeTruthy();
    expect(linear.tagName).not.toBe("BUTTON");
    expect(screen.queryByText(/\d+ tools?/)).toBeNull();

    selectTab("Skills · 1");
    const review = screen.getByTestId("catalog-row-review");
    expect(within(review).getByText("Review diffs.")).toBeTruthy();
    expect(review.tagName).not.toBe("BUTTON");
  });

  it("shows a plugin's skills and bundled MCP servers", () => {
    hosts = [ONLINE];
    renderHarnesses("claude-native");

    selectTab("Plugins · 1");
    fireEvent.click(screen.getByTestId("catalog-row-toolkit"));

    expect(screen.getByRole("heading", { name: "toolkit" })).toBeTruthy();
    expect(screen.getByText("2 skills · 1 MCP")).toBeTruthy();
    expect(screen.getByText("lint")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Plugins" }));
    expect(screen.getByRole("tab", { name: "Plugins · 1" }).getAttribute("aria-selected")).toBe(
      "true",
    );
  });

  it("shows loading, then a per-tab error for a listing the host couldn't report", () => {
    hosts = [ONLINE];
    inventory = { ...INVENTORY, status: "loading" };
    renderHarnesses("claude-native");
    expect(screen.getByText("Loading MCP servers…")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "MCP servers" })).toBeTruthy();
    cleanup();

    inventory = { ...INVENTORY, unavailable: ["mcps"] };
    renderHarnesses("claude-native");
    expect(screen.getByText("Couldn't load MCP servers from my-laptop.")).toBeTruthy();
  });

  it("says the catalog isn't listed for a ready harness the inventory doesn't cover", () => {
    hosts = [{ ...ONLINE, configured_harnesses: { "pi-native": true } }];
    renderHarnesses("pi-native");

    expect(screen.getByText(/aren't listed for Pi yet/)).toBeTruthy();
    expect(screen.queryByRole("tab", { name: /MCP servers/ })).toBeNull();
  });

  it("offers Set up instead of the catalog for a harness that isn't ready", () => {
    hosts = [ONLINE];
    renderHarnesses("codex-native");

    expect(screen.getByText(/Set up Codex on my-laptop/)).toBeTruthy();
    expect(screen.getByTestId("harness-action-codex-native")).toBeTruthy();
    expect(screen.queryByRole("tab", { name: /MCP servers/ })).toBeNull();
  });

  it("falls back to the grid for an unknown harness slug", () => {
    hosts = [ONLINE];
    renderHarnesses("not-a-harness");

    expect(screen.getByRole("heading", { name: "Harnesses" })).toBeTruthy();
  });
});
