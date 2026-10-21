import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

export interface SessionLink {
  source_id: string;
  target_id: string;
  source_title: string | null;
  target_title: string | null;
  approved_by: string;
  approved_at: number;
  active: boolean;
  can_disconnect: boolean;
}

export interface SessionLinkPage {
  data: SessionLink[];
  next_cursor: string | null;
}

export async function listSessionLinks(
  sessionId: string,
  incoming = false,
  after?: string,
  signal?: AbortSignal,
): Promise<SessionLinkPage> {
  const query = new URLSearchParams({ incoming: String(incoming), limit: "50" });
  if (after) query.set("after", after);
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/links?${query}`,
    { signal },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export async function connectSessions(sourceId: string, targetId: string): Promise<void> {
  const response = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sourceId)}/links`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_id: targetId }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
}

export async function disconnectSessions(sourceId: string, targetId: string): Promise<void> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sourceId)}/links/${encodeURIComponent(targetId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
}
