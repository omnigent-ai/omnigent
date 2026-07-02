/**
 * Minimal HTTP client for the Omnigent /v1 REST surface (local server only).
 *
 * Pure payload/accumulation functions are co-located here and tested in
 * isolation; the actual fetch calls go through the injected `fetchImpl` so
 * tests can supply a stub.
 *
 * This is the LOCAL-ONLY slice: a local server needs no credentials, so there is
 * no bearer header and no remote/Databricks handling — every request is plain.
 *
 * API surface used by this slice:
 *   GET /v1/sessions   — list sessions (OpenAI-style cursor pagination)
 */

export type FetchFn = typeof fetch;

export interface ClientOptions {
  /** Server base URL (e.g. "http://127.0.0.1:6767"). */
  baseUrl: string;
  fetchImpl?: FetchFn;
}

export interface ApiResponse<T> {
  ok: boolean;
  status: number;
  data?: T;
  error?: string;
}

/**
 * Low-level fetch — the central request point for all /v1 calls. `ok` mirrors
 * `Response.ok` (2xx); a network/throw failure yields `{ ok:false, status:0 }`.
 */
export async function apiFetch<T>(
  opts: ClientOptions,
  path: string,
  init: RequestInit = {},
): Promise<ApiResponse<T>> {
  const { baseUrl, fetchImpl = fetch } = opts;
  const url = `${baseUrl.replace(/\/$/, "")}${path}`;
  const headers = {
    "Content-Type": "application/json",
    Accept: "application/json",
    ...((init.headers as Record<string, string> | undefined) ?? {}),
  };
  let res: Response;
  try {
    res = await fetchImpl(url, { ...init, headers });
  } catch (err) {
    return { ok: false, status: 0, error: err instanceof Error ? err.message : String(err) };
  }

  if (res.ok) {
    try {
      const data = (await res.json()) as T;
      return { ok: true, status: res.status, data };
    } catch {
      return { ok: true, status: res.status };
    }
  }
  return { ok: false, status: res.status, error: `HTTP ${res.status}` };
}

// ── Sessions ──────────────────────────────────────────────────────────────────

/**
 * A session as returned by `GET /v1/sessions` (pinned from a live capture).
 * Timestamps are unix SECONDS; `title`/`workspace`/`git_branch` are OPTIONAL and
 * absent on some sessions; `archived` is a BOOLEAN (not a status value).
 */
export interface Session {
  id: string;
  agent_id?: string;
  agent_name?: string;
  status?: string; // open string enum: "running" | "idle" | ...
  created_at?: number; // unix SECONDS
  updated_at?: number; // unix SECONDS
  title?: string;
  workspace?: string; // abs path
  git_branch?: string;
  archived?: boolean;
  [key: string]: unknown;
}

/** One page of the OpenAI-style cursor-paginated `GET /v1/sessions` response. */
export interface SessionsPage {
  object: "list";
  data: Session[];
  first_id?: string | null;
  last_id?: string | null;
  has_more?: boolean;
}

/** Query options for a single `GET /v1/sessions` page. */
export interface ListSessionsOptions {
  limit?: number;
  after?: string;
}

/**
 * Pure: concatenate page `data` in order, stopping once `cap` sessions are reached.
 * `truncated` is true when the final consumed page still reports `has_more` AND the
 * accumulated total reached the cap (i.e. there is more on the server we did not fetch).
 */
export function accumulateSessions(
  pages: SessionsPage[],
  cap: number,
): { sessions: Session[]; truncated: boolean } {
  const sessions: Session[] = [];
  let lastHasMore = false;
  for (const page of pages) {
    lastHasMore = page.has_more === true;
    for (const s of page.data) {
      if (sessions.length >= cap) break;
      sessions.push(s);
    }
    if (sessions.length >= cap) break;
  }
  const truncated = lastHasMore && sessions.length >= cap;
  return { sessions, truncated };
}

/** Fetch a single page of sessions. `limit`/`after` are omitted when undefined. */
export async function listSessionsPage(
  opts: ClientOptions,
  page: ListSessionsOptions = {},
): Promise<ApiResponse<SessionsPage>> {
  const params = new URLSearchParams();
  if (page.limit !== undefined) params.set("limit", String(page.limit));
  if (page.after !== undefined) params.set("after", page.after);
  const query = params.toString();
  return apiFetch<SessionsPage>(opts, `/v1/sessions${query ? `?${query}` : ""}`);
}

/**
 * List sessions, following the `after = last_id` cursor while `has_more` is true
 * and the accumulated total is below `cap`. Non-ok responses propagate as-is
 * (without throwing) so callers can map them to the error state.
 */
export async function listSessions(
  opts: ClientOptions,
  cap = 200,
): Promise<ApiResponse<{ sessions: Session[]; truncated: boolean }>> {
  const pages: SessionsPage[] = [];
  let after: string | undefined;
  let lastStatus = 200;
  while (true) {
    const res = await listSessionsPage(opts, { after });
    if (!res.ok || !res.data) {
      return { ok: res.ok, status: res.status, error: res.error };
    }
    lastStatus = res.status;
    pages.push(res.data);
    const total = pages.reduce((n, p) => n + p.data.length, 0);
    const next = res.data.last_id ?? undefined;
    // Stop on the normal signals (no more, no cursor, cap reached) plus two
    // defensive guards against a misbehaving server that would otherwise spin
    // this loop forever: an empty page, or a cursor that did not advance.
    if (
      res.data.has_more !== true ||
      !next ||
      total >= cap ||
      res.data.data.length === 0 ||
      next === after
    ) {
      break;
    }
    after = next;
  }
  const { sessions, truncated } = accumulateSessions(pages, cap);
  return { ok: true, status: lastStatus, data: { sessions, truncated } };
}
