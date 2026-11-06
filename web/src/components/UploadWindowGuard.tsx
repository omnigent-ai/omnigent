import { useEffect, useRef } from "react";
import { toast } from "sonner";
import { useViewerId } from "@/hooks/useViewerId";
import { discardUploads } from "@/lib/workspaceUploadController";
import {
  isActiveUpload,
  ownerKey,
  uploadTotals,
  useUploadStore,
  type UploadRow,
} from "@/store/uploadStore";
import { UploadConflicts } from "@/shell/UploadConflicts";
import { formatBytes } from "@/shell/fileStatusUtils";

export function installUploadWindowGuard(target: Document): () => void {
  const guard = (event: DragEvent) => {
    if (!Array.from(event.dataTransfer?.types ?? []).includes("Files")) return;
    event.preventDefault();
    if (event.type === "drop" && event.target instanceof Element) {
      const disabled = event.target.closest<HTMLElement>("[data-upload-disabled-reason]");
      if (disabled?.dataset.uploadDisabledReason)
        toast.error(disabled.dataset.uploadDisabledReason);
    }
  };
  target.addEventListener("dragover", guard);
  target.addEventListener("drop", guard);
  return () => {
    target.removeEventListener("dragover", guard);
    target.removeEventListener("drop", guard);
  };
}

export function UploadWindowGuard() {
  const identity = useViewerId();
  const announcement = useUploadStore((state) => state.announcement);
  const rows = useUploadStore((state) => state.rows);
  const visibleTrays = useUploadStore((state) => state.visibleTrays);
  const toastIds = useRef(new Set<string>());
  useEffect(() => {
    const groups = new Map<string, UploadRow[]>();
    for (const row of Object.values(rows)) {
      if (row.identity !== identity || row.origin !== window.location.origin) continue;
      const key = ownerKey(row);
      groups.set(key, [...(groups.get(key) ?? []), row]);
    }
    const activeIds = new Set<string>();
    for (const [key, group] of groups) {
      const totals = uploadTotals(group);
      if (!totals.active || visibleTrays[key]) continue;
      const id = `workspace-uploads:${key}`;
      activeIds.add(id);
      toast(
        `Uploads: ${totals.done}/${group.length} ${group.length === 1 ? "file" : "files"} · ${formatBytes(totals.acknowledged)} / ${formatBytes(totals.total)}`,
        { id, duration: Infinity },
      );
    }
    for (const id of toastIds.current) if (!activeIds.has(id)) toast.dismiss(id);
    toastIds.current = activeIds;
  }, [rows, visibleTrays, identity]);
  useEffect(() => installUploadWindowGuard(document), []);
  useEffect(() => {
    if (identity === null) return;
    const origin = window.location.origin;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (!Object.values(useUploadStore.getState().rows).some(isActiveUpload)) return;
      event.preventDefault();
      event.returnValue = "";
    };
    const discard = () => discardUploads(origin, identity);
    window.addEventListener("beforeunload", beforeUnload);
    window.addEventListener("pagehide", discard);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      window.removeEventListener("pagehide", discard);
      discard();
    };
  }, [identity]);
  return (
    <>
      <div aria-live="polite" role="status" className="sr-only">
        {announcement}
      </div>
      <UploadConflicts />
    </>
  );
}
