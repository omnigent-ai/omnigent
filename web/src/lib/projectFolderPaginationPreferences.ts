// Per-device choice of how many sessions an expanded sidebar project folder lists:
// its first page with a "Show more" row (the default), or all of them.

import { createStoredChoicePreference } from "./storedChoicePreference";

export const PROJECT_FOLDER_PAGINATION_STORAGE_KEY = "omnigent:project-folder-pagination";

const PROJECT_FOLDER_PAGINATION_CHOICES = ["first-page", "all"] as const;

export type ProjectFolderPagination = (typeof PROJECT_FOLDER_PAGINATION_CHOICES)[number];

export const DEFAULT_PROJECT_FOLDER_PAGINATION: ProjectFolderPagination = "first-page";

// How many sessions a paginated folder shows at first and adds with each Show more click.
export const PROJECT_FOLDER_PAGE_SIZE = 5;

const preference = createStoredChoicePreference(
  PROJECT_FOLDER_PAGINATION_STORAGE_KEY,
  PROJECT_FOLDER_PAGINATION_CHOICES,
  DEFAULT_PROJECT_FOLDER_PAGINATION,
);

export const readProjectFolderPagination = preference.read;
export const writeProjectFolderPagination = preference.write;
export const notifyProjectFolderPaginationChanged = preference.notifyChanged;

/** Live value of the preference, including changes made in other windows. */
export function useProjectFolderPagination(): ProjectFolderPagination {
  return preference.use();
}
