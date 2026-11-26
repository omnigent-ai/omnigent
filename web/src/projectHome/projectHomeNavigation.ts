export function projectHomePath(projectName: string): string {
  return `/projects/${encodeURIComponent(projectName)}`;
}

export function projectOrchestratorPath(
  projectName: string,
  search = new URLSearchParams(),
): string {
  const next = new URLSearchParams(search);
  for (const key of [
    "ticket",
    "session",
    "view",
    "orchestrator",
    "agent",
    PROJECT_PANEL_PERSONA_PARAM,
  ])
    next.delete(key);
  return `${projectHomePath(projectName)}/orchestrator${next.size ? `?${next}` : ""}`;
}

/** Compatibility with project-chat links through 0.25.0; new links use the side-panel route. */
export function isLegacyOrchestratorLink(search: URLSearchParams): boolean {
  return (
    search.get("view") === "chat" || search.has("orchestrator") || search.get("agent") === "primary"
  );
}

/** Query parameter naming the activity position of a ticket's open question to scroll to. */
export const TICKET_ASK_PARAM = "ask";

/** The project home's query parameter selecting a view other than the board. */
export const PROJECT_TAB_PARAM = "tab";
export const PROJECT_SUMMARY_TAB = "summary";
export const PROJECT_RULES_TAB = "rules";

/** The project home's views: the board, its Summary, and the project's Rules. */
export type ProjectHomeView = "board" | "summary" | "rules";

/** The view a project home's search selects; anything unknown is the board. */
export function projectHomeView(search: URLSearchParams): ProjectHomeView {
  const tab = search.get(PROJECT_TAB_PARAM);
  return tab === PROJECT_SUMMARY_TAB ? "summary" : tab === PROJECT_RULES_TAB ? "rules" : "board";
}

/** The project's "How this project works" page. */
export function projectRulesPath(projectName: string): string {
  return `${projectHomePath(projectName)}?${PROJECT_TAB_PARAM}=${PROJECT_RULES_TAB}`;
}

/** The project home's query parameter naming the session in its side panel. */
export const PROJECT_PANEL_SESSION_PARAM = "session";

/** The project home's query parameter naming a persona's session open in the side chat. */
export const PROJECT_PANEL_PERSONA_PARAM = "persona";

/** The board with a persona's session open in the side chat, like the Lead's. */
export function personaPanelPath(
  projectName: string,
  sessionId: string,
  search = new URLSearchParams(),
): string {
  const next = new URLSearchParams(search);
  for (const key of [
    "ticket",
    TICKET_ASK_PARAM,
    PROJECT_PANEL_SESSION_PARAM,
    "view",
    "orchestrator",
    "agent",
  ])
    next.delete(key);
  next.set(PROJECT_PANEL_PERSONA_PARAM, sessionId);
  return `${projectHomePath(projectName)}?${next}`;
}

export function ticketSelectionIds(search: URLSearchParams): string[] {
  return [...new Set((search.get("tickets") ?? "").split(",").filter(Boolean))];
}

export function ticketBatchSearch(ticketIds: readonly string[]): string {
  const next = new URLSearchParams({ view: "board" });
  if (ticketIds.length) next.set("tickets", [...new Set(ticketIds)].join(","));
  return `?${next}`;
}

export function ticketPanelSearch(ticketId: string, search = new URLSearchParams()): string {
  const next = new URLSearchParams(search);
  next.set("ticket", ticketId);
  // The panel lives on the Board, so a ticket opened from Summary or Rules shows the Board.
  next.delete(PROJECT_TAB_PARAM);
  // A scroll target belongs to the ticket it was opened for.
  next.delete(TICKET_ASK_PARAM);
  next.delete(PROJECT_PANEL_SESSION_PARAM);
  next.delete(PROJECT_PANEL_PERSONA_PARAM);
  next.delete("orchestrator");
  next.delete("agent");
  return `?${next}`;
}

/** Resolve a project's board or orchestrator-panel route under any mount path. */
export function projectNameFromHomePath(pathname: string): string | null {
  const match = /\/projects\/([^/]+)(?:\/orchestrator)?\/?$/.exec(pathname);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}
