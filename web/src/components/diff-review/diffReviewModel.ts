// The diff review's source-independent model: the changed files it lists, the
// filters that hide some of them, and the tree search. Loaders (working copy,
// pull request) produce `DiffReviewFile`s; the renderer only reads this model.

import { getFiletypeFromFileName, type SupportedLanguages } from "@pierre/diffs";
import { orderTreePaths } from "@/components/file-tree/vendored";
import { isGeneratedPath, isTestPath } from "./fileClassification";

/** How a file changed in the review. */
export type DiffReviewChange = "added" | "modified" | "deleted";

/** One changed file as the review lists it, before or after its diff loads. */
export interface DiffReviewFile {
  path: string;
  change: DiffReviewChange;
  /** Line counts the source reports up front; null when unknown. */
  additions: number | null;
  deletions: number | null;
  /** Display name of the file's language, e.g. "TypeScript". */
  language: string;
  isBinary: boolean;
  /** A renamed file's path before the rename; absent or null otherwise. */
  previousPath?: string | null;
  isTest: boolean;
  isGenerated: boolean;
  /** Whether the reader owns the file, such as under OWNERS; absent where nobody can say. */
  ownedByViewer?: boolean;
  /** Short notes for the file's header, such as "Type changed". */
  notes?: readonly string[];
}

/** Whether test files are shown, skipped, or the only ones shown. */
export type DiffTestFileView = "all" | "skipTests" | "onlyTests";

export interface DiffReviewFilters {
  testFileView: DiffTestFileView;
  showGeneratedFiles: boolean;
  /** Languages to show; null shows every language. */
  selectedLanguages: readonly string[] | null;
  /** Shows only the files the reader owns; applies only while some file is owned. */
  ownedFilesOnly: boolean;
}

export const DEFAULT_DIFF_REVIEW_FILTERS: DiffReviewFilters = {
  testFileView: "all",
  showGeneratedFiles: true,
  selectedLanguages: null,
  ownedFilesOnly: false,
};

/** Every way the filter menu, toolbar, and ⌘K change the filters. */
export type DiffFilterAction =
  | { type: "setTestFileView"; value: DiffTestFileView }
  | { type: "setShowGeneratedFiles"; value: boolean }
  | { type: "setOwnedFilesOnly"; value: boolean }
  | { type: "toggleLanguage"; language: string }
  /** Shows only `language`; when it's the only language there, that's no language filter. */
  | { type: "showOnlyLanguage"; language: string }
  | { type: "showAllLanguages" }
  | { type: "hideAllLanguages" }
  | { type: "clearFilters" };

const LANGUAGE_NAMES: Partial<Record<SupportedLanguages, string>> = {
  cpp: "C++",
  csharp: "C#",
  css: "CSS",
  html: "HTML",
  javascript: "JavaScript",
  json: "JSON",
  jsonnet: "Jsonnet",
  jsx: "JSX",
  lua: "Lua",
  proto: "Protocol Buffers",
  sql: "SQL",
  text: "Plain Text",
  toml: "TOML",
  tsx: "TSX",
  typescript: "TypeScript",
  xml: "XML",
  yaml: "YAML",
};

/** The syntax-highlighting language for `path`. */
export function syntaxLanguage(path: string): SupportedLanguages {
  return getFiletypeFromFileName(path);
}

/** How a file's language reads in the filter menu. */
export function displayLanguage(language: SupportedLanguages): string {
  return LANGUAGE_NAMES[language] ?? `${language[0]?.toUpperCase() ?? ""}${language.slice(1)}`;
}

/** A review file for `path`, classified by language, tests, and generated paths. */
export function reviewFile(
  path: string,
  change: DiffReviewChange,
  counts: { additions?: number | null; deletions?: number | null; isBinary?: boolean } = {},
): DiffReviewFile {
  return {
    path,
    change,
    additions: counts.additions ?? null,
    deletions: counts.deletions ?? null,
    language: displayLanguage(syntaxLanguage(path)),
    isBinary: counts.isBinary ?? false,
    isTest: isTestPath(path),
    isGenerated: isGeneratedPath(path),
  };
}

/** The test-file view a click moves to: all → skip tests → only tests → all. */
export function nextTestFileView(view: DiffTestFileView): DiffTestFileView {
  switch (view) {
    case "all":
      return "skipTests";
    case "skipTests":
      return "onlyTests";
    case "onlyTests":
      return "all";
  }
}

export function testFileViewLabel(view: DiffTestFileView): string {
  switch (view) {
    case "all":
      return "Show all files";
    case "skipTests":
      return "Skip test files";
    case "onlyTests":
      return "Show only test files";
  }
}

/** Whether the filters show `file`. */
export function isFileVisible(file: DiffReviewFile, filters: DiffReviewFilters): boolean {
  if (filters.selectedLanguages && !filters.selectedLanguages.includes(file.language)) return false;
  if (filters.testFileView === "skipTests" && file.isTest) return false;
  if (filters.testFileView === "onlyTests" && !file.isTest) return false;
  if (filters.ownedFilesOnly && !file.ownedByViewer) return false;
  return filters.showGeneratedFiles || !file.isGenerated;
}

/** Whether any filter hides files. */
export function hasActiveFilters(filters: DiffReviewFilters): boolean {
  return (
    filters.selectedLanguages !== null ||
    filters.testFileView !== "all" ||
    !filters.showGeneratedFiles ||
    filters.ownedFilesOnly
  );
}

/** How many of `files` the reader owns. */
export function ownedFileCount(files: readonly DiffReviewFile[]): number {
  return files.filter((file) => file.ownedByViewer).length;
}

/** `filters` as they apply to `files`: owning nothing listed turns the owned-only filter off. */
export function effectiveReviewFilters(
  files: readonly DiffReviewFile[],
  filters: DiffReviewFilters,
): DiffReviewFilters {
  if (!filters.ownedFilesOnly || ownedFileCount(files) > 0) return filters;
  return { ...filters, ownedFilesOnly: false };
}

/**
 * `files` in the order the changed-files tree lists them, which the diff follows too: folders
 * before files at each level, then by name. The tree's own ordering decides, so they can't drift.
 */
export function treeOrderedFiles<File extends { path: string }>(files: readonly File[]): File[] {
  const byPath = new Map(files.map((file) => [file.path, file]));
  const ordered = orderTreePaths([...byPath.keys()], { flattenEmptyDirectories: true })
    .map((path) => byPath.get(path))
    .filter((file): file is File => file !== undefined);
  // A path the tree couldn't place, such as one ending in "/", still shows, last.
  const placed = new Set(ordered.map((file) => file.path));
  return [...ordered, ...files.filter((file) => !placed.has(file.path))];
}

/** How many files each language has, sorted by name. */
export function languageCounts(
  files: readonly DiffReviewFile[],
): { language: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const file of files) counts.set(file.language, (counts.get(file.language) ?? 0) + 1);
  return [...counts]
    .map(([language, count]) => ({ language, count }))
    .sort((left, right) => left.language.localeCompare(right.language));
}

/** One language's share of a review's changed lines. */
export interface LanguageLineCounts {
  language: string;
  files: number;
  additions: number;
  deletions: number;
}

/** A review's line counts: in all, and per language, most changed lines first. */
export interface LineBreakdown {
  totals: { additions: number; deletions: number };
  languages: LanguageLineCounts[];
}

/**
 * Totals and per-language counts of `files` in one pass, from `countsFor`, the
 * same counts for every file, so the languages always add up to the totals.
 */
export function lineBreakdown(
  files: readonly DiffReviewFile[],
  countsFor: (file: DiffReviewFile) => { additions: number; deletions: number },
): LineBreakdown {
  const totals = { additions: 0, deletions: 0 };
  const byLanguage = new Map<string, LanguageLineCounts>();
  for (const file of files) {
    const counts = countsFor(file);
    totals.additions += counts.additions;
    totals.deletions += counts.deletions;
    const entry = byLanguage.get(file.language) ?? {
      language: file.language,
      files: 0,
      additions: 0,
      deletions: 0,
    };
    entry.files += 1;
    entry.additions += counts.additions;
    entry.deletions += counts.deletions;
    byLanguage.set(file.language, entry);
  }
  const changed = (entry: LanguageLineCounts) => entry.additions + entry.deletions;
  const languages = [...byLanguage.values()].sort(
    (left, right) => changed(right) - changed(left) || left.language.localeCompare(right.language),
  );
  return { totals, languages };
}

/** The language the filter shows alone, chosen as such; null for no filter or several. */
export function onlySelectedLanguage(filters: DiffReviewFilters): string | null {
  const selected = filters.selectedLanguages;
  return selected?.length === 1 ? (selected[0] ?? null) : null;
}

/** Apply one filter change. Selecting every language again clears the language filter. */
export function applyFilterAction(
  filters: DiffReviewFilters,
  action: DiffFilterAction,
  files: readonly DiffReviewFile[],
): DiffReviewFilters {
  switch (action.type) {
    case "setTestFileView":
      return { ...filters, testFileView: action.value };
    case "setShowGeneratedFiles":
      return { ...filters, showGeneratedFiles: action.value };
    case "setOwnedFilesOnly":
      return { ...filters, ownedFilesOnly: action.value };
    case "showAllLanguages":
      return { ...filters, selectedLanguages: null };
    case "hideAllLanguages":
      return { ...filters, selectedLanguages: [] };
    case "showOnlyLanguage": {
      const available = languageCounts(files).map(({ language }) => language);
      const everySelected = available.every((language) => language === action.language);
      return { ...filters, selectedLanguages: everySelected ? null : [action.language] };
    }
    case "toggleLanguage": {
      const available = languageCounts(files).map(({ language }) => language);
      const selected = new Set(filters.selectedLanguages ?? available);
      if (selected.has(action.language)) selected.delete(action.language);
      else selected.add(action.language);
      const everySelected = available.every((language) => selected.has(language));
      return { ...filters, selectedLanguages: everySelected ? null : [...selected] };
    }
    case "clearFilters":
      return DEFAULT_DIFF_REVIEW_FILTERS;
  }
}

/**
 * The files a review shows: those the filters keep, plus `linkedPath` when a
 * link asked for it, so following a link never lands on a hidden file.
 */
export function visibleReviewFiles(
  files: readonly DiffReviewFile[],
  filters: DiffReviewFilters,
  linkedPath?: string,
): DiffReviewFile[] {
  return files.filter((file) => file.path === linkedPath || isFileVisible(file, filters));
}

function normalizedTreeSearch(query: string | null | undefined): string {
  return (query?.trim() ?? "").replaceAll("\\", "/").toLowerCase();
}

/** Paths whose text contains the tree search, ignoring case; all paths without one. */
export function filterReviewPaths(paths: readonly string[], query: string | null): string[] {
  const needle = normalizedTreeSearch(query);
  if (!needle) return [...paths];
  return paths.filter((path) => path.toLowerCase().includes(needle));
}

/** Whether a tree search is narrowing the list. */
export function hasTreeSearch(query: string | null): boolean {
  return normalizedTreeSearch(query) !== "";
}

/** The CodeView item id for a file. */
export function reviewItemId(path: string): string {
  return `diff:${path}`;
}

/** The file path a CodeView item id names. */
export function pathFromReviewItemId(id: string): string {
  return id.startsWith("diff:") ? id.slice("diff:".length) : id;
}
