import { useQuery } from "@tanstack/react-query";
import { listProjectIdentities, listProjectIdentityProposals } from "@/lib/projectsApi";

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

/** Query key for a project's pending identity proposals. */
export function projectIdentityProposalsKey(projectId: string | null) {
  return ["project-identity-proposals", projectId] as const;
}

/** Identity proposals awaiting the owner; agents' proposals also refresh it via projects_changed. */
export function useProjectIdentityProposals(projectId: string | null) {
  return useQuery({
    queryKey: projectIdentityProposalsKey(projectId),
    queryFn: () => listProjectIdentityProposals(projectId as string),
    enabled: projectId !== null,
    refetchInterval: 15_000,
  });
}
