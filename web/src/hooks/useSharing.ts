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
}

/** Partial update for ``PUT /v1/sharing`` — set either or both. */
export interface SharingUpdate {
  sharing_mode?: SharingMode;
  public_sharing?: boolean;
}

const QUERY_KEY = ["sharing"];

async function fetchSharing(): Promise<SharingState> {
  const res = await authenticatedFetch("/v1/sharing");
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as SharingState;
}

/** Fetch the current server-wide sharing settings (admin only). */
export function useSharing() {
  return useQuery({ queryKey: QUERY_KEY, queryFn: fetchSharing, staleTime: 5_000 });
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
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });
}
