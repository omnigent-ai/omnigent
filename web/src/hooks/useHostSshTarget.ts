import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { normalizeSshTarget } from "@/lib/editorLinks";

/** Live host metadata, shared by folder/file controls and refreshed on host events. */
export function useHostSshTarget(hostId: string | null): string | null {
  const { data } = useQuery({
    queryKey: ["hosts", hostId, "ssh-target"],
    enabled: hostId !== null,
    queryFn: async ({ signal }) => {
      try {
        const response = await authenticatedFetch(
          `/v1/hosts/${encodeURIComponent(hostId!)}?require_live=true`,
          { signal: AbortSignal.any([signal, AbortSignal.timeout(3000)]) },
        );
        if (!response.ok) return null;
        return normalizeSshTarget((await response.json()).ssh_target);
      } catch {
        return null;
      }
    },
    staleTime: 30_000,
    refetchInterval: hostId === null ? false : 30_000,
    retry: false,
  });
  return data ?? null;
}
