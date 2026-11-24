// While ⌘K is open, read the SSH names its file rows can use, so their "open in an editor
// over SSH" rows show up: the focused file's host, and the host of the session ⌘K acts on.

import { useEffect } from "react";
import { useSessionHostId } from "../RevealInFileManager";
import { useFocusedFile } from "./focusedFile";
import { useResolveFileSshTarget } from "./useFileCommandFacts";

/**
 * Read the SSH names ⌘K's file rows need while `open`: the focused file's host, and the host
 * of `conversationId`, the session ⌘K acts on (in a grid, the selected pane's).
 */
export function usePaletteSshTargets(open: boolean, conversationId: string | null): void {
  const focused = useFocusedFile();
  const resolveFocused = useResolveFileSshTarget(
    focused?.hostId ?? null,
    focused?.conversationId ?? null,
  );
  const resolveSession = useResolveFileSshTarget(useSessionHostId(conversationId), conversationId);
  useEffect(() => {
    if (!open) return;
    resolveFocused();
    resolveSession();
  }, [open, resolveFocused, resolveSession]);
}
