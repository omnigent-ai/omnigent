import { useSessionHostId } from "../RevealInFileManager";
import { useFocusedFile } from "./focusedFile";
import { useFileSshTargetDemand } from "./useFileCommandFacts";

/**
 * Read the SSH names ⌘K's file rows need while `open`: the focused file's host, and the host
 * of `conversationId`, the session ⌘K acts on (in a grid, the selected pane's).
 */
export function usePaletteSshTargets(open: boolean, conversationId: string | null): void {
  const focused = useFocusedFile();
  useFileSshTargetDemand(focused?.hostId ?? null, focused?.conversationId ?? null, open);
  useFileSshTargetDemand(useSessionHostId(conversationId), conversationId, open);
}
