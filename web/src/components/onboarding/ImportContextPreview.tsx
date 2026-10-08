// Dev-only preview of the import modal with sample data: open the app with
// `?import-preview` to show it, or `?import-preview=loading|offline|partial`.

import { useState } from "react";
import type { HarnessInventoryStatus } from "@/hooks/useHarnessInventory";
import { ImportContextModal } from "./ImportContextModal";
import { MOCK_IMPORT_CONTEXT } from "./importContextMock";

export default function ImportContextPreview() {
  const [variant] = useState(() =>
    new URLSearchParams(window.location.search).get("import-preview"),
  );
  const [open, setOpen] = useState(variant !== null);
  if (!open) return null;
  const status: HarnessInventoryStatus =
    variant === "loading" || variant === "offline" ? variant : "ready";
  return (
    <ImportContextModal
      open={open}
      onOpenChange={setOpen}
      context={variant === "partial" ? { ...MOCK_IMPORT_CONTEXT, mcps: [] } : MOCK_IMPORT_CONTEXT}
      status={status}
      hostName="dev-laptop"
      unavailable={variant === "partial" ? ["mcps"] : []}
      onConfirm={() => console.info("[import-preview] confirmed")}
    />
  );
}
