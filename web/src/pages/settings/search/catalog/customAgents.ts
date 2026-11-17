import { settingsEntry } from "./types";

/** Each listed agent opens its own page, named by the agent; none of its data is searchable. */
const OPENS_AGENT = { rowActions: { open: { dataName: true } } } as const;
/** Back to the agent list, named around the list's label: "Back to Custom agents". */
const BACK_TO_LIST = { before: "Back to " } as const;

export const CUSTOM_AGENTS_LIST_ENTRY = settingsEntry({
  id: "custom-agents-list",
  section: "custom-agents",
  label: "Custom agents",
  description: "The agents you can start sessions with: the server's own, and the ones you made.",
  keywords: ["agents", "custom agent", "create agent", "new agent", "server agents", "built-in"],
  parts: {
    search: "Search agents",
    create: "Create custom agent",
    loadMoreServer: "Load more server agents",
    loadMoreYours: "Load more agents",
    retryServer: "Retry loading server agents",
    retryYours: "Retry loading your agents",
  },
  // Paging and retry show only while a list has more, or failed to load.
  presence: {
    loadMoreServer: { states: ["more server agents"] },
    loadMoreYours: { states: ["more agents"] },
    retryServer: { states: ["server agents failed"] },
    retryYours: { states: ["agents failed"] },
  },
  regions: { serverAgents: OPENS_AGENT, yourAgents: OPENS_AGENT },
  focus: "search",
});

/**
 * The create page, reached from the list's Create. Its fields and buttons are the form's own,
 * which the shell's dialog shares; the MCP server rows are not parts yet.
 */
export const CUSTOM_AGENT_CREATE_ENTRY = settingsEntry({
  id: "custom-agent-create",
  section: "custom-agents",
  label: "Create custom agent",
  description:
    "Make an agent of your own: its name, harness, model, system instructions and MCP tools.",
  keywords: ["new agent", "create agent", "custom agent", "system instructions", "mcp tools"],
  parts: {
    back: "Back to Custom agents",
    name: "Name",
    description: "Description",
    harness: "Harness",
    model: "Model",
    instructions: "System instructions",
    addServer: "Add server",
    serverName: "Server name",
    serverTransport: "Transport",
    removeServer: "Remove server",
    command: "Command",
    arguments: "Arguments",
    environment: "Environment variables",
    url: "Server URL",
    headers: "HTTP headers",
    cancel: "Cancel",
    submit: "Save agent",
  },
  // Every server shows the same fields, so each is named after its server, or its place in the
  // list for its own name; only the static names are searchable.
  nameTemplates: {
    back: BACK_TO_LIST,
    serverName: { before: "Name for server " },
    serverTransport: { before: "Transport for " },
    removeServer: { before: "Remove " },
    command: { before: "Command for " },
    arguments: { before: "Arguments for " },
    environment: { before: "Environment variables for " },
    url: { before: "URL for " },
    headers: { before: "HTTP headers for " },
  },
  partChoices: { serverTransport: ["stdio", "http"] },
  presence: {
    serverName: { states: ["has servers"] },
    serverTransport: { states: ["has servers"] },
    removeServer: { states: ["has servers"] },
    command: { states: ["stdio server"] },
    arguments: { states: ["stdio server"] },
    environment: { states: ["stdio server"] },
    url: { states: ["http server"] },
    headers: { states: ["http server"] },
  },
  overlays: {
    // The harnesses the server knows, named by their labels.
    harness: { runtimeChoices: {} },
    serverTransport: { captured: true },
  },
  focus: "name",
  // Its own page, reached from the list's Create.
  revealedBy: "custom-agents-list",
});

/**
 * One agent's page, reached from the list, so search reveals it through the list. Back and
 * Retry aren't parts yet: Back's name would change, and Retry's is shared with other lists.
 */
export const CUSTOM_AGENT_DETAIL_ENTRY = settingsEntry({
  id: "custom-agent-detail",
  section: "custom-agents",
  label: "Agent configuration",
  description:
    "One agent's description, harness, model and instructions, which you can edit for an agent you made.",
  keywords: ["agent", "instructions", "model", "harness", "description", "delete agent", "edit"],
  parts: {
    back: "Back to Custom agents",
    delete: "Delete agent",
    confirmDelete: "Delete",
    retry: "Retry loading this agent",
    retryConfiguration: "Retry loading the configuration",
    name: "Name",
    description: "Description",
    harness: "Harness",
    model: "Model",
    instructions: "Instructions",
    addServer: "Add server",
    reloadLatest: "Reload latest",
    cancel: "Cancel",
    save: "Save changes",
    discard: "Discard changes",
  },
  nameTemplates: { back: BACK_TO_LIST },
  nameAlternatives: { confirmDelete: { inUse: "Remove anyway" } },
  // An agent you made can be deleted; one you can edit shows its form once it loads.
  presence: {
    delete: { states: ["owned"] },
    confirmDelete: { overlay: "delete" },
    retry: { states: ["load failed"] },
    retryConfiguration: { states: ["configuration failed"] },
    name: { states: ["editing"] },
    description: { states: ["editing"] },
    harness: { states: ["editing"] },
    model: { states: ["editing"] },
    instructions: { states: ["editing"] },
    addServer: { states: ["editing"] },
    reloadLatest: { states: ["edit conflict"] },
    cancel: { states: ["editing"] },
    save: { states: ["editing"] },
    discard: { overlay: "cancel" },
  },
  overlays: {
    delete: { dialogActions: ["Cancel"], captured: true },
    // Back, Cancel and Reload latest ask before discarding a draft; Cancel names the dialog.
    cancel: { dialogActions: ["Keep editing"], captured: true },
  },
  // The agent's MCP servers, each with its arguments and credentials; none of their data is
  // searchable. A field is named after its server, or its place in the list for a new one.
  regions: {
    servers: {
      rowActions: {
        remove: "Remove server",
        undoRemove: "Undo removal",
        addArgument: "Add argument",
        addHeader: "Add header",
        addVariable: "Add variable",
      },
      rowFields: {
        name: { before: "Name for server " },
        transport: { before: "Transport for " },
        description: { before: "Description for " },
        url: { before: "URL for " },
        command: { before: "Command for " },
      },
    },
    arguments: {
      rowActions: { remove: { dataName: true } },
      rowFields: { value: { before: "Argument " } },
    },
    credentials: {
      rowActions: { remove: "Remove credential" },
      rowFields: {
        name: { before: "Name for credential " },
        action: { before: "Action for " },
        value: { before: "Value for " },
      },
    },
  },
  // Each agent's own page, so the list stands in for it.
  revealedBy: "custom-agents-list",
});

export const CUSTOM_AGENTS_SETTINGS = [
  CUSTOM_AGENTS_LIST_ENTRY,
  CUSTOM_AGENT_CREATE_ENTRY,
  CUSTOM_AGENT_DETAIL_ENTRY,
] as const;
