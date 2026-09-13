import { useQuery } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/identity";
import type { HostWorktree } from "@/hooks/useHostWorktrees";
import { useSessionActive, useTrailingInvalidate } from "@/hooks/useWorkspaceChangedFiles";

/**
 * Discriminated worktree-list outcome for a session's workspace.
 *
 * Distinct from {@link useHostWorktrees}, which collapses every HTTP 400
 * (genuinely-not-a-repo AND a transient git failure) into `[]`. That ambiguity
 * is fine for the new-session picker ("nothing to offer") but not for a status
 * bar, where `[]` must not be advertised as "not a git repository". Here only
 * an explicit not-a-repo error yields `not_git`; every other failure is
 * `unknown`.
 */
export type SessionWorktreesResult =
  { status: "ok"; worktrees: HostWorktree[] } | { status: "not_git" } | { status: "unknown" };

interface HostWorktreesResponse {
  object: string;
  data: HostWorktree[];
}

async function fetchSessionWorktrees(
  hostId: string,
  repoPath: string,
): Promise<SessionWorktreesResult> {
  const params = new URLSearchParams({ path: repoPath });
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/worktrees?${params.toString()}`,
  );
  if (res.ok) {
    const body = (await res.json()) as HostWorktreesResponse;
    return { status: "ok", worktrees: body.data };
  }
  if (res.status === 400) {
    // 400 covers both "not a git repository" and other git failures. Classify
    // not_git only on the explicit message; any other failure stays unknown.
    const text = await res.text().catch(() => "");
    return /not a git (?:repo|repository)/i.test(text)
      ? { status: "not_git" }
      : { status: "unknown" };
  }
  // Host offline (409), missing (404), server error — genuinely unknown, never
  // a claim about the repo.
  return { status: "unknown" };
}

/**
 * Live git worktrees for a session's workspace, preserving the not-a-repo
 * reason and refreshing at turn boundaries.
 *
 * No `placeholderData`: on a host/workspace switch the query must report
 * `undefined` (→ loading) rather than advertise the previous workspace's
 * worktrees as the new one's. A trailing invalidate refetches when the session
 * goes active → idle, so a branch the agent switched to during the turn is
 * picked up without a manual refresh.
 *
 * @param sessionId - Session id, for the turn-end invalidate (`null` disables it).
 * @param hostId - Host the workspace lives on (`null` disables the query).
 * @param workspace - Absolute workspace path (`null`/empty disables the query).
 */
export function useSessionWorktrees(
  sessionId: string | null | undefined,
  hostId: string | null | undefined,
  workspace: string | null | undefined,
) {
  useTrailingInvalidate(
    sessionId ?? undefined,
    useSessionActive(sessionId ?? undefined),
    "session-git-worktrees",
  );
  return useQuery({
    // sessionId leads the key so the trailing invalidate (prefix match) hits it;
    // hostId + workspace are part of the key so a switch is a fresh entry.
    queryKey: ["session-git-worktrees", sessionId ?? null, hostId ?? null, workspace ?? null],
    queryFn: () => fetchSessionWorktrees(hostId as string, workspace as string),
    enabled: !!hostId && !!workspace,
    staleTime: 5_000,
  });
}
