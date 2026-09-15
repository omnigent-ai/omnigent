import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type { Session, SkillSummary, SkillsStatus } from "@/lib/types";

/** Both composers receive complete catalogs directly from host-backed requests. */
export async function fetchSkills(url: string, signal: AbortSignal): Promise<SkillSummary[]> {
  const response = await authenticatedFetch(url, { signal });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  const body = (await response.json()) as { skills?: SkillSummary[] };
  if (!Array.isArray(body.skills)) throw new Error("Invalid host skills response");
  return body.skills;
}

export function useSessionSkills(
  session: Session | null | undefined,
  hostOnline: boolean | null | undefined,
  starting: boolean,
) {
  const available = !!session?.hostId && !!session.workspace && hostOnline !== false;
  const query = useQuery({
    queryKey: [
      "session-skills",
      session?.id,
      session?.hostId,
      session?.workspace,
      session?.agentId,
      session?.harness,
      session?.subAgentName,
    ],
    queryFn: ({ signal }) =>
      fetchSkills(`/v1/sessions/${encodeURIComponent(session!.id)}/skills`, signal),
    enabled: available,
    staleTime: 30_000,
    refetchInterval: available ? 60_000 : false,
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
