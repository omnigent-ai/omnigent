import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { normalizeSshTarget } from "@/lib/editorLinks";

/** Live host metadata, shared by folder/file controls and refreshed on host events. */
export function useHostSshTarget(hostId: string | null): string | null {
  const { data } = useQuery({
    queryKey: ["host-ssh-target", hostId],
    enabled: hostId !== null,
    queryFn: async ({ signal }) => {
      const controller = new AbortController();
      const abort = () => controller.abort();
      if (signal.aborted) abort();
      else signal.addEventListener("abort", abort, { once: true });
      const timeout = window.setTimeout(abort, 3000);
      try {
        const response = await authenticatedFetch(
          `/v1/hosts/${encodeURIComponent(hostId!)}?require_live=true`,
          { signal: controller.signal },
        );
        if (!response.ok) return null;
        return normalizeSshTarget((await response.json()).ssh_target);
      } catch {
        return null;
      } finally {
        window.clearTimeout(timeout);
        signal.removeEventListener("abort", abort);
      }
    },
    staleTime: 30_000,
    refetchInterval: hostId === null ? false : 30_000,
    retry: false,
  });
  return data ?? null;
}
