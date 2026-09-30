// Wires the import modal to a real host: the reviewable dialog Settings opens,
// and the gate that opens it for a requested host or once per newly connected host.

import { useEffect, useState } from "react";
import { ImportContextModal } from "@/components/onboarding/ImportContextModal";
import { Button } from "@/components/ui/button";
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
