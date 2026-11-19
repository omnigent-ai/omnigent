// Query hooks for thingies: the viewer's list, those shared with them, one thingy, the viewer's
// shelf (pinned thingies) and the thingies each session shows in its side panel. The sidebar,
// every session's side panel and the thingy pages share these caches.
import { useEffect } from "react";
import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import {
  type Thingy,
  type ThingyShelf,
  deleteThingy,
  getSessionThingies,
  getThingy,
  getThingyShelf,
  linkThingyToSession,
  listSharedThingies,
  listThingies,
  pinThingy,
  renameThingy,
} from "@/thingies/api/thingyApi";
import { onSessionThingiesChanged } from "@/thingies/host/thingySession";

export const THINGIES_KEY = ["thingies"] as const;
export const THINGY_LIST_KEY = [...THINGIES_KEY, "list"] as const;
export const SHARED_THINGIES_KEY = [...THINGIES_KEY, "shared"] as const;
export const THINGY_SHELF_KEY = [...THINGIES_KEY, "shelf"] as const;

export function thingyKey(thingyId: string) {
  return [...THINGIES_KEY, "thingy", thingyId] as const;
}

export function sessionThingiesKey(sessionId: string) {
  return [...THINGIES_KEY, "session", sessionId] as const;
}

// An agent can add a thingy to its session while the side panel is open.
const SESSION_THINGIES_POLL_MS = 30_000;

/** Whether the server has thingies. */
export function useThingiesEnabled(): boolean {
  return isFeatureEnabled(useServerInfo(), "thingies");
}

/** The thingies the viewer owns. */
export function useThingies() {
  return useQuery({ queryKey: THINGY_LIST_KEY, queryFn: ({ signal }) => listThingies(signal) });
}

/** Thingies other people share with the viewer or their groups. */
export function useSharedThingies() {
  return useQuery({
    queryKey: SHARED_THINGIES_KEY,
    queryFn: ({ signal }) => listSharedThingies(signal),
  });
}

export function useThingy(thingyId: string | null) {
  return useQuery({
    queryKey: thingyKey(thingyId ?? ""),
    queryFn: ({ signal }) => getThingy(thingyId!, signal),
    enabled: thingyId !== null,
  });
}

/** The viewer's shelf, while thingies are on. */
export function useThingyShelf() {
  const enabled = useThingiesEnabled();
  return useQuery({
    queryKey: THINGY_SHELF_KEY,
    queryFn: ({ signal }) => getThingyShelf(signal),
    enabled,
    staleTime: 30_000,
  });
}

/** The thingies shown beside a session; undefined until loaded or while thingies are off. */
export function useSessionThingies(sessionId: string, enabled = true): Thingy[] | undefined {
  const thingiesEnabled = useThingiesEnabled();
  const { data } = useQuery({
    queryKey: sessionThingiesKey(sessionId),
    queryFn: ({ signal }) => getSessionThingies(sessionId, signal),
    enabled: thingiesEnabled && enabled,
    refetchInterval: SESSION_THINGIES_POLL_MS,
    staleTime: 10_000,
  });
  return thingiesEnabled && enabled ? data : undefined;
}

/**
 * Keeps the side panels' caches current when thingy code links a thingy to a session, so its
 * tab shows without waiting for the next poll.
 */
export function useLinkedThingiesSync(): void {
  const queryClient = useQueryClient();
  useEffect(
    () =>
      onSessionThingiesChanged((sessionId, thingies) =>
        queryClient.setQueryData(sessionThingiesKey(sessionId), thingies),
      ),
    [queryClient],
  );
}

/** Replaces a thingy wherever it is cached, for example after a rename. */
export function updateCachedThingy(queryClient: QueryClient, thingy: Thingy): void {
  const replace = (items: Thingy[] | undefined) =>
    items?.map((item) => (item.id === thingy.id ? { ...item, ...thingy } : item));
  queryClient.setQueryData(thingyKey(thingy.id), thingy);
  queryClient.setQueryData<Thingy[]>(THINGY_LIST_KEY, replace);
  queryClient.setQueriesData<Thingy[]>({ queryKey: [...THINGIES_KEY, "session"] }, replace);
  queryClient.setQueryData<ThingyShelf>(THINGY_SHELF_KEY, (shelf) =>
    shelf
      ? {
          ...shelf,
          pinned: shelf.pinned.map((pin) =>
            pin.id === thingy.id ? { ...pin, name: thingy.name } : pin,
          ),
        }
      : shelf,
  );
}

/** Marks every thingy cache stale, after a change the caches can't patch (sharing, delete). */
export function invalidateThingies(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: THINGIES_KEY });
}

export function usePinThingy() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ thingyId, pinned }: { thingyId: string; pinned: boolean }) =>
      pinThingy(thingyId, pinned),
    onSuccess: (shelf) => queryClient.setQueryData(THINGY_SHELF_KEY, shelf),
  });
}

/** Shows a thingy beside a session, or stops showing it there. */
export function useShowThingyInSession() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      thingyId,
      sessionId,
      shown,
    }: {
      thingyId: string;
      sessionId: string;
      shown: boolean;
    }) => linkThingyToSession(thingyId, sessionId, shown),
    onSuccess: (thingies, { sessionId }) =>
      queryClient.setQueryData(sessionThingiesKey(sessionId), thingies),
  });
}

export function useRenameThingy() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ thingyId, name }: { thingyId: string; name: string }) =>
      renameThingy(thingyId, name),
    onSuccess: (thingy) => updateCachedThingy(queryClient, thingy),
  });
}

/** Deletes a thingy for everyone, and drops it from every cache. */
export function useDeleteThingy() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (thingyId: string) => deleteThingy(thingyId),
    onSuccess: (_result, thingyId) => {
      const without = (items: Thingy[] | undefined) =>
        items?.filter((item) => item.id !== thingyId);
      queryClient.setQueryData<Thingy[]>(THINGY_LIST_KEY, without);
      queryClient.setQueriesData<Thingy[]>({ queryKey: [...THINGIES_KEY, "session"] }, without);
      queryClient.removeQueries({ queryKey: thingyKey(thingyId) });
      invalidateThingies(queryClient);
    },
  });
}
