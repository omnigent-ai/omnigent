import { afterEach, expect, it, vi } from "vitest";
import {
  migrateLegacyProjectFolderPagination,
  parseProjectFolderPageSize,
  PROJECT_FOLDER_COLLAPSE_STORAGE_KEY,
  PROJECT_FOLDER_PAGE_SIZE,
  PROJECT_FOLDER_PAGE_SIZE_STORAGE_KEY,
  readProjectFolderCollapse,
  readProjectFolderPageSize,
  writeProjectFolderCollapse,
  writeProjectFolderPageSize,
} from "./projectFolderPaginationPreferences";

const LEGACY_STORAGE_KEY = "omnigent:project-folder-pagination";

// Storage failures and live updates are covered by storedChoicePreference.test.ts.
afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

it("collapses by default and stores only the switch being off", () => {
  expect(readProjectFolderCollapse()).toBe(true);
  localStorage.setItem(PROJECT_FOLDER_COLLAPSE_STORAGE_KEY, "no");
  expect(readProjectFolderCollapse()).toBe(true);

  writeProjectFolderCollapse(false);
  expect(localStorage.getItem(PROJECT_FOLDER_COLLAPSE_STORAGE_KEY)).toBe("false");
  expect(readProjectFolderCollapse()).toBe(false);

  writeProjectFolderCollapse(true);
  expect(localStorage.getItem(PROJECT_FOLDER_COLLAPSE_STORAGE_KEY)).toBeNull();
});

it("defaults the page size to five and stores only another whole number", () => {
  expect(readProjectFolderPageSize()).toBe(PROJECT_FOLDER_PAGE_SIZE);

  writeProjectFolderPageSize(3);
  expect(localStorage.getItem(PROJECT_FOLDER_PAGE_SIZE_STORAGE_KEY)).toBe("3");
  expect(readProjectFolderPageSize()).toBe(3);

  for (const stored of ["0", "51", "2.5", "-3", "abc", ""]) {
    localStorage.setItem(PROJECT_FOLDER_PAGE_SIZE_STORAGE_KEY, stored);
    expect(readProjectFolderPageSize()).toBe(PROJECT_FOLDER_PAGE_SIZE);
  }

  writeProjectFolderPageSize(PROJECT_FOLDER_PAGE_SIZE);
  expect(localStorage.getItem(PROJECT_FOLDER_PAGE_SIZE_STORAGE_KEY)).toBeNull();
});

it.each([
  { text: "1", size: 1 },
  { text: "50", size: 50 },
  { text: " 12 ", size: 12 },
  { text: "0", size: undefined },
  { text: "51", size: undefined },
  { text: "2.5", size: undefined },
  { text: "-3", size: undefined },
  { text: "1e1", size: undefined },
  { text: "abc", size: undefined },
  { text: "", size: undefined },
])("reads $text as page size $size", ({ text, size }) => {
  expect(parseProjectFolderPageSize(text)).toBe(size);
});

it("turns the switch off for an earlier build's All choice and drops its key", () => {
  localStorage.setItem(LEGACY_STORAGE_KEY, "all");
  // A failed save keeps the legacy choice for the next start to retry.
  const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("quota exceeded");
  });
  migrateLegacyProjectFolderPagination();
  expect(localStorage.getItem(LEGACY_STORAGE_KEY)).toBe("all");
  expect(readProjectFolderCollapse()).toBe(true);

  setItem.mockRestore();
  migrateLegacyProjectFolderPagination();
  expect(readProjectFolderCollapse()).toBe(false);
  expect(localStorage.getItem(LEGACY_STORAGE_KEY)).toBeNull();

  // Any other legacy value keeps the default.
  localStorage.clear();
  localStorage.setItem(LEGACY_STORAGE_KEY, "false");
  migrateLegacyProjectFolderPagination();
  expect(localStorage.getItem(PROJECT_FOLDER_COLLAPSE_STORAGE_KEY)).toBeNull();
  expect(localStorage.getItem(LEGACY_STORAGE_KEY)).toBeNull();
});
