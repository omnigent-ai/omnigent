import { useQuery } from "@tanstack/react-query";
import { getConfiguredServer, getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";

// Mode-agnostic admin gate, sourced from the `/v1/me` identity probe
// (the shared `users.is_admin` column). Unlike `useMe`, which reads the
// accounts-only `/auth/me` endpoint, this works in EVERY auth mode —
// header, accounts, AND OIDC/SSO — so admin chrome (the Members /
// Policies settings sections) can surface under OIDC where `/auth/me`
// doesn't exist.
const QUERY_KEY = ["identity-is-admin"];

/** Cache key for one Server's admin flag; tests address the same entry through it. */
export function isAdminQueryKey(server: string | null): (string | null)[] {
  return [...QUERY_KEY, server];
}

/**
 * Whether the current user is an admin, per `GET /v1/me`. Returns false
 * until identity resolves. Server enforces the flag on every admin route
 * regardless — this is chrome only.
 */
export function useIsAdmin(): boolean {
  // Identity is per-Server, and an embedded host can repoint the app at
  // another Server in place (remounting it, which re-reads this), so the
  // cached flag is keyed by the Server identity.ts binds its cache to.
  const server = getConfiguredServer();
  const { data } = useQuery<boolean>({
    queryKey: isAdminQueryKey(server),
    queryFn: async () => {
      await resolveIdentity();
      // A switch mid-probe would file the new Server's flag under this key.
      if (getConfiguredServer() !== server) {
        throw new Error("host switched Servers during the identity probe");
      }
      return getCurrentIsAdmin();
    },
    // The probe never rejects on its own, so that switch is the only failure
    // and the remounted app re-reads under the new key.
    retry: false,
    // The seed below may predate the /v1/me answer, and the refetch is a free
    // read of the cached identity, so a mount never trusts it as fresh.
    staleTime: 0,
    // Seed from the already-resolved cache so first paint is correct when
    // identity resolved during boot (the common case).
    initialData: getCurrentIsAdmin,
  });
  return data;
}
