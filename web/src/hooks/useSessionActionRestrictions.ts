import { useHosts } from "./useHosts";
import { useSession } from "./useSession";
import {
  SESSION_ACTIONS_LOADING,
  SESSION_ACTIONS_UNAVAILABLE,
  sessionActionRestrictions,
  type SessionActionSource,
} from "@/lib/sessionCapabilities";

export interface SessionActionRestrictions {
  forkDisabledReason?: string;
  switchHostDisabledReason?: string;
}

export function useSessionActionRestrictions(
  sessionId: string | null | undefined,
  fallback?: SessionActionSource | null,
): SessionActionRestrictions {
  const { session, isLoading, error } = useSession(sessionId);
  const hostId = session ? session.hostId : (fallback?.hostId ?? fallback?.host_id);
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
  // Fork needs only the session; the host record matters only to switching.
  let sessionLookupReason: string | undefined;
  if (sessionId && !session && error) sessionLookupReason = SESSION_ACTIONS_UNAVAILABLE;
  else if (sessionId && isLoading) sessionLookupReason = SESSION_ACTIONS_LOADING;
  // Cached lists can omit shared hosts without making supported actions unavailable.
  let lookupReason = sessionLookupReason;
  if (lookupReason === undefined && hostId && !hosts && hostsError) {
    lookupReason = SESSION_ACTIONS_UNAVAILABLE;
  } else if (lookupReason === undefined && hostId && hostsLoading) {
    lookupReason = SESSION_ACTIONS_LOADING;
  }
  const hostMissing = Boolean(hostId && hosts && !host);
  return {
    forkDisabledReason: sessionLookupReason,
    switchHostDisabledReason:
      restrictions.switchHostDisabledReason ??
      lookupReason ??
      (hostMissing ? SESSION_ACTIONS_UNAVAILABLE : undefined),
  };
}
