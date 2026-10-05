// Integration tests for Settings → Harnesses: real per-host readiness
// derivation (harnessReadinessOnHost runs unmocked) driving the card status,
// Set-up affordance, Installed filter, and the details page. The host query, the
// host inventory, and the heavy composer/dialog imports are mocked so it renders
// without a backend.

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
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
    "## Instructions\nRead the diff.\n![private image](https://example.test/pixel)\n<script>secret()</script>",
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
  arg_count: 2,
};
let startupError: ApiError | null = null;
const startupCalls = vi.fn();
let hosts: Host[] = [];
vi.mock("@/hooks/useHosts", async (importActual) => ({
  ...(await importActual()),
  useHosts: () => ({ data: hosts }),
  useHarnessStartup: (hostId: string, harness: string) => {
    startupCalls(hostId, harness);
    return { data: startupError ? undefined : STARTUP, error: startupError, isPending: false };
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
  setupDialogProps.mockReset();
  startupCalls.mockClear();
  startupError = null;
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
    expect(screen.getByText("/opt/bin/claude")).toBeTruthy();
    expect(screen.getByText("2 configured arguments (values hidden)")).toBeTruthy();
    expect(screen.getByText(/harness.claude-native.command/)).toBeTruthy();
    expect(screen.getByText(/workspace's .omnigent/)).toBeTruthy();
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

describe("Launch settings compatibility", () => {
  it.each([404, 501, 502])("keeps the credential when startup returns %s", (status) => {
    hosts = [ONLINE];
    startupError = new ApiError("unavailable", status, null);
    renderHarnesses("claude-native");
    selectTab("Settings");
    expect(screen.getByText("Signed in")).toBeTruthy();
    expect(screen.queryByText("Path to binary")).toBeNull();
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
  expect(screen.queryByRole("img", { name: "Connected" })).toBeNull();
  fireEvent.click(screen.getByTestId("catalog-row-linear"));
  expect(mcpLookups).toContainEqual([ONLINE.host_id, "claude", "linear", "toolkit", true]);
  expect(mcpLookups.some((lookup) => lookup[2] === "github" && lookup[4])).toBe(false);
  expect(screen.getByText("read_docs")).toBeTruthy();
  expect(screen.getByText("· 1 tool")).toBeTruthy();
  expect(screen.getByRole("img", { name: "Connected" })).toBeTruthy();
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
  [502, "Couldn't reach github."],
  [503, "Host is busy probing other MCP servers. Collapse and reopen to retry."],
  [504, "Couldn't reach github."],
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
  expect(screen.queryByText("Couldn't reach github.")).toBeNull();
});

it.each([
  [
    "needs_auth",
    "Authentication required. Harness sign-in credentials cannot be reused for this probe.",
  ],
  ["timeout", "MCP probe timed out."],
  ["unreachable", "Couldn't reach this MCP server."],
  ["unsupported", "This MCP configuration cannot be probed from the host."],
])("reports probe status %s without a tool count", (connection, label) => {
  hosts = [ONLINE];
  mcpResult = { data: { tools: [], connection, truncated: false }, isPending: false };
  renderHarnesses("claude-native");
  fireEvent.click(screen.getByTestId("catalog-row-github"));
  expect(screen.getByRole("img", { name: label })).toBeTruthy();
  expect(screen.getByText(label)).toBeTruthy();
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
