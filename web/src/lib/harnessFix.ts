import type { QueryClient } from "@tanstack/react-query";
import type { HarnessAccount, Host } from "@/hooks/useHosts";
import { harnessReadinessOnHost, type HarnessCredentialKind } from "@/lib/harnessSetup";

export type HarnessFixMode = "reconfigure" | "update" | "install" | "switch";

export interface HarnessFixResult {
  configuredHarnesses: Record<string, boolean | string>;
  credentialKind?: HarnessCredentialKind;
  version?: string;
  gatewayInference?: Record<string, boolean> | null;
  harnessAccounts?: Record<string, HarnessAccount> | null;
}

/** Readiness of *harness* on *host*, reduced to what the grid and details show. */
export function harnessStatus(harness: string, host: Host | null) {
  const readiness = harnessReadinessOnHost(harness, host);
  return {
    ready: readiness.state === "available" && readiness.reason === "ready",
    // Only "setup-required" / "broken" are actionable. A host that reports no
    // readiness (older host → `readiness-unknown`) stays neutral: no badge, no
    // Set-up button, rather than a false "needs setup" on a working harness.
    needsSetup: readiness.state === "setup-required" || readiness.state === "broken",
    reason:
      readiness.reason === "ready" || readiness.reason === "readiness-unknown"
        ? null
        : readiness.reason,
  };
}

export function harnessFixMode(reason: string | null): HarnessFixMode {
  if (reason === "version-too-low") return "update";
  if (reason === "binary-missing" || reason === "unconfigured") return "install";
  return "reconfigure";
}

/** Apply confirmed readiness to every host-list variant used by the composer and Settings. */
export function applyHarnessFixResult(
  queryClient: QueryClient,
  hostId: string,
  { configuredHarnesses, harnessAccounts, gatewayInference }: HarnessFixResult,
): void {
  queryClient.setQueriesData<Host[]>({ queryKey: ["hosts"] }, (hosts) =>
    hosts?.map((host) =>
      host.host_id === hostId
        ? {
            ...host,
            configured_harnesses: configuredHarnesses,
            ...(harnessAccounts !== undefined ? { harness_accounts: harnessAccounts } : {}),
            ...(gatewayInference !== undefined ? { gateway_inference: gatewayInference } : {}),
          }
        : host,
    ),
  );
}
