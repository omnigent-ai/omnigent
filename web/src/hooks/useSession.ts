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
import { useRef } from "react";
import { ApiError, getSessionSlim } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";

/**
 * Upper bound on parent-chain hops when resolving a session's
 * top-level root. Spawn trees are shallow (the rail renders 3 levels);
 * the cap only guards against a pathological/corrupt parent chain,
 * returning the deepest ancestor reached as a best-effort root.
 */
const MAX_ROOT_WALK_HOPS = 8;

/**
 * Consecutive ``404``s that stop the poll. A deleted session never comes
 * back, so re-asking forever is pure noise; two in a row rules out the
 * single-response race of a snapshot read landing mid-delete.
 */
const MAX_NOT_FOUND_POLLS = 2;

interface UseSessionResult {
  session: Session | null;
  isLoading: boolean;
  error: Error | null;
}

export interface UseSessionOptions {
  /**
   * Re-ask for the snapshot on this interval (ms) while mounted; omitted keeps
   * the query long-lived (chatStore-refreshed) as before.
   *
   * Some snapshot fields are DISCOVERED after the snapshot the UI cached and
   * have no event channel of their own — `warnings` (e.g.
   * `subagent_routing_unenforced`) is recorded by a server-side watcher while
   * the session runs, so the surface rendering it has to re-ask or the banner
   * would only ever appear after a hard reload.
   */
  refetchIntervalMs?: number;
  /**
   * Ask the server to re-read runner-backed state on EVERY fetch, not just
   * the first. Off by default: `refresh_state=true` drops the runner's
   * skills / model-options caches, so a polling caller would pop them on
   * every tick and hand back empty lists while they refill.
   */
  refreshStateOnEveryFetch?: boolean;
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
 * process caches — only that first fetch, though: see
 * ``refreshStateOnEveryFetch``.
 */
export function useSession(
  conversationId: string | null | undefined,
  options?: UseSessionOptions,
): UseSessionResult {
  const queryClient = useQueryClient();
  const queryKey = conversationId ? ["session", conversationId] : ["session", null];
  // Consecutive 404s for the session currently being watched, tracked here
  // rather than off query state (which resets its failure count at the start
  // of every fetch). Navigating to another session starts a fresh streak.
  const notFound = useRef({ id: conversationId ?? null, count: 0 });
  if (notFound.current.id !== (conversationId ?? null)) {
    notFound.current = { id: conversationId ?? null, count: 0 };
  }
  const notFoundStreak = notFound.current;
  const { data, isLoading, error } = useQuery({
    queryKey,
    queryFn: async () => {
      // Cache-cold = the first fetch for this session, the only one that
      // needs the server to re-read runner state.
      const refreshState =
        options?.refreshStateOnEveryFetch === true ||
        queryClient.getQueryData<Session>(queryKey) === undefined;
      try {
        const session = await getSessionSlim(conversationId as string, { refreshState });
        notFoundStreak.count = 0;
        return session;
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) notFoundStreak.count += 1;
        else notFoundStreak.count = 0;
        throw err;
      }
    },
    enabled: Boolean(conversationId),
    staleTime: Infinity,
    retry: false,
    // Per-observer, so only the caller that asked for it polls (and TanStack
    // pauses it while the tab is hidden). Stops for good once the session is
    // gone — a deleted session is never coming back.
    refetchInterval: () =>
      options?.refetchIntervalMs !== undefined && notFoundStreak.count < MAX_NOT_FOUND_POLLS
        ? options.refetchIntervalMs
        : false,
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
