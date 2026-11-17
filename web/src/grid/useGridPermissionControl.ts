import { useRef, useState } from "react";
import { toast } from "sonner";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import {
  nextPermissionModePress,
  permissionChip,
  permissionChipKind,
  type PermissionModeCycleMiss,
} from "@/lib/permissionModeCycle";
import { isOwnerLevel } from "@/lib/permissionsApi";
import type { Session } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";

/**
 * A grid cell's permission chip and its Shift+Tab cycle, both on that cell's
 * session. As in the full-screen composer, only its owner can switch the mode,
 * not while the session is unreachable, and one switch runs at a time.
 */
export function useGridPermissionControl(
  session: Session | null | undefined,
  permissionLevel: number | null,
  unreachable: boolean,
) {
  const sessionId = session?.id ?? null;
  const entry = useConversationEntryState(sessionId);
  const kind = session ? permissionChipKind(session) : null;
  const chip =
    kind === null
      ? null
      : permissionChip(kind, entry.claudePermissionMode, entry.codexApprovalMode);
  const owner = isOwnerLevel(permissionLevel);
  const [busy, setBusy] = useState(false);
  // Refs gate a second press in the same tick, before the busy state re-renders.
  const busyRef = useRef(false);
  const missRef = useRef<PermissionModeCycleMiss | null>(null);

  // Read the live entry, not this render: a switch may have landed since.
  const liveMode = (): string => {
    const state = sessionId === null ? undefined : conversationRegistry.peek(sessionId)?.getState();
    if (state === undefined) return "";
    return kind === "codex" ? state.codexApprovalMode : state.claudePermissionMode;
  };

  /** Switches the cell's session to *mode*; resolves whether it landed there. */
  const switchMode = async (mode: string): Promise<boolean> => {
    if (sessionId === null || kind === null) return false;
    busyRef.current = true;
    setBusy(true);
    try {
      const store = useChatStore.getState();
      if (kind === "codex") await store.setCodexApprovalMode(mode, sessionId);
      else await store.setClaudePermissionMode(mode, sessionId);
      const landed = liveMode() === mode;
      if (landed) missRef.current = null;
      return landed;
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Unable to change permissions");
      return false;
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  /** Shift+Tab: steps the cell's session to its next mode. Returns whether it took the key. */
  const cycle = (): boolean => {
    if (!owner || unreachable || chip === null || sessionId === null) return false;
    // Hold the key while a switch is in flight so focus doesn't jump away.
    if (busyRef.current) return true;
    const selected = liveMode();
    if (!chip.options.some((option) => option.value === selected)) return false;
    const next = nextPermissionModePress(chip.options, selected, sessionId, missRef.current);
    if (next === null) return false;
    void switchMode(next).then((landed) => {
      if (!landed) missRef.current = { sessionId, from: liveMode(), past: next };
    });
    return true;
  };

  return {
    chip,
    harness: entry.sessionHarness,
    owner,
    unreachable,
    busy,
    select: (mode: string) => void switchMode(mode),
    cycle,
  };
}

export type GridPermissionControl = ReturnType<typeof useGridPermissionControl>;
