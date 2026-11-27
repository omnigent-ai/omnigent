// The labels this workspace user added recently in a repository, kept on this device so the
// label picker offers them first. Each keeps its color and description, so one found only by
// searching GitHub still shows after a restart. They're dropped once GitHub says they're gone.

import { z } from "zod";

import { createDeviceRecentList } from "@/lib/deviceRecentList";
import type { RepositoryLabel } from "@/lib/repositoryLabelCache";

/** How many labels a repository remembers, most recent first. */
export const MAX_RECENT_LABELS = 8;

const sameName = (left: string, right: string) => left.toLowerCase() === right.toLowerCase();

const list = createDeviceRecentList<RepositoryLabel>({
  storagePrefix: "omnigent:recent-labels",
  field: "labels",
  maxItems: MAX_RECENT_LABELS,
  maxScopes: 50,
  item: z.object({
    name: z.string().min(1),
    color: z
      .string()
      .regex(/^[0-9a-f]{6}$/)
      .nullable(),
    description: z.string().nullable(),
  }),
  same: (left, right) => sameName(left.name, right.name),
});

/** Where a repository's recent labels live: the server, workspace user, host and repository. */
export interface RecentLabelsScope {
  host: string;
  repository: string;
}

const scopeParts = (scope: RecentLabelsScope) => [scope.host, scope.repository];

/** Drop other users' and servers' lists, and the oldest past the repository limit. */
export const pruneRecentLabels = list.prune;

/** A repository's recent labels, most recent first. */
export function recentLabels(scope: RecentLabelsScope): RepositoryLabel[] {
  return list.read(scopeParts(scope));
}

/**
 * Who recent labels are kept for now: this server's workspace user, or null without one. Taken
 * when a change is made, so its answer is recorded for that user only.
 */
export const recentLabelsOwner = list.owner;

/**
 * Remember labels just added, most recent first, for `owner` (from `recentLabelsOwner` when the
 * change was made). Nothing is kept once another user or server is signed in instead.
 */
export function recordRecentLabels(
  scope: RecentLabelsScope,
  added: readonly RepositoryLabel[],
  owner: string | null,
): void {
  list.record(
    scopeParts(scope),
    added.map(({ name, color, description }) => ({ name, color, description })),
    owner,
  );
}

/**
 * Update remembered labels from what GitHub says now. A complete list drops the ones it lacks
 * and refreshes the rest; a lookup drops the ones it names gone and refreshes the ones it found.
 * Labels neither mentions stay as they were.
 */
export function reconcileRecentLabels(
  scope: RecentLabelsScope,
  answer:
    | { kind: "complete"; labels: readonly RepositoryLabel[] }
    | { kind: "lookup"; found: ReadonlyMap<string, RepositoryLabel | null> },
): void {
  const recent = recentLabels(scope);
  if (recent.length === 0) return;
  const next: RepositoryLabel[] = [];
  for (const label of recent) {
    if (answer.kind === "complete") {
      const current = answer.labels.find((candidate) => sameName(candidate.name, label.name));
      if (current) next.push(current);
      continue;
    }
    const entry = [...answer.found].find(([name]) => sameName(name, label.name));
    if (!entry) next.push(label);
    else if (entry[1]) next.push(entry[1]);
  }
  const same =
    next.length === recent.length &&
    next.every(
      (label, index) =>
        label.name === recent[index].name &&
        label.color === recent[index].color &&
        label.description === recent[index].description,
    );
  if (!same) list.write(scopeParts(scope), next);
}

/** A repository's recent labels, re-read when they change or another user signs in. */
export function useRecentLabels(scope: RecentLabelsScope | null): RepositoryLabel[] {
  return list.useItems(scope && scopeParts(scope));
}

/** Forget subscriptions, so a test starts clean. */
export const resetRecentLabelsForTests = list.resetForTests;
