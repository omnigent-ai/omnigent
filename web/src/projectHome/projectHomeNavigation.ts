export function projectHomePath(projectName: string): string {
  return `/projects/${encodeURIComponent(projectName)}`;
}

/** The project home's query parameter naming the session in its side panel. */
export const PROJECT_PANEL_SESSION_PARAM = "session";

/** A relative link that opens a session in the project home's side panel. */
export function projectSessionPanelSearch(sessionId: string): string {
  return `?${new URLSearchParams({ [PROJECT_PANEL_SESSION_PARAM]: sessionId })}`;
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
