// What the label picker lists, from the repository's labels, the ones this user added recently,
// GitHub's matches for typed text where the repository has more labels than one read lists, and
// the labels checked while the picker is open. Each label shows once, in the first group that has
// it: recent, then listed, then found.

import type { RepositoryLabel } from "@/lib/repositoryLabelCache";

export interface LabelPickerGroups {
  /** Recent labels, most recent first, with the repository's current color and description. */
  recent: RepositoryLabel[];
  /** The repository's labels the picker read, sorted by name, without the recent ones. */
  listed: RepositoryLabel[];
  /** GitHub's matches for the typed text, then checked labels, that neither group above holds. */
  found: RepositoryLabel[];
}

const key = (label: Pick<RepositoryLabel, "name">) => label.name.toLowerCase();

export function labelPickerGroups({
  listed,
  recent,
  matches = [],
  chosen = [],
}: {
  listed: readonly RepositoryLabel[];
  recent: readonly RepositoryLabel[];
  matches?: readonly RepositoryLabel[];
  /** Checked while the picker is open, so one found by a search stays once the search clears. */
  chosen?: readonly RepositoryLabel[];
}): LabelPickerGroups {
  const byName = new Map(listed.map((label) => [key(label), label]));
  const recentGroup = recent.map((label) => byName.get(key(label)) ?? label);
  const shown = new Set(recentGroup.map(key));
  const listedGroup = listed.filter((label) => !shown.has(key(label)));
  for (const label of listedGroup) shown.add(key(label));
  const found: RepositoryLabel[] = [];
  for (const label of [...matches, ...chosen]) {
    if (shown.has(key(label))) continue;
    shown.add(key(label));
    found.push(label);
  }
  return { recent: recentGroup, listed: listedGroup, found };
}

/** The recent labels a lookup should check: the ones an incomplete list doesn't hold. */
export function recentLabelsToCheck(
  listed: readonly RepositoryLabel[],
  hasMore: boolean,
  recent: readonly RepositoryLabel[],
): string[] {
  if (!hasMore) return [];
  const names = new Set(listed.map(key));
  return recent.filter((label) => !names.has(key(label))).map((label) => label.name);
}
