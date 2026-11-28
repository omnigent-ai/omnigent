// The routes that open a session, shared by the route table and code that asks which
// session a URL shows.

import { matchPath } from "react-router-dom";

import { stripBasePath } from "@/lib/basePath";

export const CHAT_SESSION_ROUTE = "/c/:conversationId";
export const CANVAS_SESSION_ROUTE = "/canvas/c/:conversationId";

/**
 * The session `pathname` opens, matched as the router matches it, or null. `rebase` maps a
 * route to the app's mount, as `useRebasePath` does when the app is embedded.
 */
export function sessionIdAtPath(
  pathname: string,
  rebase: (path: string) => string = (path) => path,
): string | null {
  const appPath = stripBasePath(pathname);
  for (const route of [CHAT_SESSION_ROUTE, CANVAS_SESSION_ROUTE]) {
    const match = matchPath({ path: rebase(route), end: true }, appPath);
    if (match?.params.conversationId) return match.params.conversationId;
  }
  return null;
}
