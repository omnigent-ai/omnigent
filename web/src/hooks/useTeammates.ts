import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/**
 * UI-facing harness-internal teammate record.
 *
 * Mirrors the ``TeammateSummary`` schema returned by
 * ``GET /v1/sessions/{id}/teammates``. A teammate (today: Claude Code
 * agent teams) runs inside the harness process — it has no Omnigent
 * session, so it can never appear in ``useChildSessions``; the server
 * folds it from the session's ``teammate_message`` items instead.
 */
export interface TeammateInfo {
  /** The teammate's name, e.g. ``"buddy"``. */
  teammate_id: string;
  /** ``"idle"`` after its latest idle notification, else ``"active"``. */
  status: "active" | "idle";
  /** Teammate accent color name, e.g. ``"blue"``; ``null`` when unknown. */
  color: string | null;
  /** ``summary`` attribute of the newest prose delivery, or ``null``. */
  last_summary: string | null;
  /** Single-line preview of the newest prose delivery, or ``null``. */
  last_message_preview: string | null;
}

interface TeammateWire {
  teammate_id: string;
  status?: string;
  color?: string | null;
  last_summary?: string | null;
  last_message_preview?: string | null;
}

interface TeammatesResponse {
  object: "list";
  data: TeammateWire[];
}

/** TanStack Query key for a session's teammates. */
export function teammatesQueryKey(conversationId: string): readonly unknown[] {
  return ["conversation", conversationId, "teammates"];
}

/**
 * Fetch the teammates for a session. Exported for unit testing of the
 * HTTP-shape contract; production code should call ``useTeammates``.
 */
export async function fetchTeammates(sessionId: string): Promise<TeammateInfo[]> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/teammates`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const json = (await res.json()) as TeammatesResponse;
  return json.data.map((row) => ({
    teammate_id: row.teammate_id,
    status: row.status === "idle" ? "idle" : "active",
    color: row.color ?? null,
    last_summary: row.last_summary ?? null,
    last_message_preview: row.last_message_preview ?? null,
  }));
}

interface UseTeammatesResult {
  teammates: TeammateInfo[];
}

/**
 * Live harness-internal teammate list for a conversation.
 *
 * Deliveries land at turn boundaries (there is no push path for
 * teammates yet), so a short poll while the rail is mounted keeps the
 * roster current enough without a stream subscription.
 *
 * @param conversationId - Parent session id, or ``null`` to disable.
 */
export function useTeammates(conversationId: string | null): UseTeammatesResult {
  const { data } = useQuery({
    queryKey:
      conversationId === null
        ? ["conversation", null, "teammates"]
        : teammatesQueryKey(conversationId),
    queryFn: () => fetchTeammates(conversationId as string),
    enabled: conversationId !== null,
    retry: false,
    refetchInterval: 10_000,
  });
  return { teammates: data ?? [] };
}
