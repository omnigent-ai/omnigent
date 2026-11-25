import { hashKey, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import type { PullRequestListSnapshot } from "@/hooks/usePullRequestListStats";
import type {
  PullRequestMacRead,
  usePullRequestRowReads,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  rowReaderHeld,
  snapshotReadDue,
  type RowOverviewState,
} from "@/lib/pullRequestRowSnapshots";

/** A list row as the snapshot follower sees it. */
export interface RowSnapshotFollower {
  association: { url: string };
  data: { read_on_mac?: PullRequestMacRead } | undefined;
  offline: boolean;
}

/**
 * Reads a followed row's overview when the list's batched snapshot of its PR changes. Each
 * row's own query state decides, so a read in flight, a failed read or a held reader waits.
 */
export function useRowSnapshotReads({
  live,
  snapshots,
  rowReads,
  enabled,
  readUrls,
}: {
  live: readonly RowSnapshotFollower[];
  snapshots: ReadonlyMap<string, PullRequestListSnapshot> | undefined;
  rowReads: ReturnType<typeof usePullRequestRowReads>;
  enabled: boolean;
  /** A windowed list's read targets: only they read; every row without it. */
  readUrls?: ReadonlySet<string>;
}): void {
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!enabled || !snapshots) return;
    for (const { association, data, offline } of live) {
      if (readUrls && !readUrls.has(association.url)) continue;
      const snapshot = snapshots.get(association.url);
      const target = rowReads.targetOf(association.url);
      if (!snapshot || offline || !data?.read_on_mac || !target) continue;
      const queryKey = rowReads.keyOf(association.url);
      // By its hash: a long list's cache holds hundreds of reads, too many to search per row.
      const query = queryClient.getQueryCache().get(hashKey(queryKey));
      if (!query) continue;
      const due = snapshotReadDue({
        state: query.state as RowOverviewState,
        snapshot,
        rowReader: data.read_on_mac.login,
        held: rowReaderHeld(target),
      });
      if (due) void queryClient.refetchQueries({ queryKey, exact: true }, { cancelRefetch: false });
    }
  }, [enabled, live, snapshots, rowReads, queryClient, readUrls]);
}
