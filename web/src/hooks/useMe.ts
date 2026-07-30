import { useQuery } from "@tanstack/react-query";
import { type CurrentAccount, getMe } from "@/lib/accountsApi";

// Shared cache key for the current account (GET /auth/me). Consumers that read
// through this hook share one query, so admin gating is instant and re-mounts
// don't re-probe within the stale window. The settings sidebar nav uses it to
// admin-gate the Members / Policies sub-categories.
//
// NOTE: the MembersPage / PoliciesPage still probe via a direct getMe() call
// (their own loading / login-bounce state machine predates this hook), so they
// don't yet share this cache — deduping those is a possible follow-up.
const QUERY_KEY = ["auth-me"];

/**
 * Fetch the current account (accounts auth only). `enabled` gates the request
 * so non-accounts deploys — where `/auth/me` isn't meaningful — never fire it.
 * Returns `null` when unauthenticated. Cached briefly so admin gating is
 * instant across consumers of this hook without re-probing on every navigation.
 */
export function useMe(enabled = true) {
  return useQuery<CurrentAccount | null>({
    queryKey: QUERY_KEY,
    queryFn: getMe,
    enabled,
    staleTime: 30_000,
  });
}
