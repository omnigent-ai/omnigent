// The file link a page arrived with. Until the server says which sidebar shows, the classic one
// can rewrite the URL (its viewer drops `diff` before the diff loads), so the tabs sidebar opens
// the link as it arrived instead.

import { useCallback, useLayoutEffect } from "react";
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
 * it; any other replace forgets it, so the link lasts until it leaves the URL. Only committed
 * locations count, so a render React discards changes nothing. `peek(path)` returns that
 * query when it links to `path`, the same each time, as a replayed effect needs.
 */
export function useArrivedFileLink(): { peek: (path: string) => URLSearchParams | null } {
  const location = useLocation();
  const navigationType = useNavigationType();
  // Before the shell's own effects, which read it, in the same commit.
  useLayoutEffect(() => {
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
  }, [location, navigationType]);
  const peek = useCallback(
    (path: string) => (arrived?.params.get("file") === path ? arrived.params : null),
    [],
  );
  return { peek };
}
