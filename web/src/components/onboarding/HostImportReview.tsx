// Wires the import modal to a real host: the reviewable dialog Settings opens,
// and the gate that opens it for a requested host, such as the one onboarding set up.

import { useEffect, useRef, useState } from "react";
import { ImportContextModal } from "@/components/onboarding/ImportContextModal";
import { Button } from "@/components/ui/button";
import { useHarnessInventory, type HarnessInventory } from "@/hooks/useHarnessInventory";
import { useHosts, type Host } from "@/hooks/useHosts";
import { resolveRunnerHost } from "@/hooks/useOnboardingRunnerHost";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { getHostIdentity, isElectronShell, takeOnboardingRunner } from "@/lib/nativeBridge";
import {
  clearImportReviewRequest,
  markImportsReviewed,
  requestImportReview,
  useImportReviewRequest,
  type ImportReviewTarget,
} from "@/lib/importReviewState";

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
      hostName={hostName}
      loadingMessage={loadingMessage}
    />
  );
}

/** The import modal for one host, loading its inventory only while open. */
export function HostImportsDialog({
  host,
  open,
  onOpenChange,
  showHostName = false,
}: {
  host: Host;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  showHostName?: boolean;
}) {
  const inventory = useHarnessInventory(host, { enabled: open });
  return (
    <InventoryModal
      hostId={host.host_id}
      hostName={showHostName ? host.name : undefined}
      inventory={inventory}
      open={open}
      onOpenChange={onOpenChange}
    />
  );
}

/**
 * Opens the import modal only for the host passed to `requestImportReview`,
 * e.g. the one desktop onboarding just connected; never on its own.
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

/** Requests a review of the runner onboarding handed over, once per page load. */
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
 * can be identified, and shows nothing if several hosts could be it.
 */
function RequestedImportReview({ target }: { target: ImportReviewTarget }) {
  const { data: hosts } = useHosts();
  const byRunner = target.hostId === undefined;
  const identity = useLocalIdentity(byRunner);
  // Once identified, the host stays put even if more hosts come online.
  const identified = useRef<string | null>(null);
  let hostId: string | null = target.hostId ?? identified.current;
  let ambiguous = false;
  if (byRunner && identity && identified.current === null) {
    if (target.runner === "local") {
      hostId = identity.hostId;
    } else {
      ({ hostId, ambiguous } = resolveRunnerHost("remote", identity.hostId, hosts));
    }
    identified.current = hostId;
  }
  // Without this machine's identity, "remote" could mistake it for the other host.
  const unresolvable =
    byRunner && (identity === null || (target.runner === "local" && identity?.hostId === null));
  useEffect(() => {
    if (ambiguous || unresolvable) clearImportReviewRequest();
  }, [ambiguous, unresolvable]);

  const host = hosts?.find((candidate) => candidate.host_id === hostId) ?? null;
  const inventory = useHarnessInventory(host, { awaitConnection: true });
  // Wait for the hosts and identity so an ambiguous remote never flashes open.
  if (byRunner && (hosts === undefined || identity === undefined)) return null;
  if (ambiguous || unresolvable) return null;
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

/** Settings rows that reopen the import modal for each online machine. */
export function ReviewImportsPanel() {
  const { data: hosts } = useHosts();
  const onlineHosts = (hosts ?? []).filter((host) => host.status === "online");
  const [openHostId, setOpenHostId] = useState<string | null>(null);
  const openHost = onlineHosts.find((host) => host.host_id === openHostId);

  if (onlineHosts.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        None of your machines are online. Start one with{" "}
        <code className="rounded bg-muted px-1 py-0.5 font-mono">omnigent host</code> to review what
        its harnesses bring over.
      </p>
    );
  }
  return (
    <>
      <ul className="flex flex-col">
        {onlineHosts.map((host) => (
          <li
            key={host.host_id}
            className="flex items-center justify-between gap-4 border-b border-border py-3 first:pt-0 last:border-b-0 last:pb-0"
          >
            <span className="min-w-0 truncate text-ui font-medium">{host.name}</span>
            <Button
              variant="outline"
              size="sm"
              componentId="settings.import.reviewImports"
              aria-label={`Review imports on ${host.name}`}
              onClick={() => setOpenHostId(host.host_id)}
            >
              Review imports
            </Button>
          </li>
        ))}
      </ul>
      {openHost && (
        <HostImportsDialog
          host={openHost}
          open
          onOpenChange={(open) => {
            if (!open) setOpenHostId(null);
          }}
          showHostName={onlineHosts.length > 1}
        />
      )}
    </>
  );
}
