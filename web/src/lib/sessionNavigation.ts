// Search params whose values belong to one session's restored workspace state.
// They must not carry across links that switch to another session.
export const SESSION_SCOPED_SEARCH_PARAMS = ["file", "diff", "comment", "view", "message"] as const;

export function sessionNavigationSearch(search: string): string {
  const params = new URLSearchParams(search);
  for (const key of SESSION_SCOPED_SEARCH_PARAMS) params.delete(key);
  const next = params.toString();
  return next ? `?${next}` : "";
}
