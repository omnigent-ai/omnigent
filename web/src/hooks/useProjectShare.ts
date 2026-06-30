/**
 * TanStack Query hooks for project-wide sharing.
 * Wraps the fetch functions in `lib/projectShareApi.ts`.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type ProjectMember,
  type ProjectShareStatus,
  getProjectShareStatus,
  leaveProject,
  listProjectMembers,
  shareProject,
  unshareProject,
} from "@/lib/projectShareApi";

function projectShareKey(projectName: string) {
  return ["projectShare", projectName] as const;
}

function projectMembersKey(projectName: string) {
  return ["projectMembers", projectName] as const;
}

/** Fetch a project's aggregate share state for the current user. */
export function useProjectShareStatus(projectName: string | null) {
  return useQuery({
    queryKey: projectShareKey(projectName ?? ""),
    queryFn: () => getProjectShareStatus(projectName!),
    enabled: !!projectName,
  });
}

/** Fetch the list of grantees on a project (manage-level only). */
export function useProjectMembers(projectName: string | null) {
  return useQuery({
    queryKey: projectMembersKey(projectName ?? ""),
    queryFn: () => listProjectMembers(projectName!),
    enabled: !!projectName,
  });
}

/**
 * Invalidate every query that a share/leave mutation can affect: the project's
 * own status + member list, and the sidebar's project/conversation lists (a
 * members-share makes the project appear/disappear in other surfaces too).
 */
function useInvalidateProjectShare(projectName: string) {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: projectShareKey(projectName) });
    void qc.invalidateQueries({ queryKey: projectMembersKey(projectName) });
    void qc.invalidateQueries({ queryKey: ["projects"] });
    void qc.invalidateQueries({ queryKey: ["conversations"] });
  };
}

/** Share a project: fan a grant out across every chat the caller manages. */
export function useShareProject(projectName: string) {
  const invalidate = useInvalidateProjectShare(projectName);
  return useMutation({
    mutationFn: ({ userId, level }: { userId: string; level: number }) =>
      shareProject(projectName, userId, level),
    onSuccess: invalidate,
  });
}

/** Revoke a grantee from every chat in a project. */
export function useUnshareProject(projectName: string) {
  const invalidate = useInvalidateProjectShare(projectName);
  return useMutation({
    mutationFn: (userId: string) => unshareProject(projectName, userId),
    onSuccess: invalidate,
  });
}

/** Leave a project (the caller drops their own non-owner grants). */
export function useLeaveProject(projectName: string) {
  const invalidate = useInvalidateProjectShare(projectName);
  return useMutation({
    mutationFn: () => leaveProject(projectName),
    onSuccess: invalidate,
  });
}

export type { ProjectMember, ProjectShareStatus };
