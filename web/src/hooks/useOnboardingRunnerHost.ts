import { useEffect, useRef, useState } from "react";
import type { Host } from "@/hooks/useHosts";
import { getHostIdentity, isElectronShell, takeOnboardingRunner } from "@/lib/nativeBridge";

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
  const [runner, setRunner] = useState<"local" | "remote" | null | undefined>(() =>
    isElectronShell() ? undefined : null,
  );
  const asked = useRef(false);
  useEffect(() => {
    if (asked.current || runner !== undefined) return;
    asked.current = true;
    void takeOnboardingRunner().then(setRunner, () => setRunner(null));
  }, [runner]);

  // This machine's host id, undefined until the shell answers. Nothing resolves
  // before then, or "remote" could mistake this laptop for the other host.
  const [localHostId, setLocalHostId] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    if (!runner) return;
    let cancelled = false;
    void getHostIdentity().then((identity) => {
      if (cancelled) return;
      // A null hostId means this machine never hosted, so it can't be the remote
      // host. Without the identity at all, or for "local" without an id, give up.
      if (identity === null || (runner === "local" && !identity.hostId)) setRunner(null);
      else setLocalHostId(identity.hostId ?? null);
    });
    return () => {
      cancelled = true;
    };
  }, [runner]);
  const identityKnown = localHostId !== undefined;

  const online = (hosts ?? []).filter((h) => h.status === "online");
  // "remote" resolves only when exactly one other host is online; with several, the user picks.
  const others = online.filter((h) => h.host_id !== localHostId);
  let hostId: string | null = null;
  if (runner === "local" && identityKnown) {
    hostId = online.find((h) => h.host_id === localHostId)?.host_id ?? null;
  } else if (runner === "remote" && identityKnown && others.length === 1) {
    hostId = others[0].host_id;
  }

  // Stop waiting after the grace period, unless the runner already resolved.
  useEffect(() => {
    if (runner === null || hostId) return;
    const timer = setTimeout(() => setRunner(null), ONBOARDING_RUNNER_GRACE_MS);
    return () => clearTimeout(timer);
  }, [runner, hostId]);

  // Waits only while the runner may still appear; several candidates can't resolve.
  const pending =
    runner === undefined ||
    (runner !== null && !identityKnown) ||
    (runner === "local" && hostId === null) ||
    (runner === "remote" && others.length === 0);
  return { pending, hostId };
}
