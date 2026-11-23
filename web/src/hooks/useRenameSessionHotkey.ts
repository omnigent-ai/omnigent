import { useCurrentSessionShortcut } from "@/hooks/useCurrentSessionShortcut";
import { isMacPlatform } from "@/lib/hotkeys";
import { matchesShortcut } from "@/lib/shortcuts/registry";

/** Cmd/Ctrl+Alt+R. */
export function isRenameSessionHotkey(
  event: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
): boolean {
  return matchesShortcut(event, "session.rename", { mac: isMac });
}

/**
 * Bind alongside the owner-only title editor so unavailable sessions keep their keys.
 * `sessionId` is the session it renames, so menus show ⌘⌥R only on that session's rows.
 */
export function useRenameSessionHotkey(
  onRename: () => void,
  {
    sessionId,
    enabled = true,
    isMac = isMacPlatform(),
  }: { sessionId?: string; enabled?: boolean; isMac?: boolean } = {},
): void {
  useCurrentSessionShortcut("session.rename", onRename, enabled, isMac, sessionId);
}
