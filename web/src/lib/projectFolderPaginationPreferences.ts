// Per-device choice of whether an expanded sidebar project folder lists its
// sessions a page at a time behind "Show more" (the default), and the page size.

import { createStoredPreference } from "./storedChoicePreference";

export const PROJECT_FOLDER_COLLAPSE_STORAGE_KEY = "omnigent:project-folder-collapse";
export const PROJECT_FOLDER_PAGE_SIZE_STORAGE_KEY = "omnigent:project-folder-page-size";
// Earlier builds stored "all" here to list every session in a folder.
const LEGACY_PAGINATION_STORAGE_KEY = "omnigent:project-folder-pagination";

export const DEFAULT_PROJECT_FOLDER_COLLAPSE = true;

// How many sessions a folder shows at first and adds with each Show more click.
export const PROJECT_FOLDER_PAGE_SIZE = 5;
export const PROJECT_FOLDER_PAGE_SIZE_MIN = 1;
export const PROJECT_FOLDER_PAGE_SIZE_MAX = 50;

/** The page size `text` holds, or undefined unless it is a whole number in range. */
export function parseProjectFolderPageSize(text: string): number | undefined {
  if (!/^\d+$/.test(text.trim())) return undefined;
  const size = Number(text);
  return size >= PROJECT_FOLDER_PAGE_SIZE_MIN && size <= PROJECT_FOLDER_PAGE_SIZE_MAX
    ? size
    : undefined;
}

const collapsePreference = createStoredPreference<boolean>(
  PROJECT_FOLDER_COLLAPSE_STORAGE_KEY,
  (stored) => (stored === "true" ? true : stored === "false" ? false : undefined),
  DEFAULT_PROJECT_FOLDER_COLLAPSE,
);

const pageSizePreference = createStoredPreference(
  PROJECT_FOLDER_PAGE_SIZE_STORAGE_KEY,
  parseProjectFolderPageSize,
  PROJECT_FOLDER_PAGE_SIZE,
);

export const readProjectFolderCollapse = collapsePreference.read;
export const writeProjectFolderCollapse = collapsePreference.write;
export const readProjectFolderPageSize = pageSizePreference.read;
export const writeProjectFolderPageSize = pageSizePreference.write;

/** Re-read both preferences after a same-tab write that bypassed them (settings import). */
export function notifyProjectFolderPaginationChanged(): void {
  collapsePreference.notifyChanged();
  pageSizePreference.notifyChanged();
}

/** Live switch value, including changes made in other windows. */
export function useProjectFolderCollapse(): boolean {
  return collapsePreference.use();
}

/** Live page size, including changes made in other windows. */
export function useProjectFolderPageSize(): number {
  return pageSizePreference.use();
}

/** Map an earlier build's stored "all" to the switch being off, then drop its key. */
export function migrateLegacyProjectFolderPagination(): void {
  if (typeof window === "undefined") return;
  try {
    const legacy = window.localStorage.getItem(LEGACY_PAGINATION_STORAGE_KEY);
    if (legacy === null) return;
    if (legacy === "all") writeProjectFolderCollapse(false);
    window.localStorage.removeItem(LEGACY_PAGINATION_STORAGE_KEY);
  } catch {
    // localStorage access errors are non-fatal; the next start tries again.
  }
}
