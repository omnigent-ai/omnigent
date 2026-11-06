import { useQuery } from "@tanstack/react-query";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";

// Effective admin access from `/v1/me`, shared across auth providers.
const QUERY_KEY = ["identity-is-admin"];

/**
 * Whether the current user has active admin access. Returns false
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
    // Preserve cached access on first paint while still awaiting pending identity.
    placeholderData: getCurrentIsAdmin,
  });
  return data ?? false;
}
