import { authenticatedFetch } from "@/lib/identity";

export type ChatImportSource = "claude" | "codex";

export interface RecentNativeChat {
  session_id: string;
  title: string | null;
  workspace: string | null;
  item_count: number;
}

async function responseError(response: Response): Promise<Error> {
  try {
    const body = (await response.json()) as { detail?: string; error?: { message?: string } };
    return new Error(body.detail ?? body.error?.message ?? `Request failed (${response.status})`);
  } catch {
    return new Error(`Request failed (${response.status})`);
  }
}

export async function listRecentNativeChats(
  hostId: string,
  source: ChatImportSource,
  limit = 10,
): Promise<RecentNativeChat[]> {
  const params = new URLSearchParams({ source, limit: String(limit) });
  const response = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/chat-imports?${params.toString()}`,
  );
  if (!response.ok) throw await responseError(response);
  const body = (await response.json()) as { sessions: RecentNativeChat[] };
  return body.sessions;
}

export async function importNativeChat(
  hostId: string,
  source: ChatImportSource,
  sessionId: string,
): Promise<string> {
  const loaded = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/chat-imports/load`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, session_id: sessionId }),
    },
  );
  if (!loaded.ok) throw await responseError(loaded);

  const payload = (await loaded.json()) as Record<string, unknown>;
  const imported = await authenticatedFetch("/v1/imports", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, host_id: hostId, force: false }),
  });
  if (!imported.ok) throw await responseError(imported);
  const result = (await imported.json()) as { session_id: string };
  return result.session_id;
}
