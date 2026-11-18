// Opens the import modal for a requested host, such as the one desktop onboarding set up.

import { useEffect, useRef, useState } from "react";
import { ImportContextModal } from "@/components/onboarding/ImportContextModal";
import { useHarnessInventory, type HarnessInventory } from "@/hooks/useHarnessInventory";
import { useHosts, type Host } from "@/hooks/useHosts";
import { resolveRunnerHost, useLocalHostIdentity } from "@/hooks/useOnboardingRunnerHost";
import {
  clearImportReviewRequest,
  importsReviewed,
  markImportsReviewed,
  requestImportReview,
  useImportReviewRequest,
  type ImportReviewTarget,
} from "@/lib/importReviewState";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isElectronShell, takeOnboardingRunner, type OnboardingRunner } from "@/lib/nativeBridge";

function InventoryModal({
  hostId,
  hostName,
  inventory,
  open,
  onOpenChange,
  loadingMessage,
}: {
  /** Null while a requested host isn't identified yet; nothing is marked reviewed. */
  hostId: string | null;
  /** Shown when the user has several machines. */
  hostName?: string;
  loadingMessage?: string;
  inventory: HarnessInventory;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <ImportContextModal
      open={open}
      onOpenChange={(next) => {
        // Confirm and dismiss both count as reviewed.
        if (!next && hostId !== null) markImportsReviewed(hostId);
        onOpenChange(next);
      }}
      onConfirm={() => {}}
      context={inventory.context}
      status={inventory.status}
      unavailable={inventory.unavailable}
      mcpUnsupported={inventory.mcpUnsupported}
      hostName={hostName}
      loadingMessage={loadingMessage}
    />
  );
}

/**
 * Opens the import modal only for the host passed to `requestImportReview`,
 * such as the one desktop onboarding just connected; never on its own.
 */
export function ImportReviewGate() {
  const info = useServerInfo();
  const target = useImportReviewRequest();
  if (!isFeatureEnabled(info, "import_review")) return null;
  return (
    <>
      <OnboardingImportReview />
      {target !== null && (
        <RequestedImportReview key={target.hostId ?? target.runner} target={target} />
      )}
    </>
  );
}

/** Requests a review of the runner desktop onboarding handed over, once per page load. */
function OnboardingImportReview() {
  const asked = useRef(false);
  useEffect(() => {
    if (asked.current || !isElectronShell()) return;
    asked.current = true;
    void takeOnboardingRunner("importReview").then((runner) => {
      if (runner) requestImportReview({ runner });
    });
  }, []);
  return null;
}

/** Where a runner-only review stands once the shell and host list answer. */
type RunnerReviewTarget =
  | { status: "loading" }
  | { status: "waiting" }
  | { status: "found"; hostId: string }
  | { status: "abandoned" };

function resolveReviewTarget(
  runner: OnboardingRunner,
  identity: { hostId: string | null } | null | undefined,
  hosts: Host[] | undefined,
): RunnerReviewTarget {
  if (identity === undefined || hosts === undefined) return { status: "loading" };
  // Without this machine's identity, "remote" could mistake it for the other host.
  if (identity === null) return { status: "abandoned" };
  // "local" names this machine even before it's online, so the review can wait for it.
  if (runner === "local") {
    return identity.hostId ? { status: "found", hostId: identity.hostId } : { status: "abandoned" };
  }
  // The shell hands "remote" over once Arca reports online, so a lone other host is Arca.
  const resolved = resolveRunnerHost("remote", identity.hostId, hosts);
  if (resolved.ambiguous) return { status: "abandoned" };
  return resolved.hostId ? { status: "found", hostId: resolved.hostId } : { status: "waiting" };
}

/** Loading copy while the target isn't online; the default once it's fetching inventory. */
function connectingMessage(host: Host | null, runner: ImportReviewTarget["runner"]) {
  if (host?.status === "online") return undefined;
  if (host) return `Connecting to ${host.name}…`;
  if (runner === "remote") return "Connecting to Arca…";
  if (runner === "local") return "Connecting this Mac…";
  return "Connecting…";
}

/**
 * Shows only the requested host, loading until it connects; never another host.
 * A runner-only request waits, without a time limit, until that runner's host
 * can be identified, and shows nothing if several hosts could be it or this
 * device already reviewed it.
 */
function RequestedImportReview({ target }: { target: ImportReviewTarget }) {
  const { data: hosts } = useHosts();
  const identity = useLocalHostIdentity(target.hostId === undefined);
  // The runner's host once found; it stays put even if more hosts come online.
  const [found, setFound] = useState<{ hostId: string; reviewed: boolean } | null>(null);
  const resolution =
    target.hostId === undefined && found === null
      ? resolveReviewTarget(target.runner, identity, hosts)
      : null;
  const resolvedHostId = resolution?.status === "found" ? resolution.hostId : null;
  useEffect(() => {
    if (resolvedHostId !== null) {
      setFound({ hostId: resolvedHostId, reviewed: importsReviewed(resolvedHostId) });
    }
  }, [resolvedHostId]);
  const abandoned = resolution?.status === "abandoned" || found?.reviewed === true;
  useEffect(() => {
    if (abandoned) clearImportReviewRequest();
  }, [abandoned]);

  const hostId = target.hostId ?? found?.hostId ?? null;
  const host = hosts?.find((candidate) => candidate.host_id === hostId) ?? null;
  const inventory = useHarnessInventory(abandoned ? null : host, { awaitConnection: true });
  // Opens once the host is settled or still connecting, so an ambiguous or
  // already reviewed host never flashes open.
  if (abandoned || resolution?.status === "loading" || resolution?.status === "found") return null;
  return (
    <InventoryModal
      hostId={hostId}
      hostName={host?.name}
      loadingMessage={connectingMessage(host, target.runner)}
      inventory={inventory}
      open
      onOpenChange={(next) => {
        if (!next) clearImportReviewRequest();
      }}
    />
  );
}
