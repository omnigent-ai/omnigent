// TanStack Query hooks for the runner's filesystem resources API.
//
// Two separate endpoints, two separate hooks:
//
//   useWorkspaceChangedFiles — calls `/changes` (registry-backed,
//     flat list of every file created/modified/deleted since session start).
//     Used by the flat "changed files" view in FilesPanel.
//
//   useWorkspaceAllFiles — calls `/filesystem` (directory listing, on-disk
//     state, no change annotations).  Used by the folder tree view in
//     FilesPanel.
//
// Both hooks return `available: false` on 404 so the UI can degrade
// gracefully when the runner has no OS environment for the session
// (e.g. cloud-only agents).

import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { authenticatedFetch } from "@/lib/identity";
import { useChatStore } from "@/store/chatStore";

/** True when `id` is the focused conversation and its agent loop is live. */
function useSessionActive(conversationId: string | undefined): boolean {
  const focusedId = useChatStore((s) => s.conversationId);
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  if (!conversationId || conversationId !== focusedId) return false;
  return sessionStatus === "running" || sessionStatus === "waiting";
}

/**
 * Fire one query invalidation when the session transitions active → idle.
 *
 * Polling stops the instant `sessionActive` flips to false, but the agent's
 * final file writes may not yet be in the last polled snapshot (last poll
 * was up to 10s ago, registry may have been updated since). A trailing
 * invalidate refetches once so the panel reflects end-of-turn state without
 * the user having to reload the page.
 */
function useTrailingInvalidate(
  conversationId: string | undefined,
  sessionActive: boolean,
  queryKeyPrefix: string,
) {
  const queryClient = useQueryClient();
  const prev = useRef<{ id: string | undefined; active: boolean }>({
    id: conversationId,
    active: sessionActive,
  });
  useEffect(() => {
    const sameSession = prev.current.id === conversationId;
    const justWentIdle = sameSession && prev.current.active && !sessionActive;
    prev.current = { id: conversationId, active: sessionActive };
    if (justWentIdle && conversationId) {
      queryClient.invalidateQueries({ queryKey: [queryKeyPrefix, conversationId] });
    }
  }, [conversationId, sessionActive, queryClient, queryKeyPrefix]);
}

// The primary workspace environment is always "default".  Terminals also each
// expose an environment (id: "terminal_<name>_<session_key>"), but the files
// panel and file viewer target the primary workspace only.
const DEFAULT_ENVIRONMENT_ID = "default";

interface WorkspaceQueryOptions {
  enabled?: boolean;
}

// ── Changed files (flat, registry-backed) ────────────────────────────────────

export interface WorkspaceChangedFile {
  path: string;
  name: string;
  /** Change status: "created", "modified", or "deleted". */
  status: "created" | "modified" | "deleted";
  bytes: number | null;
  modified_at: number | null;
}

export interface WorkspaceChangedFilesResult {
  available: boolean;
  data: WorkspaceChangedFile[];
}

/**
 * The session's runner is bound but not currently connected (the server's
 * `runner_unavailable` error, HTTP 503). For host-bound sessions this is
 * recoverable — sending a message wakes the runner — so callers render a
 * "reconnect" hint instead of a raw error. Distinct from a 404 (no OS
 * environment at all, e.g. a cloud-only agent), which degrades to
 * `available: false`.
 */
export class RunnerOfflineError extends Error {
  constructor() {
    super("runner offline");
    this.name = "RunnerOfflineError";
  }
}

// ── Runner-boot retry policy ──────────────────────────────────────────────────
//
// A freshly-bound session whose runner is still booting/connecting its WS
// tunnel answers 503 (``runner_unavailable``) until it comes up. The previous
// budget — 3 retries at a flat 1.5s (~4.5s) — gave up before a cold runner
// finished, so the Working-folder panel flashed "Failed to load: 503". These
// queries instead retry the runner-offline case with capped exponential
// backoff for ~2 minutes, long enough to outlast a cold boot; the reconnect
// hint only shows once that budget is exhausted (a genuinely offline runner).

/**
 * Max retry attempts for a still-connecting runner. With the backoff schedule
 * below (1s, 2s, 4s, 8s, then 15s cap) this spans ~2 minutes.
 */
export const MAX_RUNNER_OFFLINE_RETRIES = 12;

/** Capped exponential backoff: 1s, 2s, 4s, 8s, then 15s for every later try. */
export function runnerOfflineRetryDelay(attemptIndex: number): number {
  return Math.min(1000 * 2 ** attemptIndex, 15_000);
}

/**
 * Retry only the runner-offline case (a new session whose runner is still
 * connecting) so it resolves before any error UI; other failures (e.g. 500s)
 * surface immediately rather than being delayed.
 */
export function shouldRetryRunnerOffline(failureCount: number, error: Error): boolean {
  return error instanceof RunnerOfflineError && failureCount <= MAX_RUNNER_OFFLINE_RETRIES;
}

/**
 * Whether a 503 response is the app's `runner_unavailable` error rather
 * than a generic infrastructure 503.
 *
 * A 503 is NOT always the bound runner being offline: the Databricks Apps
 * front door / gateway returns 503 while the app restarts or cold-starts.
 * Only the app-level error carries `{"error": {"code": "runner_unavailable"}}`,
 * so match on that — a bare/HTML 503 falls through to the normal
 * error+retry path instead of the (misleading) "agent is asleep" hint.
 */
export async function isRunnerUnavailable503(res: Response): Promise<boolean> {
  try {
    const body = (await res.json()) as { error?: { code?: string } };
    return body?.error?.code === "runner_unavailable";
  } catch {
    // Non-JSON body (gateway/front-door 503) → not the app error.
    return false;
  }
}

interface ChangedFilesResponse {
  object: "list";
  data: Array<{
    path: string;
    name: string;
    status: "created" | "modified" | "deleted";
    bytes: number | null;
    modified_at: number | null;
  }>;
  has_more: boolean;
}

async function fetchWorkspaceChangedFiles(
  conversationId: string,
): Promise<WorkspaceChangedFilesResult> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/environments/${DEFAULT_ENVIRONMENT_ID}/changes`,
  );
  if (res.status === 404) {
    return { available: false, data: [] };
  }
  // The bound runner isn't connected. Throw the typed error so the panel
  // can show a reconnect hint — but only for the app's runner_unavailable
  // code, and only after retries are exhausted (see the hook's retry):
  // a new session whose runner is still spinning up also 503s, but its
  // runner connects within the retry window, so the hint never shows.
  if (res.status === 503 && (await isRunnerUnavailable503(res))) {
    throw new RunnerOfflineError();
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const json = (await res.json()) as ChangedFilesResponse;
  const data: WorkspaceChangedFile[] = json.data.map((e) => ({
    path: e.path,
    name: e.name,
    status: e.status,
    bytes: e.bytes,
    modified_at: e.modified_at,
  }));
  return { available: true, data };
}

/**
 * Fetch files changed in the local workspace during the current session.
 *
 * Backed by the runner's filesystem registry (watchdog).  Returns every
 * file created, modified, or deleted since the session began, regardless
 * of directory depth.  Returns `available: false` when the runner has no
 * OS environment for this session (404).
 */
export function useWorkspaceChangedFiles(
  conversationId: string | undefined,
  options: WorkspaceQueryOptions = {},
) {
  const queryEnabled = options.enabled ?? true;
  const runnerOnline = useSessionRunnerOnline(conversationId);
  const environmentQuery = useWorkspaceEnvironment(conversationId, {
    enabled: queryEnabled,
  });
  const sessionActive = useSessionActive(conversationId);
  useTrailingInvalidate(conversationId, sessionActive, "workspace-changed-files");
  return useQuery({
    queryKey: ["workspace-changed-files", conversationId],
    queryFn: () => fetchWorkspaceChangedFiles(conversationId!),
    enabled:
      queryEnabled &&
      !!conversationId &&
      runnerOnline !== false &&
      environmentQuery.data?.available === true,
    // Capped-backoff retry of the runner-offline case (see
    // shouldRetryRunnerOffline). Whether the eventual error reads as
    // "asleep" vs the plain empty state is decided by the session's
    // `failed` status, not by retries.
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
    // No polling: the SSE ``session.changed_files.invalidated`` event
    // (runner-emitted after file-mutating tools, throttled) drives
    // refetches via chatStore, and ``useTrailingInvalidate`` backstops
    // the final state at end-of-turn. The cold GET on mount bootstraps.
    staleTime: 5_000,
  });
}

// ── All files (directory listing, on-disk state) ──────────────────────────────

export interface WorkspaceFile {
  path: string;
  name: string;
  type: "file" | "directory";
  bytes: number | null;
  modified_at: number | null;
}

export interface WorkspaceAllFilesResult {
  available: boolean;
  data: WorkspaceFile[];
}

interface FilesystemListResponse {
  object: "list";
  data: Array<{
    id: string;
    name: string;
    path: string;
    type: string;
    bytes: number | null;
    modified_at: number | null;
  }>;
  has_more: boolean;
}

/** Normalize a raw filesystem list payload into `WorkspaceFile[]`. */
function mapFilesystemEntries(json: FilesystemListResponse): WorkspaceFile[] {
  return json.data.map((e) => ({
    path: e.path,
    name: e.name,
    type: e.type === "directory" ? "directory" : "file",
    bytes: e.bytes,
    modified_at: e.modified_at,
  }));
}

async function fetchWorkspaceAllFiles(conversationId: string): Promise<WorkspaceAllFilesResult> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/environments/${DEFAULT_ENVIRONMENT_ID}/filesystem?limit=1000&order=asc`,
  );
  if (res.status === 404) {
    return { available: false, data: [] };
  }
  // See fetchWorkspaceChangedFiles: only the app's runner_unavailable 503
  // (not an infra/front-door 503) is the offline runner, and the hook
  // retries so a still-connecting new session resolves before the hint.
  if (res.status === 503 && (await isRunnerUnavailable503(res))) {
    throw new RunnerOfflineError();
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const json = (await res.json()) as FilesystemListResponse;
  return { available: true, data: mapFilesystemEntries(json) };
}

/**
 * Fetch all files in the workspace root directory (on-disk state).
 *
 * Backed by a directory listing of the runner's OS environment cwd.
 * Returns top-level entries only (no recursive walk).  Returns
 * `available: false` when the runner has no OS environment for this
 * session (404).
 */
export function useWorkspaceAllFiles(
  conversationId: string | undefined,
  options: WorkspaceQueryOptions = {},
) {
  const queryEnabled = options.enabled ?? true;
  const runnerOnline = useSessionRunnerOnline(conversationId);
  const environmentQuery = useWorkspaceEnvironment(conversationId, {
    enabled: queryEnabled,
  });
  const sessionActive = useSessionActive(conversationId);
  useTrailingInvalidate(conversationId, sessionActive, "workspace-all-files");
  return useQuery({
    queryKey: ["workspace-all-files", conversationId],
    queryFn: () => fetchWorkspaceAllFiles(conversationId!),
    enabled:
      queryEnabled &&
      !!conversationId &&
      runnerOnline !== false &&
      environmentQuery.data?.available === true,
    // Capped-backoff retry of the runner-offline case (see
    // shouldRetryRunnerOffline). The asleep-vs-empty decision is made by
    // the session's `failed` status downstream, not by retries.
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 5_000,
  });
}

// ── Recursive file search ──────────────────────────────────────────────────────

async function fetchWorkspaceFileSearch(
  conversationId: string,
  query: string,
  include: string,
  exclude: string,
): Promise<WorkspaceFile[]> {
  const params = new URLSearchParams({ limit: "500" });
  if (query) params.set("q", query);
  if (include) params.set("include", include);
  if (exclude) params.set("exclude", exclude);
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/environments/${DEFAULT_ENVIRONMENT_ID}/search?${params}`,
  );
  // 404 means the runner has no OS environment for this session (cloud-only
  // agent).  Mirror the behaviour of useWorkspaceAllFiles: return empty
  // results rather than surfacing an error.
  if (res.status === 404) return [];
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return mapFilesystemEntries((await res.json()) as FilesystemListResponse);
}

/**
 * Search for files recursively in the workspace by name/path substring,
 * optionally scoped by include/exclude glob filters (VSCode-style).
 *
 * Calls the server-side ``/search`` endpoint which performs a full directory
 * walk so results include files in unexpanded subdirectories.  The query is
 * disabled when ``query`` is empty — the include/exclude globs only narrow an
 * active text query, they do not search on their own.
 *
 * @param conversationId Session/conversation id, or undefined when not ready.
 * @param query Free-text name/path substring; required for the query to fire.
 * @param include Comma-separated include globs, e.g. ``"*.ts, src/**"``.
 * @param exclude Comma-separated exclude globs, e.g. ``"**\/node_modules"``.
 */
export function useWorkspaceFileSearch(
  conversationId: string | undefined,
  query: string,
  include: string | undefined = undefined,
  exclude: string | undefined = undefined,
  options: WorkspaceQueryOptions = {},
) {
  const runnerOnline = useSessionRunnerOnline(conversationId);
  const trimmed = query.trim();
  const trimmedInclude = include?.trim() ?? "";
  const trimmedExclude = exclude?.trim() ?? "";
  return useQuery({
    queryKey: ["workspace-file-search", conversationId, trimmed, trimmedInclude, trimmedExclude],
    queryFn: () =>
      fetchWorkspaceFileSearch(conversationId!, trimmed, trimmedInclude, trimmedExclude),
    enabled:
      (options.enabled ?? true) && !!conversationId && trimmed.length > 0 && runnerOnline !== false,
    staleTime: 5_000,
    placeholderData: (prev) => prev,
  });
}

// ── Directory contents (lazy, on-demand) ──────────────────────────────────────

async function fetchWorkspaceDirectory(
  conversationId: string,
  dirPath: string,
): Promise<WorkspaceFile[]> {
  const encodedPath = dirPath.split("/").map(encodeURIComponent).join("/");
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/environments/${DEFAULT_ENVIRONMENT_ID}/filesystem/${encodedPath}?limit=1000&order=asc`,
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return mapFilesystemEntries((await res.json()) as FilesystemListResponse);
}

// ── Path existence check (parent-directory listing) ──────────────────────────

/**
 * Heuristic gate: does this inline-code string look like a workspace-relative
 * file path worth verifying against the filesystem?
 *
 * Keeps us from firing a directory listing for every backtick span (`git
 * status`, `useState`, `npm test`, …). A candidate must have a parent segment
 * before its first slash, not be absolute (the FileViewer rejects absolute
 * paths), not be a URL or carry a query/fragment, and not have whitespace
 * before its first slash (which marks a command like `git diff src/app`
 * rather than a path). Every slash-delimited segment must be non-empty and
 * not a `.`/`..` traversal segment (rejects `a/`, `a//b`, `../x`). Spaces
 * *inside* a segment are allowed — `docs/Design Notes.md` is a real filename
 * and segments are URL-encoded when listed.
 */
export function looksLikeWorkspaceFilePath(text: string): boolean {
  if (!text) return false;
  if (text.startsWith("/")) return false; // absolute paths are rejected by FileViewer
  if (text.includes("://")) return false; // URLs (http://, file://, …)
  if (text.includes("?") || text.includes("#")) return false; // query/fragment → not a plain path
  const slash = text.indexOf("/");
  if (slash <= 0) return false; // need a parent segment before the first slash
  // Whitespace before the first slash means a command, not a path.
  if (/\s/.test(text.slice(0, slash))) return false;
  const segments = text.split("/");
  // Need at least parent + basename, every segment non-empty (rejects "a/",
  // "a//b"), and no "."/".." traversal segments.
  if (segments.length < 2) return false;
  return segments.every((seg) => seg !== "" && seg !== "." && seg !== "..");
}

/**
 * Resolve a path mentioned in chat to a workspace-relative path, or null.
 *
 * The filesystem API (existence check, FileViewer) speaks workspace-relative
 * paths, but the agent often writes absolute (``/home/u/ws/foo.md``) or
 * home-relative (``~/ws/foo.md``) forms. This collapses those onto ``root``:
 *
 *  - plain relative (``src/app.tsx``) → returned unchanged (the caller's
 *    existing path-shape heuristic still gates it).
 *  - ``~``-prefixed → expanded with ``home``, then stripped of ``root``.
 *  - absolute under ``root`` → stripped of ``root``.
 *
 * Returns null when the path is absolute/home-relative but lies OUTSIDE the
 * workspace root (can't open in the FileViewer), is the root directory itself,
 * or is ``~``-relative with no ``home`` to expand.
 *
 * The returned relative path is always free of empty/``.``/``..`` segments: an
 * absolute path with interior traversal (``/root/ws/../etc/hosts``) would strip
 * to ``../etc/hosts``, which could escape the workspace once turned into a
 * fetch/FileViewer URL — those resolve to null instead. URLs and paths carrying
 * a query/fragment (``?``/``#``) are rejected up-front (mirroring
 * {@link looksLikeWorkspaceFilePath}) so an absolute span like
 * ``/root/ws/foo.md#L12`` doesn't strip to ``foo.md#L12`` and fire a doomed
 * existence check that can never match a real file.
 *
 * @param text Raw path string from an inline-code span.
 * @param root Absolute workspace root, e.g. ``"/home/u/ws"``, or null.
 * @param home Absolute runner home, e.g. ``"/home/u"``, or null.
 * @returns Workspace-relative path (no leading slash), or null if not
 *   resolvable into the workspace.
 */
export function toWorkspaceRelativePath(
  text: string,
  root: string | null,
  home: string | null,
): string | null {
  if (!text) return null;
  // URLs / query / fragment can never name a workspace file. Reject before
  // any stripping so a "trusted" absolute path doesn't carry these markers
  // past the existence-check heuristic and trigger a fetch that can't match.
  if (text.includes("://") || text.includes("?") || text.includes("#")) return null;
  let p = text;
  if (home && (p === "~" || p.startsWith("~/"))) {
    // Strip a trailing slash off home so "/" home (root user) doesn't
    // double up: "/" + "/ws" → "//ws".
    p = home.replace(/\/+$/, "") + p.slice(1);
  }
  if (!p.startsWith("/")) {
    // A leftover "~" means home-relative with no home to expand → unresolvable.
    if (p.startsWith("~")) return null;
    return hasUnsafeSegments(p) ? null : p; // plain relative path
  }
  // Absolute: must live under the workspace root to be openable.
  if (!root) return null;
  const normRoot = root.replace(/\/+$/, "");
  if (p === normRoot) return null; // the root directory itself, not a file
  const prefix = `${normRoot}/`;
  if (!p.startsWith(prefix)) return null; // absolute but outside the workspace
  const rel = p.slice(prefix.length);
  // The stripped tail may still contain interior traversal (e.g.
  // "/root/ws/../etc/hosts" → "../etc/hosts"). Reject it so the resolved
  // path can't escape the workspace via a normalized fetch/FileViewer URL.
  return hasUnsafeSegments(rel) ? null : rel;
}

/**
 * True when a relative path has any empty, ``.``, or ``..`` segment — i.e. it
 * is non-canonical and could traverse outside its base once resolved.
 */
function hasUnsafeSegments(rel: string): boolean {
  return rel.split("/").some((seg) => seg === "" || seg === "." || seg === "..");
}

async function fetchDirEntriesTolerant(
  conversationId: string,
  dirPath: string,
): Promise<WorkspaceFile[]> {
  // An empty dirPath is the workspace root — its listing lives at the bare
  // ``/filesystem`` endpoint, not ``/filesystem/`` (a root-level file like
  // ``foo.md`` resolves to a "" parent).
  const base = `/v1/sessions/${encodeURIComponent(conversationId)}/resources/environments/${DEFAULT_ENVIRONMENT_ID}/filesystem`;
  const encodedPath = dirPath.split("/").map(encodeURIComponent).join("/");
  const res = await authenticatedFetch(
    dirPath === "" ? `${base}?limit=1000&order=asc` : `${base}/${encodedPath}?limit=1000&order=asc`,
  );
  // 404 = the directory (or the whole OS environment) is absent, so the file
  // can't exist. Degrade to "no entries" rather than surfacing an error.
  if (res.status === 404) return [];
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return mapFilesystemEntries((await res.json()) as FilesystemListResponse);
}

/**
 * Check whether `path` names an existing *file* in the session workspace.
 *
 * Backed by a listing of the path's PARENT directory — cheap (one stat-level
 * listing, metadata only), shared across sibling files via the React Query
 * cache, and far lighter than a recursive `/search` walk or a full content
 * read. Returns `false` while loading, when `path` is null or not path-shaped,
 * or when the runner has no OS environment for this session.
 *
 * @param conversationId Session/conversation id, or undefined when not ready.
 * @param path Candidate workspace-relative path, or null to disable the check.
 * @param trusted When true, skip the {@link looksLikeWorkspaceFilePath}
 *   heuristic — the caller already proved the path is workspace-relative by
 *   resolving an absolute/home-relative form against the root (see
 *   {@link toWorkspaceRelativePath}). Such a path may be a bare basename
 *   (``foo.md``, no interior slash) that the heuristic would reject.
 */
export function useWorkspaceFileExists(
  conversationId: string | undefined,
  path: string | null,
  trusted = false,
): boolean {
  const runnerOnline = useSessionRunnerOnline(conversationId);
  const candidate = path && (trusted || looksLikeWorkspaceFilePath(path)) ? path : null;
  // Parent of a root-level file (no slash) is "" — the workspace root listing.
  const parentDir = candidate
    ? candidate.includes("/")
      ? candidate.slice(0, candidate.lastIndexOf("/"))
      : ""
    : null;
  const query = useQuery({
    // Distinct prefix from `useWorkspaceDirectory` ("workspace-dir") because
    // this query tolerates 404 and that one throws — they must not share a
    // cache entry with conflicting queryFns.
    queryKey: ["workspace-dir-listing", conversationId, parentDir],
    queryFn: () => fetchDirEntriesTolerant(conversationId!, parentDir!),
    enabled: !!conversationId && parentDir !== null && runnerOnline !== false,
    // Longer TTL than the root/changed-files queries (5s): a referenced file's
    // existence rarely changes mid-conversation, and this fires per inline
    // path span, so a 30s cache keeps repeated mentions from re-listing.
    staleTime: 30_000,
  });
  if (!candidate) return false;
  return (query.data ?? []).some((e) => e.type === "file" && e.path === candidate);
}

// ── Default environment (working folder root) ─────────────────────────────────

export interface WorkspaceEnvironment {
  /** Whether the default filesystem environment exists for this session. */
  available: boolean;
  /** Absolute path to the workspace root, or null if not available. */
  root: string | null;
  /**
   * Absolute path to the runner's home directory, or null when the server
   * doesn't report it. Used to expand a leading ``~`` in agent-mentioned
   * paths before resolving them against {@link root}.
   */
  home: string | null;
}

async function fetchWorkspaceEnvironment(conversationId: string): Promise<WorkspaceEnvironment> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/environments/${DEFAULT_ENVIRONMENT_ID}`,
  );
  if (res.status === 404) return { available: false, root: null, home: null };
  if (res.status === 503 && (await isRunnerUnavailable503(res))) {
    throw new RunnerOfflineError();
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const json = (await res.json()) as { metadata?: { root?: string; home?: string } };
  const root = json.metadata?.root ?? null;
  const home = json.metadata?.home ?? null;
  return { available: root !== null, root, home };
}

/**
 * Fetch the default environment resource for a session.
 *
 * Returns the workspace root path from the environment's metadata, or
 * ``null`` when the runner has no filesystem configured for this session
 * (``metadata.root`` absent in the 200 response).
 */
export function useWorkspaceEnvironment(
  conversationId: string | undefined,
  options: WorkspaceQueryOptions = {},
) {
  const runnerOnline = useSessionRunnerOnline(conversationId);
  return useQuery({
    queryKey: ["workspace-environment", conversationId],
    queryFn: () => fetchWorkspaceEnvironment(conversationId!),
    enabled: (options.enabled ?? true) && !!conversationId && runnerOnline !== false,
    retry: shouldRetryRunnerOffline,
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 60_000,
  });
}

/**
 * Fetch the contents of a specific workspace directory on demand.
 *
 * Used by the folder tree view to lazily load subdirectory children
 * when the user expands a directory node.  The query is disabled when
 * `dirPath` is null (collapsed or not yet requested).
 */
export function useWorkspaceDirectory(conversationId: string | undefined, dirPath: string | null) {
  const runnerOnline = useSessionRunnerOnline(conversationId);
  return useQuery({
    queryKey: ["workspace-dir", conversationId, dirPath],
    queryFn: () => fetchWorkspaceDirectory(conversationId!, dirPath!),
    enabled: !!conversationId && !!dirPath && runnerOnline !== false,
    staleTime: 5_000,
  });
}
