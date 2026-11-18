import { skipToken, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useCallback, useRef } from "react";
import { authenticatedFetch } from "@/lib/identity";
import { ApiError } from "@/lib/sessionsApi";
import type { Session, SkillsCatalog, SkillSummary, SkillsStatus } from "@/lib/types";

export type SkillsTarget =
  | {
      sessionId: string;
      hostId?: never;
      harness?: never;
      path?: never;
      agentId?: never;
      /** Cache dependencies for the scope derived by the server. */
      scope?: Pick<Session, "hostId" | "harness" | "workspace" | "agentId" | "subAgentName">;
    }
  | {
      sessionId?: never;
      hostId: string;
      harness: string;
      path: string;
      agentId?: string;
      scope?: never;
    };

/**
 * Both composers receive catalogs directly from host-backed requests.
 * `refresh` makes the host rescan disk instead of serving its cached catalog.
 */
export async function fetchSkills(
  target: SkillsTarget,
  signal: AbortSignal,
  { refresh = false }: { refresh?: boolean } = {},
): Promise<SkillsCatalog> {
  const params =
    target.sessionId !== undefined
      ? new URLSearchParams({ session_id: target.sessionId })
      : new URLSearchParams({ host_id: target.hostId, harness: target.harness, path: target.path });
  if (target.agentId !== undefined) params.set("agent_id", target.agentId);
  if (refresh) params.set("refresh", "true");
  const response = await authenticatedFetch(`/v1/skills?${params}`, { signal });
  if (!response.ok) {
    throw new ApiError(`${response.status} ${response.statusText}`, response.status, null);
  }
  const body = (await response.json()) as { skills?: SkillSummary[]; warnings?: unknown };
  if (!Array.isArray(body.skills)) throw new Error("Invalid host skills response");
  // Older servers omit warnings; their catalogs were always presented as complete.
  // Deduplicated so each warning is a unique row (and React key) in the menu.
  const warnings = Array.isArray(body.warnings)
    ? Array.from(
        new Set(body.warnings.filter((warning): warning is string => typeof warning === "string")),
      )
    : [];
  return { skills: body.skills, warnings };
}

/** Shared cache key, so other host-scoped readers reuse the composer's catalogs. */
export function skillsQueryKey(target: SkillsTarget | null) {
  return ["skills", target?.sessionId, target] as const;
}

/** Replace *target*'s cached catalog with a fresh disk scan. */
async function rescanSkillsCatalog(queryClient: QueryClient, target: SkillsTarget): Promise<void> {
  const queryKey = skillsQueryKey(target);
  const rescan = async (): Promise<boolean> => {
    // Cancel any stale fetch; later readers then share this rescan's request.
    await queryClient.cancelQueries({ queryKey });
    const previous = queryClient.getQueryState<SkillsCatalog>(queryKey);
    let rescanned = false;
    try {
      await queryClient.fetchQuery({
        queryKey,
        queryFn: async ({ signal }) => {
          const catalog = await fetchSkills(target, signal, { refresh: true });
          rescanned = true;
          return catalog;
        },
        staleTime: 0,
      });
    } catch (error) {
      // A failed rescan leaves the loaded catalog usable, and as stale as it was.
      if (previous?.data !== undefined)
        queryClient.setQueryData(queryKey, previous.data, { updatedAt: previous.dataUpdatedAt });
      throw error;
    }
    return rescanned;
  };
  // An invalidation (e.g. a session rebind) can swap the rescan for a cached read.
  if (!(await rescan()) && !(await rescan()))
    throw new Error("The skills rescan was replaced by a cached read");
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
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: skillsQueryKey(target),
    queryFn: available ? ({ signal }) => fetchSkills(target, signal) : skipToken,
    enabled: available,
    staleTime: 30_000,
    refetchInterval: (skillsQuery) =>
      available && (target.sessionId !== undefined || skillsQuery.state.status === "error")
        ? 60_000
        : false,
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
  // One rescan at a time: a second would cancel the first mid-request.
  const reloadQueue = useRef<Promise<void>>(Promise.resolve());
  /** Replace the catalog with a fresh disk scan, e.g. after `/reload-skills`. */
  const reload = useCallback(() => {
    if (target === null || !available) return Promise.resolve();
    const run = reloadQueue.current
      .catch(() => {})
      .then(() => rescanSkillsCatalog(queryClient, target));
    reloadQueue.current = run;
    return run;
  }, [available, queryClient, target]);
  return {
    skills: available ? (query.data?.skills ?? []) : [],
    skillsWarnings: available ? (query.data?.warnings ?? []) : [],
    skillsStatus,
    refetch: query.refetch,
    reload,
  };
}
