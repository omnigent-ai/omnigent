import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type { SkillSummary, SkillsStatus } from "@/lib/types";

/** Both composers receive complete catalogs directly from host-backed requests. */
async function fetchSkills(url: string, signal: AbortSignal): Promise<SkillSummary[]> {
  const response = await authenticatedFetch(url, { signal });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  const body = (await response.json()) as { skills?: SkillSummary[] };
  if (!Array.isArray(body.skills)) throw new Error("Invalid host skills response");
  return body.skills;
}

interface SkillsOptions {
  hostId?: string | null;
  harness?: string | null;
  path?: string | null;
  sessionId?: string | null;
  /** Invalidate session catalogs when the agent or sub-agent changes. */
  agentId?: string | null;
  subAgentName?: string | null;
  enabled?: boolean;
  starting?: boolean;
}

/** Discover composer skills, with optional session authorization and agent scope. */
export function useSkills({
  hostId,
  harness,
  path,
  sessionId,
  agentId,
  subAgentName,
  enabled = true,
  starting = false,
}: SkillsOptions) {
  const available = enabled && !!hostId && !!path && (!!sessionId || !!harness);
  const query = useQuery({
    queryKey: ["skills", sessionId, hostId, harness, path, agentId, subAgentName],
    queryFn: ({ signal }) => {
      // The server derives session targets from the authorized session row.
      const params = sessionId
        ? new URLSearchParams({ session_id: sessionId })
        : new URLSearchParams({ host_id: hostId!, harness: harness!, path: path! });
      return fetchSkills(`/v1/skills?${params}`, signal);
    },
    enabled: available,
    staleTime: 30_000,
    refetchInterval: available && sessionId ? 60_000 : false,
    retry: false,
  });
  const skillsStatus: SkillsStatus = !available
    ? starting
      ? "loading"
      : "unavailable"
    : query.isPending || (query.isFetching && !query.data)
      ? "loading"
      : query.isError
        ? "error"
        : "ready";
  return { skills: available ? (query.data ?? []) : [], skillsStatus, refetch: query.refetch };
}
