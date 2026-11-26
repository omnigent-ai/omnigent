// Names the sidebar as it shows now, to bring it back from the sidebar's menu or ⌘K.

import { BookmarkPlusIcon } from "lucide-react";
import { type FormEvent, useEffect, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { SAVED_VIEW_NAME_LIMIT, type SaveViewResult } from "@/lib/sidebarSavedViews";

import { SAVE_VIEW_PROBLEM_TEXT } from "./savedViewActions";

export interface SaveSidebarViewDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Saves the sidebar as it is under `name`; the dialog closes once it's saved. */
  onSave: (name: string) => SaveViewResult;
}

export function SaveSidebarViewDialog({ open, onOpenChange, onSave }: SaveSidebarViewDialogProps) {
  const [name, setName] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const nameId = useId();
  const problemId = useId();
  // Each opening starts blank.
  useEffect(() => {
    if (!open) return;
    setName("");
    setProblem(null);
  }, [open]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    event.stopPropagation();
    const result = onSave(name);
    if (result.ok) {
      onOpenChange(false);
      return;
    }
    setProblem(SAVE_VIEW_PROBLEM_TEXT[result.problem]);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md" data-testid="save-sidebar-view-dialog">
        <form className="contents" onSubmit={submit}>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <BookmarkPlusIcon className="size-4" />
              <span>Save sidebar view</span>
            </DialogTitle>
            <DialogDescription>
              Keeps the sidebar's grouping, ordering, row details and filters under a name, on this
              device.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-muted-foreground" htmlFor={nameId}>
              Name
            </label>
            <Input
              id={nameId}
              value={name}
              onChange={(event) => {
                setName(event.currentTarget.value);
                setProblem(null);
              }}
              maxLength={SAVED_VIEW_NAME_LIMIT}
              placeholder="Open pull requests"
              autoFocus
              aria-invalid={problem !== null || undefined}
              aria-describedby={problem ? problemId : undefined}
              data-testid="save-sidebar-view-dialog-name"
              componentId="save_sidebar_view_dialog.name"
            />
            {problem && (
              <p id={problemId} role="alert" className="text-ui text-destructive">
                {problem}
              </p>
            )}
          </div>
          <DialogFooter>
            <Button
              type="submit"
              disabled={name.trim() === ""}
              data-testid="save-sidebar-view-dialog-save"
              componentId="save_sidebar_view_dialog.save"
            >
              Save view
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
