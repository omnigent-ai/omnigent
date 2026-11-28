// Whether a pull request view may ask its session's workspace. Its own module, so the workspace
// hooks and the session-free scope don't import each other.

import { useIsSessionFreePullRequestScope } from "@/hooks/sessionFreePullRequestScope";
import { useWorkspaceServeable } from "@/hooks/useWorkspaceChangedFiles";

/** Whether a pull request view's workspace can be asked; never inside a session-free view. */
export function usePullRequestViewServeable(
  conversationId: string | undefined,
): boolean | undefined {
  const serveable = useWorkspaceServeable(conversationId);
  return useIsSessionFreePullRequestScope(conversationId) ? false : serveable;
}
