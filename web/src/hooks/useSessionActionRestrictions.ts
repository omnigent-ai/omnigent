import { useHosts } from "./useHosts";
import { useSession } from "./useSession";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { sandboxProviderOptions } from "@/lib/capabilities";
import {
  SANDBOX_FORK_UNSUPPORTED,
  SESSION_ACTIONS_LOADING,
  SESSION_ACTIONS_UNAVAILABLE,
  sessionActionRestrictions,
  type SessionActionRestrictions,
  type SessionActionSource,
} from "@/lib/sessionCapabilities";

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
  const info = useServerInfo();
  // A same-provider fork needs this server to offer a fresh sandbox from that provider.
  const { forkSandboxProvider } = restrictions;
  let forkTargetReason: string | undefined;
  if (forkSandboxProvider !== undefined) {
    if (info === "loading") forkTargetReason = SESSION_ACTIONS_LOADING;
    else if (
      !info.managed_sandboxes_enabled ||
      !sandboxProviderOptions(info).includes(forkSandboxProvider)
    ) {
      forkTargetReason = SANDBOX_FORK_UNSUPPORTED;
    }
  }
  const loading = Boolean(sessionId && isLoading) || Boolean(hostId && hostsLoading);
  // Cached lists can omit shared hosts without making supported forks unavailable.
  const lookupFailed =
    Boolean(sessionId && !session && error) || Boolean(hostId && !hosts && hostsError);
  let lookupReason: string | undefined;
  if (lookupFailed) lookupReason = SESSION_ACTIONS_UNAVAILABLE;
  else if (loading) lookupReason = SESSION_ACTIONS_LOADING;
  const hostMissing = Boolean(hostId && hosts && !host);
  // A managed source's provider decides whether it forks, so wait for its host.
  const hostPending = Boolean(hostId && hostsLoading);
  return {
    forkDisabledReason: hostPending
      ? lookupReason
      : (restrictions.forkDisabledReason ?? forkTargetReason ?? lookupReason),
    forkSandboxProvider,
    switchHostDisabledReason:
      restrictions.switchHostDisabledReason ??
      lookupReason ??
      (hostMissing ? SESSION_ACTIONS_UNAVAILABLE : undefined),
  };
}
