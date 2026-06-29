// Single-conversation snapshot hook keyed on conversation id.
//
// Shares the ``["session", id]`` TanStack Query cache with
// ``chatStore.bindStream`` (which fetches the snapshot on conversation
// bind). So when a hook caller and the chat bind fire concurrently,
// TanStack dedupes them and they observe the same result.
//
// The primary consumer is permission resolution: child (sub-agent)
// sessions are filtered out of the sidebar conversations list, so
// AppShell / ChatPage can't derive ``permissionLevel`` from there.
// Reading it from the single-fetch snapshot instead means the rail
// gets the user's actual level for any conversation they navigate to.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getSessionSlim } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";

/**
 * Upper bound on parent-chain hops when resolving a session's
 * top-level root. Spawn trees are shallow (the rail renders 3 levels);
 * the cap only guards against a pathological/corrupt parent chain,
 * returning the deepest ancestor reached as a best-effort root.
 */
const MAX_ROOT_WALK_HOPS = 8;

interface UseSessionResult {
  session: Session | null;
  isLoading: boolean;
  error: Error | null;
}

/**
 * Read the cached single-session snapshot for a conversation, falling
 * back to ``GET /v1/sessions/{id}`` when nothing is cached yet.
 *
 * Pass ``null`` to disable. The query is otherwise long-lived
 * (``staleTime: Infinity``) because chatStore is the source of truth
 * for refresh — it refetches on every bind, which writes back into
 * this same cache key. A cache-cold page load asks the server to
 * refresh runner-backed state so browser refresh pierces stale AP
 * process caches.
 */
export function useSession(conversationId: string | null | undefined): UseSessionResult {
  const { data, isLoading, error } = useQuery({
    queryKey: conversationId ? ["session", conversationId] : ["session", null],
    queryFn: () => getSessionSlim(conversationId as string, { refreshState: true }),
    enabled: Boolean(conversationId),
    staleTime: Infinity,
    retry: false,
  });
  return {
    session: data ?? null,
    isLoading,
    error: (error as Error | null) ?? null,
  };
}

/**
 * Resolve the top-level root of a session's spawn tree by walking the
 * ``parentSessionId`` chain upward.
 *
 * Drives the Agents rail: the rail renders the whole tree from the
 * top-level session, so when the user is viewing a grandchild the root
 * is two-plus hops up, not just ``parentSessionId``. Each hop reuses
 * the shared ``["session", id]`` snapshot cache (via ``fetchQuery``),
 * so walking a tree the user navigated through usually costs zero
 * network requests. A session's parent link is immutable, so the
 * resolved root is cached forever (``staleTime: Infinity``).
 *
 * @param conversationId - The active session, e.g. ``"conv_abc123"``;
 *   ``null`` disables resolution.
 * @param parentSessionId - The active session's parent from its
 *   snapshot: ``null`` marks a top-level session, ``undefined`` a
 *   snapshot still loading (resolution disabled).
 * @returns The root session id; ``conversationId`` itself for
 *   top-level sessions, or ``null`` while the walk (or the snapshot
 *   feeding it) is unresolved.
 */
export function useRootSessionId(
  conversationId: string | null,
  parentSessionId: string | null | undefined,
): string | null {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ["rootSessionId", conversationId],
    enabled: Boolean(conversationId) && parentSessionId != null,
    staleTime: Infinity,
    retry: false,
    queryFn: async () => {
      let id = parentSessionId as string;
      for (let hop = 0; hop < MAX_ROOT_WALK_HOPS; hop++) {
        const hopId = id;
        // Each hop's request URL is the previous hop's parentSessionId,
        // so the chain is inherently serial.
        // oxlint-disable-next-line no-await-in-loop
        const session = await queryClient.fetchQuery({
          queryKey: ["session", hopId],
          queryFn: () => getSessionSlim(hopId),
          staleTime: Infinity,
          retry: false,
        });
        if (session.parentSessionId == null) return hopId;
        id = session.parentSessionId;
      }
      return id;
    },
  });
  if (parentSessionId === null) return conversationId;
  return data ?? null;
}

/**
 * Resolve the top-level root of the *active* conversation in one call,
 * fetching its snapshot for the parent link and walking up from there.
 *
 * The sidebar lists only top-level sessions — child (sub-agent) rows are
 * omitted. When the user clicks a sub-agent in the Agents rail the URL
 * becomes ``/c/<childId>``, which matches no sidebar row, so a row that
 * compares its id against the raw active id loses its highlight. Comparing
 * against this resolved root instead keeps the owning top-level session
 * highlighted while viewing any of its descendants.
 *
 * Returns ``null`` while the snapshot or the parent walk is still loading
 * (callers fall back to the raw active id for that one render), and the
 * active id itself for a top-level session.
 *
 * @param activeConversationId - The conversation rendered in main, or
 *   ``null`` when on a non-chat route (disables resolution).
 */
export function useActiveRootSessionId(
  activeConversationId: string | null | undefined,
): string | null {
  const id = activeConversationId ?? null;
  const { session } = useSession(id);
  return useRootSessionId(id, session?.parentSessionId);
}
