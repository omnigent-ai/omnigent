import { authenticatedFetch } from "./identity";

export interface SandboxProfile {
  id: string;
  name: string;
  description: string;
  provider: string;
  config: Record<string, unknown>;
  revision: string;
  enabled: boolean;
}

export interface SandboxProfilesResponse {
  profiles: SandboxProfile[];
  providers: string[];
  can_manage: boolean;
  next_cursor: string | null;
}

export type SandboxProfileInput = Omit<SandboxProfile, "id" | "revision">;
export const SANDBOX_PROFILES_QUERY_KEY = ["sandbox-profiles"];

async function profileRequest(path = "", init?: RequestInit): Promise<Response> {
  const response = await authenticatedFetch(`/v1/sandbox-profiles${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(
      body?.error?.message ??
        (typeof body?.detail === "string" ? body.detail : null) ??
        `Sandbox profile request failed (${response.status}).`,
    );
  }
  return response;
}

export async function fetchSandboxProfiles(): Promise<SandboxProfilesResponse> {
  const result: SandboxProfilesResponse = await (await profileRequest("?limit=100")).json();
  while (result.next_cursor) {
    // eslint-disable-next-line no-await-in-loop -- Each page needs the preceding cursor.
    const page: SandboxProfilesResponse = await profileRequest(
      `?limit=100&cursor=${encodeURIComponent(result.next_cursor)}`,
    ).then((response) => response.json());
    result.profiles.push(...page.profiles);
    result.next_cursor = page.next_cursor;
  }
  return result;
}

export async function saveSandboxProfile(
  id: string,
  profile: SandboxProfileInput,
): Promise<SandboxProfile> {
  return (
    await profileRequest(`/${encodeURIComponent(id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profile),
    })
  ).json();
}

export async function disableSandboxProfile(id: string): Promise<void> {
  await profileRequest(`/${encodeURIComponent(id)}`, { method: "DELETE" });
}
