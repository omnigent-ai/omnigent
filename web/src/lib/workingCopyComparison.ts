// Comparing a session's working copy with its branch point (where the branch left
// its default branch, uncommitted and untracked changes included) or with a typed
// base and head. The session's host lists each comparison and serves its files
// through the workspace API; HEAD comparisons come from the runner as before. This
// module holds the comparison views, the user-facing texts, and the decision of
// which base a session shows.

import { Code, ConnectError } from "@connectrpc/connect";

import type { WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { type RevisionField, type RevisionSpec, sameRevisions } from "@/lib/customComparison";
import { workspacePathInRepository } from "@/lib/repositoryFolder";
import type { WorkingCopyBaseIntent } from "@/lib/workingCopyBasePreference";
import {
  ChangedFileProblem,
  ChangeStatus,
  ComparisonField,
  ComparisonKind,
  ComparisonProblem as WireComparisonProblem,
  type ChangedFile,
  type ComparisonInit,
  type ComparisonUnavailable,
  GitEntryKind,
  type ListWorkingCopyChangesResponse,
} from "@/lib/workspaceSidebarApi";
import {
  fileContentText,
  NOT_AVAILABLE_ON_HOST,
  WorkspaceMethodUnavailableError,
  workspaceSidebarSource,
} from "@/lib/workspaceSidebarSource";

/** One changed file in a branch-point comparison. */
export interface ComparisonEntry extends WorkspaceChangedFile {
  /** A file whose kind changed, or one with merge conflicts; null otherwise. */
  change: "type_changed" | "conflicted" | null;
  baseKind: EntryKind | null;
  workingKind: EntryKind | null;
  binary: boolean;
  /** Why this file can't be compared, known when it was listed. */
  problem: "filtered" | "missing_object" | null;
  /** Changes whenever the file may have changed: the working file's metadata and both blobs. */
  fingerprint: string;
}

export type EntryKind = "file" | "symlink" | "submodule";

/** A comparison the session's host listed. */
export interface BranchPointComparison {
  id: string;
  /** The ref the branch point was taken from, such as `origin/main`. */
  ref: string;
  baseOid: string;
  headOid: string;
  refOid: string;
  entries: ComparisonEntry[];
  /** Files left out because their names can't be shown. */
  unrepresentableNames: number;
  /** When the page received this listing (`Date.now()`). */
  listedAt: number;
  /** For a typed base and head, what was asked for and how the host names them; null for the branch point. */
  revisions?: RevisionComparison | null;
}

/** A comparison of a typed base and head: the merge base of the two against the head. */
export interface RevisionComparison {
  /** What the page asked for; the listing is shown only under this. */
  spec: RevisionSpec;
  baseDisplay: string;
  headDisplay: string;
  /** The head is the working copy, with uncommitted and untracked changes. */
  headIsWorkingCopy: boolean;
}

/** Why a session can't be compared with its branch point right now. */
export interface ComparisonProblem {
  reason: string;
  /** May pass on its own, or with Retry; definitive problems don't. */
  transient: boolean;
  ref?: string;
  shallow?: boolean;
  /** For a typed comparison, the field whose revision is the problem. */
  field?: RevisionField;
  /** The host's own words, such as Git's last error line. */
  detail?: string;
}

export type BranchPointListing =
  | { kind: "comparison"; comparison: BranchPointComparison }
  | { kind: "unavailable"; problem: ComparisonProblem };

/** The session's host predates comparing a repository folder inside the workspace. */
export const FOLDER_NOT_SERVED = "folder_not_served";
/** The host refused the folder, as one outside the workspace or in a Git directory. */
export const FOLDER_REFUSED = "folder_refused";

/** Why a repository folder's listing failed, where the folder itself is the reason. */
function folderProblemFrom(error: unknown): ComparisonProblem | null {
  if (error instanceof WorkspaceMethodUnavailableError) {
    return { reason: FOLDER_NOT_SERVED, transient: false };
  }
  if (error instanceof ConnectError && error.code === Code.InvalidArgument) {
    return { reason: FOLDER_REFUSED, transient: false };
  }
  return null;
}

/** Reasons a whole comparison can't be made that won't pass on their own. */
const DEFINITIVE_REASONS = new Set([
  "not_a_repository",
  "no_commits",
  "no_default_branch",
  "no_branch_point",
  "multiple_repositories",
  "partial_clone_needs_newer_git",
  "not_served",
  FOLDER_NOT_SERVED,
  FOLDER_REFUSED,
  // A typed base or head: only a different revision helps.
  "invalid_revision",
  "base_not_found",
  "head_not_found",
  "no_common_history",
]);

/** Whether a list refusal means the comparison can't be made here, as opposed to not yet. */
export function isDefinitiveReason(reason: string): boolean {
  return DEFINITIVE_REASONS.has(reason);
}

/** Refusals that mean the session's host is reconnecting; none come from the workspace API yet. */
const RECONNECTING_REASONS = new Set<string>();

/** Whether a refusal means the session's host is reconnecting. */
export function isReconnectingReason(reason: string): boolean {
  return RECONNECTING_REASONS.has(reason);
}

const PROBLEM_REASONS: Record<WireComparisonProblem, string> = {
  [WireComparisonProblem.UNSPECIFIED]: "git_failed",
  [WireComparisonProblem.NOT_A_REPOSITORY]: "not_a_repository",
  [WireComparisonProblem.NO_COMMITS]: "no_commits",
  [WireComparisonProblem.NO_DEFAULT_BRANCH]: "no_default_branch",
  [WireComparisonProblem.NO_BRANCH_POINT]: "no_branch_point",
  [WireComparisonProblem.MISSING_OBJECT]: "missing_object",
  [WireComparisonProblem.PARTIAL_CLONE_NEEDS_NEWER_GIT]: "partial_clone_needs_newer_git",
  [WireComparisonProblem.TIMEOUT]: "timeout",
  [WireComparisonProblem.TOO_MANY_FILES]: "too_many_files",
  [WireComparisonProblem.LISTING_TOO_LARGE]: "listing_too_large",
  [WireComparisonProblem.GIT_UNAVAILABLE]: "git_unavailable",
  [WireComparisonProblem.GIT_FAILED]: "git_failed",
  [WireComparisonProblem.MULTIPLE_REPOSITORIES]: "multiple_repositories",
  [WireComparisonProblem.INVALID_REVISION]: "invalid_revision",
  [WireComparisonProblem.BASE_NOT_FOUND]: "base_not_found",
  [WireComparisonProblem.HEAD_NOT_FOUND]: "head_not_found",
  [WireComparisonProblem.NO_COMMON_HISTORY]: "no_common_history",
};

const PROBLEM_FIELDS: Partial<Record<ComparisonField, RevisionField>> = {
  [ComparisonField.BASE]: "base",
  [ComparisonField.HEAD]: "head",
  [ComparisonField.BOTH]: "both",
};

/** A comparison problem from the host's answer. */
export function comparisonProblemFrom(unavailable: ComparisonUnavailable): ComparisonProblem {
  const reason = PROBLEM_REASONS[unavailable.problem] ?? "git_failed";
  const field = PROBLEM_FIELDS[unavailable.field];
  return {
    reason,
    transient: !isDefinitiveReason(reason),
    ...(unavailable.reference ? { ref: unavailable.reference } : {}),
    ...(reason === "no_branch_point" ? { shallow: unavailable.shallow } : {}),
    ...(field ? { field } : {}),
    ...(unavailable.detail ? { detail: unavailable.detail } : {}),
  };
}

/** Why a listing failed: a method no route serves, or the host's own error. */
export function listingProblemFrom(error: unknown): ComparisonProblem {
  if (error instanceof WorkspaceMethodUnavailableError) {
    return { reason: error.reason, transient: false };
  }
  return {
    reason: "request_failed",
    transient: true,
    ...(error instanceof Error && error.message ? { detail: error.message } : {}),
  };
}

const ENTRY_KINDS: Partial<Record<GitEntryKind, EntryKind>> = {
  [GitEntryKind.FILE]: "file",
  [GitEntryKind.SYMLINK]: "symlink",
  [GitEntryKind.SUBMODULE]: "submodule",
};

function changedStatus(status: ChangeStatus): ComparisonEntry["status"] {
  if (status === ChangeStatus.ADDED) return "created";
  if (status === ChangeStatus.DELETED) return "deleted";
  return "modified";
}

const count = (value: bigint | undefined): number | null =>
  value === undefined ? null : Number(value);

/** What a file's next read needs: the blobs each side is read from. */
interface ComparedFile {
  baseObject: string;
  /** The head's blob for a committed head; null for the working copy. */
  committedObject: string | null;
  deleted: boolean;
  binary: boolean;
  baseKind: EntryKind | null;
  workingKind: EntryKind | null;
  problem: ComparisonEntry["problem"];
}

/** One listed comparison, so its files can be read until a newer listing replaces it. */
interface ListedComparison {
  baseOid: string;
  headOid: string;
  files: Map<string, ComparedFile>;
  /** The folder relative to the workspace the files were listed in; empty for the workspace. */
  repository: string;
}

/** The latest listings by session and comparison, newest last; old ones go past the limit. */
const listedComparisons = new Map<string, ListedComparison>();
const LISTED_COMPARISON_LIMIT = 20;

/** Sessions comparing the same commits list their own workspaces, so each keeps its own files. */
function listingKey(sessionId: string, comparisonId: string): string {
  return JSON.stringify([sessionId, comparisonId]);
}

function remember(sessionId: string, id: string, listed: ListedComparison): void {
  const key = listingKey(sessionId, id);
  listedComparisons.delete(key);
  listedComparisons.set(key, listed);
  while (listedComparisons.size > LISTED_COMPARISON_LIMIT) {
    const oldest = listedComparisons.keys().next().value;
    if (oldest === undefined) break;
    listedComparisons.delete(oldest);
  }
}

/** Forget a session's listings, as when its workspace moves; its reads then list again. */
export function forgetListedComparisons(sessionId: string): void {
  const prefix = JSON.stringify([sessionId]).slice(0, -1) + ",";
  for (const key of [...listedComparisons.keys()]) {
    if (key.startsWith(prefix)) listedComparisons.delete(key);
  }
}

/** Forget every listing, for tests. */
export function resetListedComparisonsForTests(): void {
  listedComparisons.clear();
}

function comparisonEntry(file: ChangedFile): ComparisonEntry {
  const working = file.working;
  return {
    path: file.path,
    name: file.path.split("/").at(-1) ?? file.path,
    status: changedStatus(file.status),
    bytes: working ? Number(working.size) : count(file.baseSize),
    modified_at: working ? Number(working.modifiedUnixNanos / 1_000_000n) / 1000 : null,
    lines_added: count(file.linesAdded),
    lines_removed: count(file.linesRemoved),
    change:
      file.status === ChangeStatus.TYPE_CHANGED
        ? "type_changed"
        : file.status === ChangeStatus.CONFLICTED
          ? "conflicted"
          : null,
    baseKind: ENTRY_KINDS[file.baseKind] ?? null,
    workingKind: ENTRY_KINDS[file.workingKind] ?? null,
    binary: file.binary,
    problem:
      file.problem === ChangedFileProblem.FILTERED
        ? "filtered"
        : file.problem === ChangedFileProblem.MISSING_OBJECT
          ? "missing_object"
          : null,
    fingerprint: JSON.stringify([
      file.baseObject,
      file.committed?.object ?? null,
      working
        ? [
            working.size,
            working.modifiedUnixNanos,
            working.changedUnixNanos,
            working.inode,
            working.device,
          ].map(String)
        : null,
    ]),
  };
}

/** What to ask the host for: the branch point, or the typed base and head. */
export function comparisonRequest(spec: RevisionSpec | null): ComparisonInit {
  return spec
    ? { kind: ComparisonKind.REVISIONS, baseRevision: spec.base, headRevision: spec.head }
    : { kind: ComparisonKind.BRANCH_POINT };
}

/**
 * The comparison view of the host's listing, remembering its files for later reads;
 * null when it's for another kind of comparison than `spec` asked for.
 */
export function branchPointListingFrom(
  sessionId: string,
  response: ListWorkingCopyChangesResponse,
  listedAt: number,
  spec: RevisionSpec | null = null,
  repository = "",
): BranchPointListing | null {
  const base = response.base;
  if (!base) return null;
  if (base.unavailable) {
    return { kind: "unavailable", problem: comparisonProblemFrom(base.unavailable) };
  }
  const expected = spec ? ComparisonKind.REVISIONS : ComparisonKind.BRANCH_POINT;
  if (base.kind !== expected || !base.base) return null;
  const headIsWorkingCopy = spec ? base.headIsWorkingCopy : true;
  // The same commits name the same comparison; a newer listing of them replaces its files.
  // A worktree shares its commits with the workspace, so its folder is part of the name.
  const id = JSON.stringify([
    base.kind,
    base.base,
    headIsWorkingCopy ? "" : base.head,
    ...(repository ? [repository] : []),
  ]);
  const files = new Map<string, ComparedFile>();
  for (const file of response.files) {
    files.set(file.path, {
      baseObject: file.baseObject,
      committedObject: headIsWorkingCopy ? null : (file.committed?.object ?? null),
      deleted: file.status === ChangeStatus.DELETED,
      binary: file.binary,
      baseKind: ENTRY_KINDS[file.baseKind] ?? null,
      workingKind: ENTRY_KINDS[file.workingKind] ?? null,
      problem:
        file.problem === ChangedFileProblem.FILTERED
          ? "filtered"
          : file.problem === ChangedFileProblem.MISSING_OBJECT
            ? "missing_object"
            : null,
    });
  }
  remember(sessionId, id, { baseOid: base.base, headOid: base.head, files, repository });
  return {
    kind: "comparison",
    comparison: {
      id,
      ref: base.reference,
      baseOid: base.base,
      headOid: base.head,
      refOid: base.referenceCommit,
      entries: response.files.map(comparisonEntry),
      unrepresentableNames: response.unrepresentableNames,
      listedAt,
      revisions: spec
        ? {
            spec,
            baseDisplay: base.baseDisplay || spec.base,
            headDisplay: base.headDisplay || spec.head,
            headIsWorkingCopy,
          }
        : null,
    },
  };
}

/**
 * Lists the session's branch-point comparison, or the typed `spec`, from its host.
 * Rejects with {@link WorkspaceMethodUnavailableError} while no route serves it.
 */
export async function fetchBranchPointListing(
  sessionId: string,
  {
    signal,
    spec = null,
    repository = "",
  }: {
    refresh?: boolean;
    signal?: AbortSignal;
    spec?: RevisionSpec | null;
    /** A folder relative to the workspace, such as a worktree; empty lists the workspace. */
    repository?: string;
  } = {},
): Promise<BranchPointListing> {
  let response: ListWorkingCopyChangesResponse;
  try {
    response = await workspaceSidebarSource().listWorkingCopyChanges(
      sessionId,
      comparisonRequest(spec),
      signal,
      repository,
    );
  } catch (error) {
    const problem = repository && !signal?.aborted ? folderProblemFrom(error) : null;
    if (problem) return { kind: "unavailable", problem };
    throw error;
  }
  const listing = branchPointListingFrom(sessionId, response, Date.now(), spec, repository);
  if (!listing) return { kind: "unavailable", problem: { reason: "git_failed", transient: true } };
  return listing;
}

/** Branch and tag names to suggest for a typed base or head. */
export interface RepositoryRefs {
  refs: string[];
  /** Only the first names were listed. */
  truncated: boolean;
  /** Such as `origin/main`; empty when the repository has none. */
  defaultBranchRef: string;
}

/** Lists the session's branch and tag names from its host. */
export async function fetchRepositoryRefs(
  sessionId: string,
  signal?: AbortSignal,
  repository = "",
): Promise<RepositoryRefs> {
  const response = await workspaceSidebarSource().listRepositoryRefs(sessionId, signal, repository);
  return {
    refs: response.refs,
    truncated: response.truncated,
    defaultBranchRef: response.defaultBranchRef,
  };
}

/** One file's text under a branch-point comparison. */
export interface ComparisonFileText {
  comparisonId: string;
  path: string;
  before: string | null;
  after: string | null;
}

/** Why one file of a comparison can't be shown; `reason` names the refusal. */
export class ComparisonFileError extends Error {
  readonly reason: string;
  constructor(reason: string) {
    super(comparisonFileMessage(reason));
    this.name = "ComparisonFileError";
    this.reason = reason;
  }
}

/** A file's answer arrived after its comparison, fingerprint, or permission moved on. */
export class StaleComparisonReadError extends Error {
  constructor() {
    super("This file changed while loading.");
    this.name = "StaleComparisonReadError";
  }
}

/** Refusals after which the page lists the comparison again before trying the file. */
export function needsRelist(reason: string): boolean {
  return reason === "comparison_expired" || reason === "not_in_comparison";
}

/** Refusals that won't pass by trying the same file again. */
const SETTLED_FILE_REASONS = new Set([
  "too_large_text",
  "missing_object",
  "filtered",
  "binary",
  "submodule",
  "not_served",
]);

/** Whether a file refusal is the file's answer, rather than a failure worth retrying. */
export function isSettledFileReason(reason: string): boolean {
  return SETTLED_FILE_REASONS.has(reason);
}

/** Connect codes a base read answers with: the base moved, or the clone lacks the blob. */
const FAILED_PRECONDITION = 9;
const NOT_FOUND = 5;

/** A read's refusal, in the reasons a comparison's files are shown with. */
function fileReadError(error: unknown): unknown {
  if (error instanceof WorkspaceMethodUnavailableError)
    return new ComparisonFileError("not_served");
  if (error instanceof Error && error.name === "ConnectError") {
    const code = (error as Error & { code?: number }).code;
    if (code === FAILED_PRECONDITION) return new ComparisonFileError("comparison_expired");
    if (code === NOT_FOUND) return new ComparisonFileError("missing_object");
  }
  return error;
}

/** One side's text: a blob at a commit, or null when that side has no file. */
async function blobText(
  sessionId: string,
  commit: string,
  path: string,
  object: string,
  signal: AbortSignal | undefined,
  repository: string,
): Promise<string | null> {
  if (!object) return null;
  const file = await workspaceSidebarSource().readBaseFile(
    sessionId,
    { commit, path, object },
    signal,
    repository,
  );
  if (file.truncated) throw new ComparisonFileError("too_large_text");
  const text = fileContentText(file);
  if (text === null) throw new ComparisonFileError("binary");
  return text;
}

/**
 * Reads one file of a listed comparison from the session's host: the base blob,
 * and the head's blob or the working file. Binary files and submodules have no
 * text and reject with their reason; a comparison no longer listed rejects so
 * the caller lists it again.
 */
export async function fetchComparisonFileText(
  sessionId: string,
  comparisonId: string,
  path: string,
  signal?: AbortSignal,
): Promise<ComparisonFileText> {
  const listed = listedComparisons.get(listingKey(sessionId, comparisonId));
  if (!listed) throw new ComparisonFileError("comparison_expired");
  const file = listed.files.get(path);
  if (!file) throw new ComparisonFileError("not_in_comparison");
  if (file.problem) throw new ComparisonFileError(file.problem);
  if (file.baseKind === "submodule" || file.workingKind === "submodule") {
    throw new ComparisonFileError("submodule");
  }
  if (file.binary) throw new ComparisonFileError("binary");
  try {
    const { repository } = listed;
    const before = await blobText(
      sessionId,
      listed.baseOid,
      path,
      file.baseObject,
      signal,
      repository,
    );
    let after: string | null = null;
    if (file.committedObject !== null) {
      after = await blobText(
        sessionId,
        listed.headOid,
        path,
        file.committedObject,
        signal,
        repository,
      );
    } else if (!file.deleted) {
      // Working files are read by their workspace path, inside the repository's folder.
      const working = await workspaceSidebarSource().readFile(
        sessionId,
        workspacePathInRepository(path, repository),
        signal,
      );
      if (working.truncated) throw new ComparisonFileError("too_large_text");
      after = fileContentText(working);
      if (after === null) throw new ComparisonFileError("binary");
    }
    return { comparisonId, path, before, after };
  } catch (error) {
    throw fileReadError(error);
  }
}

// ── What the user reads ────────────────────────────────────────────────────

/** A file's message when a comparison can't show its diff. */
export function comparisonFileMessage(reason: string): string {
  switch (reason) {
    case "too_large_text":
      return "Too large to compare";
    case "missing_object":
      return "The branch-point version isn't available in this clone.";
    case "filtered":
      return "Can't compare: this file uses a Git filter (such as LFS)";
    case "binary":
      return "Binary file; no text to compare";
    case "submodule":
      return "Submodule commit changed";
    case "comparison_expired":
    case "not_in_comparison":
      return "This comparison changed. Refresh to compare it again.";
    case "not_served":
      return NOT_AVAILABLE_ON_HOST;
    default:
      return "Couldn't load this file's branch-point version.";
  }
}

/** Why a whole comparison can't be made, by the host's reason. */
export function comparisonProblemMessage(problem: ComparisonProblem): string {
  const ref = problem.ref || "the default branch";
  switch (problem.reason) {
    case "not_a_repository":
      return "This workspace isn't a Git repository.";
    case "no_commits":
      return "No commits yet.";
    case "no_default_branch":
      return "This repository's remote has no default branch set (`git remote set-head origin --auto` sets it).";
    case "no_branch_point":
      return problem.shallow
        ? `No branch point with ${ref} (the clone may be shallow).`
        : `No branch point with ${ref}.`;
    case "multiple_repositories":
      return "Branch point works for a single Git repository.";
    case "partial_clone_needs_newer_git":
      return "This partial clone needs Git 2.44 or later to compare with the branch point.";
    case "timeout":
      return "Comparing took too long.";
    case "missing_object":
      return "The branch point isn't available in this clone.";
    case "too_many_files":
      return "More than 10,000 changed files.";
    case "listing_too_large":
      return "Too many changes to list here (the change list is larger than 8 MB).";
    case "git_unavailable":
      return "Git isn't available on this session's machine.";
    case "not_served":
      return BRANCH_POINT_NOT_SERVED;
    case FOLDER_NOT_SERVED:
      return "Update Omnigent on this session's machine to see Diffs for this folder.";
    case FOLDER_REFUSED:
      return "Diffs can't show this folder. Choose a folder inside the workspace.";
    default:
      return problem.detail
        ? `Couldn't compare with the branch point: ${problem.detail}`
        : "Couldn't compare with the branch point.";
  }
}

/** How a typed comparison's head reads in a sentence. */
function headPhrase(spec: RevisionSpec): string {
  return spec.head ? `“${spec.head}”` : "the working copy";
}

/** Why a typed comparison can't be made, by the host's reason. */
export function revisionProblemMessage(problem: ComparisonProblem, spec: RevisionSpec): string {
  const base = `“${spec.base}”`;
  const head = `“${spec.head || "HEAD"}”`;
  switch (problem.reason) {
    case "invalid_revision":
      return problem.field === "head"
        ? `Head ${head} isn't a valid revision name.`
        : `Base ${base} isn't a valid revision name.`;
    case "base_not_found":
      return `Base ${base} wasn't found in this repository.`;
    case "head_not_found":
      return `Head ${head} wasn't found in this repository.`;
    case "no_common_history":
      return `Base ${base} and Head ${head} don't share a common commit.`;
    case "not_served":
      return REVISIONS_NOT_SERVED;
    case "missing_object":
      return "A commit this comparison needs isn't available in this clone.";
    case "multiple_repositories":
      return "Comparing revisions works for a single Git repository.";
    case "partial_clone_needs_newer_git":
      return "This partial clone needs Git 2.44 or later to compare revisions.";
    case "not_a_repository":
    case "no_commits":
    case FOLDER_NOT_SERVED:
    case FOLDER_REFUSED:
    case "timeout":
    case "too_many_files":
    case "listing_too_large":
    case "git_unavailable":
      return comparisonProblemMessage(problem);
    default:
      return problem.detail
        ? `Couldn't compare ${base} with ${headPhrase(spec)}: ${problem.detail}`
        : `Couldn't compare ${base} with ${headPhrase(spec)}.`;
  }
}

export const BRANCH_POINT_NOT_SERVED = `Comparing with the branch point: ${NOT_AVAILABLE_ON_HOST}`;
export const REVISIONS_NOT_SERVED = `Comparing other branches, tags and commits: ${NOT_AVAILABLE_ON_HOST}`;
export const COMPARING_REVISIONS = "Comparing…";
export const COMPARING = "Comparing with the branch point…";
export const RECONNECTING = "Reconnecting to this session's machine…";
export const COULD_NOT_REACH = "couldn't reach this session's machine.";
/** How an uncommitted-changes fallback or override note starts. */
export const SHOWING_UNCOMMITTED = "Showing uncommitted changes";

// ── Which base a session shows ─────────────────────────────────────────────

/** Whether the session's host serves Git comparisons: still asking, yes, or not yet. */
export type WorkspaceGitRoute =
  | { state: "checking" }
  | { state: "served" }
  | { state: "unserved" }
  /** Asking failed; asking again may help. */
  | { state: "unreachable"; message: string };

/** Whether this session can be compared with its branch point. */
export type BranchPointSupport =
  | { kind: "available" }
  /** Still asking the session's host. */
  | { kind: "checking" }
  /** Won't work here: the session's host doesn't serve comparisons. */
  | { kind: "unsupported"; message: string; reason: string }
  /** The session's host couldn't be asked now; that may pass. */
  | { kind: "unreachable"; message: string; reason: string };

function supportFor(route: WorkspaceGitRoute | null, unservedMessage: string): BranchPointSupport {
  if (!route || route.state === "checking") return { kind: "checking" };
  if (route.state === "served") return { kind: "available" };
  if (route.state === "unreachable") {
    return { kind: "unreachable", reason: "request_failed", message: route.message };
  }
  return { kind: "unsupported", reason: "not_served", message: unservedMessage };
}

/** Whether the session's branch point can be compared, from its host's answer. */
export function branchPointSupport({
  route,
}: {
  route: WorkspaceGitRoute | null;
}): BranchPointSupport {
  return supportFor(route, BRANCH_POINT_NOT_SERVED);
}

/** Whether a typed base and head can be compared, from the same answer. */
export function revisionSupport({
  route,
}: {
  route: WorkspaceGitRoute | null;
}): BranchPointSupport {
  return supportFor(route, REVISIONS_NOT_SERVED);
}

/** What a session's Changes and review show. */
export type EffectiveComparison =
  | {
      base: "head";
      /** The host's listing of the last commit's changes, in a repository folder. */
      listing?: BranchPointComparison;
      /** Why Automatic, or the person, chose the last commit here; null for an explicit choice. */
      note: string | null;
      /** The person chose to see uncommitted changes for this session only. */
      override: boolean;
      /** Automatic fell back for a reason that may pass, so trying again may help. */
      retry: boolean;
    }
  | {
      base: "branch-point";
      comparison: BranchPointComparison;
      /** Shown while the comparison can't be refreshed; its changes stay on screen. */
      status: { kind: "reconnecting" | "stale"; message: string } | null;
    }
  | { base: "pending"; message: string }
  | {
      base: "unavailable";
      message: string;
      /** Whether trying again may help. */
      retry: boolean;
      /** For a typed comparison, the field to fix. */
      field?: RevisionField;
    };

/**
 * Whether `comparison`'s after side is the working copy, the text comments anchor to; false for a
 * typed comparison with a commit or branch head, and while there's nothing to compare yet.
 */
export function comparisonHeadIsWorkingCopy(comparison: EffectiveComparison): boolean {
  if (comparison.base === "head") return true;
  if (comparison.base !== "branch-point") return false;
  return comparison.comparison.revisions?.headIsWorkingCopy ?? true;
}

export interface EffectiveComparisonInput {
  intent: WorkingCopyBaseIntent;
  support: BranchPointSupport;
  /** The workspace spans several repositories (the runner reports a root per file). */
  multipleRepositories: boolean;
  /** The comparison this session has, kept through transient failures. */
  established: BranchPointComparison | null;
  /** Why the latest listing gave no comparison, if it didn't. */
  problem: ComparisonProblem | null;
  /** Automatic waited long enough for a first comparison. */
  connectTimedOut: boolean;
  /** The person chose "Show uncommitted changes" for this session. */
  headOverride: boolean;
  /** The base and head the person typed for this session; it wins over `intent`. */
  custom?: RevisionSpec | null;
  /** Whether typed comparisons work for this session. */
  revisionSupport?: BranchPointSupport;
  /**
   * The review is of a repository folder, whose last commit only the host lists, as
   * `lastCommitSpec`: the last commit shows that listing, and nothing falls back to the runner.
   */
  lastCommitSpec?: RevisionSpec | null;
}

/**
 * What a typed comparison shows. Like an explicit Branch point, it never
 * falls back to the last commit, and only a listing of exactly what was typed
 * is shown under it.
 */
function typedComparison(
  input: EffectiveComparisonInput,
  custom: RevisionSpec,
): EffectiveComparison {
  const support = input.revisionSupport ?? { kind: "checking" };
  const { problem } = input;
  if (support.kind === "unsupported") {
    return {
      base: "unavailable",
      message: `Comparing “${custom.base}” with ${headPhrase(custom)} isn't available for this session right now. ${support.message}`,
      retry: false,
    };
  }
  if (problem && !problem.transient) {
    return {
      base: "unavailable",
      message: revisionProblemMessage(problem, custom),
      retry: false,
      ...(problem.field ? { field: problem.field } : {}),
    };
  }
  const established =
    input.established?.revisions && sameRevisions(input.established.revisions.spec, custom)
      ? input.established
      : null;
  const reconnecting = problem !== null && isReconnectingReason(problem.reason);
  const transient =
    support.kind === "unreachable"
      ? support.message
      : problem
        ? revisionProblemMessage(problem, custom)
        : null;
  if (established) {
    return {
      base: "branch-point",
      comparison: established,
      status:
        support.kind === "checking" || reconnecting
          ? { kind: "reconnecting", message: RECONNECTING }
          : transient
            ? { kind: "stale", message: `Couldn't refresh: ${transient}` }
            : null,
    };
  }
  if (transient && !reconnecting) return { base: "unavailable", message: transient, retry: true };
  return { base: "pending", message: COMPARING_REVISIONS };
}

/**
 * Which base a session shows. Automatic means the branch point when this
 * session's comparison is ready, otherwise the last commit; it never mixes the
 * two. An explicit Branch point that can't be made says why rather than
 * showing uncommitted changes, until the person chooses to.
 */
export function effectiveComparison(input: EffectiveComparisonInput): EffectiveComparison {
  const effective = workspaceComparison(input);
  const spec = input.lastCommitSpec;
  return spec && effective.base === "head" ? hostLastCommit(input, spec, effective) : effective;
}

/**
 * The last commit in a repository folder, which only the host lists: its listing once
 * made, and otherwise why not, never the workspace's changes from the runner.
 */
function hostLastCommit(
  input: EffectiveComparisonInput,
  spec: RevisionSpec,
  shown: Extract<EffectiveComparison, { base: "head" }>,
): EffectiveComparison {
  const support = input.revisionSupport ?? { kind: "checking" };
  const { problem } = input;
  if (support.kind === "unsupported") {
    return { base: "unavailable", message: support.message, retry: false };
  }
  if (problem && !problem.transient) {
    return { base: "unavailable", message: comparisonProblemMessage(problem), retry: false };
  }
  const listing =
    input.established?.revisions && sameRevisions(input.established.revisions.spec, spec)
      ? input.established
      : null;
  if (listing) return { ...shown, listing };
  const reconnecting = problem !== null && isReconnectingReason(problem.reason);
  let transient: string | null = null;
  if (support.kind === "unreachable") transient = support.message;
  else if (problem) transient = comparisonProblemMessage(problem);
  if (transient && !reconnecting) return { base: "unavailable", message: transient, retry: true };
  return { base: "pending", message: COMPARING };
}

function workspaceComparison(input: EffectiveComparisonInput): EffectiveComparison {
  const { intent, support, problem } = input;
  if (input.custom) return typedComparison(input, input.custom);
  // A typed comparison's listing never shows under the branch point.
  const established = input.established?.revisions ? null : input.established;
  if (intent === "head") return { base: "head", note: null, override: false, retry: false };
  if (input.headOverride) {
    return { base: "head", note: `${SHOWING_UNCOMMITTED}.`, override: true, retry: false };
  }
  // Automatic shows uncommitted changes in place of the branch point, except in a repository
  // folder, whose uncommitted changes the runner can't list.
  const fallsBack = intent === "automatic" && !input.lastCommitSpec;
  const definitive =
    support.kind === "unsupported"
      ? support.message
      : input.multipleRepositories
        ? comparisonProblemMessage({ reason: "multiple_repositories", transient: false })
        : problem && !problem.transient
          ? comparisonProblemMessage(problem)
          : null;
  if (definitive) {
    return fallsBack
      ? { base: "head", note: definitive, override: false, retry: false }
      : { base: "unavailable", message: definitive, retry: false };
  }
  const transient =
    support.kind === "unreachable"
      ? support.message
      : problem
        ? comparisonProblemMessage(problem)
        : null;
  if (established) {
    if (support.kind === "checking" || (problem && isReconnectingReason(problem.reason))) {
      return {
        base: "branch-point",
        comparison: established,
        status: { kind: "reconnecting", message: RECONNECTING },
      };
    }
    return {
      base: "branch-point",
      comparison: established,
      status: transient ? { kind: "stale", message: `Couldn't refresh: ${transient}` } : null,
    };
  }
  if (transient && !(problem && isReconnectingReason(problem.reason))) {
    return fallsBack
      ? {
          base: "head",
          note: `${SHOWING_UNCOMMITTED}: ${transient}`,
          override: false,
          retry: true,
        }
      : { base: "unavailable", message: transient, retry: true };
  }
  if (fallsBack && input.connectTimedOut) {
    return {
      base: "head",
      note: `${SHOWING_UNCOMMITTED}: ${COULD_NOT_REACH}`,
      override: false,
      retry: true,
    };
  }
  return { base: "pending", message: COMPARING };
}

/**
 * The host's listing a comparison shows, whose files are read through it: the branch point
 * or a typed comparison, or the last commit in a repository folder. Null when the runner
 * lists the changes, or nothing is listed yet.
 */
export function hostListing(comparison: EffectiveComparison): BranchPointComparison | null {
  if (comparison.base === "branch-point") return comparison.comparison;
  return comparison.base === "head" ? (comparison.listing ?? null) : null;
}

/** The toolbar's short name for a comparison, and its tooltip. */
export function branchPointLabel(comparison: BranchPointComparison): {
  label: string;
  description: string;
} {
  if (comparison.revisions) {
    const { baseDisplay, headDisplay, headIsWorkingCopy } = comparison.revisions;
    return {
      label: headIsWorkingCopy ? `vs ${baseDisplay}` : `${baseDisplay} … ${headDisplay}`,
      description: headIsWorkingCopy
        ? `Compared with where the working copy's branch left ${baseDisplay}, including uncommitted and untracked changes.`
        : `Commits on ${headDisplay} since it left ${baseDisplay}.`,
    };
  }
  const ref = comparison.ref || "the default branch";
  const sameAsHead = comparison.baseOid !== "" && comparison.baseOid === comparison.headOid;
  return {
    label: sameAsHead ? `vs ${ref} (same as last commit)` : `vs ${ref}`,
    description: `Compared with where this branch left ${ref}${
      comparison.baseOid ? ` (${comparison.baseOid.slice(0, 7)})` : ""
    }, including uncommitted and untracked changes.`,
  };
}
