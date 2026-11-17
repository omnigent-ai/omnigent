import { useEffect, useRef, useState } from "react";
import type { Host } from "@/hooks/useHosts";
import {
  getHostIdentity,
  isElectronShell,
  takeOnboardingRunner,
  type OnboardingRunner,
} from "@/lib/nativeBridge";

/**
 * The online host *runner* names, given this machine's host id: this machine
 * for "local", the only other online host for "remote". Several other online
 * hosts leave "remote" ambiguous, so it names none; `waiting` means no
 * candidate is online yet.
 */
export function resolveRunnerHost(
  runner: OnboardingRunner,
  localHostId: string | null,
  hosts: Host[] | undefined,
): { hostId: string | null; ambiguous: boolean; waiting: boolean } {
  const online = (hosts ?? []).filter((host) => host.status === "online");
  if (runner === "local") {
    const hostId = online.find((host) => host.host_id === localHostId)?.host_id ?? null;
    return { hostId, ambiguous: false, waiting: hostId === null };
  }
  const others = online.filter((host) => host.host_id !== localHostId);
  return {
    hostId: others.length === 1 ? others[0].host_id : null,
    ambiguous: others.length > 1,
    waiting: others.length === 0,
  };
}

/**
 * This machine's identity from the desktop shell, read once *enabled*:
 * undefined while the shell answers, null when it can't say. A null
 * `hostId` means this machine never hosted.
 */
export function useLocalHostIdentity(
  enabled: boolean,
): { hostId: string | null } | null | undefined {
  const [identity, setIdentity] = useState<{ hostId: string | null } | null | undefined>(undefined);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void getHostIdentity().then((result) => {
      if (!cancelled) setIdentity(result ? { hostId: result.hostId ?? null } : null);
    });
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return identity;
}

/** How long the new-session picker waits for the onboarding runner to come online. */
export const ONBOARDING_RUNNER_GRACE_MS = 30_000;

/**
 * The runner picked during desktop onboarding, resolved to an online host:
 * this machine's host for "local", the only other online host for "remote".
 * `pending` holds the picker's own default while the runner is still coming
 * online; after the grace period it gives up and the normal default applies.
 */
export function useOnboardingRunnerHost(hosts: Host[] | undefined): {
  pending: boolean;
  hostId: string | null;
} {
  // undefined = not asked yet; the shell hands the choice over only once.
  const [runner, setRunner] = useState<OnboardingRunner | null | undefined>(() =>
    isElectronShell() ? undefined : null,
  );
  const asked = useRef(false);
  useEffect(() => {
    if (asked.current || runner !== undefined) return;
    asked.current = true;
    void takeOnboardingRunner("hostPicker").then(setRunner, () => setRunner(null));
  }, [runner]);

  // Nothing resolves before the shell names this machine, or "remote" could
  // mistake this laptop for the other host.
  const identity = useLocalHostIdentity(Boolean(runner));
  // Without the identity at all, or for "local" without an id, give up.
  const unusable = identity === null || (runner === "local" && identity?.hostId === null);
  useEffect(() => {
    if (runner && unusable) setRunner(null);
  }, [runner, unusable]);

  // "remote" resolves only when exactly one other host is online; with several, the user picks.
  const resolved =
    runner && identity ? resolveRunnerHost(runner, identity.hostId, hosts) : undefined;
  const hostId = resolved?.hostId ?? null;

  // Stop waiting after the grace period, unless the runner already resolved.
  useEffect(() => {
    if (runner === null || hostId) return;
    const timer = setTimeout(() => setRunner(null), ONBOARDING_RUNNER_GRACE_MS);
    return () => clearTimeout(timer);
  }, [runner, hostId]);

  // Waits only while the runner may still appear; several candidates can't resolve.
  const pending =
    runner === undefined || (runner !== null && !identity) || resolved?.waiting === true;
  return { pending, hostId };
}
