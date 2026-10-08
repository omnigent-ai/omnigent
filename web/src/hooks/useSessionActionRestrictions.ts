import { useHosts } from "./useHosts";
import { useSession } from "./useSession";
import {
  SESSION_ACTIONS_LOADING,
  SESSION_ACTIONS_UNAVAILABLE,
  sessionActionRestrictions,
  type SessionActionSource,
} from "@/lib/sessionCapabilities";

export function useSessionActionRestrictions(
  sessionId: string | null | undefined,
  fallback?: SessionActionSource | null,
) {
  const { session, isLoading, error } = useSession(sessionId);
  const hostId = session?.hostId ?? fallback?.hostId ?? fallback?.host_id;
  const {
    data: hosts,
    isLoading: hostsLoading,
    error: hostsError,
  } = useHosts({
    includeSandbox: true,
    enabled: !!hostId,
  });
  const host = hosts?.find((candidate) => candidate.host_id === hostId);
  const restrictions = sessionActionRestrictions(session ?? fallback, host);
  const loading = Boolean(sessionId && isLoading) || Boolean(hostId && hostsLoading);
  // Successful host lists can omit shared hosts; their snapshot carries the managed label.
  const lookupFailed =
    Boolean(sessionId && !session && error) || Boolean(hostId && !hosts && hostsError);
  const lookupReason = lookupFailed
    ? SESSION_ACTIONS_UNAVAILABLE
    : loading
      ? SESSION_ACTIONS_LOADING
      : undefined;
  return {
    forkDisabledReason: restrictions.forkDisabledReason ?? lookupReason,
    switchHostDisabledReason: restrictions.switchHostDisabledReason ?? lookupReason,
  };
}
