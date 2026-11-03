// Integration tests for Settings → Harnesses: real per-host readiness
// derivation (harnessReadinessOnHost runs unmocked) driving the card status,
// Set-up affordance, Installed filter, and the details page. The host query, the
// host inventory, and the heavy composer/dialog imports are mocked so it renders
// without a backend.

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/tooltip";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SkillContent } from "@/hooks/useSkillContent";
import type { HarnessInventory } from "@/hooks/useHarnessInventory";
import { ApiError } from "@/lib/sessionsApi";
import type { HarnessStartup, Host } from "@/hooks/useHosts";
import { SettingsHarnessesSection } from "./SettingsHarnessesSection";

let pluginMetadataRequested = false;
const CONTENT: SkillContent = {
  name: "review",
  description: "Review diffs.",
  content:
    "## Instructions\nRead the diff.\n[guide](./GUIDE.md) [docs](https://example.test/docs)\n![private image](https://example.test/pixel)\n<script>secret()</script>",
  truncated: false,
};
let contentQuery: { data?: SkillContent; error?: unknown; isPending: boolean } = {
  data: CONTENT,
  isPending: false,
};
let contentLookup: [string, string, string] | null = null;
let contentSource: string | undefined;
vi.mock("@/hooks/useSkillContent", () => ({
  useSkillContent: (
    hostId: string,
    harness: string,
    name: string,
    { sourceId }: { sourceId?: string },
  ) => {
    contentLookup = [hostId, harness, name];
    contentSource = sourceId;
    return contentQuery;
  },
}));
let mcpResult: {
  data?: {
    tools: { name: string; description: string | null }[];
    connection: string;
    truncated: boolean;
  };
  error?: unknown;
  isPending: boolean;
} = {
  data: {
    tools: [{ name: "read_docs", description: "Read documentation" }],
    connection: "connected",
    truncated: false,
  },
  isPending: false,
};
let mcpLookups: [string, string, string, string | undefined, boolean][] = [];
let mcpSources: (string | undefined)[] = [];
vi.mock("@/hooks/useMcpServerTools", () => ({
  useMcpServerTools: (
    host: string,
    harness: string,
    server: string,
    plugin: string | undefined,
    { enabled, sourceId }: { enabled: boolean; sourceId?: string },
  ) => {
    mcpLookups.push([host, harness, server, plugin, enabled]);
    if (enabled) mcpSources.push(sourceId);
    return enabled ? mcpResult : { isPending: true };
  },
}));
const STARTUP: HarnessStartup = {
  command: "claude",
  resolved_path: "/opt/bin/claude",
  command_source: "config",
  arg_count: 4,
  args: ["--model", "opus", "--api-key", "synthetic-secret"],
  configured_command: "claude",
  configured_args: ["--model", "opus", "--api-key", "synthetic-secret"],
  environment: { inherit: true, variables: {}, unset: [] },
};
let startupData = STARTUP;
let startupError: ApiError | null = null;
const startupCalls = vi.fn();
const accountRefreshCalls = vi.fn();
let hosts: Host[] = [];
vi.mock("@/hooks/useHosts", async (importActual) => ({
  ...(await importActual()),
  useHosts: () => {
    return useQuery({
      queryKey: ["hosts", { includeSandbox: false }],
      queryFn: async () => hosts,
      initialData: hosts,
    });
  },
  useHarnessStartup: (hostId: string, harness: string) => {
    startupCalls(hostId, harness);
    return { data: startupError ? undefined : startupData, error: startupError, isPending: false };
  },
  useRefreshHarnessAccounts: (hostId: string | null, enabled: boolean) => {
    accountRefreshCalls(hostId, enabled);
    return { data: undefined, isPending: false };
  },
}));

// Claude has two MCP servers (one from the toolkit plugin), a skill, and that
// plugin; the Codex entries must not leak into Claude's tabs.
const INVENTORY: HarnessInventory = {
  status: "ready",
  unavailable: [],
  mcpUnsupported: false,
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
  useHarnessInventory: (_host: Host, options: { includePluginMetadata: boolean }) => {
    pluginMetadataRequested = options.includePluginMetadata;
    return inventory;
  },
}));

// The "Set up" button is gated on the harness_install feature (like New Chat),
// so drive that flag through the server-info mock.
let harnessInstall = true;
let databricksFeatures = true;
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    databricks_features: databricksFeatures,
    features: { harness_install: harnessInstall },
    installable_harnesses: [
      "claude-native",
      "codex-native",
      "opencode-native",
      "pi-native",
      "qwen-native",
    ],
  }),
}));

// The real ComposerAgentIcon pulls in the whole composer; stub it to a marker.
vi.mock("@/shell/NewChatDialog", () => ({
  ComposerAgentIcon: () => <span data-testid="agent-icon" />,
}));

const fixDialogProps = vi.fn();
vi.mock("./HarnessFixDialog", () => ({
  HarnessFixDialog: (props: {
    open: boolean;
    mode: "reconfigure" | "update" | "install" | "switch";
    harness: string;
    host: Host;
    onFixed: (result: {
      configuredHarnesses: Record<string, boolean | string>;
      credentialKind?: "isaac" | "personal";
      gatewayInference?: Record<string, boolean> | null;
    }) => void;
  }) => {
    fixDialogProps(props);
    return props.open ? (
      <div
        data-testid="fix-dialog"
        data-harness={props.harness}
        data-host={props.host.host_id}
        data-mode={props.mode}
      >
        <button
          type="button"
          onClick={() =>
            props.onFixed({
              configuredHarnesses: { [props.harness]: true },
              credentialKind: "isaac",
              gatewayInference: { [props.harness]: true },
            })
          }
        >
          Complete fix
        </button>
      </div>
    ) : null;
  },
}));

function renderHarnesses(harness?: string, search = "") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <MemoryRouter
          initialEntries={[`/settings/harnesses${harness ? `/${harness}` : ""}${search}`]}
        >
          <SettingsHarnessesSection />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return queryClient;
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
  contentQuery = { data: CONTENT, isPending: false };
  contentLookup = null;
  contentSource = undefined;
  mcpResult = {
    data: {
      tools: [{ name: "read_docs", description: "Read documentation" }],
      connection: "connected",
      truncated: false,
    },
    isPending: false,
  };
  mcpLookups = [];
  mcpSources = [];
  cleanup();
  hosts = [];
  inventory = INVENTORY;
  harnessInstall = true;
  databricksFeatures = true;
  fixDialogProps.mockReset();
  startupCalls.mockClear();
  accountRefreshCalls.mockClear();
  startupData = STARTUP;
  startupError = null;
});

describe("Harnesses grid", () => {
  it("requests accounts only for a selected capable host while setup is enabled", () => {
    hosts = [{ ...ONLINE, harness_accounts_supported: true }];
    renderHarnesses();
    expect(accountRefreshCalls).toHaveBeenLastCalledWith("h1", true);

    cleanup();
    accountRefreshCalls.mockClear();
    harnessInstall = false;
    renderHarnesses();
    expect(accountRefreshCalls).toHaveBeenLastCalledWith("h1", false);
  });

  it("preselects the linked online host and keeps it after returning to the grid", () => {
    hosts = [ONLINE, { ...ONLINE, host_id: "host & two", name: "session-host" }];
    renderHarnesses("claude-native", "?host=host%20%26%20two&tab=settings");
    expect(startupCalls).toHaveBeenCalledWith("host & two", "claude-native");
    fireEvent.click(screen.getByRole("link", { name: /Harnesses/ }));
    expect(screen.getByRole("button", { name: "session-host" })).toBeInTheDocument();
  });

  it.each([
    ["offline", [ONLINE, { ...ONLINE, host_id: "linked", status: "offline" as const }]],
    ["no longer listed", [ONLINE]],
  ])("falls back to an online host if the linked host is %s", (_, linkedHosts) => {
    hosts = linkedHosts;
    renderHarnesses("claude-native", "?host=linked&tab=settings");
    expect(startupCalls).toHaveBeenCalledWith("h1", "claude-native");
    expect(startupCalls).not.toHaveBeenCalledWith("linked", "claude-native");
  });

  it("shows the configured credential and a reconfiguration warning with its reason", async () => {
    hosts = [ONLINE];
    renderHarnesses();

    expect(within(card("claude-native")).getByText("Configured")).toBeTruthy();
    expect(within(card("claude-native")).getByText("· Signed in")).toBeTruthy();
    expect(screen.queryByTestId("harness-action-claude-native")).toBeNull();

    const warning = within(card("codex-native")).getByLabelText(
      "Needs reconfiguration: Missing credentials",
    );
    expect(warning.textContent).toContain("Needs reconfiguration");
    expect(screen.getByTestId("harness-settings-codex-native")).toBeTruthy();
    fireEvent.focus(warning);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Missing credentials");
  });

  it("opens install for a not-installed harness on the selected host", () => {
    hosts = [{ ...ONLINE, configured_harnesses: { "codex-native": "binary-missing" } }];
    renderHarnesses();

    fireEvent.click(screen.getByTestId("harness-action-codex-native"));

    const dialog = screen.getByTestId("fix-dialog");
    expect(dialog.getAttribute("data-harness")).toBe("codex-native");
    expect(dialog.getAttribute("data-host")).toBe("h1");
    expect(dialog.getAttribute("data-mode")).toBe("install");
    expect(within(card("codex-native")).queryByText("Needs reconfiguration")).toBeNull();
    expect(within(card("codex-native")).getByText("Not installed")).toBeTruthy();
  });

  it("does not offer one-click install for a not-installed Cursor harness", () => {
    hosts = [{ ...ONLINE, configured_harnesses: { "cursor-native": "binary-missing" } }];
    renderHarnesses();

    expect(screen.queryByTestId("harness-action-cursor-native")).toBeNull();
    expect(screen.getByTestId("harness-settings-cursor-native")).toBeTruthy();
  });

  it("uses the existing inventory label instead of guessing a personal subscription", () => {
    hosts = [{ ...ONLINE, gateway_inference: { "claude-native": false } }];
    renderHarnesses();

    expect(within(card("claude-native")).getByText("· Signed in")).toBeTruthy();
    expect(within(card("claude-native")).queryByText(/Personal subscription/)).toBeNull();
  });

  it("treats unknown readiness (host reports nothing) as neutral, not needs-setup", () => {
    // Older host: configured_harnesses null → every harness is readiness-unknown.
    hosts = [{ ...ONLINE, configured_harnesses: null }];
    renderHarnesses();

    // No false "needs setup": no badge, no Set-up button, no bogus "Installed".
    expect(screen.queryByTestId("harness-action-codex-native")).toBeNull();
    expect(screen.queryByTestId("harness-action-claude-native")).toBeNull();
    expect(within(card("claude-native")).queryByText("Installed")).toBeNull();
    expect(screen.queryByText("Needs reconfiguration")).toBeNull();
  });

  it("hides Set-up (keeps the badge) when harness_install is disabled", () => {
    // Flag off + binary-missing: the setup dialog would be a dead end (no
    // runnable install step), so we show status only — matching New Chat.
    harnessInstall = false;
    hosts = [{ ...ONLINE, configured_harnesses: { "codex-native": "binary-missing" } }];
    renderHarnesses();

    expect(within(card("codex-native")).getByText("Binary missing")).toBeTruthy();
    expect(within(card("codex-native")).queryByText("Not installed")).toBeNull();
    expect(within(card("codex-native")).queryByLabelText(/Needs reconfiguration/)).toBeNull();
    expect(screen.queryByTestId("harness-action-codex-native")).toBeNull();
    expect(screen.queryByTestId("harness-settings-codex-native")).toBeNull();
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

  it("links ready and broken harness cards to details while the feature is enabled", () => {
    hosts = [ONLINE];
    renderHarnesses();

    expect(card("claude-native").getAttribute("href")).toBe("/settings/harnesses/claude-native");
    expect(card("codex-native").getAttribute("href")).toBe(
      "/settings/harnesses/codex-native?tab=settings",
    );
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

  it("opens a broken harness on Settings from its gear", () => {
    hosts = [ONLINE];
    renderHarnesses();

    fireEvent.click(screen.getByTestId("harness-settings-codex-native"));
    expect(selected("Settings")).toBe("true");
    expect(screen.getByText(/No ChatGPT account or API key on my-laptop/)).toBeTruthy();
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
    // Known servers show their brand spelling, as on the tool-call rows.
    expect(within(screen.getByTestId("catalog-row-github")).getByText("GitHub")).toBeTruthy();
    expect(within(screen.getByTestId("catalog-row-linear")).getByText("Linear")).toBeTruthy();
    expect(screen.queryByTestId("catalog-row-docs")).toBeNull();

    // The credential lives under the Settings tab.
    selectTab("Settings");
    expect(screen.getByText("Isaac")).toBeTruthy();
    expect(screen.getByText("Databricks-managed credential via the Unity Gateway")).toBeTruthy();
    expect(screen.getByText("/opt/bin/claude")).toBeTruthy();
    for (const arg of STARTUP.args ?? []) expect(screen.getAllByText(arg)).toHaveLength(1);
    expect(screen.getByText(/Sessions and workspaces may override/)).toBeTruthy();
    expect(screen.getByText(/harness.claude-native in ~\/.omnigent\/config.yaml/)).toBeTruthy();
  });

  it("keeps the Isaac label when databricks_features is false", () => {
    databricksFeatures = false;
    hosts = [{ ...ONLINE, gateway_inference: { "claude-native": true } }];
    renderHarnesses();

    expect(within(card("claude-native")).getByText("· Isaac")).toBeTruthy();
    fireEvent.click(screen.getByTestId("harness-settings-claude-native"));
    expect(screen.getByText("Isaac")).toBeTruthy();
    expect(screen.getByText("Databricks-managed credential via the Unity Gateway")).toBeTruthy();
  });

  it("keeps tool discovery lazy and shows host-reported details", () => {
    hosts = [ONLINE];
    renderHarnesses("claude-native");

    // Collapsed rows have no tool count or status until probed.
    const linear = screen.getByTestId("catalog-row-linear");
    expect(within(linear).getByText("toolkit plugin")).toBeTruthy();
    expect(linear.tagName).toBe("BUTTON");
    expect(mcpLookups.every((lookup) => !lookup[4])).toBe(true);
    expect(screen.queryByText(/\d+ tools?/)).toBeNull();

    selectTab("Skills · 1");
    const review = screen.getByTestId("catalog-row-review");
    expect(within(review).getByText("Review diffs.")).toBeTruthy();
    expect(review.tagName).toBe("BUTTON");
    expect(contentLookup).toBeNull();
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

  it.each([false, true])("shows the MCP listing error (unsupported: %s)", (mcpUnsupported) => {
    hosts = [ONLINE];
    inventory = { ...INVENTORY, status: "loading" };
    renderHarnesses("claude-native");
    expect(screen.getByText("Loading MCP servers…")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "MCP servers" })).toBeTruthy();
    cleanup();

    inventory = { ...INVENTORY, unavailable: ["mcps"], mcpUnsupported };
    renderHarnesses("claude-native");
    expect(
      screen.getByText(
        mcpUnsupported
          ? "Please update host my-laptop to list MCP servers."
          : "Couldn't load MCP servers from my-laptop.",
      ),
    ).toBeTruthy();
  });

  it("says the catalog isn't listed for a ready harness the inventory doesn't cover", () => {
    hosts = [{ ...ONLINE, configured_harnesses: { "pi-native": true } }];
    renderHarnesses("pi-native");

    expect(screen.getByText(/aren't listed for Pi yet/)).toBeTruthy();
    expect(screen.queryByRole("tab", { name: /MCP servers/ })).toBeNull();
  });

  it("uses the credential card as the warning for a missing credential", () => {
    hosts = [ONLINE];
    renderHarnesses("codex-native");

    expect(screen.queryByText(/Codex can’t sign in on my-laptop/)).toBeNull();
    expect(
      screen.getByText(
        "No ChatGPT account or API key on my-laptop, so new sessions can’t use Codex.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("Not signed in")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Reconfigure" })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Reconfigure credential" })).toBeNull();
    expect(screen.getByRole("tab", { name: /MCP servers/ })).toBeTruthy();
  });

  it("opens reconfiguration from the credential card", () => {
    hosts = [ONLINE];
    renderHarnesses("codex-native");

    fireEvent.click(screen.getByRole("button", { name: "Reconfigure" }));
    expect(screen.getByTestId("fix-dialog")).toHaveAttribute("data-mode", "reconfigure");

    cleanup();
    renderHarnesses("codex-native");
    fireEvent.click(
      screen
        .getByText(/No ChatGPT account or API key on my-laptop/)
        .closest("button") as HTMLButtonElement,
    );
    expect(screen.getByTestId("fix-dialog")).toHaveAttribute("data-mode", "reconfigure");
  });

  it("shows account identity and provider labels from host metadata", () => {
    hosts = [
      {
        ...ONLINE,
        configured_harnesses: {
          "claude-native": true,
          "codex-native": true,
          "pi-native": true,
          "opencode-native": true,
        },
        harness_accounts: {
          claude: {
            kind: "subscription",
            email: "person@example.com",
            plan: "Max",
          },
          codex: { kind: "chatgpt", email: "codex@example.com", plan: "Plus" },
          pi: {
            kind: "providers",
            providers: [{ id: "anthropic", label: "Anthropic", auth_type: "oauth" }],
          },
          opencode: {
            kind: "providers",
            providers: [
              { id: "anthropic", label: "Anthropic", auth_type: "api_key" },
              { id: "openrouter", label: "OpenRouter", auth_type: "api_key" },
            ],
          },
        },
      },
    ];
    renderHarnesses();

    expect(within(card("claude-native")).getByText("· person@example.com · Max")).toBeTruthy();
    expect(within(card("codex-native")).getByText("· codex@example.com · Plus")).toBeTruthy();
    expect(within(card("pi-native")).getByText("· Anthropic")).toBeTruthy();
    expect(within(card("opencode-native")).getByText("· Anthropic, OpenRouter")).toBeTruthy();

    fireEvent.click(screen.getByTestId("harness-settings-opencode-native"));
    expect(screen.getByText("Providers")).toBeTruthy();
    expect(screen.getByText("Anthropic (API key) · OpenRouter (API key)")).toBeTruthy();
  });

  it("shows an expired amber credential card without a duplicate banner", () => {
    hosts = [
      {
        ...ONLINE,
        harness_accounts: {
          codex: {
            kind: "chatgpt",
            email: "codex@example.com",
            plan: "Plus",
            expired: true,
          },
        },
      },
    ];
    renderHarnesses("codex-native");

    expect(screen.getByText("Sign-in expired")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Sign in again" })).toBeTruthy();
    expect(screen.queryByText(/Codex can’t sign in/)).toBeNull();
  });

  it("keeps a ready gateway credential authoritative over stale account metadata", () => {
    hosts = [
      {
        ...ONLINE,
        configured_harnesses: { "codex-native": true },
        gateway_inference: { "codex-native": true },
        harness_accounts: { codex: { kind: "unknown", expired: true } },
      },
    ];
    renderHarnesses("codex-native");
    selectTab("Settings");

    expect(screen.getAllByText("Isaac").length).toBeGreaterThan(0);
    expect(screen.queryByText("Not signed in")).toBeNull();
    expect(screen.queryByText("Sign-in expired")).toBeNull();
  });

  it("keeps ready provider metadata authoritative over an expired probe", () => {
    hosts = [
      {
        ...ONLINE,
        configured_harnesses: { "opencode-native": true },
        harness_accounts: {
          opencode: {
            kind: "providers",
            expired: true,
            providers: [{ id: "anthropic", label: "Anthropic", auth_type: "api_key" }],
          },
        },
      },
    ];
    renderHarnesses("opencode-native");

    expect(screen.getByText("Providers")).toBeTruthy();
    expect(screen.queryByText("Not signed in")).toBeNull();
    expect(screen.queryByText("Sign-in expired")).toBeNull();
  });

  it("opens switch-account confirmation for a ready subscription", () => {
    hosts = [
      {
        ...ONLINE,
        harness_login_supported: true,
        harness_accounts: {
          claude: {
            kind: "subscription",
            email: "person@example.com",
            plan: "Pro",
          },
        },
      },
    ];
    renderHarnesses("claude-native");
    selectTab("Settings");

    fireEvent.click(screen.getByRole("button", { name: "Switch account" }));
    expect(screen.getByTestId("fix-dialog")).toHaveAttribute("data-mode", "switch");
  });

  it("does not offer account switching when the host lacks login support", () => {
    hosts = [
      {
        ...ONLINE,
        harness_accounts: {
          claude: { kind: "subscription", email: "person@example.com", plan: "Pro" },
        },
      },
    ];
    renderHarnesses("claude-native");
    selectTab("Settings");

    expect(screen.queryByRole("button", { name: "Switch account" })).toBeNull();
    expect(screen.getAllByText("Configured").length).toBeGreaterThan(0);
  });

  it.each([
    ["codex-native", { kind: "api_key" as const }],
    [
      "opencode-native",
      {
        kind: "providers" as const,
        providers: [{ id: "anthropic", label: "Anthropic", auth_type: "api_key" as const }],
      },
    ],
  ])("does not offer account switching for %s non-subscription credentials", (harness, account) => {
    hosts = [
      {
        ...ONLINE,
        configured_harnesses: { [harness]: true },
        harness_login_supported: true,
        harness_accounts: { [harness === "codex-native" ? "codex" : "opencode"]: account },
      },
    ];
    renderHarnesses(harness);
    if (screen.queryByRole("tab", { name: "Settings" })) selectTab("Settings");

    expect(screen.queryByRole("button", { name: "Switch account" })).toBeNull();
  });

  it("uses Update for an outdated harness", () => {
    hosts = [{ ...ONLINE, configured_harnesses: { "codex-native": "version-too-low" } }];
    renderHarnesses("codex-native");

    expect(
      screen.getByText(
        "Codex is out of date on my-laptop. Update it so new sessions can use Codex.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText("Not signed in")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    expect(screen.getByTestId("fix-dialog")).toHaveAttribute("data-mode", "update");
  });

  it("uses Set up for a not-installed Cursor harness", () => {
    hosts = [{ ...ONLINE, configured_harnesses: { "cursor-native": "binary-missing" } }];
    renderHarnesses("cursor-native");

    expect(screen.queryByRole("button", { name: "Install" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Set up" }));
    expect(screen.getByTestId("fix-dialog")).toHaveAttribute("data-mode", "install");
  });

  it("uses Set up instead of Update for an outdated Cursor harness", () => {
    hosts = [{ ...ONLINE, configured_harnesses: { "cursor-native": "version-too-low" } }];
    renderHarnesses("cursor-native");

    expect(screen.queryByRole("button", { name: "Update" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Set up" }));
    expect(screen.getByTestId("fix-dialog")).toHaveAttribute("data-mode", "update");
  });

  it("uses the credential-card Reconfigure action for Cursor needs-auth", () => {
    hosts = [{ ...ONLINE, configured_harnesses: { "cursor-native": "needs-auth" } }];
    renderHarnesses("cursor-native");

    expect(screen.queryByRole("button", { name: "Set up" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Reconfigure" }));
    expect(screen.getByTestId("fix-dialog")).toHaveAttribute("data-mode", "reconfigure");
  });

  it("uses generic credential copy for Pi needs-auth", () => {
    hosts = [{ ...ONLINE, configured_harnesses: { "pi-native": "needs-auth" } }];
    renderHarnesses("pi-native");

    expect(
      screen.getByText(
        "No API key or gateway credential on my-laptop, so new sessions can’t use Pi.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/No Claude subscription or API key/)).toBeNull();
  });

  it("uses Claude credential copy for Claude needs-auth", () => {
    hosts = [{ ...ONLINE, configured_harnesses: { "claude-native": "needs-auth" } }];
    renderHarnesses("claude-native");

    expect(
      screen.getByText(
        "No Claude subscription or API key on my-laptop, so new sessions can’t use Claude Code.",
      ),
    ).toBeTruthy();
  });

  it("never calls a missing binary reconfiguration", () => {
    hosts = [{ ...ONLINE, configured_harnesses: { "codex-native": "binary-missing" } }];
    renderHarnesses("codex-native");

    expect(screen.getAllByText("Not installed")).toHaveLength(2);
    expect(screen.queryByText("Needs reconfiguration")).toBeNull();
  });

  it("keeps an unconfigured credential family on the install path", () => {
    hosts = [{ ...ONLINE, configured_harnesses: { "codex-native": false } }];
    renderHarnesses("codex-native");

    expect(screen.getAllByText("Not configured")).toHaveLength(2);
    expect(screen.getByText(/Codex isn’t set up on my-laptop/)).toBeTruthy();
    expect(screen.queryByText("Not signed in")).toBeNull();
    expect(screen.queryByText(/No ChatGPT account or API key on my-laptop/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    expect(screen.getByTestId("fix-dialog")).toHaveAttribute("data-mode", "install");
  });

  it("uses dedicated recovery copy for a readiness error", () => {
    hosts = [{ ...ONLINE, configured_harnesses: { "codex-native": "readiness-error" } }];
    renderHarnesses("codex-native");

    expect(
      screen.getByText("Codex isn’t working on my-laptop. Run setup again to fix it."),
    ).toBeTruthy();
    expect(screen.queryByText(/credential is missing/)).toBeNull();
  });

  it("applies the fixed readiness immediately and shows the ready notice", () => {
    hosts = [ONLINE];
    const queryClient = renderHarnesses("codex-native");
    const hostsKey = ["hosts", { includeSandbox: false }];
    queryClient.setQueryData(hostsKey, [ONLINE]);

    fireEvent.click(screen.getByRole("button", { name: "Reconfigure" }));
    fireEvent.click(screen.getByRole("button", { name: "Complete fix" }));

    expect(screen.getByText("Codex is ready on my-laptop. Checked just now.")).toBeTruthy();
    expect(screen.getByText("· Isaac")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Reconfigure" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reconfigure credential" })).toBeNull();
    expect(queryClient.getQueryData<Host[]>(hostsKey)?.[0].configured_harnesses).toEqual({
      "codex-native": true,
    });
    expect(queryClient.getQueryData<Host[]>(hostsKey)?.[0].gateway_inference).toEqual({
      "codex-native": true,
    });
  });

  it("keeps the original broken detail when harness_install is disabled", () => {
    harnessInstall = false;
    hosts = [ONLINE];
    renderHarnesses("codex-native");

    expect(screen.getByText(/Set up Codex on my-laptop/)).toBeTruthy();
    expect(screen.queryByText("Machine")).toBeNull();
    expect(screen.queryByRole("tab", { name: /MCP servers/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reconfigure" })).toBeNull();
    expect(screen.queryByText("Not signed in")).toBeNull();
    expect(screen.queryByText(/No ChatGPT account or API key on my-laptop/)).toBeNull();
  });

  it("keeps the working detail unchanged when harness_install is disabled", () => {
    harnessInstall = false;
    hosts = [{ ...ONLINE, gateway_inference: { "claude-native": true } }];
    renderHarnesses("claude-native");

    selectTab("Settings");
    expect(screen.getByText("Unity Gateway")).toBeTruthy();
    expect(screen.queryByText("Isaac")).toBeNull();
    expect(screen.queryByText("Machine")).toBeNull();
  });

  it("hides fix actions for a managed sandbox host", () => {
    hosts = [{ ...ONLINE, sandbox_provider: "modal" }];
    renderHarnesses("codex-native");

    expect(screen.queryByRole("button", { name: "Reconfigure" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reconfigure credential" })).toBeNull();
    expect(screen.queryByTestId("fix-dialog")).toBeNull();
  });

  it("hides Sign in again for an expired account on a managed sandbox host", () => {
    hosts = [
      {
        ...ONLINE,
        sandbox_provider: "modal",
        harness_accounts: { codex: { kind: "chatgpt", expired: true } },
      },
    ];
    renderHarnesses("codex-native");

    expect(screen.getByLabelText("Needs reconfiguration: Missing credentials")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Sign in again" })).toBeNull();
  });

  it("falls back to the grid for an unknown harness slug", () => {
    hosts = [ONLINE];
    renderHarnesses("not-a-harness");

    expect(screen.getByRole("heading", { name: "Harnesses" })).toBeTruthy();
  });
});

describe("Launch settings compatibility", () => {
  it.each(["claude-native", "codex-native"])(
    "shows one startup configuration with separate env values and arguments for %s",
    (harness) => {
      hosts = [{ ...ONLINE, configured_harnesses: { [harness]: true } }];
      startupData = {
        ...STARTUP,
        command: "isaac",
        resolved_path: "/opt/bin/isaac",
        args: ["codex", "--", ""],
        configured_command: "/usr/bin/env",
        configured_args: [
          "-i",
          "-u",
          "REMOVED",
          "TOKEN=synthetic-secret=with spaces",
          "EMPTY=",
          "isaac",
          "codex",
          "--",
          "",
        ],
        environment: {
          inherit: false,
          variables: { TOKEN: "synthetic-secret=with spaces", EMPTY: "" },
          unset: ["REMOVED"],
        },
      };
      renderHarnesses(harness);
      selectTab("Settings");
      const startupSection = screen
        .getByRole("heading", { name: "Startup configuration" })
        .closest("section")!;
      expect(
        within(startupSection)
          .getAllByRole("heading", { level: 3 })
          .map((heading) => heading.textContent),
      ).toEqual(["Command", "Environment", "Arguments"]);
      expect(within(startupSection).getByText("/opt/bin/isaac")).toBeTruthy();
      const envSection = within(startupSection)
        .getByRole("heading", { name: "Environment" })
        .closest("div")!;
      expect(within(envSection).getByText("TOKEN")).toBeTruthy();
      expect(within(envSection).getByText("synthetic-secret=with spaces")).toBeTruthy();
      expect(within(envSection).getByText("EMPTY")).toBeTruthy();
      expect(within(envSection).getByText('""')).toBeTruthy();
      expect(within(envSection).getByText("Inherited environment cleared.")).toBeTruthy();
      expect(within(envSection).getByText("REMOVED")).toBeTruthy();
      const argsSection = within(startupSection)
        .getByRole("heading", { name: "Arguments" })
        .closest("div")!;
      expect(
        within(argsSection)
          .getAllByRole("listitem")
          .map((item) => item.textContent),
      ).toEqual(["codex", "--", '""']);
      expect(screen.queryByText("Configured invocation")).toBeNull();
      expect(screen.queryByText("Environment overrides")).toBeNull();
      expect(screen.queryByText("Startup arguments")).toBeNull();
      expect(screen.queryByText("/usr/bin/env")).toBeNull();
      expect(screen.queryByText("TOKEN=synthetic-secret=with spaces")).toBeNull();
      expect(screen.getAllByText("synthetic-secret=with spaces")).toHaveLength(1);
      expect(screen.getByText(/not a running session's full command or environment/)).toBeTruthy();
    },
  );

  it("does not invent env settings for an opaque env wrapper", () => {
    hosts = [ONLINE];
    startupData = {
      ...STARTUP,
      command: "/usr/bin/env",
      resolved_path: "/usr/bin/env",
      args: ["-S", "TOKEN=synthetic-secret isaac codex --"],
      configured_command: "/usr/bin/env",
      configured_args: ["-S", "TOKEN=synthetic-secret isaac codex --"],
      environment: null,
    };
    renderHarnesses("claude-native");
    selectTab("Settings");
    expect(screen.getByText(/Cannot separate environment values/)).toBeTruthy();
    expect(screen.getByText("/usr/bin/env")).toBeTruthy();
    expect(screen.getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "-S",
      "TOKEN=synthetic-secret isaac codex --",
    ]);
    expect(screen.queryByText("Inherited values are not listed.")).toBeNull();
    expect(screen.queryByText("Configured invocation")).toBeNull();
  });

  it("requests an update when the host reports args without env metadata", () => {
    hosts = [ONLINE];
    startupData = { ...STARTUP, configured_args: undefined, environment: undefined };
    renderHarnesses("claude-native");
    selectTab("Settings");
    expect(screen.getByText("Update my-laptop to see environment settings.")).toBeTruthy();
    expect(screen.queryByText("Configured invocation")).toBeNull();
  });

  it("asks for a host update when an older host reports only the argument count", () => {
    hosts = [ONLINE];
    startupData = { ...STARTUP, args: null, configured_args: null, environment: null };
    renderHarnesses("claude-native");
    selectTab("Settings");
    expect(
      screen.getByText("4 configured arguments. Update my-laptop to view values."),
    ).toBeTruthy();
  });

  it.each([404, 501, 502])("keeps the credential when startup returns %s", (status) => {
    hosts = [ONLINE];
    startupError = new ApiError("unavailable", status, null);
    renderHarnesses("claude-native");
    selectTab("Settings");
    expect(screen.getByText("Signed in")).toBeTruthy();
    expect(screen.queryByText("Startup configuration")).toBeNull();
    if (status === 501)
      expect(screen.getByText("Update my-laptop to see launch settings.")).toBeTruthy();
    if (status === 502)
      expect(screen.getByText("Couldn't load launch settings from my-laptop.")).toBeTruthy();
    if (status === 404) expect(screen.queryByText(/launch settings/)).toBeNull();
  });

  it("uses the host selected on the grid", () => {
    hosts = [ONLINE, { ...ONLINE, host_id: "h2", name: "second-host" }];
    renderHarnesses();
    fireEvent.pointerDown(screen.getByRole("button", { name: "my-laptop" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(screen.getByRole("menuitem", { name: "second-host" }));
    fireEvent.click(screen.getByTestId("harness-settings-claude-native"));
    expect(startupCalls).toHaveBeenLastCalledWith("h2", "claude-native");
  });
});

it("shows installed plugin metadata and disabled bundled servers", () => {
  hosts = [ONLINE];
  inventory = {
    ...INVENTORY,
    context: {
      ...INVENTORY.context,
      plugins: [
        {
          id: "claude:hooks@market",
          harness: "claude",
          name: "hooks",
          skills: [],
          description: "Hook helpers",
          marketplace: "market",
          version: "1.2.3",
          enabled: false,
          mcp_servers: ["bundled"],
          has_hooks: true,
          has_commands: true,
        },
      ],
    },
  };
  renderHarnesses("claude-native");
  selectTab("Plugins · 1");
  expect(screen.getByTestId("catalog-row-hooks").textContent).toContain("Disabled");
  fireEvent.click(screen.getByTestId("catalog-row-hooks"));
  expect(screen.getByText("Hook helpers")).toBeTruthy();
  expect(screen.getByText(/v1\.2\.3 · market · Disabled/)).toBeTruthy();
  selectTab("MCPs · 1");
  expect(screen.getByTestId("catalog-row-bundled")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Plugins" }));
  expect(screen.getByRole("tab", { name: "Plugins · 1" }).getAttribute("aria-selected")).toBe(
    "true",
  );
});

it.each(["codex-native", "cursor-native"])(
  "does not request Claude plugin metadata on %s",
  (harness) => {
    hosts = [{ ...ONLINE, configured_harnesses: { [harness]: true } }];
    renderHarnesses(harness);
    expect(pluginMetadataRequested).toBe(false);
  },
);

it("opens skill markdown only on demand and returns to Skills", () => {
  hosts = [ONLINE];
  renderHarnesses("claude-native");
  expect(contentLookup).toBeNull();
  selectTab("Skills · 1");
  fireEvent.click(screen.getByTestId("catalog-row-review"));
  expect(contentLookup).toEqual([ONLINE.host_id, "claude-native", "review"]);
  expect(screen.getByRole("heading", { name: "Instructions" })).toBeTruthy();
  expect(document.querySelector("img")).toBeNull();
  expect(document.querySelector("script")).toBeNull();
  expect(screen.queryByRole("link", { name: "guide" })).toBeNull();
  expect(screen.getByRole("link", { name: "docs" }).getAttribute("href")).toBe(
    "https://example.test/docs",
  );
  fireEvent.click(screen.getByRole("button", { name: "Skills" }));
  expect(screen.getByRole("tab", { name: "Skills · 1" }).getAttribute("aria-selected")).toBe(
    "true",
  );
});

it("opens a namespaced plugin skill and returns to that plugin", () => {
  hosts = [ONLINE];
  contentQuery = { data: { ...CONTENT, truncated: true }, isPending: false };
  renderHarnesses("claude-native");
  selectTab("Plugins · 1");
  fireEvent.click(screen.getByTestId("catalog-row-toolkit"));
  fireEvent.click(screen.getByTestId("catalog-row-lint"));
  expect(contentLookup).toEqual([ONLINE.host_id, "claude-native", "toolkit:lint"]);
  expect(screen.getByText(/Contents truncated/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "toolkit" }));
  expect(screen.getByRole("heading", { name: "toolkit" })).toBeTruthy();
});

it.each([501, 502, 504])("reports skill content failures (%s)", (status) => {
  hosts = [ONLINE];
  contentQuery = { error: new ApiError("private", status, null), isPending: false };
  renderHarnesses("claude-native");
  selectTab("Skills · 1");
  fireEvent.click(screen.getByTestId("catalog-row-review"));
  expect(
    screen.getByText(
      status === 501 ? "Update my-laptop to see skill contents." : "Couldn't load skill contents.",
    ),
  ).toBeTruthy();
  expect(screen.queryByText("private")).toBeNull();
});

it("disables plain and plugin skill links after an old-server 404", () => {
  hosts = [ONLINE];
  contentQuery = { error: new ApiError("404", 404, null), isPending: false };
  renderHarnesses("claude-native");
  selectTab("Skills · 1");
  fireEvent.click(screen.getByTestId("catalog-row-review"));
  expect(screen.getByTestId("catalog-row-review").tagName).not.toBe("BUTTON");
  expect(screen.queryByText("Couldn't load skill contents.")).toBeNull();
  selectTab("Plugins · 1");
  fireEvent.click(screen.getByTestId("catalog-row-toolkit"));
  expect(screen.getByTestId("catalog-row-lint").tagName).not.toBe("BUTTON");
});

it.each([false, true])("opens installed plugin skill IDs when enabled=%s", (enabled) => {
  hosts = [ONLINE];
  inventory = {
    ...INVENTORY,
    context: {
      ...INVENTORY.context,
      plugins: ["first", "second"].map((marketplace) => ({
        id: marketplace,
        name: "toolkit",
        harness: "claude",
        marketplace,
        enabled,
        skills: ["lint"],
        skill_entries: [{ id: `${marketplace}-skill`, name: "lint" }],
      })),
    },
  };
  renderHarnesses("claude-native");
  selectTab("Plugins · 2");
  fireEvent.click(screen.getAllByTestId("catalog-row-toolkit")[1]);
  fireEvent.click(screen.getByTestId("catalog-row-lint"));
  expect(contentSource).toBe("second-skill");
  expect(contentLookup).toEqual([ONLINE.host_id, "claude-native", "toolkit:lint"]);
  expect(screen.getByRole("heading", { name: "Instructions" })).toBeTruthy();
});

it("requests a host update instead of guessing installed skill identity", () => {
  hosts = [ONLINE];
  inventory = {
    ...INVENTORY,
    context: {
      ...INVENTORY.context,
      plugins: [
        {
          ...INVENTORY.context.plugins[0],
          marketplace: "market",
          enabled: true,
        },
      ],
    },
  };
  renderHarnesses("claude-native");
  selectTab("Plugins · 1");
  fireEvent.click(screen.getByTestId("catalog-row-toolkit"));
  expect(screen.getByText("Update my-laptop to read installed plugin skills.")).toBeTruthy();
  expect(screen.getByTestId("catalog-row-lint").tagName).not.toBe("BUTTON");
  expect(contentLookup).toBeNull();
});

it("probes only the expanded server, showing its tools, count and status", () => {
  hosts = [ONLINE];
  renderHarnesses("claude-native");
  expect(mcpLookups.every((lookup) => !lookup[4])).toBe(true);
  expect(screen.queryByRole("status")).toBeNull();
  fireEvent.click(screen.getByTestId("catalog-row-linear"));
  expect(mcpLookups).toContainEqual([ONLINE.host_id, "claude", "linear", "toolkit", true]);
  expect(mcpLookups.some((lookup) => lookup[2] === "github" && lookup[4])).toBe(false);
  expect(screen.getByText("read_docs")).toBeTruthy();
  const toolDetails = screen.getByText("Read documentation").closest("details")!;
  expect(toolDetails.open).toBe(false);
  fireEvent.click(screen.getByText("read_docs"));
  expect(toolDetails.open).toBe(true);
  expect(screen.getByText("· 1 tool")).toBeTruthy();
  expect(screen.getByRole("status").textContent).toBe("Connected");
  expect(screen.getByRole("status").firstElementChild).toHaveClass("bg-success");
  expect(screen.queryByText(/Tools reported when probed/)).toBeNull();
  fireEvent.click(screen.getByTestId("catalog-row-linear"));
  expect(screen.queryByText("read_docs")).toBeNull();
});

it("uses the same lazy tools accordion on plugin pages", () => {
  hosts = [ONLINE];
  renderHarnesses("claude-native");
  selectTab("Plugins · 1");
  fireEvent.click(screen.getByTestId("catalog-row-toolkit"));
  selectTab("MCPs · 1");
  expect(mcpLookups.every((lookup) => !lookup[4])).toBe(true);
  fireEvent.click(screen.getByTestId("catalog-row-linear"));
  expect(screen.getByText("read_docs")).toBeTruthy();
  expect(mcpLookups).toContainEqual([ONLINE.host_id, "claude", "linear", "toolkit", true]);
});

it.each([
  [501, "Update my-laptop to list tools."],
  [502, "Couldn't reach GitHub."],
  [503, "Host is busy probing other MCP servers. Collapse and reopen to retry."],
  [504, "Couldn't reach GitHub."],
])("shows the MCP error for %s", (status, message) => {
  hosts = [ONLINE];
  mcpResult = { error: new ApiError("private", status, null), isPending: false };
  renderHarnesses("claude-native");
  fireEvent.click(screen.getByTestId("catalog-row-github"));
  expect(screen.getByText(message)).toBeTruthy();
  expect(screen.queryByText("private")).toBeNull();
});

it("hides MCP expansion on old-server 404", () => {
  hosts = [ONLINE];
  mcpResult = { error: new ApiError("404", 404, null), isPending: false };
  renderHarnesses("claude-native");
  fireEvent.click(screen.getByTestId("catalog-row-github"));
  expect(screen.getByTestId("catalog-row-github").tagName).not.toBe("BUTTON");
  expect(screen.getByTestId("catalog-row-linear").tagName).not.toBe("BUTTON");
  // The fallback rows use the same display names as the expandable ones.
  expect(within(screen.getByTestId("catalog-row-github")).getByText("GitHub")).toBeTruthy();
  expect(within(screen.getByTestId("catalog-row-linear")).getByText("Linear")).toBeTruthy();
  expect(screen.queryByText("Couldn't reach GitHub.")).toBeNull();
});

it.each([
  [
    "needs_auth",
    "Needs auth",
    "bg-warning",
    "Authentication required. Harness sign-in credentials cannot be reused for this probe.",
  ],
  ["timeout", "Failed to connect", "bg-destructive", "MCP probe timed out."],
  ["unreachable", "Failed to connect", "bg-destructive", "Couldn't reach this MCP server."],
  [
    "unsupported",
    "Failed to connect",
    "bg-destructive",
    "This MCP configuration cannot be probed from the host.",
  ],
])("reports probe status %s without a tool count", (connection, statusLabel, color, detail) => {
  hosts = [ONLINE];
  mcpResult = { data: { tools: [], connection, truncated: false }, isPending: false };
  renderHarnesses("claude-native");
  fireEvent.click(screen.getByTestId("catalog-row-github"));
  const status = screen.getByRole("status");
  expect(status.textContent).toBe(statusLabel);
  expect(status.firstElementChild).toHaveClass(color);
  expect(screen.getByText(detail)).toBeTruthy();
  expect(screen.queryByText(/· 0 tools/)).toBeNull();
});

it.each([false, true])("uses installed MCP identity and honors enabled=%s", (enabled) => {
  hosts = [ONLINE];
  inventory = {
    ...INVENTORY,
    context: {
      ...INVENTORY.context,
      plugins: ["first", "second"].map((marketplace) => ({
        id: marketplace,
        name: "toolkit",
        harness: "claude",
        marketplace,
        enabled,
        skills: [],
        mcp_servers: ["docs"],
        mcp_entries: [{ id: `${marketplace}-mcp`, name: "docs" }],
      })),
    },
  };
  renderHarnesses("claude-native");
  selectTab("Plugins · 2");
  fireEvent.click(screen.getAllByTestId("catalog-row-toolkit")[1]);
  selectTab("MCPs · 1");
  const row = screen.getByTestId("catalog-row-docs");
  fireEvent.click(row);
  if (enabled) {
    expect(mcpSources).toContain("second-mcp");
    expect(mcpSources).not.toContain("first-mcp");
    expect(screen.getByText("read_docs")).toBeTruthy();
  } else {
    expect(row.tagName).not.toBe("BUTTON");
    expect(screen.getByText(/This plugin is disabled/)).toBeTruthy();
    expect(mcpSources).toEqual([]);
    expect(mcpLookups.every((lookup) => !lookup[4])).toBe(true);
  }
});

it("requests a host update instead of guessing installed MCP identity", () => {
  hosts = [ONLINE];
  inventory = {
    ...INVENTORY,
    context: {
      ...INVENTORY.context,
      plugins: [
        {
          ...INVENTORY.context.plugins[0],
          marketplace: "market",
          enabled: true,
          mcp_servers: ["docs"],
        },
      ],
    },
  };
  renderHarnesses("claude-native");
  selectTab("Plugins · 1");
  fireEvent.click(screen.getByTestId("catalog-row-toolkit"));
  selectTab("MCPs · 1");
  expect(screen.getByText("Update my-laptop to inspect installed plugin MCP tools.")).toBeTruthy();
  expect(screen.getByTestId("catalog-row-docs").tagName).not.toBe("BUTTON");
  expect(mcpSources).toEqual([]);
});
