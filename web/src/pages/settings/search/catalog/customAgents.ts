import { settingsEntry } from "./types";

/** Each listed agent opens its own page, named by the agent; none of its data is searchable. */
const OPENS_AGENT = { rowActions: { open: { dataName: true } } } as const;

export const CUSTOM_AGENTS_LIST_ENTRY = settingsEntry({
  id: "custom-agents-list",
  section: "custom-agents",
  label: "Custom agents",
  description: "The agents you can start sessions with: the server's own, and the ones you made.",
  keywords: ["agents", "custom agent", "create agent", "new agent", "server agents", "built-in"],
  parts: {
    search: "Search agents",
    create: "Create",
    loadMoreServer: "Load more server agents",
    loadMoreYours: "Load more agents",
  },
  // Paging shows only while a list has more agents to load.
  presence: {
    loadMoreServer: { states: ["more server agents"] },
    loadMoreYours: { states: ["more agents"] },
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
  // The required marker is part of each label, so each name keeps it.
  parts: {
    name: "Name *",
    description: "Description",
    harness: "Harness",
    model: "Model *",
    instructions: "System instructions",
    addServer: "Add server",
    cancel: "Cancel",
    submit: "Save agent",
  },
  overlays: {
    // The harnesses the server knows, named by their labels.
    harness: { runtimeChoices: {} },
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
    delete: "Delete agent",
    confirmDelete: "Delete",
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
  nameAlternatives: { confirmDelete: { inUse: "Remove anyway" } },
  // An agent you made can be deleted; one you can edit shows its form once it loads.
  presence: {
    delete: { states: ["owned"] },
    confirmDelete: { overlay: "delete" },
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
  // Each agent's own page, so the list stands in for it.
  revealedBy: "custom-agents-list",
});

export const CUSTOM_AGENTS_SETTINGS = [
  CUSTOM_AGENTS_LIST_ENTRY,
  CUSTOM_AGENT_DETAIL_ENTRY,
] as const;
