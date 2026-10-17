import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { cancelUpload, retryUpload } from "@/lib/workspaceUploadController";
import { useUploadStore } from "@/store/uploadStore";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

export function UploadConflicts() {
  const allRows = useUploadStore((state) => state.rows);
  const client = useQueryClient();
  const [applyToAll, setApplyToAll] = useState(true);
  const rows = Object.values(allRows);
  const first = rows.find(
    (row) =>
      row.status === "conflict" &&
      !rows.some(
        (other) => other.batch === row.batch && ["queued", "connecting"].includes(other.status),
      ),
  );
  const conflicts = first
    ? rows.filter((row) => row.batch === first.batch && row.status === "conflict")
    : [];
  const decide = (choice: "replace" | "rename" | "skip") => {
    for (const row of applyToAll ? conflicts : conflicts.slice(0, 1)) {
      if (choice === "skip") void cancelUpload(row.key);
      else void retryUpload(row.key, client, choice);
    }
  };
  return (
    <Dialog
      open={conflicts.length > 0}
      onOpenChange={(open) => {
        if (!open) decide("skip");
      }}
    >
      <DialogContent showCloseButton={false}>
        <DialogTitle>Files already exist</DialogTitle>
        <DialogDescription>
          {conflicts.length} {conflicts.length === 1 ? "file has" : "files have"} the same name in
          the destination. Choose how to upload {conflicts.length === 1 ? "it" : "them"}.
        </DialogDescription>
        <ul className="max-h-40 overflow-auto text-ui">
          {conflicts.map((row) => (
            <li key={row.key} className="truncate" title={row.name}>
              {row.name}
            </li>
          ))}
        </ul>
        <label className="flex items-center gap-2 text-ui">
          <input
            type="checkbox"
            checked={applyToAll}
            onChange={(event) => setApplyToAll(event.target.checked)}
          />
          Apply to all
        </label>
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => decide("replace")}>Replace</Button>
          <Button variant="secondary" onClick={() => decide("rename")}>
            Keep both
          </Button>
          <Button variant="ghost" onClick={() => decide("skip")}>
            Skip
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
