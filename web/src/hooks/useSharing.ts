import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { SharingMode } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";

/** Server-wide sharing settings from ``GET /v1/sharing`` (admin). */
export interface SharingState {
  object: "sharing";
  sharing_mode: SharingMode;
  /** False when the deployment injects its own mode resolver (not file-backed). */
  editable: boolean;
  /** Available tiers, most-permissive first. */
  options: SharingMode[];
  /** Whether public (anyone-with-the-link) access may be granted. */
  public_sharing_enabled: boolean;
  /** False when the deployment manages public access itself (not file-backed). */
  public_sharing_editable: boolean;
  public_sharing_max_level?: "read" | "edit";
  public_sharing_max_level_editable?: boolean;
  public_sharing_max_level_options?: ("read" | "edit")[];
  /** Which new sessions start with a public read grant. */
  default_public_sessions: DefaultPublicSessions;
  /** False when the deployment manages this default itself (not file-backed). */
  default_public_sessions_editable: boolean;
  default_public_sessions_options: DefaultPublicSessions[];
}

/** ``off`` = all private, ``sandbox`` = cloud sandbox sessions public, ``all`` = every session. */
export type DefaultPublicSessions = "off" | "sandbox" | "all";

/** Partial update for ``PUT /v1/sharing``: set any subset. */
export interface SharingUpdate {
  sharing_mode?: SharingMode;
  public_sharing?: boolean;
  public_sharing_max_level?: "read" | "edit";
  default_public_sessions?: DefaultPublicSessions;
}

const QUERY_KEY = ["sharing"];
const PUBLIC_MAX_LEVEL_QUERY_KEY = ["sharing", "public-max-level"];

export function usePublicSharingMaxLevel(enabled: boolean) {
  return useQuery({
    queryKey: PUBLIC_MAX_LEVEL_QUERY_KEY,
    enabled,
    staleTime: 0,
    queryFn: async (): Promise<"read" | "edit"> => {
      try {
        const response = await authenticatedFetch("/v1/info");
        if (!response.ok) return "read";
        const info = await response.json();
        return info.public_sharing_max_level === "edit" ? "edit" : "read";
      } catch {
        return "read";
      }
    },
  });
}

async function fetchSharing(): Promise<SharingState> {
  const res = await authenticatedFetch("/v1/sharing");
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as SharingState;
}

/** Fetch the current server-wide sharing settings (admin only). */
export function useSharing({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({ queryKey: QUERY_KEY, queryFn: fetchSharing, staleTime: 5_000, enabled });
}

/** PUT /v1/sharing — update the mode and/or public-access setting (admin). */
export function useSetSharing() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (update: SharingUpdate) => {
      const res = await authenticatedFetch("/v1/sharing", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(update),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
      }
      return (await res.json()) as SharingState;
    },
    onSuccess: (data) => {
      // Reflect the new value immediately, then revalidate.
      queryClient.setQueryData(QUERY_KEY, data);
      queryClient.setQueryData(PUBLIC_MAX_LEVEL_QUERY_KEY, data.public_sharing_max_level ?? "read");
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });
}
