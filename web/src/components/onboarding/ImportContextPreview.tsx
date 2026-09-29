// Dev-only preview of the import modal with sample data: open the app with
// `?import-preview` to show it. Remove once the modal is wired to real discovery.

import { useState } from "react";
import { ImportContextModal } from "./ImportContextModal";
import { MOCK_IMPORT_CONTEXT } from "./importContextMock";

export default function ImportContextPreview() {
  const [open, setOpen] = useState(() =>
    new URLSearchParams(window.location.search).has("import-preview"),
  );
  if (!open) return null;
  return (
    <ImportContextModal
      open={open}
      onOpenChange={setOpen}
      context={MOCK_IMPORT_CONTEXT}
      onConfirm={(selection) => console.info("[import-preview] confirmed", selection)}
    />
  );
}
