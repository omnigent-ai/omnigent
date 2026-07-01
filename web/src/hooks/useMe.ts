import { useQuery } from "@tanstack/react-query";
import { type CurrentAccount, getMe } from "@/lib/accountsApi";

// Shared cache key for the current account (GET /auth/me). Both the settings
// sidebar nav (to admin-gate the Members / Policies sub-categories) and the
// Settings page sections read it, so keying them off one query avoids a
// duplicate probe per mount.
const QUERY_KEY = ["auth-me"];

/**
 * Fetch the current account (accounts auth only). `enabled` gates the request
 * so non-accounts deploys — where `/auth/me` isn't meaningful — never fire it.
 * Returns `null` when unauthenticated. Cached briefly so admin gating is
 * instant across the sidebar + page without re-probing on every navigation.
 */
export function useMe(enabled = true) {
  return useQuery<CurrentAccount | null>({
    queryKey: QUERY_KEY,
    queryFn: getMe,
    enabled,
    staleTime: 30_000,
  });
}
