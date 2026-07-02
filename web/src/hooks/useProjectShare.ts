/**
 * TanStack Query hooks for project sharing + membership.
 * Wraps the fetch functions in `lib/projectsApi.ts`, keyed by project id.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type ProjectMember,
  leaveProject,
  listProjectMembers,
  shareProject,
  unshareProject,
} from "@/lib/projectsApi";

function projectMembersKey(projectId: string) {
  return ["projectMembers", projectId] as const;
}

/** Fetch a project's grantees (manage access required). */
export function useProjectMembers(projectId: string | null) {
  return useQuery({
    queryKey: projectMembersKey(projectId ?? ""),
    queryFn: () => listProjectMembers(projectId!),
    enabled: !!projectId,
  });
}

/**
 * Invalidate everything a share/leave touches: the project's member list, the
 * project list (its members/public flags), and the sidebar/conversation lists
 * (a share changes which sessions a member can see).
 */
function useInvalidateShare(projectId: string) {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: projectMembersKey(projectId) });
    void qc.invalidateQueries({ queryKey: ["projects"] });
    void qc.invalidateQueries({ queryKey: ["conversations"] });
    void qc.invalidateQueries({ queryKey: ["project-sessions"] });
  };
}

/** Share a project with a user, or all members / anyone with the link. */
export function useShareProject(projectId: string) {
  const invalidate = useInvalidateShare(projectId);
  return useMutation({
    mutationFn: ({ userId, level }: { userId: string; level: number }) =>
      shareProject(projectId, userId, level),
    onSuccess: invalidate,
  });
}

/** Revoke a grantee from a project. */
export function useUnshareProject(projectId: string) {
  const invalidate = useInvalidateShare(projectId);
  return useMutation({
    mutationFn: (userId: string) => unshareProject(projectId, userId),
    onSuccess: invalidate,
  });
}

/** Leave a project (the caller drops their own non-owner grant). */
export function useLeaveProject(projectId: string) {
  const invalidate = useInvalidateShare(projectId);
  return useMutation({
    mutationFn: () => leaveProject(projectId),
    onSuccess: invalidate,
  });
}

export type { ProjectMember };
