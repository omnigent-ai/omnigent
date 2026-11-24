// The routes that open a session, shared by the route table and code that asks which
// session a URL shows.

import { matchRoutes } from "react-router-dom";

import { getBasePath } from "@/lib/basePath";

export const CHAT_SESSION_ROUTE = "/c/:conversationId";
export const CANVAS_SESSION_ROUTE = "/canvas/c/:conversationId";

/**
 * The session `pathname` opens, or null. It matches as the router does: under the server's base
 * path, which the router is given as its basename, and with the path decoded. `rebase` maps a
 * route to the app's mount, as `useRebasePath` does when the app is embedded.
 */
export function sessionIdAtPath(
  pathname: string,
  rebase: (path: string) => string = (path) => path,
): string | null {
  const routes = [CHAT_SESSION_ROUTE, CANVAS_SESSION_ROUTE].map((route) => ({
    path: rebase(route),
  }));
  const matches = matchRoutes(routes, pathname, getBasePath() || undefined);
  return matches?.[0]?.params.conversationId ?? null;
}
