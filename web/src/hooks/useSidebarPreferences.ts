/**
 * TanStack Query hook syncing the sidebar's per-user preferences (pins +
 * collapse/expand state) with the server.
 *
 * The sidebar hydrates from `localStorage` synchronously on mount (no flash),
 * then reconciles against the server copy this hook fetches: once the server
 * resolves, its value wins; every local mutation writes through to both
 * `localStorage` (cache) and the server (source of truth). On a failed fetch
 * the sidebar stays purely local — pinning must never break when the endpoint
 * is unavailable.
 */

import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type SidebarPreferenceKey,
  type SidebarPreferences,
  getSidebarPreferences,
  putSidebarPreference,
} from "@/lib/sidebarPreferencesApi";

const QUERY_KEY = ["sidebar-preferences"] as const;

export interface SidebarPreferencesSync {
  /** Whether server sync is active at all. `false` on single-user / loopback
   * deploys, where the sidebar is purely localStorage-backed. */
  enabled: boolean;
  /** The server's stored preferences, or `undefined` until (and unless) the
   * fetch resolves. Each key is absent when the user has never written it. */
  serverPreferences: SidebarPreferences | undefined;
  /** Whether the server fetch has resolved successfully at least once. Gates
   * hydration and server write-through so a pre-resolve local edit never
   * clobbers the server copy before it's been read. */
  isResolved: boolean;
  /** Write one preference through to the server (and the shared query cache).
   * A no-op when syncing is disabled. Failures are swallowed — the local cache
   * already holds the value. */
  writePreference: (key: SidebarPreferenceKey, value: string[]) => void;
}

/**
 * Sync sidebar preferences with the server.
 *
 * @param enabled Gate the whole sync. Single-user / loopback deploys pass
 *   `false` so the sidebar stays localStorage-only (no server to sync with).
 */
export function useSidebarPreferences(enabled: boolean): SidebarPreferencesSync {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: getSidebarPreferences,
    enabled,
    staleTime: 30_000,
    // A missing/unauthenticated endpoint must not spin — fall back to local.
    retry: false,
  });

  const mutation = useMutation({
    mutationFn: ({ key, value }: { key: SidebarPreferenceKey; value: string[] }) =>
      putSidebarPreference(key, value),
  });

  const writePreference = useCallback(
    (key: SidebarPreferenceKey, value: string[]) => {
      if (!enabled) return;
      // Reflect the write in the shared cache immediately so a re-mount
      // hydrates from the latest value rather than a stale server read.
      queryClient.setQueryData<SidebarPreferences>(QUERY_KEY, (prev) => ({
        ...(prev ?? {}),
        [key]: value,
      }));
      mutation.mutate({ key, value });
    },
    [enabled, queryClient, mutation],
  );

  return {
    enabled,
    serverPreferences: query.data,
    isResolved: enabled && query.isSuccess,
    writePreference,
  };
}
