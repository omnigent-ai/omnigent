// The workspace API methods the workspace sidebar reads through, with the signatures of
// the workspace API client. Each answers null when no route serves it for the session.

import type { FileContentResponse } from "@/hooks/useFileContent";
import type { ContentSearchResult } from "@/lib/contentSearch";
import {
  type FileSearchOptions,
  type FoundEntries,
  listBranchCommitsViaWorkspaceApi,
  listDirectoryViaWorkspaceApi,
  type ListedEntry,
  listRepositoryRefsViaWorkspaceApi,
  listWorkingCopyChangesViaWorkspaceApi,
  readBaseFileViaWorkspaceApi,
  readFileViaWorkspaceApi,
  readLinkTextViaWorkspaceApi,
  resolveRepositoryContextViaWorkspaceApi,
  resolveWorkingCopyBaseViaWorkspaceApi,
  searchFilesViaWorkspaceApi,
  searchTextViaWorkspaceApi,
  type TextSearchOptions,
} from "@/lib/workspaceApi";

// ── Search results, in the REST routes' shapes, as the client returns them ─────

export type { ContentMatch, ContentSearchResult } from "@/lib/contentSearch";
export type { FileSearchOptions, FoundEntries, TextSearchOptions } from "@/lib/workspaceApi";

// ── Git comparisons, as the workspace API's messages ──────────────────────────
// Plain views of the generated messages, so callers and tests build them as literals;
// the client's answers are assigned to them below, so a changed message fails to compile.

export const ComparisonKind = {
  UNSPECIFIED: 0,
  /** The last commit (HEAD). */
  LAST_COMMIT: 1,
  /** The merge base of HEAD and the default branch. */
  BRANCH_POINT: 2,
  /** The merge base of `baseRevision` and `headRevision`, compared with that head. */
  REVISIONS: 3,
} as const;
export type ComparisonKind = (typeof ComparisonKind)[keyof typeof ComparisonKind];

export const ComparisonProblem = {
  UNSPECIFIED: 0,
  NOT_A_REPOSITORY: 1,
  NO_COMMITS: 2,
  NO_DEFAULT_BRANCH: 3,
  NO_BRANCH_POINT: 4,
  MISSING_OBJECT: 5,
  PARTIAL_CLONE_NEEDS_NEWER_GIT: 6,
  TIMEOUT: 7,
  TOO_MANY_FILES: 8,
  LISTING_TOO_LARGE: 9,
  GIT_UNAVAILABLE: 10,
  GIT_FAILED: 11,
  MULTIPLE_REPOSITORIES: 12,
  INVALID_REVISION: 13,
  BASE_NOT_FOUND: 14,
  HEAD_NOT_FOUND: 15,
  NO_COMMON_HISTORY: 16,
} as const;
export type ComparisonProblem = (typeof ComparisonProblem)[keyof typeof ComparisonProblem];

export const ComparisonField = {
  UNSPECIFIED: 0,
  BASE: 1,
  HEAD: 2,
  BOTH: 3,
} as const;
export type ComparisonField = (typeof ComparisonField)[keyof typeof ComparisonField];

export const ChangeStatus = {
  UNSPECIFIED: 0,
  ADDED: 1,
  MODIFIED: 2,
  DELETED: 3,
  TYPE_CHANGED: 4,
  CONFLICTED: 5,
} as const;
export type ChangeStatus = (typeof ChangeStatus)[keyof typeof ChangeStatus];

export const GitEntryKind = {
  /** No entry on this side, e.g. the base of an added file. */
  UNSPECIFIED: 0,
  FILE: 1,
  SYMLINK: 2,
  SUBMODULE: 3,
} as const;
export type GitEntryKind = (typeof GitEntryKind)[keyof typeof GitEntryKind];

export const ChangedFileProblem = {
  UNSPECIFIED: 0,
  /** A Git filter, such as LFS, changes how this file is stored. */
  FILTERED: 1,
  /** The file's base version isn't in this clone. */
  MISSING_OBJECT: 2,
} as const;
export type ChangedFileProblem = (typeof ChangedFileProblem)[keyof typeof ChangedFileProblem];

export const RepositoryContextProblem = {
  UNSPECIFIED: 0,
  NOT_A_REPOSITORY: 1,
  UNSUPPORTED_CONFIG: 2,
  REPOSITORY_CHANGING: 3,
  TIMEOUT: 4,
  GIT_UNAVAILABLE: 5,
  GIT_FAILED: 6,
} as const;
export type RepositoryContextProblem =
  (typeof RepositoryContextProblem)[keyof typeof RepositoryContextProblem];

export const PushUnresolved = {
  UNSPECIFIED: 0,
  REFSPEC: 1,
  PUSH_URL: 2,
  PUSH_DEFAULT: 3,
} as const;
export type PushUnresolved = (typeof PushUnresolved)[keyof typeof PushUnresolved];

/** What to compare; an unspecified kind means the last commit. */
export interface ComparisonInit {
  kind?: ComparisonKind;
  /** For REVISIONS: any revision Git resolves to a commit; empty means the default branch. */
  baseRevision?: string;
  /** Empty compares with the working copy. */
  headRevision?: string;
}

/** Why no comparison could be made. */
export interface ComparisonUnavailable {
  problem: ComparisonProblem;
  reference: string;
  shallow: boolean;
  detail: string;
  field: ComparisonField;
}

/** A resolved comparison. Commits are full object names. */
export interface WorkingCopyBase {
  kind: ComparisonKind;
  head: string;
  base: string;
  reference: string;
  referenceCommit: string;
  /** Set when no comparison could be made; the other fields are then empty. */
  unavailable?: ComparisonUnavailable;
  mergeBase: string;
  baseDisplay: string;
  headDisplay: string;
  headIsWorkingCopy: boolean;
}

/** A working file's own metadata at listing time; any change means the file changed. */
export interface WorkingFileMetadata {
  size: bigint;
  modifiedUnixNanos: bigint;
  changedUnixNanos: bigint;
  inode: bigint;
  device: bigint;
}

export interface CommittedFile {
  object: string;
  size?: bigint;
}

export interface ChangedFile {
  /** Relative to the workspace root. */
  path: string;
  status: ChangeStatus;
  baseKind: GitEntryKind;
  workingKind: GitEntryKind;
  /** The base blob, for `readBaseFile`. Empty for added files. */
  baseObject: string;
  baseSize?: bigint;
  linesAdded?: bigint;
  linesRemoved?: bigint;
  binary: boolean;
  problem: ChangedFileProblem;
  /** Absent when there is no working file (deleted). */
  working?: WorkingFileMetadata;
  /** For a committed head: the file's blob there. */
  committed?: CommittedFile;
}

/** Files sorted by path, at most 10,000. */
export interface ListWorkingCopyChangesResponse {
  base?: WorkingCopyBase;
  files: ChangedFile[];
  /** Changed files left out because their names aren't valid UTF-8. */
  unrepresentableNames: number;
}

export interface ListRepositoryRefsResponse {
  refs: string[];
  truncated: boolean;
  defaultBranchRef: string;
}

export interface BranchCommit {
  object: string;
  subject: string;
  authorName: string;
  authorTimeUnix: bigint;
  root: boolean;
}

export interface ListBranchCommitsResponse {
  base?: WorkingCopyBase;
  commits: BranchCommit[];
  truncated: boolean;
}

export interface RepositoryRemote {
  name: string;
  host: string;
  owner: string;
  repo: string;
}

export interface PushTarget {
  remoteName: string;
  branch: string;
  host: string;
  owner: string;
  repo: string;
  trackingSha: string;
}

export interface ResolveRepositoryContextResponse {
  problem: RepositoryContextProblem;
  branch: string;
  headSha: string;
  push?: PushTarget;
  pushUnresolved: PushUnresolved;
  upstreamRef: string;
  remotes: RepositoryRemote[];
  unsupportedRemoteCount: number;
  truncatedRemotes: boolean;
}

// ── The API ───────────────────────────────────────────────────────────────────

/** Each method answers null when no route serves it, so the caller falls back or says so. */
export interface WorkspaceSidebarApi {
  listDirectory: (
    sessionId: string,
    path: string,
    maxEntries: number,
    signal?: AbortSignal,
  ) => Promise<{ data: ListedEntry[]; hasMore: boolean } | null>;
  readFile: (
    sessionId: string,
    path: string,
    signal?: AbortSignal,
    sshTarget?: string | null,
  ) => Promise<FileContentResponse | null>;
  searchFiles: (
    sessionId: string,
    query: string,
    options: FileSearchOptions,
    signal?: AbortSignal,
  ) => Promise<FoundEntries | null>;
  searchText: (
    sessionId: string,
    query: string,
    options: TextSearchOptions,
    signal?: AbortSignal,
  ) => Promise<ContentSearchResult | null>;
  resolveWorkingCopyBase: (
    sessionId: string,
    comparison: ComparisonInit,
    signal?: AbortSignal,
  ) => Promise<WorkingCopyBase | null>;
  listWorkingCopyChanges: (
    sessionId: string,
    comparison: ComparisonInit,
    signal?: AbortSignal,
  ) => Promise<ListWorkingCopyChangesResponse | null>;
  readBaseFile: (
    sessionId: string,
    file: { commit: string; path: string; object: string },
    signal?: AbortSignal,
  ) => Promise<FileContentResponse | null>;
  readLinkText: (
    sessionId: string,
    path: string,
    signal?: AbortSignal,
  ) => Promise<Uint8Array | null>;
  listRepositoryRefs: (
    sessionId: string,
    signal?: AbortSignal,
  ) => Promise<ListRepositoryRefsResponse | null>;
  listBranchCommits: (
    sessionId: string,
    baseRevision: string,
    signal?: AbortSignal,
  ) => Promise<ListBranchCommitsResponse | null>;
  resolveRepositoryContext: (
    sessionId: string,
    signal?: AbortSignal,
  ) => Promise<ResolveRepositoryContextResponse | null>;
}

/**
 * Every method goes through the workspace API client, which picks the direct or server
 * route and answers null when no route's host serves the method.
 */
export const DEFAULT_WORKSPACE_SIDEBAR_API: WorkspaceSidebarApi = {
  listDirectory: listDirectoryViaWorkspaceApi,
  readFile: readFileViaWorkspaceApi,
  searchFiles: searchFilesViaWorkspaceApi,
  searchText: searchTextViaWorkspaceApi,
  resolveWorkingCopyBase: resolveWorkingCopyBaseViaWorkspaceApi,
  listWorkingCopyChanges: listWorkingCopyChangesViaWorkspaceApi,
  readBaseFile: readBaseFileViaWorkspaceApi,
  readLinkText: readLinkTextViaWorkspaceApi,
  listRepositoryRefs: listRepositoryRefsViaWorkspaceApi,
  listBranchCommits: listBranchCommitsViaWorkspaceApi,
  resolveRepositoryContext: resolveRepositoryContextViaWorkspaceApi,
};

let currentApi = DEFAULT_WORKSPACE_SIDEBAR_API;

/** The API the sidebar reads through. */
export function workspaceSidebarApi(): WorkspaceSidebarApi {
  return currentApi;
}

/** Replaces the API, for tests; `null` restores the default. */
export function setWorkspaceSidebarApiForTests(api: WorkspaceSidebarApi | null): void {
  currentApi = api ?? DEFAULT_WORKSPACE_SIDEBAR_API;
}
