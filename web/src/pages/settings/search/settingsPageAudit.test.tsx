// Every Settings section that can be audited now, rendered by the real page with its real
// controls, providers and queries, against an explicit table of server answers. Every control
// it renders answers to the catalog, and every row of an audited section renders somewhere.
// What's left out, and why, is listed below rather than skipped silently.

import { writeRetentionDays } from "@/lib/retentionPreferences";
import { EmbeddedProvider } from "@/lib/embedded";
import { writeThemePalette } from "@/lib/themePalette";
import { PROJECT_LABEL_KEY } from "@/lib/sessionListCache";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { setOmnigentHostConfig } from "@/lib/host";
import { SettingsPage } from "@/pages/SettingsPage";
import type { SettingsSectionId } from "@/shell/settingsNav";
import {
  accessibleName,
  auditOpenedPortals,
  auditSettingsDomFindings,
  domAuditProblems,
  settingsCoverageProblems,
  type SettingsDomFinding,
  type SettingsDomScenarioAudit,
} from "@/test/settingsDomAudit";
import { type SettingsRoute, settingsRouteTable } from "@/test/settingsPageRoutes";
import {
  observeRendered,
  provenOverlayOrigin,
  waitUntilSettled,
  type Witness,
  wholePageProblems,
  witnessed,
  witnessedNow,
} from "@/test/settingsPageSteps";

import { SECTION_CATALOGS, SETTINGS_ENTRIES } from "./catalog";
import type { SettingsEntry } from "./catalog/types";
import { UNOWNED_CONTROLS } from "./settingsUnownedBaseline";
import {
  HARNESS_CARD_ENTRIES,
  FIX_DIALOG_PARTS,
  HARNESS_CREDENTIAL_ENTRIES,
  HARNESS_DETAIL_ENTRIES,
  HARNESS_PLUGIN_ENTRIES,
  HARNESS_SKILL_ENTRIES,
  HARNESSES_GRID_ENTRY,
  isInventoryPageHarness,
  isSkillPageHarness,
  MCP_SERVER_LIST_ENTRIES,
  NATIVE_HARNESS_IDS,
  type NativeHarnessId,
} from "./catalog/harnesses";

const SERVER_INFO: ServerInfo = {
  ...FALLBACK_SERVER_INFO,
  accounts_enabled: true,
  login_url: "/login",
  sibling_sessions: true,
  enabled_connections: ["mcp"],
  session_title_instructions: true,
  agent_install: true,
  agent_detail: true,
  features: { custom_agents_settings_ui: true },
} as ServerInfo;

const MANAGED_AGENT = {
  id: "ag_polly",
  name: "polly",
  description: "Polly",
  harness: "claude-sdk",
  version: 1,
  created_at: 1_780_000_000,
  updated_at: null,
  builtin: true,
  skills: [],
};
const USER_AGENT = { ...MANAGED_AGENT, id: "ag_mine", name: "my-reviewer", builtin: false };
/** An agent's own page replaces the list. */
const AGENT_PAGE = "An agent's own page replaces the agent list.";
const ARCHIVED_PAGE = {
  data: [
    {
      id: "sess_archived_1",
      object: "conversation",
      title: "Archived planning session",
      created_at: 1_780_000_000,
      updated_at: 1_780_005_000,
      labels: {},
      permission_level: null,
      owner: "alice",
      archived: true,
    },
  ],
  first_id: "sess_archived_1",
  last_id: "sess_archived_1",
  has_more: false,
};
/** The archived session, filed in a project. */
const ARCHIVED_IN_PROJECT = {
  ...ARCHIVED_PAGE.data[0],
  labels: { [PROJECT_LABEL_KEY]: "Docs site" },
};
const page = (data: unknown[]) => ({ data, has_more: false, last_id: null });
const STARTUP = { resolved_path: "/usr/local/bin/agent", command_source: "default", arg_count: 0 };
/** A harness's detail page asks the host for its startup command, MCP servers, plugins and skills. */
const harnessDetailRoutes = (harness: string, command: string): SettingsRoute[] => [
  { url: `/v1/hosts/host_1/harnesses/${harness}/startup`, body: { ...STARTUP, command } },
  {
    url: `/v1/skills?host_id=host_1&harness=${harness}&path=%7E`,
    body: { skills: [], warnings: [] },
  },
];

const SHARING_BODY = {
  object: "sharing",
  sharing_mode: "on",
  editable: true,
  options: ["on", "read_only", "restricted_read_only", "off"],
  public_sharing_enabled: true,
  public_sharing_editable: true,
  public_sharing_max_level: "read",
  public_sharing_max_level_editable: true,
  public_sharing_max_level_options: ["read", "edit"],
  default_public_sessions: "off",
  default_public_sessions_editable: true,
  default_public_sessions_options: ["off", "sandbox", "all"],
  peer_session_messaging_enabled: true,
  peer_session_messaging_editable: true,
  sibling_session_destinations: "any_host",
  sibling_session_destinations_editable: true,
  sibling_session_destinations_options: ["current_host", "current_host_or_new_sandbox", "any_host"],
};

const ROUTES: readonly SettingsRoute[] = [
  { url: "/v1/me", body: { user_id: "alice", is_admin: true } },
  {
    url: "/v1/agents",
    body: { object: "list", data: [{ ...MANAGED_AGENT }], has_more: false, last_id: null },
  },
  { url: "/v1/agents?scope=user&limit=50", body: page([USER_AGENT]) },
  { url: "/v1/agents?limit=50", body: page([MANAGED_AGENT]) },
  { url: "/v1/agents?limit=50&scope=user", body: page([USER_AGENT]) },
  {
    url: "/v1/harnesses",
    body: {
      object: "list",
      data: [{ id: "claude-native", label: "Claude Code", capabilities: { effort: "anthropic" } }],
      setup_steps: {},
    },
  },
  {
    url: "/v1/hosts",
    body: {
      hosts: [
        {
          host_id: "host_1",
          name: "laptop",
          owner: "alice",
          status: "online",
          sandbox_provider: null,
          configured_harnesses: { "claude-native": true, "codex-native": true },
        },
      ],
    },
  },
  { url: "/v1/settings/session-title-instructions", body: { instructions: null, max_chars: 500 } },
  {
    url: "/v1/mcp-registry/services",
    body: {
      data: [
        {
          id: "tracker",
          title: "Tracker",
          description: "Issue tracker",
          auth: "oauth",
          connected: false,
          tools: ["search_issues"],
          connect_provider: "mcp-tracker",
        },
      ],
      next_cursor: null,
    },
  },
  {
    url: "/v1/policies",
    body: {
      object: "list",
      data: [
        {
          id: "p1",
          object: "default_policy",
          name: "block_external",
          type: "python",
          handler: "omnigent.policies.block_external",
          factory_params: null,
          enabled: true,
          created_at: 1,
          updated_at: null,
          created_by: null,
        },
      ],
    },
  },
  {
    url: "/v1/policy-registry",
    body: {
      object: "list",
      data: [
        {
          handler: "omnigent.policies.block_external",
          kind: "callable",
          name: "block_external",
          description: "Blocks external requests",
          params_schema: null,
        },
      ],
    },
  },
  {
    url: "/v1/sharing",
    body: SHARING_BODY,
  },
  {
    url: "/auth/users",
    body: {
      users: [
        {
          id: "alice",
          is_admin: true,
          created_at: 1_780_000_000,
          last_login_at: null,
          has_password: true,
        },
      ],
    },
  },
  {
    url: "/v1/hosts/host_1/mcp-servers",
    body: {
      mcp_servers: ["claude", "codex"].map((harness) => ({
        name: "docs",
        harness,
        transport: "http",
        scope: "user",
        enabled: true,
        url_host: "mcp.example.com",
      })),
    },
  },
  { url: "/v1/hosts/host_1/plugins", body: { plugins: [] } },
  ...NATIVE_HARNESS_IDS.flatMap((harness) =>
    harnessDetailRoutes(harness, harness.replace(/-native$/, "")),
  ),
  {
    url: "/v1/sessions?order=desc&sort_by=updated_at&limit=100&visibility=archived&include_archived=true",
    body: ARCHIVED_PAGE,
  },
  {
    url: "/v1/sessions?order=desc&sort_by=updated_at&limit=30&visibility=archived&expand_families=true&include_archived=true",
    body: ARCHIVED_PAGE,
  },
];

/** What shows the section has loaded its data: text it renders, a catalog row, or a labelled control. */
/**
 * Something a scenario opens after its own audit, such as a dialog or a popover, audited as
 * `section › scenario › name`. Its witness shows the opened content is ready.
 */
interface ScenarioAction {
  name: string;
  /**
   * For a captured overlay, the opener it opens from, `entry:part`: proved rendered and owned
   * before `open` runs, it stands in for that opener while the overlay stays open.
   */
  origin?: string;
  /** Puts the page where its opener shows, such as starting a selection, before the origin's proof. */
  prepare?: () => Promise<void>;
  open: () => Promise<void>;
  witness: Witness;
  /** The states the opened content puts its entries in, beside the scenario's own. */
  states?: Readonly<Record<string, readonly string[]>>;
  /** The scenario's states it ends while open, such as browsing once a selection starts. */
  leaves?: Readonly<Record<string, readonly string[]>>;
  /** What happens next while it stays open, each audited in turn before it closes. */
  steps?: readonly ScenarioStep[];
}

/** One step of an open action: something done, its witness, and the states it leaves. */
interface ScenarioStep {
  name: string;
  run: () => Promise<void>;
  witness: Witness;
  states?: Readonly<Record<string, readonly string[]>>;
}

/**
 * Content that opens inside the page, such as a form in a row, not in a portal: audited with
 * the whole page as its own snapshot, which must be clean, and then closed by `close`.
 */
interface InlineAction {
  name: string;
  open: () => Promise<void>;
  witness: Witness;
  /** The states the opened content puts its entries in, beside the scenario's own. */
  states?: Readonly<Record<string, readonly string[]>>;
  close: () => Promise<void>;
}

/** No Electron shell: the page's own starting state, before a scenario installs one. */
function resetScenario() {
  localStorage.clear();
  setOmnigentHostConfig({});
}

interface SettingsAuditScenario {
  section: SettingsSectionId;
  /** The shell (web or Electron), then the state it puts the page in. */
  scenario: string;
  /** Server info that differs from SERVER_INFO in this scenario. */
  info?: Partial<ServerInfo>;
  /** Rendered inside a browser embed, which follows its host's appearance. */
  embedded?: boolean;
  /** Defaults to the section's own page. */
  path?: string;
  /** Server answers that differ from ROUTES in this scenario. */
  routes?: readonly SettingsRoute[];
  /**
   * The states this scenario puts entries in, by entry id. Parts declared to show in one of
   * them must show; the rest of the entry's state parts must not. `ready` witnesses each.
   */
  states?: Readonly<Record<string, readonly string[]>>;
  /** Rendered once loaded; the audit waits for each, and fails if one never appears. */
  ready: readonly Witness[];
  /** Rows this scenario can't show, with why; another scenario must show each. */
  hides?: Readonly<Record<string, string>>;
  /** Puts the page's own stores in this scenario's state, after the reset and before rendering. */
  setup?: () => void;
  /** Portals to open one at a time after the scenario's audit, each audited while open. */
  actions?: readonly ScenarioAction[];
  /** Content to open inside the page, one at a time, each audited as its own snapshot. */
  inlineActions?: readonly InlineAction[];
}

const ALL_HARNESSES = HARNESS_CARD_ENTRIES.members;
const ON_DETAIL_PAGE = "It's on the harness's own page.";
// The grid shows none of the harness pages' rows.
const HARNESS_PAGE_HIDES = Object.fromEntries(
  [
    ...ALL_HARNESSES.flatMap((harness) => [
      HARNESS_DETAIL_ENTRIES.entry(harness).id,
      HARNESS_CREDENTIAL_ENTRIES.entry(harness).id,
    ]),
    ...HARNESS_PLUGIN_ENTRIES.members.map((harness) => HARNESS_PLUGIN_ENTRIES.entry(harness).id),
    ...HARNESS_SKILL_ENTRIES.members.map((harness) => HARNESS_SKILL_ENTRIES.entry(harness).id),
    ...MCP_SERVER_LIST_ENTRIES.members.map((harness) => MCP_SERVER_LIST_ENTRIES.entry(harness).id),
  ].map((id) => [id, ON_DETAIL_PAGE]),
);
const READY_CARD = ["opens its page", "has settings"];
const cardStates = (harnesses: readonly NativeHarnessId[], states: readonly string[]) =>
  Object.fromEntries(harnesses.map((harness) => [HARNESS_CARD_ENTRIES.entry(harness).id, states]));
const configured = (harnesses: readonly NativeHarnessId[], value: boolean | string) =>
  Object.fromEntries(harnesses.map((harness) => [harness, value]));
const harnessHost = (overrides: Record<string, unknown> = {}) => ({
  host_id: "host_1",
  name: "laptop",
  owner: "alice",
  status: "online",
  sandbox_provider: null,
  configured_harnesses: { "claude-native": true, "codex-native": true },
  ...overrides,
});
const hostsRoute = (hosts: readonly object[]): SettingsRoute => ({
  url: "/v1/hosts",
  body: { hosts },
});
// Settings installs harnesses here: the server offers it and lists which ones.
// What shows the title instructions loaded empty, saved, or failed to load, apart from their buttons.
const DESKTOP_ONLY = "Only the desktop shell shows it.";
const ADMIN_VIEW_NOT_LOADED =
  "Shows only to an admin whose identity is loaded, which no scenario loads yet.";
const PROJECT_KANBAN_OFF =
  "Shows only when the server turns on project kanban, which no scenario does.";
const PROACTIVE_AGENT_OFF =
  "Shows only where the server offers the proactive agent, which no scenario sets up yet.";
const OMNI_ADVISOR_OFF = "The server doesn't offer Omni Advisor.";
const OMNI_ADVISOR_SETTINGS: SettingsRoute = {
  url: "/v1/proactive-agent/settings",
  body: { workspace_enabled: true, user_enabled: true, enabled: true, can_manage_workspace: true },
};
const OMNI_ADVISOR_SETTINGS_FAIL: SettingsRoute = {
  url: "/v1/proactive-agent/settings",
  status: 500,
  body: { detail: "Try again." },
};
const OMNI_ADVISOR_PROMPT: SettingsRoute = {
  url: "/v1/proactive-agent/prompt",
  body: { prompt: "Be useful.", default_prompt: "Be useful.", overridden: false },
};
const FLOATING_PANES_UNAVAILABLE =
  "Shows only where sidebar v2 or floating panes is on, which no scenario turns on.";
const APPEARANCE_HIDES = { "pane-corners": FLOATING_PANES_UNAVAILABLE };
const GENERAL_WEB_HIDES = {
  "open-links-in-app": DESKTOP_ONLY,
  "global-shortcut": DESKTOP_ONLY,
  "floating-composer-shortcut": DESKTOP_ONLY,
  "widen-window-for-right-sidebar": DESKTOP_ONLY,
  "browser-agent-tools": DESKTOP_ONLY,
  "admin-view": ADMIN_VIEW_NOT_LOADED,
  "project-kanban-board": PROJECT_KANBAN_OFF,
  "proactive-agent": PROACTIVE_AGENT_OFF,
};
const TITLES_LOADED = "0 / 500 characters";
const TITLES_SAVED = "16 / 500 characters";
const TITLES_FAILED = "Couldn't load your title instructions.";
const NO_CONNECTIONS = "The server lists no GitHub or Databricks connection.";

/** A managed MCP service as the registry lists it. */
function service(id: string, title: string, auth: string, connected: boolean) {
  return {
    id,
    title,
    description: `${title} service`,
    auth,
    connected,
    tools: ["search"],
    connect_provider: `mcp-${id}`,
  };
}

/** A button in one catalog row, by the row's entry id and the button's name. */
function rowButton(entryId: string, name: string): HTMLElement {
  const row = document.querySelector<HTMLElement>(`[data-settings-item="${CSS.escape(entryId)}"]`);
  if (!row) throw new Error(`no row ${entryId}`);
  return within(row).getByRole("button", { name });
}

/** A button of one service's card, by the service's title. */
function serviceCard(title: string, button: string): HTMLElement {
  const card = screen.getByText(title).closest(".rounded-md") as HTMLElement;
  return within(card).getByRole("button", { name: button });
}

/** The password change's reply, held until the scenario's step releases it. */
const PASSWORD_CHANGE = (() => {
  let release: () => void = () => {};
  const reply = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { reply, release: () => release() };
})();

const INSTALL_URL = "https://github.example.com/apps/omnigent/installations/new";

/**
 * Sandbox Integrations with both connections listed, each status answered as given; null
 * answers it with a server error, as when the status can't be read.
 */
function integrationScenario({
  scenario,
  github,
  databricks,
  ready,
  hides,
  states,
}: {
  scenario: string;
  github: Record<string, unknown> | null;
  databricks: Record<string, unknown> | null;
  ready: readonly Witness[];
  hides?: Readonly<Record<string, string>>;
  states: Readonly<Record<string, readonly string[]>>;
}): SettingsAuditScenario {
  const answer = (url: string, body: Record<string, unknown> | null): SettingsRoute =>
    body ? { url, body } : { url, status: 500, body: {} };
  return {
    section: "integrations",
    scenario: `web › ${scenario}`,
    info: { enabled_connections: ["github", "databricks", "mcp"] },
    routes: [
      answer("/v1/connections/github/status", github),
      answer("/v1/connections/databricks/status", databricks),
    ],
    ready,
    hides,
    states,
  };
}

/** A harness's plugin and skill pages' rows, where it has those pages. */
const nestedPageIds = (harness: NativeHarnessId) => [
  ...(isInventoryPageHarness(harness) ? [HARNESS_PLUGIN_ENTRIES.entry(harness).id] : []),
  ...(isSkillPageHarness(harness) ? [HARNESS_SKILL_ENTRIES.entry(harness).id] : []),
];
/** A harness page's rows: its own, its credential card's, its MCP list's and its nested pages'. */
const pageRowIds = (harness: NativeHarnessId) => [
  HARNESS_DETAIL_ENTRIES.entry(harness).id,
  HARNESS_CREDENTIAL_ENTRIES.entry(harness).id,
  ...(isInventoryPageHarness(harness) ? [MCP_SERVER_LIST_ENTRIES.entry(harness).id] : []),
  ...nestedPageIds(harness),
];
const ON_OTHER_PAGE = "It's on another harness's page.";
const NESTED_PAGE_CLOSED = "Its page opens only from its row, and none is open.";
const ON_GRID = "It's on the Harnesses grid.";
/** On `harness`'s own page: the grid and the other harnesses' pages are elsewhere. */
const onHarnessPage = (harness: NativeHarnessId): Record<string, string> => ({
  ...Object.fromEntries(
    [HARNESSES_GRID_ENTRY, ...HARNESS_CARD_ENTRIES.entries].map((entry) => [entry.id, ON_GRID]),
  ),
  ...Object.fromEntries(
    ALL_HARNESSES.filter((other) => other !== harness)
      .flatMap(pageRowIds)
      .map((id) => [id, ON_OTHER_PAGE]),
  ),
  ...Object.fromEntries(nestedPageIds(harness).map((id) => [id, NESTED_PAGE_CLOSED])),
});

/** A harness's own page, on a host that has it configured and ready. */
function harnessPageScenario(harness: NativeHarnessId): SettingsAuditScenario {
  return {
    section: "harnesses",
    scenario: `web › ${HARNESS_CARD_ENTRIES.entry(harness).label} page`,
    path: `/settings/harnesses/${harness}?host=host_1`,
    routes: [hostsRoute([harnessHost({ configured_harnesses: { [harness]: true } })])],
    ready: [{ row: HARNESS_DETAIL_ENTRIES.entry(harness).id }],
    hides: onHarnessPage(harness),
  };
}

const SCENARIOS: readonly SettingsAuditScenario[] = [
  {
    section: "harnesses",
    scenario: "web",
    ready: ["laptop"],
    hides: HARNESS_PAGE_HIDES,
    states: {
      [HARNESSES_GRID_ENTRY.id]: ["has hosts"],
      ...cardStates(["claude-native", "codex-native"], READY_CARD),
    },
  },
  {
    section: "harnesses",
    scenario: "web › every harness ready",
    routes: [hostsRoute([harnessHost({ configured_harnesses: configured(ALL_HARNESSES, true) })])],
    ready: [{ label: "Machine: laptop" }],
    hides: HARNESS_PAGE_HIDES,
    states: { [HARNESSES_GRID_ENTRY.id]: ["has hosts"], ...cardStates(ALL_HARNESSES, READY_CARD) },
  },
  {
    section: "harnesses",
    scenario: "web › two machines",
    routes: [
      hostsRoute([
        harnessHost(),
        harnessHost({ host_id: "host_2", name: "desktop", status: "offline" }),
      ]),
    ],
    ready: [{ label: "Machine: laptop" }],
    hides: HARNESS_PAGE_HIDES,
    states: {
      [HARNESSES_GRID_ENTRY.id]: ["has hosts"],
      ...cardStates(["claude-native", "codex-native"], READY_CARD),
    },
    actions: [
      {
        name: "machine menu",
        open: () => userEvent.click(screen.getByRole("button", { name: "Machine: laptop" })),
        witness: "Offline",
      },
    ],
  },
  {
    section: "harnesses",
    scenario: "web › only an offline machine",
    routes: [hostsRoute([harnessHost({ status: "offline" })])],
    ready: [{ label: "Machine: Select a host" }],
    hides: HARNESS_PAGE_HIDES,
    states: { [HARNESSES_GRID_ENTRY.id]: ["has hosts"] },
  },
  {
    section: "harnesses",
    scenario: "web › no machine",
    routes: [hostsRoute([])],
    ready: ["Connect an online host"],
    hides: HARNESS_PAGE_HIDES,
  },
  ...NATIVE_HARNESS_IDS.filter((harness) => !isInventoryPageHarness(harness)).map(
    harnessPageScenario,
  ),
  { section: "custom-agents", scenario: "web", ready: ["my-reviewer"] },
  {
    section: "custom-agents",
    scenario: "web › more of both lists",
    routes: [
      {
        url: "/v1/agents?limit=50",
        body: { ...page([MANAGED_AGENT]), has_more: true, last_id: "ag_polly" },
      },
      {
        url: "/v1/agents?scope=user&limit=50",
        body: { ...page([USER_AGENT]), has_more: true, last_id: "ag_mine" },
      },
      {
        url: "/v1/agents?limit=50&scope=user",
        body: { ...page([USER_AGENT]), has_more: true, last_id: "ag_mine" },
      },
    ],
    ready: ["my-reviewer", { label: "Load more server agents" }, { label: "Load more agents" }],
    states: { "custom-agents-list": ["more server agents", "more agents"] },
  },
  {
    section: "custom-agents",
    scenario: "web › both lists failed",
    routes: [
      { url: "/v1/agents?limit=50", status: 500, body: {} },
      { url: "/v1/agents?scope=user&limit=50", status: 500, body: {} },
      { url: "/v1/agents?limit=50&scope=user", status: 500, body: {} },
    ],
    ready: [{ label: "Retry loading server agents" }, { label: "Retry loading your agents" }],
    states: { "custom-agents-list": ["server agents failed", "agents failed"] },
  },
  {
    section: "custom-agents",
    scenario: "web › creating an agent",
    path: "/settings/custom-agents/new",
    ready: [{ label: "System instructions" }],
    hides: { "custom-agents-list": AGENT_PAGE, "custom-agent-detail": AGENT_PAGE },
    actions: [
      {
        name: "harness list",
        open: () => userEvent.click(screen.getByRole("combobox", { name: "Harness" })),
        witness: { option: "Claude SDK" },
      },
      {
        name: "a server's transport list",
        origin: "custom-agent-create:serverTransport",
        prepare: () => userEvent.click(screen.getByRole("button", { name: "Add server" })),
        open: () =>
          userEvent.click(screen.getByRole("combobox", { name: "Transport for server 1" })),
        witness: { option: "http" },
        states: { "custom-agent-create": ["has servers", "stdio server"] },
      },
    ],
    inlineActions: [
      {
        name: "an HTTP server",
        open: async () => {
          await userEvent.click(screen.getByRole("combobox", { name: "Transport for server 1" }));
          await userEvent.click(await screen.findByRole("option", { name: "http" }));
        },
        witness: { label: "URL for server 1" },
        states: { "custom-agent-create": ["has servers", "http server"] },
        close: () => userEvent.click(screen.getByRole("button", { name: "Remove server 1" })),
      },
    ],
  },
  {
    section: "general",
    scenario: "web",
    ready: [TITLES_LOADED],
    hides: GENERAL_WEB_HIDES,
    states: { "session-title-instructions": ["loaded"] },
  },
  {
    section: "general",
    scenario: "web › title instructions saved",
    ready: [TITLES_SAVED],
    hides: GENERAL_WEB_HIDES,
    states: { "session-title-instructions": ["saved"] },
    routes: [
      {
        url: "/v1/settings/session-title-instructions",
        body: { instructions: "Use short titles", max_chars: 500 },
      },
    ],
  },
  {
    section: "general",
    scenario: "web › title instructions failed to load",
    ready: [TITLES_FAILED],
    hides: GENERAL_WEB_HIDES,
    states: { "session-title-instructions": ["load failed"] },
    routes: [
      {
        url: "/v1/settings/session-title-instructions",
        body: { detail: "Unavailable" },
        status: 503,
      },
    ],
  },
  {
    section: "git",
    scenario: "web",
    ready: ["Always use a random worktree"],
    hides: { "delete-worktrees-on-archive": "The server doesn't offer worktree cleanup." },
  },
  {
    section: "git",
    scenario: "web › worktree cleanup on",
    info: { archive_worktree_cleanup: true },
    ready: ["Delete worktrees for archived sessions"],
  },
  {
    section: "integrations",
    scenario: "web",
    ready: ["Connections"],
    hides: {
      "github-connection": NO_CONNECTIONS,
      "databricks-connection": NO_CONNECTIONS,
    },
  },
  integrationScenario({
    scenario: "not connected",
    github: {
      enabled: true,
      connected: false,
      login: null,
      scopes: null,
      install_url: INSTALL_URL,
    },
    databricks: { enabled: true, connected: false, workspace_host: null, databricks_user: null },
    ready: ["Connect GitHub to use your gh", "Connect a Databricks workspace to access"],
    states: {
      "github-connection": ["not connected", "install link"],
      "databricks-connection": ["not connected"],
    },
  }),
  integrationScenario({
    scenario: "connected",
    github: { enabled: true, connected: true, login: "alice", scopes: "repo", install_url: null },
    databricks: {
      enabled: true,
      connected: true,
      workspace_host: "workspace.example.com",
      databricks_user: "alice@example.com",
    },
    ready: ["Connected as alice.", "Connected to workspace.example.com as alice@example.com."],
    states: { "github-connection": ["connected"], "databricks-connection": ["connected"] },
  }),
  integrationScenario({
    scenario: "status unavailable",
    github: null,
    databricks: null,
    ready: ["GitHub status is unavailable.", "Databricks status is unavailable."],
    hides: {},
    states: {},
  }),
  { section: "mcp", scenario: "web", ready: ["Tracker"] },
  {
    section: "mcp",
    scenario: "web › services not connected",
    routes: [
      {
        url: "/v1/mcp-registry/services",
        body: {
          data: [
            service("tracker", "Tracker", "oauth", false),
            service("notes", "Notes", "bearer", false),
          ],
          next_cursor: null,
        },
      },
    ],
    ready: ["Tracker", "Notes"],
  },
  {
    section: "mcp",
    scenario: "web › connected services",
    routes: [
      {
        url: "/v1/mcp-registry/services",
        body: {
          data: [
            service("tracker", "Tracker", "oauth", true),
            service("notes", "Notes", "bearer", true),
            service("repos", "Repos", "github", true),
            service("weather", "Weather", "none", true),
          ],
          next_cursor: null,
        },
      },
    ],
    ready: ["Notes", "Uses your shared GitHub connection."],
    inlineActions: [
      {
        name: "Notes' token form",
        open: () => userEvent.click(serviceCard("Notes", "Update token")),
        witness: { label: "Token for Notes" },
        close: () => userEvent.click(serviceCard("Notes", "Update token")),
      },
    ],
  },
  { section: "shortcuts", scenario: "web", ready: ["Speed up common actions with the keyboard."] },
  {
    section: "account",
    scenario: "web",
    ready: ["alice"],
    routes: [
      {
        method: "POST",
        url: "/auth/users/me/password",
        body: {},
        hold: PASSWORD_CHANGE.reply,
      },
    ],
    actions: [
      {
        name: "password dialog",
        open: () => userEvent.click(screen.getByRole("button", { name: "Change password" })),
        witness: "Enter your current password and choose a new one.",
        states: { "account-password": ["editing"] },
        steps: [
          {
            name: "saving",
            run: async () => {
              const dialog = within(screen.getByRole("dialog"));
              await userEvent.type(dialog.getByLabelText("Current password"), "old secret");
              await userEvent.type(dialog.getByLabelText("New password"), "new secret");
              await userEvent.type(dialog.getByLabelText("Confirm new password"), "new secret");
              await userEvent.click(dialog.getByRole("button", { name: "Change password" }));
            },
            witness: "Changing…",
            states: { "account-password": ["busy"] },
          },
          {
            name: "changed",
            run: async () => PASSWORD_CHANGE.release(),
            witness: "Your password has been changed.",
            states: { "account-password": ["password changed"] },
          },
        ],
      },
    ],
  },
  {
    section: "admin",
    scenario: "web",
    ready: [{ row: "nested-agent-destinations" }],
    hides: { "proactive-agent-workspace": OMNI_ADVISOR_OFF },
  },
  {
    section: "admin",
    scenario: "web › Omni Advisor offered",
    info: { proactive_agent_available: true },
    routes: [OMNI_ADVISOR_SETTINGS, OMNI_ADVISOR_PROMPT],
    ready: [{ row: "nested-agent-destinations" }],
    states: { "proactive-agent-workspace": ["prompt loaded", "settings loaded"] },
  },
  {
    section: "admin",
    scenario: "web › Omni Advisor failed to load",
    info: { proactive_agent_available: true },
    routes: [OMNI_ADVISOR_SETTINGS_FAIL, OMNI_ADVISOR_PROMPT],
    ready: [{ row: "nested-agent-destinations" }],
    states: { "proactive-agent-workspace": ["load failed", "prompt loaded"] },
  },
  {
    section: "archived",
    scenario: "web",
    ready: ["Archived planning session"],
    states: { "archived-sessions": ["browsing"] },
    actions: [
      {
        name: "retention list",
        open: () =>
          userEvent.click(
            screen.getByRole("combobox", { name: "Mark archived sessions as expired after" }),
          ),
        witness: { option: "After 7 days" },
      },
      {
        name: "a session's delete dialog",
        open: () => userEvent.click(screen.getByRole("button", { name: "Delete session" })),
        witness: "Delete session?",
      },
      // Last: the selection it starts stays on the page.
      {
        name: "bulk delete dialog",
        origin: "archived-sessions:bulkDelete",
        prepare: async () => {
          await userEvent.click(screen.getByRole("button", { name: "Select" }));
          await userEvent.click(screen.getByText("Archived planning session"));
        },
        open: () => userEvent.click(screen.getByRole("button", { name: "Delete 1" })),
        witness: "Delete 1 session(s)?",
        states: { "archived-sessions": ["selecting"] },
        leaves: { "archived-sessions": ["browsing"] },
      },
    ],
  },
  {
    section: "archived",
    scenario: "web › expired, in a project, with more pages",
    // Kept 7 days, so the old session is expired; filed in a project; and a page follows.
    setup: () => writeRetentionDays(7),
    routes: [
      {
        url: "/v1/sessions?order=desc&sort_by=updated_at&limit=100&visibility=archived&include_archived=true",
        body: { ...ARCHIVED_PAGE, data: [ARCHIVED_IN_PROJECT] },
      },
      {
        url: "/v1/sessions?order=desc&sort_by=updated_at&limit=30&visibility=archived&expand_families=true&include_archived=true",
        body: { ...ARCHIVED_PAGE, data: [ARCHIVED_IN_PROJECT], has_more: true },
      },
    ],
    ready: [
      "Archived planning session",
      "Expired",
      { label: "Filter archived sessions by project" },
    ],
    states: { "archived-sessions": ["browsing", "has projects", "expired sessions", "more pages"] },
    actions: [
      {
        name: "project list",
        open: () =>
          userEvent.click(
            screen.getByRole("combobox", { name: "Filter archived sessions by project" }),
          ),
        witness: { option: "Docs site" },
      },
      {
        name: "delete expired dialog",
        origin: "archived-sessions:deleteExpired",
        open: () => userEvent.click(screen.getByRole("button", { name: "Delete expired" })),
        witness: "Delete expired sessions?",
      },
    ],
  },
  {
    section: "appearance",
    hides: APPEARANCE_HIDES,
    scenario: "web",
    ready: ["Settings data"],
    // Collapsing project sessions is on by default, so its page size shows.
    states: { "project-folder-pagination": ["collapsed"] },
    actions: [
      {
        name: "settings import dialog",
        open: () => userEvent.click(screen.getByRole("button", { name: "Import" })),
        witness: "Import settings",
      },
      {
        name: "reset dialog",
        open: () => userEvent.click(screen.getByRole("button", { name: "Reset to defaults" })),
        witness: "Reset appearance?",
      },
      {
        name: "agent picker list",
        open: () => userEvent.click(rowButton("picker-visibility", "Edit")),
        witness: { label: "Filter harnesses and agents" },
      },
    ],
  },
  {
    section: "appearance",
    scenario: "web › sidebar v2",
    info: { features: { ...SERVER_INFO.features, workspace_sidebar_v2: true } },
    ready: ["Settings data"],
    states: { "project-folder-pagination": ["collapsed"] },
  },
  {
    section: "appearance",
    hides: APPEARANCE_HIDES,
    scenario: "web › custom palette",
    setup: () => writeThemePalette("custom"),
    ready: ["Background tint"],
    states: { "project-folder-pagination": ["collapsed"] },
    actions: [
      {
        name: "accent picker",
        open: () => userEvent.click(screen.getByRole("button", { name: /^Accent: / })),
        witness: { label: "Accent hue" },
      },
      {
        name: "background tint picker",
        open: () => userEvent.click(screen.getByRole("button", { name: /^Background tint: / })),
        witness: { label: "Background tint hue" },
      },
    ],
  },
  {
    section: "appearance",
    hides: APPEARANCE_HIDES,
    scenario: "web › agents failed to load",
    routes: [{ url: "/v1/agents", status: 500, body: {} }],
    ready: ["Couldn't load this server's harnesses and agents."],
    states: { "picker-visibility": ["load failed"], "project-folder-pagination": ["collapsed"] },
  },
  {
    section: "appearance",
    hides: APPEARANCE_HIDES,
    scenario: "web › in a Databricks embed",
    embedded: true,
    setup: () =>
      setOmnigentHostConfig({
        themeSettingsUrl: "https://workspace.example.com/settings/appearance",
      }),
    ready: ["Databricks appearance settings"],
    states: { "theme-mode": ["follows Databricks"], "project-folder-pagination": ["collapsed"] },
  },
  {
    section: "members",
    scenario: "web",
    ready: ["alice"],
    routes: [
      {
        method: "POST",
        url: "/auth/invite",
        body: {
          token: "invite-token",
          register_url: "https://omnigent.example.com/register/invite-token",
          expires_at: 1_780_086_400,
          is_admin: false,
        },
      },
    ],
    actions: [
      {
        name: "invite dialog",
        open: () => userEvent.click(screen.getByRole("button", { name: "Invite member" })),
        witness: "Create invite",
        states: { "members-invite": ["choosing"] },
      },
      {
        name: "invite created",
        open: async () => {
          await userEvent.click(screen.getByRole("button", { name: "Invite member" }));
          await userEvent.click(await screen.findByRole("button", { name: "Create invite" }));
        },
        witness: "Send this URL to the new member",
        states: { "members-invite": ["invite created"] },
      },
    ],
  },
];
/** The theme color picker's popover controls, which the source guard's baseline already counts. */
const COLOR_PICKER = "src/components/theme/ThemeColorPicker.tsx › ThemeColorPicker";
/**
 * Those controls by section, tag and accessible name, each mapped to its baseline key. The picker's
 * trigger is owned under the Settings page, so only the popover's controls can be unowned here.
 */
const COLOR_PICKER_CONTROLS: Readonly<Record<string, string>> = Object.fromEntries(
  ["Accent", "Background tint"].flatMap((label) => [
    [`appearance › button › Randomize ${label.toLowerCase()}`, `${COLOR_PICKER} › button`],
    [`appearance › div › ${label} saturation and brightness`, `${COLOR_PICKER} › div`],
    [`appearance › input › ${label} hue`, `${COLOR_PICKER} › input`],
    [`appearance › input › ${label} hex value`, `${COLOR_PICKER} › input`],
  ]),
);

/**
 * Drops the unowned findings that the baseline accepts, as many as its count allows, from one
 * audited snapshot: the page, or one portal open in it.
 */
function withoutBaselineControls(
  section: SettingsSectionId,
  findings: readonly SettingsDomFinding[],
): SettingsDomFinding[] {
  const used = new Map<string, number>();
  return findings.filter((finding) => {
    const { element } = finding;
    if (finding.kind !== "unowned" || element === undefined) return true;
    const key = `${section} › ${element.tagName.toLowerCase()} › ${accessibleName(element)}`;
    const baseline = COLOR_PICKER_CONTROLS[key];
    if (baseline === undefined) return true;
    const count = (used.get(baseline) ?? 0) + 1;
    used.set(baseline, count);
    return count > (UNOWNED_CONTROLS[baseline]?.count ?? 0);
  });
}

/** Renders the real page at `path` under its providers, kept mounted until the audit ends. */
function renderSettingsPage(
  path: string,
  info: Partial<ServerInfo> = {},
  embedded = false,
): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const settings = <SettingsPage />;
  render(
    <QueryClientProvider client={client}>
      <CapabilitiesProvider info={{ ...SERVER_INFO, ...info }}>
        <TooltipProvider>
          <MemoryRouter initialEntries={[path]}>
            {embedded ? <EmbeddedProvider>{settings}</EmbeddedProvider> : settings}
          </MemoryRouter>
        </TooltipProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
  return client;
}

const rowSelector = (id: string) => `[data-settings-item="${CSS.escape(id)}"]`;

/**
 * Runs an action and waits for its witness. What it opened is what's new in the body, found
 * whatever its markers say, so content that lost its owner marker is still audited.
 */
async function openAction(where: string, action: ScenarioAction): Promise<Element[]> {
  const before = new Set(document.body.children);
  await action.open();
  await witnessed(action.witness).catch((error: unknown) => {
    throw new Error(`${where} never showed ${JSON.stringify(action.witness)}`, { cause: error });
  });
  // The overlay primitive's focus guards only hold focus inside it; they aren't controls.
  const opened = [...document.body.children].filter(
    (node) => !before.has(node) && !node.matches("[data-radix-focus-guard]"),
  );
  if (opened.length === 0) throw new Error(`${where} opened nothing`);
  return opened;
}

/** Closes what an action opened, as Escape does, and waits for it to go. */
async function closeAction(opened: readonly Element[]) {
  await userEvent.keyboard("{Escape}");
  await waitFor(() => expect(opened.filter((node) => node.isConnected)).toEqual([]), {
    timeout: 5_000,
  });
}

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** What auditing a set of scenarios found, and what rendered, for the whole page's checks. */
interface ScenarioAudits {
  audits: SettingsDomScenarioAudit[];
  problems: string[];
  observedRows: Set<string>;
  observedParts: Set<string>;
}

/** Renders and audits each of `scenarios` in turn; a scenario that fails is reported, not thrown. */
async function auditScenarios(scenarios: readonly SettingsAuditScenario[], shared: ScenarioAudits) {
  const entries = new Map(SETTINGS_ENTRIES.map((entry) => [entry.id, entry]));
  for (const scenario of scenarios) {
    // oxlint-disable-next-line no-await-in-loop -- each scenario renders its own page
    await auditOneScenario(scenario, { ...shared, entries });
  }
}

/** Audits one scenario, recording what fails in it rather than throwing past the others. */
async function auditOneScenario(
  scenario: SettingsAuditScenario,
  shared: ScenarioAudits & { entries: ReadonlyMap<string, SettingsEntry> },
) {
  try {
    await auditScenario(scenario, shared);
  } catch (error) {
    shared.problems.push(error instanceof Error ? error.message : String(error));
  } finally {
    cleanup();
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
  }
}

async function auditScenario(
  {
    section,
    scenario,
    info,
    embedded,
    path = `/settings/${section}`,
    routes: changed = [],
    ready,
    hides = {},
    setup,
    actions = [],
    inlineActions = [],
    states = {},
  }: SettingsAuditScenario,
  {
    audits,
    problems,
    observedRows,
    observedParts,
    entries,
  }: ScenarioAudits & {
    entries: ReadonlyMap<string, SettingsEntry>;
  },
) {
  {
    const where = `${section} › ${scenario}`;
    resetScenario();
    setup?.();
    const routes = settingsRouteTable([...ROUTES, ...changed]);
    vi.stubGlobal("fetch", routes.fetch);
    const client = renderSettingsPage(path, info, embedded);
    for (const witness of ready) {
      // oxlint-disable-next-line no-await-in-loop -- each witness in turn
      await witnessed(witness).catch((error: unknown) => {
        const text = document.body.textContent?.replace(/\s+/g, " ").slice(0, 300);
        throw new Error(`${where} never showed ${JSON.stringify(witness)}; it shows: ${text}`, {
          cause: error,
        });
      });
    }
    await waitUntilSettled({ client, routes });
    observeRendered(entries, observedRows, observedParts);
    for (const [id, reason] of Object.entries(hides)) {
      if (document.querySelector(rowSelector(id))) {
        problems.push(`${where}: ${id} renders, though the scenario hides it (${reason})`);
      }
    }
    const expected = SECTION_CATALOGS[section].filter((entry) => !(entry.id in hides));
    const drivenStates = new Map(
      Object.entries(states).map(([id, driven]) => [id, new Set(driven)]),
    );
    audits.push({
      section,
      scenario,
      findings: withoutBaselineControls(
        section,
        auditSettingsDomFindings({
          roots: [document.body],
          entries,
          expected,
          drivenStates,
        }),
      ),
    });
    const withStates = (
      more: Readonly<Record<string, readonly string[]>> = {},
      left: Readonly<Record<string, readonly string[]>> = {},
    ) => {
      const merged = new Map(drivenStates);
      for (const [id, driven] of Object.entries(more)) {
        merged.set(id, new Set([...(merged.get(id) ?? []), ...driven]));
      }
      for (const [id, ended] of Object.entries(left)) {
        merged.set(
          id,
          new Set([...(merged.get(id) ?? [])].filter((state) => !ended.includes(state))),
        );
      }
      return merged;
    };
    for (const action of actions) {
      // oxlint-disable-next-line no-await-in-loop -- each action from where the last one left
      await action.prepare?.();
      const origins = action.origin ? [provenOverlayOrigin(action.origin)] : [];
      // oxlint-disable-next-line no-await-in-loop -- one portal open at a time
      const opened = await openAction(`${where} › ${action.name}`, action);
      const snapshots = [{ name: action.name, states: action.states }];
      for (let index = 0; index <= (action.steps?.length ?? 0); index++) {
        const step = index === 0 ? undefined : action.steps![index - 1]!;
        if (step) {
          // oxlint-disable-next-line no-await-in-loop -- each step after the one before
          await step.run();
          // oxlint-disable-next-line no-await-in-loop -- its own witness
          await witnessed(step.witness).catch((error: unknown) => {
            throw new Error(`${where} › ${action.name} › ${step.name} never showed it`, {
              cause: error,
            });
          });
          snapshots.push({ name: `${action.name} › ${step.name}`, states: step.states });
        }
        const snapshot = snapshots[snapshots.length - 1]!;
        const findings = withoutBaselineControls(
          section,
          auditOpenedPortals({
            roots: [document.body],
            opened,
            entries,
            expected,
            drivenStates: withStates(snapshot.states, action.leaves),
            origins,
          }),
        );
        audits.push({ section, scenario: `${scenario} › ${snapshot.name}`, findings });
        observeRendered(entries, observedRows, observedParts);
      }
      // oxlint-disable-next-line no-await-in-loop -- closed before the next opens
      await closeAction(opened);
    }
    for (const inline of inlineActions) {
      const inlineWhere = `${where} › ${inline.name}`;
      // oxlint-disable-next-line no-await-in-loop -- one inline action at a time
      await inline.open();
      // oxlint-disable-next-line no-await-in-loop -- its witness, then the page settles
      await witnessed(inline.witness).catch((error: unknown) => {
        throw new Error(`${inlineWhere} never showed ${JSON.stringify(inline.witness)}`, {
          cause: error,
        });
      });
      // oxlint-disable-next-line no-await-in-loop -- every request it started
      await waitUntilSettled({ client, routes });
      // Its own snapshot of the whole page: nothing in it may be unowned or wrong.
      problems.push(
        ...wholePageProblems({
          where: inlineWhere,
          catalog: SECTION_CATALOGS[section],
          entries,
          hides,
          drivenStates: withStates(inline.states),
        }),
      );
      observeRendered(entries, observedRows, observedParts);
      // oxlint-disable-next-line no-await-in-loop -- closed before the next opens
      await inline.close();
      // oxlint-disable-next-line no-await-in-loop -- and gone
      await waitFor(() => expect(() => witnessedNow(inline.witness)).toThrow());
    }
    for (const request of new Set(routes.unmatched)) {
      problems.push(`${where}: no test route answers ${request}`);
    }
  }
}

// Harnesses is audited on its own, for its many dialog journeys; the findings and coverage are
// still checked over every section together, once both have run.
const ALL_AUDITS: ScenarioAudits = {
  audits: [],
  problems: [],
  observedRows: new Set(),
  observedParts: new Set(),
};

/**
 * What this audit leaves out, and why. A section or entry here is neither audited nor checked
 * for coverage; putting one back is a deliberate change to this list.
 */
const LEFT_OUT_SECTIONS: Partial<Record<SettingsSectionId, string>> = {
  sharing:
    "The scope radios' help buttons, About Managed hosts only and About All sessions, have no owner: no rule owns a choice's help button, and the source guard doesn't read the label component that draws it.",
  policies:
    'Its list\'s heading reads "Global Policies", not its catalog name, and the add dialog\'s parameter help buttons have no owner: a multi-select\'s field is named "Parameter values: …", which no rule matches.',
  import:
    'The mode row\'s visible label is "Import", not its catalog name "What to import", and the run button shares "Import".',
  cli: "Its row needs the desktop shell's CLI bridge.",
  updates: "Its rows need the desktop shell's update bridge.",
};
/**
 * The fix dialog's parts, and the buttons that open it on the harness rows, aren't checked for
 * coverage: this audit doesn't open the dialog yet.
 */
const FIX_DIALOG_OPENERS = ["setUp", "fixAction", "credentialAction"];
const FIX_DIALOG_LEFT_OUT = new Set([...Object.keys(FIX_DIALOG_PARTS), ...FIX_DIALOG_OPENERS]);
/** An entry as coverage sees it: harness rows without the parts of the fix dialog they open. */
function auditedParts(entry: SettingsEntry): SettingsEntry {
  if (entry.section !== "harnesses" || !entry.parts) return entry;
  return {
    ...entry,
    parts: Object.fromEntries(
      Object.entries(entry.parts).filter(([part]) => !FIX_DIALOG_LEFT_OUT.has(part)),
    ),
  };
}

const LEFT_OUT_ENTRIES: Readonly<Record<string, string>> = {
  ...Object.fromEntries(
    SECTION_CATALOGS["custom-agents"]
      .filter((entry) => entry.id.startsWith("custom-agent-detail"))
      .map((entry) => [
        entry.id,
        "Its row is named by its form's accessible name, not by text in the row.",
      ]),
  ),
  ...Object.fromEntries(
    [
      "open-links-in-app",
      "global-shortcut",
      "floating-composer-shortcut",
      "widen-window-for-right-sidebar",
      "browser-agent-tools",
    ].map((id) => [id, DESKTOP_ONLY]),
  ),
  "admin-view": ADMIN_VIEW_NOT_LOADED,
  "project-kanban-board": PROJECT_KANBAN_OFF,
  "proactive-agent": PROACTIVE_AGENT_OFF,
  // A page with tabs keeps its tab list above its rows, and its MCP list and Credential card in
  // tabs, so none of its rows is audited yet.
  ...Object.fromEntries(
    NATIVE_HARNESS_IDS.filter(isInventoryPageHarness)
      .flatMap(pageRowIds)
      .map((id) => [id, "Its tab list sits above its rows, so its page isn't audited yet."]),
  ),
};

describe("every Settings section, rendered by the real page", () => {
  it("answers to the catalog in every audited section but Harnesses", async () => {
    const own = { ...ALL_AUDITS, problems: [] as string[] };
    await auditScenarios(
      SCENARIOS.filter((scenario) => scenario.section !== "harnesses"),
      own,
    );
    ALL_AUDITS.problems.push(...own.problems);
    expect(own.problems).toEqual([]);
  }, 120_000);

  it("answers to the catalog on the Harnesses grid and on the pages without tabs", async () => {
    const own = { ...ALL_AUDITS, problems: [] as string[] };
    await auditScenarios(
      SCENARIOS.filter((scenario) => scenario.section === "harnesses"),
      own,
    );
    ALL_AUDITS.problems.push(...own.problems);
    expect(own.problems).toEqual([]);
  }, 120_000);

  it("finds an owner for every control it renders, and shows every row and part somewhere", () => {
    const { audits, observedRows, observedParts } = ALL_AUDITS;
    const audited = SETTINGS_ENTRIES.filter(
      (entry) => !(entry.section in LEFT_OUT_SECTIONS) && !(entry.id in LEFT_OUT_ENTRIES),
    ).map(auditedParts);
    expect([
      ...domAuditProblems(audits),
      // Every row, and every part of it, renders in at least one scenario.
      ...settingsCoverageProblems(audited, observedRows, observedParts),
    ]).toEqual([]);
  });

  it("audits every section it doesn't leave out, and leaves out only what exists", () => {
    const sectionIds = Object.keys(SECTION_CATALOGS);
    const audited = sectionIds.filter((id) => !(id in LEFT_OUT_SECTIONS));
    expect(audited.filter((id) => SCENARIOS.every((scenario) => scenario.section !== id))).toEqual(
      [],
    );
    expect(Object.keys(LEFT_OUT_SECTIONS).filter((id) => !sectionIds.includes(id))).toEqual([]);
    const ids = new Set(SETTINGS_ENTRIES.map((entry) => entry.id));
    expect(Object.keys(LEFT_OUT_ENTRIES).filter((id) => !ids.has(id))).toEqual([]);
  });
});
