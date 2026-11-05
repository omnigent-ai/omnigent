import { useQuery } from "@tanstack/react-query";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";

// The `/v1/me` identity probe reads `users.is_admin` in every auth mode,
// including OIDC/SSO, to gate the Members and Policies settings sections.
const QUERY_KEY = ["identity-is-admin"];

/**
 * Whether the current user is an admin, per `GET /v1/me`. Returns false
 * until identity resolves. Cached briefly so gating is instant across
 * consumers without re-probing on every navigation. Server enforces the
 * flag on every admin route regardless — this is chrome only.
 */
export function useIsAdmin(): boolean {
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
  return data;
}
