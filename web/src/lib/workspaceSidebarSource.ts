// Where the workspace sidebar reads a session's files and Git state. Each read goes
// through the workspace API when a route serves the method; otherwise files and
// searches use the REST routes, and Git reads say they aren't available on this host.

import { fetchFileContentViaRest, type FileContentResponse } from "@/hooks/useFileContent";
import {
  browseLocationBase,
  browseLocationSegment,
  isRunnerUnavailable503,
  RunnerOfflineError,
} from "@/hooks/useWorkspaceChangedFiles";
import { authenticatedFetch } from "@/lib/identity";
import type { ListedEntry } from "@/lib/workspaceApi";
import {
  type ComparisonInit,
  ComparisonKind,
  type ContentSearchResult,
  type FileSearchOptions,
  type FoundEntries,
  type ListBranchCommitsResponse,
  type ListRepositoryRefsResponse,
  type ListWorkingCopyChangesResponse,
  type ResolveRepositoryContextResponse,
  type TextSearchOptions,
  type WorkingCopyBase,
  workspaceSidebarApi,
} from "@/lib/workspaceSidebarApi";

/** What a Git read says until the session's host serves it. */
export const NOT_AVAILABLE_ON_HOST = "Not available on this host yet.";

/** The workspace API methods only a host can answer: REST has no route for them. */
export type HostOnlyMethod =
  | "resolveWorkingCopyBase"
  | "listWorkingCopyChanges"
  | "readBaseFile"
  | "readLinkText"
  | "listRepositoryRefs"
  | "listBranchCommits"
  | "resolveRepositoryContext";

/** No route serves `method` for this session, so the sidebar shows a notice in its place. */
export class WorkspaceMethodUnavailableError extends Error {
  readonly method: HostOnlyMethod;
  /** The comparison layer's code for this refusal. */
  readonly reason = "not_served";

  constructor(method: HostOnlyMethod) {
    super(NOT_AVAILABLE_ON_HOST);
    this.name = "WorkspaceMethodUnavailableError";
    this.method = method;
  }
}

export interface WorkspaceSidebarSource {
  listDirectory: (
    sessionId: string,
    path: string,
    maxEntries: number,
    signal?: AbortSignal,
  ) => Promise<{ data: ListedEntry[]; hasMore: boolean }>;
  readFile: (sessionId: string, path: string, signal?: AbortSignal) => Promise<FileContentResponse>;
  searchFiles: (
    sessionId: string,
    query: string,
    options: FileSearchOptions,
    signal?: AbortSignal,
  ) => Promise<FoundEntries>;
  searchText: (
    sessionId: string,
    query: string,
    options: TextSearchOptions,
    signal?: AbortSignal,
  ) => Promise<ContentSearchResult>;
  /** Whether the session's host serves Git comparisons; false until a route serves them. */
  servesGitComparisons: (sessionId: string, signal?: AbortSignal) => Promise<boolean>;
  resolveWorkingCopyBase: (
    sessionId: string,
    comparison: ComparisonInit,
    signal?: AbortSignal,
  ) => Promise<WorkingCopyBase>;
  listWorkingCopyChanges: (
    sessionId: string,
    comparison: ComparisonInit,
    signal?: AbortSignal,
  ) => Promise<ListWorkingCopyChangesResponse>;
  readBaseFile: (
    sessionId: string,
    file: { commit: string; path: string; object: string },
    signal?: AbortSignal,
  ) => Promise<FileContentResponse>;
  readLinkText: (sessionId: string, path: string, signal?: AbortSignal) => Promise<Uint8Array>;
  listRepositoryRefs: (
    sessionId: string,
    signal?: AbortSignal,
  ) => Promise<ListRepositoryRefsResponse>;
  listBranchCommits: (
    sessionId: string,
    baseRevision: string,
    signal?: AbortSignal,
  ) => Promise<ListBranchCommitsResponse>;
  resolveRepositoryContext: (
    sessionId: string,
    signal?: AbortSignal,
  ) => Promise<ResolveRepositoryContextResponse>;
}

function sessionResourceUrl(sessionId: string, rest: string): string {
  return `/v1/sessions/${encodeURIComponent(sessionId)}/resources/environments/default/${rest}`;
}

/** An abort keeps its own reason, whichever layer noticed it. */
function rethrowIfAborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) throw signal.reason;
}

/** The API's answer, or the fallback's when no route serves the method. */
async function viaApiOr<Value>(
  signal: AbortSignal | undefined,
  viaApi: () => Promise<Value | null>,
  fallback: () => Promise<Value>,
): Promise<Value> {
  let answer: Value | null;
  try {
    answer = await viaApi();
  } catch (error) {
    rethrowIfAborted(signal);
    throw error;
  }
  rethrowIfAborted(signal);
  return answer ?? fallback();
}

/** A host-only read: the API's answer, or {@link WorkspaceMethodUnavailableError}. */
function hostOnly<Value>(
  method: HostOnlyMethod,
  signal: AbortSignal | undefined,
  viaApi: () => Promise<Value | null>,
): Promise<Value> {
  return viaApiOr(signal, viaApi, () =>
    Promise.reject(new WorkspaceMethodUnavailableError(method)),
  );
}

/** A REST failure, keeping the classes the panels already tell apart. */
async function restFailure(response: Response): Promise<Error> {
  if (await isRunnerUnavailable503(response)) return new RunnerOfflineError();
  return new Error(`${response.status} ${response.statusText}`);
}

interface RestListing {
  data: {
    id?: string;
    name: string;
    path: string;
    type: "file" | "directory";
    bytes: number | null;
    modified_at: number | null;
  }[];
  has_more?: boolean;
  truncated?: boolean;
}

function listedEntries(body: RestListing): ListedEntry[] {
  return body.data.map((entry) => ({
    id: entry.id ?? entry.path,
    name: entry.name,
    path: entry.path,
    type: entry.type,
    bytes: entry.bytes,
    modified_at: entry.modified_at,
  }));
}

async function restListDirectory(
  sessionId: string,
  path: string,
  maxEntries: number,
  signal?: AbortSignal,
): Promise<{ data: ListedEntry[]; hasMore: boolean }> {
  const params = new URLSearchParams({ limit: String(maxEntries), order: "asc" });
  const base = browseLocationBase(path);
  if (base) params.set("base", base);
  const segment = browseLocationSegment(path);
  const response = await authenticatedFetch(
    sessionResourceUrl(sessionId, `filesystem${segment ? `/${segment}` : ""}?${params}`),
    { signal },
  );
  if (!response.ok) throw await restFailure(response);
  const body = (await response.json()) as RestListing;
  return { data: listedEntries(body), hasMore: body.has_more === true };
}

async function restSearchFiles(
  sessionId: string,
  query: string,
  { path, include, exclude, limit }: FileSearchOptions,
  signal?: AbortSignal,
): Promise<FoundEntries> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  if (include) params.set("include", include);
  if (exclude) params.set("exclude", exclude);
  const base = browseLocationBase(path);
  if (base) params.set("base", base);
  const segment = browseLocationSegment(path);
  const response = await authenticatedFetch(
    sessionResourceUrl(sessionId, `search${segment ? `/${segment}` : ""}?${params}`),
    { signal },
  );
  if (!response.ok) throw await restFailure(response);
  const body = (await response.json()) as RestListing;
  return {
    data: listedEntries(body),
    hasMore: body.has_more === true,
    truncated: body.truncated === true,
  };
}

async function restSearchText(
  sessionId: string,
  query: string,
  { caseSensitive, regex, include, exclude }: TextSearchOptions,
  signal?: AbortSignal,
): Promise<ContentSearchResult> {
  const params = new URLSearchParams({
    q: query,
    case_sensitive: String(caseSensitive),
    regex: String(regex),
    include,
    exclude,
  });
  const response = await authenticatedFetch(
    sessionResourceUrl(sessionId, `content-search?${params}`),
    { signal },
  );
  const body = await response.json().catch(() => null);
  if (!response.ok || body?.error || !Array.isArray(body?.matches)) {
    throw new Error(
      (typeof body?.error === "string" ? body.error : body?.error?.message) ||
        (typeof body?.detail === "string" ? body.detail : null) ||
        "Content search is unavailable. Check that the runner is online and up to date.",
    );
  }
  return {
    matches: body.matches,
    truncated: body.truncated === true,
    warning: typeof body.warning === "string" ? body.warning : null,
  };
}

/** The source over the current API, with REST for what REST can answer. */
export const DEFAULT_WORKSPACE_SIDEBAR_SOURCE: WorkspaceSidebarSource = {
  listDirectory: (sessionId, path, maxEntries, signal) =>
    viaApiOr(
      signal,
      // The workspace API lists only inside the workspace.
      () =>
        browseLocationBase(path)
          ? Promise.resolve(null)
          : workspaceSidebarApi().listDirectory(sessionId, path, maxEntries, signal),
      () => restListDirectory(sessionId, path, maxEntries, signal),
    ),
  readFile: (sessionId, path, signal) =>
    viaApiOr(
      signal,
      () =>
        browseLocationBase(path)
          ? Promise.resolve(null)
          : workspaceSidebarApi().readFile(sessionId, path, signal),
      () => fetchFileContentViaRest(sessionId, path, signal),
    ),
  searchFiles: (sessionId, query, options, signal) =>
    viaApiOr(
      signal,
      () => workspaceSidebarApi().searchFiles(sessionId, query, options, signal),
      () => restSearchFiles(sessionId, query, options, signal),
    ),
  searchText: (sessionId, query, options, signal) =>
    viaApiOr(
      signal,
      () => workspaceSidebarApi().searchText(sessionId, query, options, signal),
      () => restSearchText(sessionId, query, options, signal),
    ),
  servesGitComparisons: async (sessionId, signal) => {
    try {
      const base = await workspaceSidebarApi().resolveWorkingCopyBase(
        sessionId,
        { kind: ComparisonKind.LAST_COMMIT },
        signal,
      );
      rethrowIfAborted(signal);
      return base !== null;
    } catch (error) {
      rethrowIfAborted(signal);
      // The host answered, so it serves the method; the listing shows its answer.
      if (error instanceof Error && error.name === "ConnectError") return true;
      throw error;
    }
  },
  resolveWorkingCopyBase: (sessionId, comparison, signal) =>
    hostOnly("resolveWorkingCopyBase", signal, () =>
      workspaceSidebarApi().resolveWorkingCopyBase(sessionId, comparison, signal),
    ),
  listWorkingCopyChanges: (sessionId, comparison, signal) =>
    hostOnly("listWorkingCopyChanges", signal, () =>
      workspaceSidebarApi().listWorkingCopyChanges(sessionId, comparison, signal),
    ),
  readBaseFile: (sessionId, file, signal) =>
    hostOnly("readBaseFile", signal, () =>
      workspaceSidebarApi().readBaseFile(sessionId, file, signal),
    ),
  readLinkText: (sessionId, path, signal) =>
    hostOnly("readLinkText", signal, () =>
      workspaceSidebarApi().readLinkText(sessionId, path, signal),
    ),
  listRepositoryRefs: (sessionId, signal) =>
    hostOnly("listRepositoryRefs", signal, () =>
      workspaceSidebarApi().listRepositoryRefs(sessionId, signal),
    ),
  listBranchCommits: (sessionId, baseRevision, signal) =>
    hostOnly("listBranchCommits", signal, () =>
      workspaceSidebarApi().listBranchCommits(sessionId, baseRevision, signal),
    ),
  resolveRepositoryContext: (sessionId, signal) =>
    hostOnly("resolveRepositoryContext", signal, () =>
      workspaceSidebarApi().resolveRepositoryContext(sessionId, signal),
    ),
};

let currentSource = DEFAULT_WORKSPACE_SIDEBAR_SOURCE;

/** The source the sidebar reads through. */
export function workspaceSidebarSource(): WorkspaceSidebarSource {
  return currentSource;
}

/** Replaces the source, for tests; `null` restores the default. */
export function setWorkspaceSidebarSourceForTests(source: WorkspaceSidebarSource | null): void {
  currentSource = source ?? DEFAULT_WORKSPACE_SIDEBAR_SOURCE;
}

/** A file's text, decoding base64 bytes as UTF-8; null when they aren't text. */
export function fileContentText(file: FileContentResponse): string | null {
  if (file.encoding !== "base64") return file.content;
  try {
    const binary = atob(file.content);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
}
