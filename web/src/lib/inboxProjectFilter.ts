// The Inbox's project filter: which project an item belongs to, the options
// the picker offers, and the per-device memory of the last pick. The pick
// itself lives in the URL (`?project=<id>`), so a reload or a shared link
// keeps it; the stored value only restores it when the Inbox opens bare.

const STORAGE_KEY = "omnigent:inbox-project";

/** Search param carrying the chosen project id. */
export const INBOX_PROJECT_PARAM = "project";

/** One picker entry: a project with items in the current Inbox view. */
export interface InboxProjectOption {
  id: string;
  name: string;
  icon?: string | null;
  count: number;
}

/**
 * Count entries per project and return the projects that have any, sorted by
 * name. Entries without a project (unfiled sessions, automations) only count
 * toward "All projects". The `selected` project is always listed, with a zero
 * count when it has nothing, so the picker can show and clear it.
 */
export function inboxProjectOptions<T>(
  entries: readonly T[],
  projectOf: (entry: T) => string | null,
  describe: (projectId: string) => { name: string; icon?: string | null },
  selected: string | null,
): InboxProjectOption[] {
  const counts = new Map<string, number>();
  for (const entry of entries) {
    const id = projectOf(entry);
    if (id) counts.set(id, (counts.get(id) ?? 0) + 1);
  }
  if (selected && !counts.has(selected)) counts.set(selected, 0);
  return [...counts.entries()]
    .map(([id, count]) => ({ id, count, ...describe(id) }))
    .sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
}

/** Read the last project picked on this device, or null. */
export function readInboxProject(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(STORAGE_KEY) || null;
  } catch {
    return null;
  }
}

/** Remember the project pick on this device; null forgets it. */
export function writeInboxProject(projectId: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (projectId) window.localStorage.setItem(STORAGE_KEY, projectId);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // A local view preference; losing it is harmless.
  }
}
