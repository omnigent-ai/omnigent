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
  // Strip the single leading slash; the route handler re-adds it.
  const stripped = absolutePath.startsWith("/") ? absolutePath.slice(1) : absolutePath;
  if (stripped === "") {
    // The user navigated to "/" exactly. Keep a trailing slash so
    // the route still matches /filesystem/{path:path}.
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
export async function fetchHostFilesystem(
  hostId: string,
  path: string,
): Promise<HostDirectoryListing> {
  const baseUrl = buildHostFilesystemUrl(hostId, path);
  const entries: HostFilesystemEntry[] = [];
  let after: string | null = null;
  let truncated = false;
  // Sequential by necessity: each page's cursor is the previous
  // page's last entry path, so the requests can't be parallelized.
  /* oxlint-disable eslint(no-await-in-loop) */
  for (let page = 0; page < MAX_PAGES; page++) {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
    if (after !== null) {
      params.set("after", after);
    }
    const sep = baseUrl.includes("?") ? "&" : "?";
    const res = await authenticatedFetch(`${baseUrl}${sep}${params.toString()}`);
    if (!res.ok) {
      const err: FetchError = new Error(`host filesystem fetch failed: HTTP ${res.status}`);
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
  /* oxlint-enable eslint(no-await-in-loop) */
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
  });
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
    let detail: string | null = null;
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
