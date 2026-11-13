// Recently accepted QuickPick items, per picker namespace, most recent first.
// Stored in localStorage so recents survive reloads and app restarts.

export const QUICK_PICK_HISTORY_STORAGE_KEY = "omnigent.quickPick.history";
export const QUICK_PICK_HISTORY_LIMIT = 50;

type HistoryRecord = Record<string, string[]>;

/** A record with no prototype, so a namespace such as "constructor" or "__proto__" is only its own. */
function emptyRecord(): HistoryRecord {
  return Object.create(null) as HistoryRecord;
}

function readAll(): HistoryRecord {
  try {
    const raw = window.localStorage.getItem(QUICK_PICK_HISTORY_STORAGE_KEY);
    if (!raw) return emptyRecord();
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return emptyRecord();
    const record = emptyRecord();
    for (const [namespace, ids] of Object.entries(parsed)) {
      if (Array.isArray(ids)) {
        record[namespace] = ids
          .filter((id): id is string => typeof id === "string")
          .slice(0, QUICK_PICK_HISTORY_LIMIT);
      }
    }
    return record;
  } catch {
    // Corrupt or inaccessible storage only loses recents.
    return emptyRecord();
  }
}

function writeAll(record: HistoryRecord): void {
  try {
    window.localStorage.setItem(QUICK_PICK_HISTORY_STORAGE_KEY, JSON.stringify(record));
  } catch {
    // Quota or access errors shouldn't break picking.
  }
}

export function readQuickPickHistory(namespace: string): string[] {
  return readAll()[namespace] ?? [];
}

export function recordQuickPickHistory(namespace: string, id: string): void {
  const record = readAll();
  const ids = (record[namespace] ?? []).filter((existing) => existing !== id);
  record[namespace] = [id, ...ids].slice(0, QUICK_PICK_HISTORY_LIMIT);
  writeAll(record);
}

export function removeQuickPickHistory(namespace: string, id: string): void {
  const record = readAll();
  const ids = record[namespace];
  if (!ids?.includes(id)) return;
  record[namespace] = ids.filter((existing) => existing !== id);
  writeAll(record);
}
