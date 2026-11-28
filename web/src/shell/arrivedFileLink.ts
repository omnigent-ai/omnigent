// The file link a page arrived with. Until the server says which sidebar shows, the classic one
// can rewrite the URL (its viewer drops `diff` before the diff loads), so the tabs sidebar opens
// the link as it arrived instead.

import { useCallback } from "react";
import { useLocation, useNavigationType } from "@/lib/routing";
import type { WorkspaceFileOpenOptions } from "@/shell/workspaceTabs/workspacePlaceNavigation";
import { citedFilePosition } from "./filePositionState";

/** How a `?file=` link's other params open its file: cited lines, the diff, or a comment. */
export function fileLinkOpenOptions(params: URLSearchParams): WorkspaceFileOpenOptions {
  const commentId = params.get("comment") || undefined;
  return {
    ...citedFilePosition({
      line: Number(params.get("line")),
      column: Number(params.get("column")),
      endLine: Number(params.get("endLine")),
    }),
    ...(params.get("diff") === "1" ? { view: "diff" } : {}),
    ...(commentId !== undefined ? { commentId } : {}),
  };
}

// Module scope, so a shell that remounts during start-up still has the link the page arrived with.
let seenLocation: object | null = null;
let arrived: { pathname: string; params: URLSearchParams } | null = null;

/**
 * Remembers the query of the latest location reached by a page load, a push or back/forward.
 * A replace that keeps its session and file, as the app's own cleanup of the URL does, keeps
 * it; any other replace forgets it. `take(path)` returns that query once, when it links to
 * `path`, and forgets it either way.
 */
export function useArrivedFileLink(): { take: (path: string) => URLSearchParams | null } {
  const location = useLocation();
  const navigationType = useNavigationType();
  if (navigationType !== "REPLACE" && seenLocation !== location) {
    seenLocation = location;
    arrived = { pathname: location.pathname, params: new URLSearchParams(location.search) };
  } else if (
    arrived &&
    (location.pathname !== arrived.pathname ||
      new URLSearchParams(location.search).get("file") !== arrived.params.get("file"))
  ) {
    arrived = null;
  }
  const take = useCallback((path: string) => {
    const params = arrived?.params ?? null;
    arrived = null;
    return params?.get("file") === path ? params : null;
  }, []);
  return { take };
}
