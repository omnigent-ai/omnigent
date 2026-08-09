// Typed client for the `/v1/imports` session-import endpoints
// (`omnigent/server/routes/imports.py`) — the web-side equivalent of the
// `omnigent import` CLI command.
//
// The `local` routes read transcripts from the SERVER's own disk, which is the
// user's machine only on a single-user local runtime; a deployed server refuses
// them with 403. Importing another machine's chats would need the same listing
// relayed through that host, so the source is a request parameter rather than
// baked into the path.
//
// Naming: TS surface is camelCase; the wire is snake_case. The helpers below
// convert at the boundary so callers never see raw wire fields.

import { authenticatedFetch } from "./identity";
import { ApiError } from "./sessionsApi";

/**
 * Local harnesses whose transcripts Omnigent can normalize, ordered the way the
 * picker lists them (the two fully supported harnesses first). Mirrors
 * `ImportSource` in `omnigent/session_import/models.py` and the `--harness`
 * choices on `omnigent import`.
 */
export const IMPORT_SOURCES = [
  "claude",
  "codex",
  "opencode",
  "pi",
  "kiro",
  "kimi",
  "qwen",
] as const;

export type ImportSource = (typeof IMPORT_SOURCES)[number];

/** One local transcript offered to the import picker. */
export interface LocalImportSession {
  sessionId: string;
  /** First user message, or `null` when the transcript opens with tool work. */
  title: string | null;
  /** Directory the chat ran in, as recorded by the harness. */
  workspace: string | null;
  itemCount: number;
  /** POSIX seconds; `null` when the harness reported no usable time. */
  modifiedAt: number | null;
}

/** Result of an import — the new Omnigent session and how much it carried. */
export interface ImportedSession {
  sessionId: string;
  itemCount: number;
}

interface LocalImportSessionWire {
  session_id: string;
  title: string | null;
  workspace: string | null;
  item_count: number;
  modified_at: number | null;
}

async function apiErrorFromResponse(res: Response): Promise<ApiError> {
  let message = `${res.status} ${res.statusText}`;
  let code: string | null = null;
  try {
    const body = (await res.json()) as { error?: { code?: string; message?: string } };
    if (body.error?.message) message = body.error.message;
    if (body.error?.code) code = body.error.code;
  } catch {
    // Non-JSON / empty body — keep the status-line fallback.
  }
  return new ApiError(message, res.status, code);
}

/**
 * List the given harness's most recent chats on the server's machine, newest
 * first. Each entry is parsed server-side, so `itemCount` is the real number of
 * items the import would carry.
 */
export async function listLocalImportSessions(
  source: ImportSource,
  limit = 20,
): Promise<LocalImportSession[]> {
  const res = await authenticatedFetch(
    `/v1/imports/local/sessions?source=${encodeURIComponent(source)}&limit=${limit}`,
  );
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json()) as { sessions: LocalImportSessionWire[] };
  return body.sessions.map((session) => ({
    sessionId: session.session_id,
    title: session.title,
    workspace: session.workspace,
    itemCount: session.item_count,
    modifiedAt: session.modified_at,
  }));
}

/**
 * Import one local chat into Omnigent as an ordinary session.
 *
 * A source chat can only be imported once: a repeat rejects with a 409
 * {@link ApiError} naming the existing session unless `force` replaces it.
 */
export async function importLocalSession({
  source,
  externalSessionId,
  force = false,
}: {
  source: ImportSource;
  externalSessionId: string;
  force?: boolean;
}): Promise<ImportedSession> {
  const res = await authenticatedFetch("/v1/imports/local", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source,
      external_session_id: externalSessionId,
      force,
    }),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json()) as { session_id: string; item_count: number };
  return { sessionId: body.session_id, itemCount: body.item_count };
}
