import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

export interface SessionMessage {
  source_id: string;
  target_id: string;
  source_title: string | null;
  target_title: string | null;
  last_sent_at: number;
}
export interface CommunicationSetting {
  enabled: boolean;
  version: string | null;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(path, options);
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export function listSessionMessages(sessionId: string, signal?: AbortSignal) {
  return request<{ data: SessionMessage[] }>(
    `/v1/sessions/${encodeURIComponent(sessionId)}/communication-activity`,
    { signal },
  );
}
export function getOrchestrationSetting(signal?: AbortSignal) {
  return request<CommunicationSetting>("/v1/session-communication/settings", { signal });
}
export function setOrchestrationSetting(setting: CommunicationSetting) {
  return request<CommunicationSetting>("/v1/session-communication/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: !setting.enabled, expected_version: setting.version }),
  });
}
