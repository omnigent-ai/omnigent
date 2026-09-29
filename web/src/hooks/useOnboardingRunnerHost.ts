import { useEffect, useRef, useState } from "react";
import type { Host } from "@/hooks/useHosts";
import { isElectronShell, takeOnboardingRunner } from "@/lib/nativeBridge";

/** How long the new-session picker waits for the onboarding runner to come online. */
export const ONBOARDING_RUNNER_GRACE_MS = 30_000;

/**
 * The runner picked during desktop onboarding, resolved to an online host:
 * this machine's host for "local", the only other online host for "remote".
 * `pending` holds the picker's own default while the runner is still coming
 * online; after the grace period it gives up and the normal default applies.
 */
export function useOnboardingRunnerHost(
  hosts: Host[] | undefined,
  localHostId: string | null | undefined,
): { pending: boolean; hostId: string | null } {
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

  const online = (hosts ?? []).filter((h) => h.status === "online");
  // "remote" resolves only when exactly one other host is online; with several, the user picks.
  const others = online.filter((h) => h.host_id !== localHostId);
  let hostId: string | null = null;
  if (runner === "local") {
    hostId = online.find((h) => h.host_id === localHostId)?.host_id ?? null;
  } else if (runner === "remote" && others.length === 1) {
    hostId = others[0].host_id;
  }

  // Stop waiting after the grace period, unless the runner already resolved.
  useEffect(() => {
    if (!runner || hostId) return;
    const timer = setTimeout(() => setRunner(null), ONBOARDING_RUNNER_GRACE_MS);
    return () => clearTimeout(timer);
  }, [runner, hostId]);

  // Waits only while the runner may still appear; several candidates can't resolve.
  const pending =
    runner === undefined ||
    (runner === "local" && hostId === null) ||
    (runner === "remote" && others.length === 0);
  return { pending, hostId };
}
