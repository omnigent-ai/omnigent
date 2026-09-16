const SESSION_VIEW_PARAMS = ["file", "diff", "comment", "view"] as const;

/** Keep unrelated query state when switching away from a session's file or terminal view. */
export function sessionLinkParams(search: string): URLSearchParams {
  const params = new URLSearchParams(search);
  for (const key of SESSION_VIEW_PARAMS) params.delete(key);
  return params;
}

export function withSearch(path: string, params: URLSearchParams): string {
  const search = params.toString();
  return search ? `${path}?${search}` : path;
}

/** Canonical page destination. Callers may preserve non-session query parameters. */
export function sessionPageHref(sessionId: string | null, search = ""): string {
  return withSearch(sessionId === null ? "/" : `/c/${sessionId}`, sessionLinkParams(search));
}
