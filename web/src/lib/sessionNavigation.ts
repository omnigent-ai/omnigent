import { stripBasePath, withBasePath } from "./basePath";

// Search params whose values belong to one session's restored workspace state.
// They must not carry across links that switch to another session.
export const SESSION_SCOPED_SEARCH_PARAMS = [
  "file",
  "diff",
  "comment",
  "view",
  "message",
  "panel",
] as const;

export function sessionNavigationSearch(search: string): string {
  const params = new URLSearchParams(search);
  for (const key of SESSION_SCOPED_SEARCH_PARAMS) params.delete(key);
  const next = params.toString();
  return next ? `?${next}` : "";
}

/** Navigate in place to a same-origin session URL (`/c/<id>` or `/canvas/c/<id>`); false otherwise. */
export function openSameOriginSessionLink(uri: string): boolean {
  let url: URL;
  try {
    url = new URL(uri, window.location.href);
  } catch {
    return false;
  }
  if (url.origin !== window.location.origin) return false;
  if (!/^\/(?:canvas\/)?c\/[^/]+\/?$/.test(stripBasePath(url.pathname))) return false;
  // An unprefixed `/c/<id>` link is rebased under the router basename;
  // withBasePath leaves an already-prefixed link unchanged.
  const target = withBasePath(`${url.pathname}${url.search}${url.hash}`);
  const currentPath = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (target !== currentPath) {
    window.history.pushState(null, "", target);
    window.dispatchEvent(new PopStateEvent("popstate", { state: window.history.state }));
  }
  return true;
}
