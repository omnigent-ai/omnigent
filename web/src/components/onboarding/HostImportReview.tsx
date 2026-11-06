// Opens the import modal for a requested host, such as the one desktop onboarding set up.

import { useEffect, useRef, useState } from "react";
import { ImportContextModal } from "@/components/onboarding/ImportContextModal";
import { useHarnessInventory, type HarnessInventory } from "@/hooks/useHarnessInventory";
import { useHosts, type Host } from "@/hooks/useHosts";
import { resolveRunnerHost } from "@/hooks/useOnboardingRunnerHost";
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
import { getHostIdentity, isElectronShell, takeOnboardingRunner } from "@/lib/nativeBridge";

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

/**
 * This machine's identity for a runner-only request: undefined while the shell
 * answers, null when it can't say. A null `hostId` means it never hosted.
 */
function useLocalIdentity(enabled: boolean): { hostId: string | null } | null | undefined {
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
  const byRunner = target.hostId === undefined;
  const identity = useLocalIdentity(byRunner);
  // Once identified, the host stays put even if more hosts come online.
  const identified = useRef<{ hostId: string; reviewed: boolean } | null>(null);
  let ambiguous = false;
  if (byRunner && identified.current === null && identity && hosts !== undefined) {
    const resolved =
      target.runner === "local"
        ? { hostId: identity.hostId, ambiguous: false }
        : resolveRunnerHost("remote", identity.hostId, hosts);
    ambiguous = resolved.ambiguous;
    if (resolved.hostId !== null) {
      identified.current = { hostId: resolved.hostId, reviewed: importsReviewed(resolved.hostId) };
    }
  }
  const hostId = target.hostId ?? identified.current?.hostId ?? null;
  // Without this machine's identity, "remote" could mistake it for the other host.
  const unresolvable =
    byRunner && (identity === null || (target.runner === "local" && identity?.hostId === null));
  const skip = ambiguous || unresolvable || identified.current?.reviewed === true;
  useEffect(() => {
    if (skip) clearImportReviewRequest();
  }, [skip]);

  const host = hosts?.find((candidate) => candidate.host_id === hostId) ?? null;
  const inventory = useHarnessInventory(skip ? null : host, { awaitConnection: true });
  // Wait for the hosts and identity so an ambiguous remote never flashes open.
  if (byRunner && (hosts === undefined || identity === undefined)) return null;
  if (skip) return null;
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
