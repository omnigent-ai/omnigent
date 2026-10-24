// Persisted, app-global preference for the workspace folder picker: how its
// listing is sorted. Read once when a picker opens and written when the user
// flips the sort, so the choice carries over to every picker and a refresh.

export type WorkspacePickerSort = "name" | "modified";

const STORAGE_KEY = "omnigent:workspace-picker-preferences";

/** The persisted sort, or ``"name"`` when unset, malformed, or unreadable. */
export function readWorkspacePickerSort(): WorkspacePickerSort {
  if (typeof window === "undefined") return "name";
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "null");
    return (parsed as { sort?: unknown } | null)?.sort === "modified" ? "modified" : "name";
  } catch {
    return "name";
  }
}

/** Persist the sort. Swallows quota/access errors so a failed write can't break the picker. */
export function writeWorkspacePickerSort(sort: WorkspacePickerSort): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ sort }));
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}
