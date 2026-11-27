// Query hooks for gadgets: the viewer's list, those shared with them, one gadget, the viewer's
// shelf (pinned gadgets) and the gadgets each session shows in its side panel. The sidebar,
// every session's side panel and the gadget pages share these caches.
import { useEffect } from "react";
import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { ApiError } from "@/lib/sessionsApi";
import {
  type Gadget,
  type GadgetShelf,
  deleteGadget,
  getSessionGadgets,
  getGadget,
  getGadgetShelf,
  linkGadgetToSession,
  listSharedGadgets,
  listGadgets,
  pinGadget,
  renameGadget,
} from "@/gadgets/api/gadgetApi";
import { onSessionGadgetsChanged } from "@/gadgets/host/gadgetSession";

export const GADGETS_KEY = ["gadgets"] as const;
export const GADGET_LIST_KEY = [...GADGETS_KEY, "list"] as const;
export const SHARED_GADGETS_KEY = [...GADGETS_KEY, "shared"] as const;
export const GADGET_SHELF_KEY = [...GADGETS_KEY, "shelf"] as const;

export function gadgetKey(gadgetId: string) {
  return [...GADGETS_KEY, "gadget", gadgetId] as const;
}

export function sessionGadgetsKey(sessionId: string) {
  return [...GADGETS_KEY, "session", sessionId] as const;
}

// An agent can add a gadget to its session while the side panel is open.
const SESSION_GADGETS_POLL_MS = 30_000;
// How often an open gadget checks that the viewer can still open it, and at what access.
const GADGET_POLL_MS = 30_000;
const MAX_RETRIES = 3;

/** Retries a failed gadget fetch, except when the gadget is gone, which another try won't fix. */
function retryGadgetQuery(failureCount: number, error: unknown): boolean {
  return failureCount < MAX_RETRIES && !isGadgetUnavailable(error);
}

/**
 * Whether a gadget request failed because the viewer can't open the gadget: it was deleted, or
 * it is no longer shared with them. The server answers both with 404, so they can't be told apart.
 */
export function isGadgetUnavailable(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 403);
}

/** Whether the server has gadgets. */
export function useGadgetsEnabled(): boolean {
  return isFeatureEnabled(useServerInfo(), "gadgets");
}

/** The gadgets the viewer owns. */
export function useGadgets() {
  return useQuery({
    queryKey: GADGET_LIST_KEY,
    queryFn: ({ signal }) => listGadgets(signal),
  });
}

/** Gadgets other people share with the viewer or their groups. */
export function useSharedGadgets(enabled = true) {
  return useQuery({
    queryKey: SHARED_GADGETS_KEY,
    queryFn: ({ signal }) => listSharedGadgets(signal),
    enabled,
  });
}

/**
 * One gadget. An open one checks again on focus and every so often, so a change to the viewer's
 * access (or the gadget's deletion) reaches the view.
 */
export function useGadget(gadgetId: string | null, { watch = false }: { watch?: boolean } = {}) {
  return useQuery({
    queryKey: gadgetKey(gadgetId ?? ""),
    queryFn: ({ signal }) => getGadget(gadgetId!, signal),
    enabled: gadgetId !== null,
    retry: retryGadgetQuery,
    refetchOnWindowFocus: watch,
    refetchInterval: watch ? GADGET_POLL_MS : false,
  });
}

/** The viewer's shelf, while gadgets are on. */
export function useGadgetShelf() {
  const enabled = useGadgetsEnabled();
  return useQuery({
    queryKey: GADGET_SHELF_KEY,
    queryFn: ({ signal }) => getGadgetShelf(signal),
    enabled,
    staleTime: 30_000,
  });
}

/**
 * The gadgets shown beside a session (undefined until loaded or while gadgets are off), and
 * whether the first answer is in: a panel waits for it before deciding a saved tab is gone.
 */
export function useSessionGadgetsQuery(
  sessionId: string,
  enabled = true,
): { gadgets: Gadget[] | undefined; settled: boolean } {
  const gadgetsEnabled = useGadgetsEnabled();
  const on = gadgetsEnabled && enabled;
  const { data, isPending } = useQuery({
    queryKey: sessionGadgetsKey(sessionId),
    queryFn: ({ signal }) => getSessionGadgets(sessionId, signal),
    enabled: on,
    refetchInterval: SESSION_GADGETS_POLL_MS,
    // Back on the page, the tabs catch up with changes made elsewhere (a rename, a lost share).
    refetchOnWindowFocus: true,
    staleTime: 10_000,
  });
  return { gadgets: on ? data : undefined, settled: !on || !isPending };
}

/** The gadgets shown beside a session; undefined until loaded or while gadgets are off. */
export function useSessionGadgets(sessionId: string, enabled = true): Gadget[] | undefined {
  return useSessionGadgetsQuery(sessionId, enabled).gadgets;
}

/**
 * Keeps the side panels' caches current when gadget code links a gadget to a session, so its
 * tab shows without waiting for the next poll.
 */
export function useLinkedGadgetsSync(): void {
  const queryClient = useQueryClient();
  useEffect(
    () =>
      onSessionGadgetsChanged((sessionId, gadgets) =>
        queryClient.setQueryData(sessionGadgetsKey(sessionId), gadgets),
      ),
    [queryClient],
  );
}

/** Replaces a gadget wherever it is cached, for example after a rename. */
export function updateCachedGadget(queryClient: QueryClient, gadget: Gadget): void {
  const replace = (items: Gadget[] | undefined) =>
    items?.map((item) => (item.id === gadget.id ? { ...item, ...gadget } : item));
  queryClient.setQueryData(gadgetKey(gadget.id), gadget);
  queryClient.setQueryData<Gadget[]>(GADGET_LIST_KEY, replace);
  queryClient.setQueriesData<Gadget[]>({ queryKey: [...GADGETS_KEY, "session"] }, replace);
  queryClient.setQueryData<GadgetShelf>(GADGET_SHELF_KEY, (shelf) =>
    shelf
      ? {
          ...shelf,
          pinned: shelf.pinned.map((pin) =>
            pin.id === gadget.id ? { ...pin, name: gadget.name } : pin,
          ),
        }
      : shelf,
  );
}

/** Marks every gadget cache stale, after a change the caches can't patch (sharing, delete). */
export function invalidateGadgets(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: GADGETS_KEY });
}

export function usePinGadget() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ gadgetId, pinned }: { gadgetId: string; pinned: boolean }) =>
      pinGadget(gadgetId, pinned),
    onSuccess: (shelf) => queryClient.setQueryData(GADGET_SHELF_KEY, shelf),
  });
}

/** Shows a gadget beside a session, or stops showing it there. */
export function useShowGadgetInSession() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      gadgetId,
      sessionId,
      shown,
    }: {
      gadgetId: string;
      sessionId: string;
      shown: boolean;
    }) => linkGadgetToSession(gadgetId, sessionId, shown),
    onSuccess: (gadgets, { sessionId }) =>
      queryClient.setQueryData(sessionGadgetsKey(sessionId), gadgets),
  });
}

export function useRenameGadget() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ gadgetId, name }: { gadgetId: string; name: string }) =>
      renameGadget(gadgetId, name),
    onSuccess: (gadget) => updateCachedGadget(queryClient, gadget),
  });
}

/** Deletes a gadget for everyone, and drops it from every cache. */
export function useDeleteGadget() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (gadgetId: string) => deleteGadget(gadgetId),
    onSuccess: (_result, gadgetId) => {
      const without = (items: Gadget[] | undefined) =>
        items?.filter((item) => item.id !== gadgetId);
      queryClient.setQueryData<Gadget[]>(GADGET_LIST_KEY, without);
      queryClient.setQueriesData<Gadget[]>({ queryKey: [...GADGETS_KEY, "session"] }, without);
      queryClient.removeQueries({ queryKey: gadgetKey(gadgetId) });
      invalidateGadgets(queryClient);
    },
  });
}
