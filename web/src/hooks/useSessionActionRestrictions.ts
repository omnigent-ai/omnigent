import { useHosts } from "./useHosts";
import { useSession } from "./useSession";
import {
  SESSION_ACTIONS_LOADING,
  sessionActionRestrictions,
  type SessionActionSource,
} from "@/lib/sessionCapabilities";

export function useSessionActionRestrictions(
  sessionId: string | null | undefined,
  fallback?: SessionActionSource | null,
) {
  const { session, isLoading } = useSession(sessionId);
  const hostId = session?.hostId ?? fallback?.hostId ?? fallback?.host_id;
  const { data: hosts, isLoading: hostsLoading } = useHosts({
    includeSandbox: true,
    enabled: !!hostId,
  });
  const host = hosts?.find((candidate) => candidate.host_id === hostId);
  const restrictions = sessionActionRestrictions(session ?? fallback, host);
  const loading = (sessionId && isLoading) || (hostId && hostsLoading);
  return {
    forkDisabledReason:
      restrictions.forkDisabledReason ?? (loading ? SESSION_ACTIONS_LOADING : undefined),
    switchHostDisabledReason:
      restrictions.switchHostDisabledReason ?? (loading ? SESSION_ACTIONS_LOADING : undefined),
  };
}
