import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { ApiError } from "@/lib/sessionsApi";

export interface SkillContent {
  name: string;
  description: string;
  content: string;
  truncated: boolean;
}

export function useSkillContent(
  hostId: string,
  harness: string,
  name: string,
  { enabled = true, sourceId }: { enabled?: boolean; sourceId?: string } = {},
) {
  return useQuery({
    queryKey: ["skill-content", hostId, harness, name, sourceId],
    queryFn: async ({ signal }): Promise<SkillContent> => {
      const response = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId)}/harnesses/${encodeURIComponent(harness)}/skills/${encodeURIComponent(name)}${sourceId ? `?source_id=${encodeURIComponent(sourceId)}` : ""}`,
        { signal, cache: "no-store" },
      );
      if (!response.ok)
        throw new ApiError(`${response.status} ${response.statusText}`, response.status, null);
      return (await response.json()) as SkillContent;
    },
    enabled,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
  });
}
