import { randomUUID } from "@/lib/randomUUID";
import { useEffect, useState } from "react";
import { type QueryMeta, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/identity";
import { readShowIgnoredFiles } from "@/lib/ignoredFilesPreference";
import {
  cancelWorkspaceRequest,
  getDesktopFeatures,
  hasCancelWorkspaceRequestBridge,
  hasFastTrackRetryBridge,
  hasHostKeyTrustBridge,
  hasWorkspaceContentSearchBridge,
  hasWorkspaceFastTrackBridge,
  hasWorkspaceSessionRouteBridge,
  onWorkspaceAccessReady,
  workspaceContentSearch,
  type WorkspaceContentSearchOutcome,
  workspaceFastTrackRequest,
} from "@/lib/nativeBridge";
import { getSessionHost } from "@/lib/sessionHost";

/**
 * The local file fast track: in the desktop app, a session whose host is this
 * Mac has its file listings, reads, and searches answered by the shell's
 * workspace helper instead of a round trip through the server and runner. The
 * answer is the runner's JSON, so callers can't tell the routes apart, except
 * that it can leave out git-ignored files, as the user prefers. Every other
 * client, and any request the shell declines, takes HTTPS as before.
 * The page sends only the URL: the shell asks the server what the session may read.
 */

/** Response header naming the route that answered, for debugging. */
export const WORKSPACE_ROUTE_HEADER = "x-omnigent-route";
/** The route of answers from this Mac's own helper; every shell names it so. */
export const THIS_MAC_ROUTE = "workspace-helper";
/** Response header saying whether git-ignored files were left out. */
export const WORKSPACE_IGNORED_FILES_HEADER = "x-omnigent-ignored-files";

export interface WorkspaceFetchOptions {
  /**
   * Whether listings and searches follow the user's ignored-files preference.
   * Checks that a path exists pass false, so an ignored file still counts.
   */
  followIgnoredFilesPreference?: boolean;
  /**
   * Ask a listing to report each directory's chain of single-directory
   * children, so a tree can show `a/b/c` on one row. Only this Mac's shell
   * reports them; HTTPS answers without.
   */
  directoryChains?: boolean;
  /**
   * Answer only from this Mac: reject with {@link NotAnsweredLocallyError}
   * instead of taking HTTPS. Speculative loads use it; reads the user asked for don't.
   */
  localOnly?: boolean;
  /**
   * Answer only from this desktop app, on this Mac or over SSH: reject with
   * {@link NotAnsweredByDesktopError} instead of taking HTTPS. For requests the
   * server can't answer, such as content search where the server has it off.
   * Send it only to shells that report session routes.
   */
  desktopOnly?: boolean;
}

/** This Mac didn't answer a request that may not take HTTPS. */
export class NotAnsweredLocallyError extends Error {
  constructor() {
    super("This Mac didn't answer this file request.");
    this.name = "NotAnsweredLocallyError";
  }
}

/** This desktop app didn't answer a request that may not take HTTPS; `reason` says why. */
export class NotAnsweredByDesktopError extends Error {
  readonly reason: string;
  constructor(reason: string) {
    super("This desktop app didn't answer this file request.");
    this.name = "NotAnsweredByDesktopError";
    this.reason = reason;
  }
}

const PROMOTED_REQUEST =
  /^\/v1\/sessions\/([^/?]+)\/resources\/environments\/default\/(filesystem|search|content-search|changes|diff|refs|commits)(?:[/?]|$)/;
/** Endpoints the shell answers only for pinned comparisons, their refs and commits; desktop-only. */
const DESKTOP_ONLY_ENDPOINTS = new Set(["changes", "diff", "refs", "commits"]);

/** A shell capability: it hides or shows git-ignored files as the page asks. */
export const IGNORED_FILES_CAPABILITY = "ignored_files";
/** A shell capability: it says which route, this Mac or SSH, would answer a session. */
export const SESSION_ROUTE_CAPABILITY = "session_route";
/** A shell capability: it reviews and trusts a session's machine's new SSH host key. */
export const TRUST_HOST_KEY_CAPABILITY = "trust_host_key";
/** A shell capability: it compares a session's working copy with its branch point. */
export const WORKING_COPY_BASE_CAPABILITY = "working_copy_base";
/** A shell capability: the page may cancel a request it named with a `requestId`. */
export const CANCEL_REQUESTS_CAPABILITY = "cancel_requests";
/** A shell capability: it reports a session's branch, push target and remotes. */
export const REPOSITORY_CONTEXT_CAPABILITY = "repository_context";
/** A shell capability: a session-route answer can carry its fast track's status. */
export const FAST_TRACK_STATUS_CAPABILITY = "fast_track_status";
/** A shell capability: the page may ask main to try a session's fast track again. */
export const FAST_TRACK_RETRY_CAPABILITY = "fast_track_retry";
/** A shell capability: a content search streams its matches as they're found. */
export const CONTENT_SEARCH_STREAM_CAPABILITY = "content_search_stream";
/** A shell capability: it lists commits up to a named head; older shells list HEAD's instead. */
export const COMMITS_HEAD_CAPABILITY = "commits_head";
/** A shell capability: comparison requests may name `root`, another repository of the session. */
export const COMPARISON_ROOT_CAPABILITY = "comparison_root";

interface ShellFastTrack {
  supported: boolean;
  capabilities: readonly string[];
}

let shellFastTrack: Promise<ShellFastTrack> | null = null;

function shellFastTrackFeatures(): Promise<ShellFastTrack> {
  if (!hasWorkspaceFastTrackBridge()) {
    return Promise.resolve({ supported: false, capabilities: [] });
  }
  shellFastTrack ??= getDesktopFeatures().then((features) => {
    const supported = features?.workspaceFastTrack === true;
    const listed = features?.workspaceFastTrackCapabilities;
    // The first fast-track shells list none and ignore any filtering request.
    const capabilities = supported && Array.isArray(listed) ? listed : [];
    return { supported, capabilities };
  });
  return shellFastTrack;
}

/** Whether this shell offers the fast track: the bridge method plus its feature flag. */
export async function shellSupportsFastTrack(): Promise<boolean> {
  return (await shellFastTrackFeatures()).supported;
}

/** Whether this shell's fast track can hide git-ignored files; older shells can't. */
export async function shellFiltersIgnoredFiles(): Promise<boolean> {
  return (await shellFastTrackFeatures()).capabilities.includes(IGNORED_FILES_CAPABILITY);
}

/**
 * Whether this shell says which route would answer a session: it lists the
 * capability and has the bridge method. Older shells have neither.
 */
export async function shellReportsSessionRoutes(): Promise<boolean> {
  if (!hasWorkspaceSessionRouteBridge()) return false;
  return (await shellFastTrackFeatures()).capabilities.includes(SESSION_ROUTE_CAPABILITY);
}

/**
 * Whether this shell can trust a machine's new SSH host key: it lists the
 * capability and has both bridge methods. Older shells have neither.
 */
export async function shellTrustsHostKeys(): Promise<boolean> {
  if (!hasHostKeyTrustBridge()) return false;
  return (await shellFastTrackFeatures()).capabilities.includes(TRUST_HOST_KEY_CAPABILITY);
}

/**
 * Whether this shell can compare a working copy with its branch point: it
 * reports session routes and lists the capability. Older shells can't.
 */
export async function shellComparesWithBranchPoint(): Promise<boolean> {
  if (!(await shellReportsSessionRoutes())) return false;
  return (await shellFastTrackFeatures()).capabilities.includes(WORKING_COPY_BASE_CAPABILITY);
}

/** Whether this shell reports a session's repository context; older shells and Intel Macs don't. */
export async function shellServesRepositoryContext(): Promise<boolean> {
  return (await shellFastTrackFeatures()).capabilities.includes(REPOSITORY_CONTEXT_CAPABILITY);
}

/** Whether this shell lists commits up to a named head; older shells would list HEAD's. */
export async function shellListsCommitsToHead(): Promise<boolean> {
  return (await shellFastTrackFeatures()).capabilities.includes(COMMITS_HEAD_CAPABILITY);
}

/**
 * Whether this shell reviews another repository in a session's folders: it compares
 * with branch points and lists the capability. Older shells would review the workspace.
 */
export async function shellComparesOtherRoots(): Promise<boolean> {
  if (!(await shellComparesWithBranchPoint())) return false;
  return (await shellFastTrackFeatures()).capabilities.includes(COMPARISON_ROOT_CAPABILITY);
}

/** Whether this shell streams content searches: it lists the capability and has the method. */
export async function shellStreamsContentSearch(): Promise<boolean> {
  if (!hasWorkspaceContentSearchBridge()) return false;
  return (await shellFastTrackFeatures()).capabilities.includes(CONTENT_SEARCH_STREAM_CAPABILITY);
}

/** Whether this shell reports a session's fast-track status: it reports routes and lists it. */
export async function shellReportsFastTrackStatus(): Promise<boolean> {
  if (!(await shellReportsSessionRoutes())) return false;
  return (await shellFastTrackFeatures()).capabilities.includes(FAST_TRACK_STATUS_CAPABILITY);
}

/** Whether this shell retries a session's fast track: it lists the capability and has the method. */
export async function shellRetriesFastTrack(): Promise<boolean> {
  if (!hasFastTrackRetryBridge() || !(await shellReportsFastTrackStatus())) return false;
  return (await shellFastTrackFeatures()).capabilities.includes(FAST_TRACK_RETRY_CAPABILITY);
}

/** Whether this shell cancels requests by ID: it lists the capability and has the method. */
export async function shellCancelsRequests(): Promise<boolean> {
  if (!hasCancelWorkspaceRequestBridge()) return false;
  return (await shellFastTrackFeatures()).capabilities.includes(CANCEL_REQUESTS_CAPABILITY);
}

/** Whether this app can hide git-ignored files from sessions on this Mac; false until known. */
export function useIgnoredFilesFilterAvailable(): boolean {
  const [available, setAvailable] = useState(false);
  useEffect(() => {
    let current = true;
    void shellFiltersIgnoredFiles().then((supported) => {
      if (current) setAvailable(supported);
    });
    return () => {
      current = false;
    };
  }, []);
  return available;
}

/** Whether this app can review another repository in a session's folders; null until known. */
export function useOtherRootComparisons(asked: boolean): boolean | null {
  const [available, setAvailable] = useState<boolean | null>(null);
  useEffect(() => {
    if (!asked) return;
    let current = true;
    void shellComparesOtherRoots().then((supported) => {
      if (current) setAvailable(supported);
    });
    return () => {
      current = false;
    };
  }, [asked]);
  return asked ? available : null;
}

/** Queries, keyed by session ID second, whose answers depend on the route that served them. */
const ROUTE_DEPENDENT_QUERIES = new Set([
  "workspace-all-files",
  "workspace-dir",
  "workspace-file-search",
  "content-search-result",
  // A helper still verifying answers `pending`; ask again once it can answer.
  "code-host-context",
]);

/**
 * Refetch a session's listings and searches once this Mac can answer them.
 * Until access is verified they take HTTPS, which can't hide ignored files.
 */
export function refetchWhenWorkspaceAccessReady(queryClient: QueryClient): () => void {
  return onWorkspaceAccessReady((sessionId) => {
    const filters = {
      predicate: ({ queryKey, meta }: { queryKey: readonly unknown[]; meta?: QueryMeta }) =>
        ROUTE_DEPENDENT_QUERIES.has(String(queryKey[0])) &&
        queryKey[1] === sessionId &&
        // A query may decline, as a content search that already streamed does.
        (typeof meta?.replayOnAccessReady !== "function" || meta.replayOnAccessReady() === true),
    };
    // A first load still waiting on HTTPS would otherwise be reused, and its
    // unfiltered answer kept; cancelling drops it before asking again.
    void queryClient
      .cancelQueries(filters)
      .then(() => queryClient.invalidateQueries(filters))
      .catch(() => {});
  });
}

/** Keeps file listings and searches on the route this Mac verified, for the app's lifetime. */
export function useRefetchWhenWorkspaceAccessReady(): void {
  const queryClient = useQueryClient();
  useEffect(() => refetchWhenWorkspaceAccessReady(queryClient), [queryClient]);
}

function abortError(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException("The operation was aborted.", "AbortError");
}

function newRequestId(): string {
  return randomUUID();
}

/**
 * Streams one content search from the shell, handing each batch of matches to
 * `onMatches` in order. Declines without asking when the session isn't
 * host-bound or the shell can't stream. On abort it tells the shell to stop,
 * rejects at once, and drops any batch still on its way.
 */
export async function streamWorkspaceSearch(
  url: string,
  {
    signal,
    focusedPaths = [],
    desktopOnly = false,
    onMatches,
  }: {
    signal?: AbortSignal;
    focusedPaths?: readonly string[];
    desktopOnly?: boolean;
    onMatches: (matches: unknown[]) => void;
  },
): Promise<WorkspaceContentSearchOutcome> {
  const match = PROMOTED_REQUEST.exec(url);
  const sessionId = match ? decodeURIComponent(match[1]) : null;
  if (sessionId === null || getSessionHost(sessionId) === null) return { declined: "unsupported" };
  if (!(await shellStreamsContentSearch())) return { declined: "unsupported" };
  if (signal?.aborted) throw abortError(signal);
  const requestId = newRequestId();
  let onAbort: (() => void) | undefined;
  const aborted = new Promise<never>((_, reject) => {
    onAbort = () => {
      cancelWorkspaceRequest(requestId);
      reject(abortError(signal!));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
  try {
    return await Promise.race([
      workspaceContentSearch(
        {
          url,
          requestId,
          focusedPaths: [...focusedPaths],
          hideIgnored: !readShowIgnoredFiles(),
          ...(desktopOnly ? { desktopOnly: true } : {}),
        },
        (matches) => {
          if (!signal?.aborted) onMatches(matches);
        },
      ),
      aborted,
    ]);
  } finally {
    signal?.removeEventListener("abort", onAbort!);
  }
}

/**
 * Sends one request to the shell. With a signal it rejects as soon as the
 * signal aborts, and a shell that can cancel is told to stop the request.
 */
export async function askShell(
  request: Parameters<typeof workspaceFastTrackRequest>[0],
  signal: AbortSignal | null | undefined,
): Promise<Awaited<ReturnType<typeof workspaceFastTrackRequest>>> {
  if (!signal) return workspaceFastTrackRequest(request);
  if (signal.aborted) throw abortError(signal);
  // One ID per physical request, so a cancel can't reach another one.
  const requestId = (await shellCancelsRequests()) ? newRequestId() : undefined;
  if (signal.aborted) throw abortError(signal);
  let onAbort: (() => void) | undefined;
  const aborted = new Promise<never>((_, reject) => {
    onAbort = () => {
      if (requestId) cancelWorkspaceRequest(requestId);
      reject(abortError(signal));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
  try {
    return await Promise.race([
      workspaceFastTrackRequest(requestId ? { ...request, requestId } : request),
      aborted,
    ]);
  } finally {
    signal.removeEventListener("abort", onAbort!);
  }
}

/**
 * Fetches a workspace file request, from this Mac's workspace helper when the
 * desktop shell can answer it and over HTTPS otherwise.
 *
 * :param url: A server-relative workspace request URL.
 * :param init: Fetch options; only a GET with an abort signal is promoted.
 * :param options: Whether the request follows the ignored-files preference.
 */
export async function workspaceFetch(
  url: string,
  init: RequestInit = {},
  {
    followIgnoredFilesPreference = true,
    directoryChains = false,
    localOnly = false,
    desktopOnly = false,
  }: WorkspaceFetchOptions = {},
): Promise<Response> {
  const match = PROMOTED_REQUEST.exec(url);
  // HEAD changes and diffs come from the runner; only pinned comparisons go to the shell.
  const promoted = match !== null && (desktopOnly || !DESKTOP_ONLY_ENDPOINTS.has(match[2]));
  const sessionId = match && promoted ? decodeURIComponent(match[1]) : null;
  // Only a host-bound session can be on this Mac; skip the shell for the rest.
  const hostBound = sessionId !== null && getSessionHost(sessionId) !== null;
  const plainGet = (init.method ?? "GET") === "GET" && init.body == null;
  let declined = "unsupported";
  if (hostBound && plainGet && (await shellSupportsFastTrack())) {
    const hideIgnored = followIgnoredFilesPreference && !readShowIgnoredFiles();
    const result = await askShell(
      {
        url,
        hideIgnored,
        directoryChains,
        ...(localOnly ? { localOnly: true } : {}),
        ...(desktopOnly ? { desktopOnly: true } : {}),
      },
      // Only comparison requests can be cancelled; other reads finish and are dropped.
      match && DESKTOP_ONLY_ENDPOINTS.has(match[2]) ? init.signal : undefined,
    );
    if (init.signal?.aborted) throw abortError(init.signal);
    if (result.ok) {
      const fromThisMac = result.route === THIS_MAC_ROUTE;
      if (fromThisMac) localSessions.add(sessionId);
      else localSessions.delete(sessionId);
      // A shell that ignores localOnly may still answer from another machine; that answer isn't kept.
      if (localOnly && !fromThisMac) throw new NotAnsweredLocallyError();
      return new Response(result.bodyText, {
        status: 200,
        headers: {
          "content-type": "application/json",
          [WORKSPACE_ROUTE_HEADER]: result.route,
          [WORKSPACE_IGNORED_FILES_HEADER]: result.ignoredFiles ?? "shown",
        },
      });
    }
    declined = result.reason;
  }
  if (sessionId !== null) localSessions.delete(sessionId);
  if (localOnly) throw new NotAnsweredLocallyError();
  if (desktopOnly) throw new NotAnsweredByDesktopError(declined);
  return authenticatedFetch(url, init);
}

/**
 * Sessions whose latest file request this Mac's own helper answered. Work
 * that's cheap only locally, such as prefetching, checks it before asking;
 * an answer from another machine's helper doesn't count.
 */
const localSessions = new Set<string>();

/** Whether this Mac answered the session's latest file request. */
export function answersLocally(sessionId: string): boolean {
  return localSessions.has(sessionId);
}

/** Forgets cached state; for tests. */
export function resetWorkspaceFastTrackForTests(): void {
  shellFastTrack = null;
  localSessions.clear();
}
