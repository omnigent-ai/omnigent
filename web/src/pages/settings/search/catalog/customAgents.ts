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

export const CUSTOM_AGENTS_SETTINGS = [CUSTOM_AGENTS_LIST_ENTRY] as const;
