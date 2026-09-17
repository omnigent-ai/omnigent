import { useConversations } from "@/hooks/useConversations";

// Reuse component tests' session-list fixtures at the scope-cache boundary.
export function useScopeCache(
  visibility: "mine" | "shared",
  refreshIntervalMs: number | false,
  enabled = true,
) {
  return useConversations("", false, { enabled, refreshIntervalMs }, undefined, visibility);
}

export function useArchivedSessions(enabled: boolean) {
  return useConversations("", false, { enabled, snapshot: true }, undefined, "archived");
}
