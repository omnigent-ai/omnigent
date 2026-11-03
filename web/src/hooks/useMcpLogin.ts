import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

/** A harness `mcp login` running on the host (wire shape of the login routes). */
export interface McpLogin {
  login_id: string;
  state: "pending" | "succeeded" | "failed" | "expired" | "cancelled";
  /** Set only while the sign-in is pending. */
  authorization_url: string | null;
  error: string | null;
}

export interface McpLoginTarget {
  hostId: string;
  harness: string;
  server: string;
  plugin?: string;
  sourceId?: string;
}

function loginPath(hostId: string, loginId?: string): string {
  const base = `/v1/hosts/${encodeURIComponent(hostId)}/mcp-servers/login`;
  return loginId === undefined ? base : `${base}/${encodeURIComponent(loginId)}`;
}

async function readLogin(response: Response): Promise<McpLogin> {
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as McpLogin;
}

export async function startMcpLogin(target: McpLoginTarget): Promise<McpLogin> {
  const response = await authenticatedFetch(loginPath(target.hostId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      harness: target.harness,
      server: target.server,
      plugin: target.plugin,
      source_id: target.sourceId,
    }),
  });
  return readLogin(response);
}

export async function fetchMcpLogin(
  hostId: string,
  loginId: string,
  signal?: AbortSignal,
): Promise<McpLogin> {
  const response = await authenticatedFetch(loginPath(hostId, loginId), { signal });
  return readLogin(response);
}

export async function submitMcpLoginCallback(
  hostId: string,
  loginId: string,
  callbackUrl: string,
): Promise<McpLogin> {
  const response = await authenticatedFetch(`${loginPath(hostId, loginId)}/callback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ callback_url: callbackUrl }),
  });
  return readLogin(response);
}

export async function cancelMcpLogin(hostId: string, loginId: string): Promise<void> {
  const response = await authenticatedFetch(loginPath(hostId, loginId), { method: "DELETE" });
  if (!response.ok) throw await apiErrorFromResponse(response);
}

/** Sign-in pages may be https anywhere, or http only on the user's own machine. */
export function isSafeSignInUrl(value: string): boolean {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  if (url.username || url.password) return false;
  if (url.protocol === "https:") return true;
  return url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
}
