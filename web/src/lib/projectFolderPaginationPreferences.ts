// Per-device choice of whether an expanded sidebar project folder lists its
// sessions a page at a time behind "Show more" (the default) or lists them all.

import { createStoredBooleanPreference } from "./storedBooleanPreference";

export const PROJECT_FOLDER_PAGINATION_STORAGE_KEY = "omnigent:project-folder-pagination";

export const DEFAULT_PROJECT_FOLDER_PAGINATION = true;

// How many sessions a paginated folder shows at first and adds with each Show more click.
export const PROJECT_FOLDER_PAGE_SIZE = 5;

const preference = createStoredBooleanPreference(
  PROJECT_FOLDER_PAGINATION_STORAGE_KEY,
  DEFAULT_PROJECT_FOLDER_PAGINATION,
);

export const readProjectFolderPagination = preference.read;
export const writeProjectFolderPagination = preference.write;
export const notifyProjectFolderPaginationChanged = preference.notifyChanged;

/** Live value of the preference, including changes made in other windows. */
export function useProjectFolderPagination(): boolean {
  return preference.use();
}
