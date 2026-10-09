// Opens the import modal for a requested host or once per newly connected host.

import { useEffect, useState } from "react";
import { ImportContextModal } from "@/components/onboarding/ImportContextModal";
import { useHarnessInventory, type HarnessInventory } from "@/hooks/useHarnessInventory";
import { useHosts, type Host } from "@/hooks/useHosts";
import {
  clearImportReviewRequest,
  importsReviewed,
  markImportsReviewed,
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
  hostId: string;
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
        if (!next) markImportsReviewed(hostId);
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
 * Opens the import modal for the host passed to `requestImportReview`, or
 * otherwise the first time this device sees an online, user-connected host
 * whose harnesses bring MCPs, skills, or plugins.
 */
export function ImportReviewGate() {
  const target = useImportReviewRequest();
  if (target !== null) {
    return <RequestedImportReview key={target.hostId} target={target} />;
  }
  return <NewHostImportReview />;
}

/** Loading copy while the target isn't online; the default once it's fetching inventory. */
function connectingMessage(host: Host | null, runner: ImportReviewTarget["runner"]) {
  if (host?.status === "online") return undefined;
  if (host) return `Connecting to ${host.name}…`;
  if (runner === "remote") return "Connecting to Arca…";
  if (runner === "local") return "Connecting this Mac…";
  return "Connecting…";
}

/** Shows only the requested host, loading until it connects; never another host. */
function RequestedImportReview({ target }: { target: ImportReviewTarget }) {
  const { data: hosts } = useHosts();
  const host = hosts?.find((candidate) => candidate.host_id === target.hostId) ?? null;
  const inventory = useHarnessInventory(host, { awaitConnection: true });
  return (
    <InventoryModal
      hostId={target.hostId}
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

function NewHostImportReview() {
  const { data: hosts } = useHosts();
  const [openHostId, setOpenHostId] = useState<string | null>(null);
  // Hosts with nothing to show this session; they're rechecked on the next load.
  const [emptyHostIds, setEmptyHostIds] = useState<ReadonlySet<string>>(() => new Set());
  // An open host keeps the modal even after it's marked reviewed; otherwise
  // pick the first online host this device hasn't reviewed.
  const candidate =
    (openHostId !== null
      ? hosts?.find((host) => host.host_id === openHostId)
      : hosts?.find(
          (host) =>
            host.status === "online" &&
            !emptyHostIds.has(host.host_id) &&
            !importsReviewed(host.host_id),
        )) ?? null;
  const inventory = useHarnessInventory(candidate);
  const settled = candidate !== null && inventory.status === "ready";

  useEffect(() => {
    if (openHostId !== null) {
      if (candidate === null) setOpenHostId(null);
    } else if (settled && inventory.isEmpty) {
      setEmptyHostIds((ids) => new Set(ids).add(candidate.host_id));
    } else if (settled) {
      setOpenHostId(candidate.host_id);
    }
  }, [openHostId, settled, inventory.isEmpty, candidate]);

  if (candidate === null || openHostId === null) return null;
  return (
    <InventoryModal
      hostId={candidate.host_id}
      hostName={(hosts?.length ?? 0) > 1 ? candidate.name : undefined}
      inventory={inventory}
      open
      onOpenChange={(next) => {
        if (!next) setOpenHostId(null);
      }}
    />
  );
}
