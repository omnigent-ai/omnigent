import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

export interface SessionLink {
  source_id: string;
  target_id: string;
  source_title: string | null;
  target_title: string | null;
  last_requested_at: number;
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
    `/v1/sessions/${encodeURIComponent(sessionId)}/communication-activity?${query}`,
    { signal },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export interface OrchestrationSetting {
  enabled: boolean;
  version: string | null;
  updated_at: number | null;
}

export async function getOrchestrationSetting(signal?: AbortSignal): Promise<OrchestrationSetting> {
  const response = await authenticatedFetch("/v1/session-orchestration/settings", { signal });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export async function setOrchestrationSetting(
  setting: OrchestrationSetting,
): Promise<OrchestrationSetting> {
  const response = await authenticatedFetch("/v1/session-orchestration/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: !setting.enabled, expected_version: setting.version }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}
