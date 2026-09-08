// Browser-local default for each new Agents panel mount. In-panel toggles stay
// ephemeral and do not update this preference.

const STORAGE_KEY = "omnigent:default-agents-view";

export const agentsViewModes = ["list", "graph"] as const;
export type AgentsViewMode = (typeof agentsViewModes)[number];

/** Match today's product default: the Agents panel opens in list view. */
export const AGENTS_VIEW_DEFAULT: AgentsViewMode = "list";

/** Return whether a string is one of the selectable Agents panel view modes. */
export function isAgentsViewMode(value: string | null | undefined): value is AgentsViewMode {
  return value === "list" || value === "graph";
}

/** Normalize persisted or manually edited values to the product default. */
export function normalizeAgentsViewMode(value: string | null | undefined): AgentsViewMode {
  return isAgentsViewMode(value) ? value : AGENTS_VIEW_DEFAULT;
}

/** Read the persisted view, falling back safely for unavailable storage. */
export function readAgentsViewDefault(): AgentsViewMode {
  if (typeof window === "undefined") return AGENTS_VIEW_DEFAULT;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return AGENTS_VIEW_DEFAULT;
    return normalizeAgentsViewMode(raw);
  } catch {
    return AGENTS_VIEW_DEFAULT;
  }
}

/** Persist the view; the product default clears the storage key. */
export function writeAgentsViewDefault(value: AgentsViewMode): void {
  if (typeof window === "undefined") return;
  try {
    const normalized = normalizeAgentsViewMode(value);
    if (normalized === AGENTS_VIEW_DEFAULT) {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, normalized);
    }
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
}
