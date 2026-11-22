export function projectHomePath(projectName: string): string {
  return `/projects/${encodeURIComponent(projectName)}`;
}

/** The project home's query parameter naming the session in its side panel. */
export const PROJECT_PANEL_SESSION_PARAM = "session";

export type ProjectView = "chat" | "board";

/** Old ticket/session links still open Board; the project itself starts in Chat. */
export function projectView(search: URLSearchParams): ProjectView {
  const view = search.get("view");
  if (view === "chat" || view === "board") return view;
  return search.has("ticket") || search.has(PROJECT_PANEL_SESSION_PARAM) ? "board" : "chat";
}

export function projectViewSearch(view: ProjectView, search = new URLSearchParams()): string {
  const next = new URLSearchParams(search);
  next.set("view", view);
  next.delete("ticket");
  next.delete(PROJECT_PANEL_SESSION_PARAM);
  next.delete("orchestrator");
  next.delete("agent");
  if (view === "chat") next.delete("tickets");
  return `?${next}`;
}

export function ticketSelectionIds(search: URLSearchParams): string[] {
  return [...new Set((search.get("tickets") ?? "").split(",").filter(Boolean))];
}

export function ticketBatchSearch(ticketIds: readonly string[]): string {
  const next = new URLSearchParams({ view: "board" });
  if (ticketIds.length) next.set("tickets", [...new Set(ticketIds)].join(","));
  return `?${next}`;
}

/** A relative link that opens a session in the project home's side panel. */
export function projectSessionPanelSearch(sessionId: string): string {
  return `?${new URLSearchParams({ [PROJECT_PANEL_SESSION_PARAM]: sessionId })}`;
}

/** Retain the ticket while its existing conversation occupies the panel. */
export function ticketSessionSearch(
  ticketId: string,
  sessionId?: string,
  search = new URLSearchParams(),
): string {
  const params = new URLSearchParams(search);
  params.set("ticket", ticketId);
  params.delete("orchestrator");
  params.delete("agent");
  params.delete(PROJECT_PANEL_SESSION_PARAM);
  if (sessionId) params.set(PROJECT_PANEL_SESSION_PARAM, sessionId);
  else {
    params.set("view", "chat");
  }
  return `?${params}`;
}

export function ticketPanelSearch(ticketId: string, search = new URLSearchParams()): string {
  const next = new URLSearchParams(search);
  next.set("ticket", ticketId);
  next.delete(PROJECT_PANEL_SESSION_PARAM);
  next.delete("orchestrator");
  next.delete("agent");
  return `?${next}`;
}

/** The project a `…/projects/<name>` pathname opens, under any mount path; null elsewhere. */
export function projectNameFromHomePath(pathname: string): string | null {
  const match = /\/projects\/([^/]+)\/?$/.exec(pathname);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}
