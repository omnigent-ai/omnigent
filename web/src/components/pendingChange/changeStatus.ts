// The state of one change the user made, read from TanStack Query's mutation cache: in
// flight, refused, or settled. Reading the cache rather than a hook's own state keeps a
// failure visible after the control that made it unmounts, and the latest change decides,
// so a fresh change that succeeds clears an earlier failure. The mutation must outlive
// its five-minute default cache time for that, as `keepFailures` does.

import { useMutationState, useQueryClient, type Mutation } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";

export type ChangeStatus<Variables> =
  | { kind: "idle" }
  | { kind: "pending"; variables: Variables }
  | {
      kind: "failed";
      variables: Variables;
      error: Error;
      /** Forget the failure, and any earlier one it replaced; a later change replaces it anyway. */
      dismiss: () => void;
    };

const IDLE = { kind: "idle" } as const;

interface MutationSnapshot<Variables> {
  id: number;
  status: Mutation["state"]["status"];
  submittedAt: number;
  variables: Variables;
  error: Error | null;
}

/** The latest of `snapshots` that `matches` takes, as a change status. Exported for tests. */
export function latestChangeStatus<Variables>(
  snapshots: readonly MutationSnapshot<Variables>[],
  matches: (variables: Variables) => boolean,
  dismiss: (ids: readonly number[]) => void,
): ChangeStatus<Variables> {
  let latest: MutationSnapshot<Variables> | undefined;
  const failed: number[] = [];
  for (const snapshot of snapshots) {
    if (snapshot.variables === undefined || !matches(snapshot.variables)) continue;
    if (snapshot.status === "error") failed.push(snapshot.id);
    // Submission order, with the cache's own order breaking a same-millisecond tie.
    if (
      !latest ||
      snapshot.submittedAt > latest.submittedAt ||
      (snapshot.submittedAt === latest.submittedAt && snapshot.id > latest.id)
    ) {
      latest = snapshot;
    }
  }
  if (!latest) return IDLE;
  if (latest.status === "pending") return { kind: "pending", variables: latest.variables };
  if (latest.status === "error" && latest.error) {
    return {
      kind: "failed",
      variables: latest.variables,
      error: latest.error,
      // Every failure it shows over goes too, so none comes back in its place.
      dismiss: () => dismiss(failed),
    };
  }
  return IDLE;
}

function useMutationSnapshots<Variables>(mutationKey: readonly unknown[]) {
  const queryClient = useQueryClient();
  const snapshots = useMutationState({
    filters: { mutationKey },
    select: (mutation): MutationSnapshot<Variables> => ({
      id: mutation.mutationId,
      status: mutation.state.status,
      submittedAt: mutation.state.submittedAt,
      variables: mutation.state.variables as Variables,
      error: mutation.state.error,
    }),
  });
  const dismiss = useCallback(
    (ids: readonly number[]) => {
      const cache = queryClient.getMutationCache();
      for (const mutation of cache.getAll()) {
        if (ids.includes(mutation.mutationId)) cache.remove(mutation);
      }
    },
    [queryClient],
  );
  return { snapshots, dismiss };
}

/**
 * The latest change under `mutationKey` that `matches` takes. `matches` also decides whose
 * changes show: pass one that drops changes made as another workspace user or Mac account.
 */
export function useChangeStatus<Variables>(
  mutationKey: readonly unknown[],
  matches: (variables: Variables) => boolean,
): ChangeStatus<Variables> {
  const { snapshots, dismiss } = useMutationSnapshots<Variables>(mutationKey);
  return useMemo(
    () => latestChangeStatus(snapshots, matches, dismiss),
    [snapshots, matches, dismiss],
  );
}

/** As {@link useChangeStatus}, for each item `itemOf` names, e.g. each PR; idle ones are left out. */
export function useChangeStatusByItem<Variables>(
  mutationKey: readonly unknown[],
  matches: (variables: Variables) => boolean,
  itemOf: (variables: Variables) => string,
): ReadonlyMap<string, Exclude<ChangeStatus<Variables>, { kind: "idle" }>> {
  const { snapshots, dismiss } = useMutationSnapshots<Variables>(mutationKey);
  return useMemo(() => {
    const byItem = new Map<string, MutationSnapshot<Variables>[]>();
    for (const snapshot of snapshots) {
      if (snapshot.variables === undefined || !matches(snapshot.variables)) continue;
      const item = itemOf(snapshot.variables);
      byItem.set(item, [...(byItem.get(item) ?? []), snapshot]);
    }
    const statuses = new Map<string, Exclude<ChangeStatus<Variables>, { kind: "idle" }>>();
    for (const [item, group] of byItem) {
      const status = latestChangeStatus(group, () => true, dismiss);
      if (status.kind !== "idle") statuses.set(item, status);
    }
    return statuses;
  }, [snapshots, matches, itemOf, dismiss]);
}
