import type { QueryClient } from "@tanstack/react-query";
import { getCurrentUserId, subscribeIdentity } from "@/lib/identity";

/**
 * The query roots of a session's pull request info, on the new page and the classic one.
 * The server shapes that info for whoever reads it (the session owner's accounts and changes,
 * or neither), so one user's answer must never show to the next.
 */
export const PULL_REQUEST_INFO_ROOTS: ReadonlySet<unknown> = new Set([
  "pr-page-github-info",
  "github-info",
]);

const watched = new WeakMap<QueryClient, () => void>();

/**
 * Drop every cached pull request info answer when the workspace user changes, so the new user
 * sees nothing of the last one's until their own read answers. Repeat calls do nothing.
 */
export function resetPullRequestInfoOnUserSwitch(client: QueryClient): void {
  if (watched.has(client)) return;
  let user = getCurrentUserId();
  const stop = subscribeIdentity(() => {
    const previous = user;
    user = getCurrentUserId();
    if (previous === user) return;
    void client.resetQueries({
      predicate: (query) => PULL_REQUEST_INFO_ROOTS.has(query.queryKey[0]),
    });
  });
  watched.set(client, stop);
}

/** Stop watching for `client`, so a test starts clean. */
export function stopPullRequestInfoUserSwitchForTests(client: QueryClient): void {
  watched.get(client)?.();
  watched.delete(client);
}
