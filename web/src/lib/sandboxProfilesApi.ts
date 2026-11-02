import { authenticatedFetch } from "./identity";

export interface SandboxProfile {
  id: string;
  name: string;
  description: string;
  provider: string;
  revision: string;
}

export interface SandboxProfilesResponse {
  profiles: SandboxProfile[];
  managed_providers: string[];
  next_cursor: string | null;
}

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

export const SANDBOX_PROFILE_RESULT_LIMIT = 20;

export async function fetchSandboxProfiles(
  provider?: string,
  options: { search?: string; profileId?: string; signal?: AbortSignal } = {},
): Promise<SandboxProfilesResponse> {
  const params = new URLSearchParams({ limit: String(SANDBOX_PROFILE_RESULT_LIMIT) });
  if (provider) params.set("provider", provider);
  if (options.search) params.set("search", options.search);
  if (options.profileId) params.set("profile_id", options.profileId);
  return (await profileRequest(`?${params}`, { signal: options.signal })).json();
}
