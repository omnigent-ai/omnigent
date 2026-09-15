import { skipToken, useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type { Session, SkillSummary, SkillsStatus } from "@/lib/types";

export type SkillsTarget =
  | {
      sessionId: string;
      hostId?: never;
      harness?: never;
      path?: never;
      /** Cache dependencies for the scope derived by the server. */
      scope?: Pick<Session, "hostId" | "harness" | "workspace" | "agentId" | "subAgentName">;
    }
  | {
      sessionId?: never;
      hostId: string;
      harness: string;
      path: string;
      scope?: never;
    };

/** Both composers receive complete catalogs directly from host-backed requests. */
async function fetchSkills(target: SkillsTarget, signal: AbortSignal): Promise<SkillSummary[]> {
  const params =
    target.sessionId !== undefined
      ? new URLSearchParams({ session_id: target.sessionId })
      : new URLSearchParams({ host_id: target.hostId, harness: target.harness, path: target.path });
  const response = await authenticatedFetch(`/v1/skills?${params}`, { signal });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  const body = (await response.json()) as { skills?: SkillSummary[] };
  if (!Array.isArray(body.skills)) throw new Error("Invalid host skills response");
  return body.skills;
}

interface SkillsOptions {
  /** Null until the composer has a complete discovery target. */
  target: SkillsTarget | null;
  enabled?: boolean;
  starting?: boolean;
}

/** Discover composer skills, with optional session authorization and agent scope. */
export function useSkills({ target, enabled = true, starting = false }: SkillsOptions) {
  const available = enabled && target !== null;
  const query = useQuery({
    queryKey: ["skills", target?.sessionId, target],
    queryFn: available ? ({ signal }) => fetchSkills(target, signal) : skipToken,
    enabled: available,
    staleTime: 30_000,
    refetchInterval: available && target.sessionId !== undefined ? 60_000 : false,
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
