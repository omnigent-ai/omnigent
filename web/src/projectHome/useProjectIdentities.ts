import { useQuery } from "@tanstack/react-query";
import { listProjectIdentities } from "@/lib/projectsApi";

/** Query key for a project's identities; mutations invalidate it. */
export function projectIdentitiesKey(projectId: string | null) {
  return ["project-identities", projectId] as const;
}

/** The project's identities, polled so the roster's busy dots stay current. */
export function useProjectIdentities(projectId: string | null) {
  return useQuery({
    queryKey: projectIdentitiesKey(projectId),
    queryFn: () => listProjectIdentities(projectId as string),
    enabled: projectId !== null,
    refetchInterval: 5_000,
  });
}
