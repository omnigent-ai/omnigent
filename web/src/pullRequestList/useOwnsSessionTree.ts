// Whether the user owns the session tree a Pull Requests list shows, so its sub-agents' links
// are theirs to remove: a sub-agent belongs to its root's owner, as the server's own rule says.

import { useActiveRootSessionId } from "@/hooks/useSession";
import { useSessionOwner } from "@/hooks/usePermissions";
import { useViewerId } from "@/hooks/useViewerId";

import { ownsSessionTree } from "./subAgentRemoval";

/** `undefined` while the root, its owner or the viewer is still being read, or that read failed. */
export function useOwnsSessionTree(sessionId: string | null): boolean | undefined {
  const rootId = useActiveRootSessionId(sessionId);
  const owner = useSessionOwner(rootId);
  const viewerId = useViewerId();
  return ownsSessionTree(owner.isSuccess ? owner.data : undefined, viewerId);
}
