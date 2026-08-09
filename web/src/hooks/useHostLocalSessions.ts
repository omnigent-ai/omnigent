import { useQuery } from "@tanstack/react-query";

import { fetchHostLocalSessions, type ImportSourceId } from "@/lib/localSessionImportApi";

/**
 * React Query hook: browse a host's recent local chats (Claude Code / Codex)
 * for the "Import chat" picker.
 *
 * Lazy — only fires when `hostId` is set and `enabled` is true, so the
 * dialog can defer fetching until the user actually opens the import flow.
 * Unlike `useHostWorktrees`, a non-OK response (including 400) is a real
 * error and propagates to the caller rather than resolving to an empty
 * list — the dialog shows the message.
 *
 * @param hostId Host id, e.g. ``"host_a1b2..."``. `null` disables.
 * @param source Which local chat tool to browse.
 * @param enabled Whether the query should run at all, e.g. gated on the
 *   import dialog being open.
 * @returns React Query result with ``data: LocalSessionSummary[]``.
 */
export function useHostLocalSessions(
  hostId: string | null,
  source: ImportSourceId,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["host-local-sessions", hostId, source],
    queryFn: () => fetchHostLocalSessions(hostId as string, source),
    enabled: hostId !== null && enabled,
    staleTime: 5_000,
  });
}
