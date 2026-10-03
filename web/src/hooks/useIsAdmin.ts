import { useQuery } from "@tanstack/react-query";
import { getOmnigentServerIdentity } from "@/lib/host";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";

// Mode-agnostic admin gate, sourced from the `/v1/me` identity probe
// (the shared `users.is_admin` column). Unlike `useMe`, which reads the
// accounts-only `/auth/me` endpoint, this works in EVERY auth mode —
// header, accounts, AND OIDC/SSO — so admin chrome (the Members /
// Policies settings sections) can surface under OIDC where `/auth/me`
// doesn't exist.
const QUERY_KEY = ["identity-is-admin"];

/**
 * Whether the current user is an admin, per `GET /v1/me`. Returns false
 * until identity resolves. Server enforces the flag on every admin route
 * regardless — this is chrome only.
 */
export function useIsAdmin(): boolean {
  // Identity is per-Server, and an embedded host can repoint the app at
  // another Server in place, so the cached flag is keyed by its Server.
  const serverIdentity = getOmnigentServerIdentity();
  const { data } = useQuery<boolean>({
    queryKey: [...QUERY_KEY, serverIdentity],
    queryFn: async () => {
      await resolveIdentity();
      return getCurrentIsAdmin();
    },
    // The seed below may predate the /v1/me answer, and the refetch is a free
    // read of the cached identity, so a mount never trusts it as fresh.
    staleTime: 0,
    // Seed from the already-resolved cache so first paint is correct when
    // identity resolved during boot (the common case).
    initialData: getCurrentIsAdmin,
  });
  return data;
}
