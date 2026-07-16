// TanStack Query hooks for the Skill Registry.
//
// The catalog is SESSION-CONTEXTUAL: bundle/workspace/provider skills resolve
// on a bound session's runner, so the catalog + detail hooks take the active
// session id and stay idle until one exists (the page shows an empty state).
//
//  - `useActiveSkillSession()` — resolves the session id the catalog is scoped
//    to: the last-viewed chat (chatStore) if set, else the most-recent session
//    from the sidebar list. `null` when there is none.
//  - `useSkillCatalog(sessionId, includeOtherTools)` — the master-list catalog.
//    Keyed by session + the include-other-tools flag so flipping the switch or
//    changing session refetches (or serves a warm cache).
//  - `useSkillDetail(id, sessionId, includeOtherTools)` — one skill's full
//    detail in the SAME session + trust context as the list (the backend
//    resolves the same winner only when both match). Enabled only when a skill
//    is selected and a session exists.
//  - `useSkillTrust()` / `useSetSkillTrust()` — read + persist the
//    include-other-tools trust setting.

import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useConversations } from "@/hooks/useConversations";
import { useChatStore } from "@/store/chatStore";
import {
  getSkillCatalog,
  getSkillDetail,
  getSkillTrust,
  setSkillTrust,
  type SkillCatalog,
  type SkillDetail,
} from "@/lib/skillsApi";

const SKILLS_KEY = ["skills"] as const;

export function skillCatalogKey(sessionId: string | null, includeOtherTools: boolean) {
  return [...SKILLS_KEY, "catalog", sessionId, includeOtherTools] as const;
}

export function skillDetailKey(
  id: string | null,
  sessionId: string | null,
  includeOtherTools: boolean,
) {
  return [...SKILLS_KEY, "detail", sessionId, includeOtherTools, id] as const;
}

export function skillTrustKey() {
  return [...SKILLS_KEY, "trust"] as const;
}

/**
 * Resolve the session id the skill catalog should be scoped to.
 *
 * The Skills page is a top-level route (not under `/c/:id`), so there is no
 * route param to read. Prefer the last-viewed chat held in the chat store;
 * on a fresh load (store reset) fall back to the most-recently-updated
 * non-archived session from the sidebar list. Returns `null` when the user
 * has no sessions at all — the page then shows the "needs a running session"
 * empty state.
 */
export function useActiveSkillSession(): string | null {
  const storeConversationId = useChatStore((s) => s.conversationId);
  // Reuse the sidebar's cached session list (shared query key) — no extra
  // fetch when the sidebar is mounted. Server returns descending by
  // updated_at, so the first non-archived row is the most recent.
  const conversationsQuery = useConversations();
  return useMemo(() => {
    if (storeConversationId) return storeConversationId;
    const rows = (conversationsQuery.data?.pages ?? []).flatMap((p) => p.data);
    const mostRecent = rows.find((c) => !c.archived);
    return mostRecent?.id ?? null;
  }, [storeConversationId, conversationsQuery.data]);
}

/** The session-scoped catalog for the given include-other-tools mode. */
export function useSkillCatalog(sessionId: string | null, includeOtherTools: boolean) {
  return useQuery<SkillCatalog>({
    queryKey: skillCatalogKey(sessionId, includeOtherTools),
    queryFn: () => getSkillCatalog(sessionId as string, includeOtherTools),
    enabled: sessionId != null,
    // The catalog is cheap to recompute and reflects on-disk discovery, so a
    // short stale window keeps it fresh without hammering the endpoint.
    staleTime: 30_000,
  });
}

/** Full detail for the selected skill; idle until an id + session exist. */
export function useSkillDetail(
  id: string | null,
  sessionId: string | null,
  includeOtherTools: boolean,
) {
  return useQuery<SkillDetail>({
    queryKey: skillDetailKey(id, sessionId, includeOtherTools),
    queryFn: () => getSkillDetail(id as string, sessionId as string, includeOtherTools),
    enabled: id != null && sessionId != null,
    // Detail (instructions + provenance) is immutable for the life of a
    // catalog snapshot, so hold it for the session to keep selection instant.
    staleTime: 5 * 60_000,
  });
}

/** The persisted include-other-tools trust setting. */
export function useSkillTrust() {
  return useQuery<boolean>({
    queryKey: skillTrustKey(),
    queryFn: getSkillTrust,
    staleTime: 5 * 60_000,
  });
}

/** Persist the include-other-tools trust setting and refresh the catalog. */
export function useSetSkillTrust() {
  const queryClient = useQueryClient();
  return useMutation<boolean, Error, boolean>({
    mutationFn: setSkillTrust,
    onSuccess: (applied) => {
      queryClient.setQueryData(skillTrustKey(), applied);
      // Every session's catalog for either mode may now differ (widened /
      // narrowed set) — drop them so the next read reflects the new boundary.
      void queryClient.invalidateQueries({ queryKey: [...SKILLS_KEY, "catalog"] });
    },
  });
}
