// Drops the queries of a Mac read scope whose identity changed, so nothing read
// as the previous account or certificate stays on screen. Queries still in use
// are reset and read again; unused ones are removed. A shared PR record of a past
// epoch is reset without reading again: its views read under the new epoch's key.

import type { Query, QueryClient } from "@tanstack/react-query";

import { getCurrentUserId } from "@/lib/identity";
import { onNamedPullRequestWritten } from "@/lib/macNamedReads";
import { macReadEpoch, onMacReadScopeAdvanced } from "@/lib/macReadScope";
import { pullRequestRecordEpoch } from "@/lib/pullRequestRecord";

/** Whether `queryKey` holds `scope` as consecutive elements. */
export function keyHasScope(queryKey: readonly unknown[], scope: readonly unknown[]): boolean {
  if (scope.length === 0) return false;
  for (let start = 0; start + scope.length <= queryKey.length; start++) {
    if (scope.every((part, offset) => queryKey[start + offset] === part)) return true;
  }
  return false;
}

/** Keep `client`'s Mac reads in step with their scopes; returns the unbind. */
export function bindMacReadScopes(client: QueryClient): () => void {
  // What a change of reader cleared from a query still fetching. A cancelled fetch
  // rolls back to its start, which is that answer, so it's dropped again if it returns.
  const cleared = new WeakMap<Query, unknown>();
  const stopWatching = client.getQueryCache().subscribe((event) => {
    if (event.type !== "updated") return;
    const { query } = event;
    if (!cleared.has(query)) return;
    const data = query.state.data;
    if (data === undefined) return;
    const restored = data === cleared.get(query);
    cleared.delete(query);
    if (!restored) return;
    if (query.getObserversCount() === 0)
      client.removeQueries({ queryKey: query.queryKey, exact: true });
    else void client.resetQueries({ queryKey: query.queryKey, exact: true });
  });
  const stopFollowing = onMacReadScopeAdvanced((scope, move) => {
    // A shared record of an epoch the scope moved past is never read into again (its reads
    // can't publish), and its views have moved to the new one: it goes at once, whatever moved.
    const epoch = macReadEpoch(scope);
    const pastRecord = (query: Query) => {
      const recordEpoch = pullRequestRecordEpoch(query.queryKey);
      return recordEpoch !== null && recordEpoch !== epoch;
    };
    const past = client.getQueryCache().findAll({
      predicate: (candidate) => keyHasScope(candidate.queryKey, scope) && pastRecord(candidate),
    });
    for (const query of past) {
      if (query.getObserversCount() === 0) client.getQueryCache().remove(query);
      else query.reset();
    }
    const inScope = (query: Query) => keyHasScope(query.queryKey, scope) && !pastRecord(query);
    if (move === "refused") {
      dropRefused(inScope);
      // Again once the refused read has settled into its error, so that error stays on screen.
      setTimeout(() => dropRefused(inScope), 0);
      return;
    }
    // After a change of reader, a read in flight is either the one that changed it,
    // whose answer is kept, or one the scope refuses when it lands: it goes on, but
    // what it showed from the previous reader goes now.
    const settling = (query: Query) =>
      move === "changed-reader" && query.state.fetchStatus === "fetching";
    for (const query of client.getQueryCache().findAll({ predicate: inScope })) {
      if (settling(query) && query.state.data !== undefined) {
        cleared.set(query, query.state.data);
        query.setState({ data: undefined, dataUpdatedAt: 0, status: "pending", error: null });
      }
    }
    const replaced = (query: Query) => inScope(query) && !settling(query);
    client.removeQueries({
      predicate: (query) => replaced(query) && query.getObserversCount() === 0,
    });
    void client.resetQueries({ predicate: replaced });
  });
  /**
   * What a refused account showed goes at once, without reading again on its own: a read in
   * flight loses what it showed (and is dropped again if a cancel restores it), a failure keeps
   * its error and loses its data, an answer still showing reads again (and so fails too), and
   * queries nothing shows are removed.
   */
  function dropRefused(inScope: (query: Query) => boolean): void {
    for (const query of client.getQueryCache().findAll({ predicate: inScope })) {
      if (query.state.fetchStatus === "fetching") {
        if (query.state.data !== undefined) {
          cleared.set(query, query.state.data);
          query.setState({ data: undefined, dataUpdatedAt: 0 });
        }
      } else if (query.getObserversCount() === 0) {
        client.removeQueries({ queryKey: query.queryKey, exact: true });
      } else if (query.state.status === "error") {
        if (query.state.data !== undefined) query.setState({ data: undefined, dataUpdatedAt: 0 });
      } else {
        void client.resetQueries({ queryKey: query.queryKey, exact: true });
      }
    }
  }
  // A change made as the host's account: the PR's reads as a page's own login go stale. Reads
  // already out began before it, so they're cancelled first and read again after.
  const stopFollowingWrites = onNamedPullRequestWritten((prUrl, userId) => {
    if (userId !== getCurrentUserId()) return;
    const pr = prUrl.toLowerCase();
    const predicate = (query: Query) => {
      const key = query.queryKey;
      const named = key.indexOf("mac-read-as");
      return (
        named >= 0 &&
        key[named + 1] === userId &&
        key.some((part) => typeof part === "string" && part.toLowerCase() === pr)
      );
    };
    void client.cancelQueries({ predicate }).then(() => {
      if (userId !== getCurrentUserId()) return;
      return client.invalidateQueries({ predicate });
    });
  });
  return () => {
    stopWatching();
    stopFollowing();
    stopFollowingWrites();
  };
}
