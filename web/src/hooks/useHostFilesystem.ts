import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/identity";

/**
 * One entry in a directory listing returned by the host
 * filesystem endpoint. Mirrors the wire shape from
 * ``GET /v1/hosts/{id}/filesystem``.
 */
export interface HostFilesystemEntry {
  /** Basename of the entry, e.g. ``"src"``. */
  name: string;
  /**
   * Absolute path on the host, e.g.
   * ``"/Users/corey/projects/src"``. The server returns absolute
   * paths so the picker can pass entries straight through to
   * the next ``list_dir`` call without re-resolving.
   */
  path: string;
  /**
   * ``"directory"``, ``"file"``, or ``"other"``. The picker
   * disables non-directory entries since workspaces must be
   * directories (the runner ``cd``s into them).
   */
  type: string;
  /** File size in bytes for regular files, ``null`` otherwise. */
  bytes: number | null;
  /** Unix epoch seconds of last modification. */
  modified_at: number;
}

interface HostFilesystemResponse {
  object: string;
  data: HostFilesystemEntry[];
  has_more: boolean;
}

/**
 * Build the filesystem URL for a given host + absolute path.
 *
 * The path is passed through ``encodeURIComponent`` per segment
 * so names with spaces or special chars survive. An empty
 * string maps to the ``/v1/hosts/{id}/filesystem`` route which
 * forwards ``~`` to ``host.list_dir`` server-side. Absolute
 * paths land on ``/v1/hosts/{id}/filesystem/{path:path}``;
 * FastAPI strips the leading slash and the route re-adds it.
 *
 * @param hostId Host identifier, e.g. ``"host_a1b2..."``.
 * @param absolutePath Absolute path to list (e.g.
 *   ``"/Users/corey/projects"``), or empty string for home.
 * @returns The relative URL to fetch.
 */
export function buildHostFilesystemUrl(hostId: string, absolutePath: string): string {
  const base = `/v1/hosts/${encodeURIComponent(hostId)}/filesystem`;
  if (absolutePath === "") {
    return base;
  }
  if (absolutePath === "/") {
    // Browsing exactly "/" must hit /filesystem/ (with trailing
    // slash) to match the {path:path} route. Without the trailing
    // slash we'd hit the no-path route which forwards ~ instead.
    return `${base}/`;
  }
  // Windows drive and UNC paths are already absolute. Encode each
  // segment after normalizing backslashes so FastAPI captures
  // ``C:/Users/me`` (not ``/C:/Users/me``).
  if (/^[A-Za-z]:[\\/]/.test(absolutePath) || absolutePath.startsWith("\\\\")) {
    const normalized = absolutePath.replace(/\\/g, "/");
    const encoded = normalized
      .split("/")
      .map((segment) => encodeURIComponent(segment))
      .join("/");
    return `${base}/${encoded}`;
  }
  // Strip the single leading slash; the route handler re-adds it.
  const stripped = absolutePath.startsWith("/") ? absolutePath.slice(1) : absolutePath;
  if (stripped === "") {
    return `${base}/`;
  }
  const encoded = stripped
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  return `${base}/${encoded}`;
}

interface FetchError extends Error {
  status?: number;
}

// The directory listing inherits React Query's default retry count for
// transient failures; 4xx responses opt out of it (see
// ``shouldRetryHostFilesystem``).
const MAX_LIST_RETRIES = 3;

/**
 * Build a human-readable error for a failed directory listing.
 *
 * A 404 means the path doesn't exist (or isn't a directory) — the common case
 * when a user types a bad path into the picker — so it gets a plain "doesn't
 * exist" message naming the path instead of a bare status code. Other failures
 * use the server's ``detail`` when present, else fall back to the status code.
 *
 * @param res Non-OK response from a listing request.
 * @param path The path that was being listed, for the 404 message.
 * @returns The message to attach to the thrown ``FetchError``.
 */
async function describeListError(res: Response, path: string): Promise<string> {
  if (res.status === 404) {
    const where = path === "" || path === "~" ? "That directory" : path;
    return `${where} doesn't exist on this host (or isn't a directory).`;
  }
  // Host offline / timed out (409/502/504) or another failure — surface the
  // server's detail so the user sees why, else fall back to the status code.
  let detail: string | null;
  try {
    const body = (await res.json()) as { detail?: string };
    detail = typeof body.detail === "string" && body.detail !== "" ? body.detail : null;
  } catch {
    detail = null;
  }
  return detail ?? `Couldn't list the directory (HTTP ${res.status}).`;
}

/**
 * React Query retry predicate for the directory listing.
 *
 * 4xx responses are deterministic (missing path, bad path, not the owner), so
 * retrying them just delays the error behind the stale placeholder listing the
 * picker keeps on screen while a query is pending. Skip retries for those and
 * let the error surface immediately; retry only transient failures (5xx,
 * network) up to the default cap, matching the untuned default's 3 retries.
 *
 * @param failureCount Number of failures so far (0 on the first failure).
 * @param error The thrown error; a ``FetchError`` carries the HTTP ``status``.
 * @returns Whether React Query should retry the request.
 */
export function shouldRetryHostFilesystem(failureCount: number, error: Error): boolean {
  const status = (error as FetchError).status;
  if (status !== undefined && status >= 400 && status < 500) {
    return false;
  }
  return failureCount < MAX_LIST_RETRIES;
}

/**
 * A directory's listing plus whether it was cut short by the page
 * cap. ``truncated`` lets the picker tell the user the view is
 * incomplete rather than silently hiding entries.
 */
export interface HostDirectoryListing {
  /** Entries fetched so far (all of them unless ``truncated``). */
  entries: HostFilesystemEntry[];
  /**
   * ``true`` when the server still had more entries after
   * ``MAX_PAGES`` pages, so this listing is incomplete.
   */
  truncated: boolean;
}

// Request the server's max page size: the endpoint defaults to 20
// entries, which would hide directories whose name sorts past the
// first 20. At 1000 a typical directory needs a single request.
const PAGE_SIZE = 1000;
// Safety cap against a pathologically large directory looping forever.
const MAX_PAGES = 50;

/**
 * Fetch every entry in a host directory, following the endpoint's
 * ``has_more`` / ``after`` pagination to completion.
 *
 * The host paginates by entry ``path`` (sorted by name), so each
 * subsequent page uses the previous page's last entry path as the
 * forward cursor. Stops when the server reports no more entries,
 * returns an empty page, or the page cap is hit.
 *
 * @param hostId Host identifier, e.g. ``"host_a1b2..."``.
 * @param path Absolute path to list, or empty string for home.
 * @returns The directory's entries plus a ``truncated`` flag set
 *   when the page cap was hit with more entries still pending.
 * @throws FetchError carrying the HTTP status on a non-OK response.
 */
async function fetchHostFilesystem(hostId: string, path: string): Promise<HostDirectoryListing> {
  const baseUrl = buildHostFilesystemUrl(hostId, path);
  const entries: HostFilesystemEntry[] = [];
  let after: string | null = null;
  let truncated = false;
  // Sequential by necessity: each page's cursor is the previous
  // page's last entry path, so the requests can't be parallelized.
  /* oxlint-disable no-await-in-loop */
  for (let page = 0; page < MAX_PAGES; page++) {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
    if (after !== null) {
      params.set("after", after);
    }
    const sep = baseUrl.includes("?") ? "&" : "?";
    const res = await authenticatedFetch(`${baseUrl}${sep}${params.toString()}`);
    if (!res.ok) {
      const err: FetchError = new Error(await describeListError(res, path));
      err.status = res.status;
      throw err;
    }
    const body = (await res.json()) as HostFilesystemResponse;
    entries.push(...body.data);
    // Empty-page guard is defensive: a bad cursor must not loop.
    if (!body.has_more || body.data.length === 0) {
      break;
    }
    after = body.data[body.data.length - 1].path;
    // Loop is about to exit on the cap but the server has more —
    // the listing is incomplete; let the UI say so.
    if (page === MAX_PAGES - 1) {
      truncated = true;
    }
  }
  /* oxlint-enable no-await-in-loop */
  return { entries, truncated };
}

/**
 * React Query hook: list the contents of a directory on a host.
 *
 * Lazy — only fires when both ``hostId`` and ``path`` are set.
 * Paginates to completion under the hood (the picker shows a whole
 * directory at once), returning a ``truncated`` flag when the page
 * cap cuts the listing short. Cached per (host, path) so navigating
 * up/down the tree doesn't re-fetch already-seen directories. Stale
 * time is short (5s) so the picker reflects new files reasonably
 * quickly without thrashing during normal navigation.
 *
 * @param hostId Host id, e.g. ``"host_a1b2..."``. ``null`` keeps
 *   the query disabled.
 * @param path Absolute path to list, or empty string for home.
 *   ``null`` keeps the query disabled.
 * @returns React Query result with ``data: HostDirectoryListing``.
 */
export function useHostFilesystem(hostId: string | null, path: string | null) {
  return useQuery({
    queryKey: ["host-filesystem", hostId, path],
    queryFn: () => fetchHostFilesystem(hostId as string, path as string),
    enabled: hostId !== null && path !== null,
    staleTime: 5_000,
    // Keep the current directory's rows on screen while the next one
    // loads, so navigating up/into a folder doesn't flicker through
    // an empty "Loading…" collapse.
    placeholderData: (prev) => prev,
    // Don't retry a 4xx (e.g. a typed path that doesn't exist): retries keep
    // the query pending, so the placeholder above would leave the previous
    // directory's rows on screen instead of surfacing the error. Transient
    // failures still retry.
    retry: shouldRetryHostFilesystem,
  });
}

/**
 * Probe whether a directory exists (and is listable) on a host.
 *
 * Requests a single-entry ``list_dir`` page — the filesystem route
 * returns 404 for a missing (or non-directory) path, which is the
 * only existence signal exposed over HTTP (``host.stat`` is a WS
 * frame reachable from the server alone).
 *
 * @param hostId Host identifier, e.g. ``"host_a1b2..."``.
 * @param path Absolute directory path to probe.
 * @returns ``null`` when the directory is listable; otherwise a
 *   user-facing message saying why it can't be used (missing path,
 *   offline host, network failure). Never throws.
 */
export async function checkHostDirectory(hostId: string, path: string): Promise<string | null> {
  const baseUrl = buildHostFilesystemUrl(hostId, path);
  const sep = baseUrl.includes("?") ? "&" : "?";
  let res: Response;
  try {
    res = await authenticatedFetch(`${baseUrl}${sep}limit=1`);
  } catch {
    return "Couldn't verify the working directory. Check your connection and try again.";
  }
  if (res.ok) return null;
  if (res.status === 404) {
    // The route 404s for missing paths AND for paths that exist but
    // aren't listable directories (e.g. a file) — say so.
    return `The working directory ${path} doesn't exist on this host (or isn't a directory).`;
  }
  // Host offline / timed out (502/504) or another server failure —
  // surface its detail so the user sees why the check failed.
  let detail: string | null;
  try {
    const body = (await res.json()) as { detail?: string };
    detail = typeof body.detail === "string" && body.detail !== "" ? body.detail : null;
  } catch {
    detail = null;
  }
  return detail ?? `Couldn't verify the working directory (HTTP ${res.status}).`;
}

/**
 * Whether a host path is 404-missing, as opposed to every other pre-flight
 * problem (exists-but-unlistable, host offline, network error).
 *
 * The fork dialog uses this to distinguish "the source's worktree directory
 * was deleted — recreatable at the same path/branch" from problems where
 * falling back to worktree creation would be wrong.
 */
export async function hostDirectoryMissing(hostId: string, path: string): Promise<boolean> {
  const baseUrl = buildHostFilesystemUrl(hostId, path);
  const sep = baseUrl.includes("?") ? "&" : "?";
  try {
    const res = await authenticatedFetch(`${baseUrl}${sep}limit=1`);
    return res.status === 404;
  } catch {
    return false;
  }
}

/** Shape returned by ``POST /v1/hosts/{id}/directories``. */
interface CreateHostDirectoryResponse {
  object: string;
  /** Absolute path of the created directory, e.g. ``"/Users/me/new"``. */
  path: string;
}

/**
 * Create a directory on a host via ``POST /v1/hosts/{id}/directories``.
 *
 * The server forwards a ``host.create_dir`` frame to the host, which
 * runs ``os.makedirs`` (parents included) and returns the created
 * absolute path. A non-OK response carries the host's error message
 * (e.g. "directory already exists" as a 409) so the picker can show it
 * inline.
 *
 * @param hostId Host identifier, e.g. ``"host_a1b2..."``.
 * @param path Absolute (or ``~``-prefixed) directory path to create.
 * @returns The created directory's absolute path.
 * @throws FetchError carrying the HTTP status and the server's detail
 *   message on a non-OK response.
 */
export async function createHostDirectory(hostId: string, path: string): Promise<string> {
  const res = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(hostId)}/directories`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) {
    // Surface the server's detail (e.g. "directory already exists") so
    // the user sees why creation failed rather than a bare status code.
    let detail: string | null;
    try {
      const body = (await res.json()) as { detail?: string };
      detail = typeof body.detail === "string" ? body.detail : null;
    } catch {
      detail = null;
    }
    const err: FetchError = new Error(detail ?? `create directory failed: HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  const body = (await res.json()) as CreateHostDirectoryResponse;
  return body.path;
}

/**
 * React Query mutation: create a directory on a host, then refresh any
 * cached listings for that host so the new folder appears.
 *
 * Invalidates every ``["host-filesystem", hostId, *]`` query rather
 * than just the parent's, because the picker keys listings by its raw
 * path state ("" for home, absolute otherwise) and the caller may not
 * know which key the new directory's parent maps to.
 *
 * @returns A React Query mutation; call ``mutateAsync({ hostId, path })``.
 */
export function useCreateHostDirectory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ hostId, path }: { hostId: string; path: string }) =>
      createHostDirectory(hostId, path),
    onSuccess: (_createdPath, { hostId }) => {
      void queryClient.invalidateQueries({ queryKey: ["host-filesystem", hostId] });
    },
  });
}
