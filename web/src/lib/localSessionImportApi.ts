// Client for the two `/v1/imports/local-sessions` endpoints, which let a user
// browse and import Claude Code / Codex chats that already exist on a
// connected host. The wire shape is used verbatim (snake_case) — there is no
// camelCase mapping layer, since Task 7's dialog is written directly against
// these field names.

import { authenticatedFetch } from "@/lib/identity";

/** A host-local chat source this app knows how to import from. */
export type ImportSourceId = "claude" | "codex";

/** One preview turn of a host-local session, shown in the import picker. */
export interface LocalSessionPreview {
  role: string;
  text: string;
}

/**
 * Summary of one host-local chat, as returned by
 * `GET /v1/imports/local-sessions`. `workspace` and `title` are nullable —
 * the host couldn't always determine them from the transcript.
 */
export interface LocalSessionSummary {
  source: ImportSourceId;
  external_session_id: string;
  workspace: string | null;
  title: string | null;
  item_count: number;
  preview: LocalSessionPreview[];
}

/** Result of `POST /v1/imports/local-sessions`: the newly created session. */
export interface ImportedSessionResult {
  session_id: string;
  status: string;
  item_count: number;
}

interface LocalSessionsListResponse {
  object: string;
  data: LocalSessionSummary[];
}

/**
 * Extract a human-readable error message from a failed response, matching
 * `scheduledTasksApi.ts`'s `errorFromResponse`. Both error-body shapes the
 * backend can return must be handled: FastAPI's bare `{"detail": "..."}` and
 * the app's `{"error": {"message": "..."}}`. Falls back to `HTTP ${status}`
 * when neither shape is present (or the body isn't JSON).
 */
async function errorMessageFromResponse(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: string; error?: { message?: string } };
    if (body.detail) return body.detail;
    if (body.error?.message) return body.error.message;
  } catch {
    // Non-JSON / empty body — fall through to the status fallback.
  }
  return `HTTP ${res.status}`;
}

/**
 * Fetch a host's recent local chats for one source (Claude Code or Codex).
 *
 * Rows the host reports as malformed are silently skipped server-side, so
 * the returned array may be shorter than `limit`.
 *
 * @param hostId Host identifier, e.g. ``"host_a1b2..."``.
 * @param source Which local chat tool to browse.
 * @param limit Max rows to return, 1-20. Server defaults to 10 when omitted.
 * @returns The host's local sessions for that source, most recent first.
 * @throws Error with the server's message (`detail` / `error.message`) on a
 *   non-OK response — including 400 (invalid source / host failure), 403
 *   (not your host), 404 (host or session not found), 409 (host offline),
 *   and 503 (server has no host support).
 */
export async function fetchHostLocalSessions(
  hostId: string,
  source: ImportSourceId,
  limit?: number,
): Promise<LocalSessionSummary[]> {
  const params = new URLSearchParams({ host_id: hostId, source });
  if (limit !== undefined) params.set("limit", String(limit));
  const res = await authenticatedFetch(`/v1/imports/local-sessions?${params.toString()}`);
  if (!res.ok) {
    throw new Error(await errorMessageFromResponse(res));
  }
  const body = (await res.json()) as LocalSessionsListResponse;
  return body.data;
}

/**
 * Import one host-local chat into Omnigent as a new session.
 *
 * @param hostId Host identifier the session lives on.
 * @param source Which local chat tool the session came from.
 * @param externalSessionId The source tool's own session id, from
 *   {@link LocalSessionSummary.external_session_id}.
 * @returns The newly created Omnigent session's id and imported item count.
 * @throws Error with the server's message on a non-OK response — including
 *   400 (invalid source / host failure), 403 (not your host), 404 (host or
 *   session not found), 409 (host offline, or already imported), and 503
 *   (server has no host support).
 */
export async function importHostLocalSession(
  hostId: string,
  source: ImportSourceId,
  externalSessionId: string,
): Promise<ImportedSessionResult> {
  const res = await authenticatedFetch("/v1/imports/local-sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      host_id: hostId,
      source,
      external_session_id: externalSessionId,
    }),
  });
  if (!res.ok) {
    throw new Error(await errorMessageFromResponse(res));
  }
  return (await res.json()) as ImportedSessionResult;
}
