import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getCurrentIsAdmin, resolveIdentity, subscribeIdentity } from "@/lib/identity";

// Mode-agnostic admin gate, sourced from the `/v1/me` identity probe
// (the shared `users.is_admin` column). Unlike `useMe`, which reads the
// accounts-only `/auth/me` endpoint, this works in EVERY auth mode —
// header, accounts, AND OIDC/SSO — so admin chrome (the Members /
// Policies settings sections) can surface under OIDC where `/auth/me`
// doesn't exist.
const QUERY_KEY = ["identity-is-admin"];

/**
 * Whether the current user is an admin, per `GET /v1/me`. Returns false
 * until identity resolves. Cached briefly so gating is instant across
 * consumers without re-probing on every navigation. Server enforces the
 * flag on every admin route regardless — this is chrome only.
 */
export function useIsAdmin(): boolean {
  const queryClient = useQueryClient();
  const { data } = useQuery<boolean>({
    queryKey: QUERY_KEY,
    queryFn: async () => {
      await resolveIdentity();
      return getCurrentIsAdmin();
    },
    staleTime: 30_000,
    // Seed from the already-resolved cache so first paint is correct when
    // identity resolved during boot (the common case).
    initialData: getCurrentIsAdmin,
  });
  // A probe that only succeeds after boot (transient failure, retried later)
  // publishes the flag here instead of waiting for the cache to go stale.
  useEffect(
    () => subscribeIdentity(() => queryClient.setQueryData(QUERY_KEY, getCurrentIsAdmin())),
    [queryClient],
  );
  return data;
}
