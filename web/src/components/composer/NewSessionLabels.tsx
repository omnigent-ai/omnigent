// Labels for a session not created yet: chips in the new-session composer and
// the editor the composer's "+" menu opens. Project defaults come pre-filled.

import { useRef } from "react";
import { TagIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ComposerChipRow } from "@/components/composer/ChatComposer";
import { LabelEditor, type LabelEditorHandle } from "@/components/LabelEditor";
import { userLabelEntries } from "@/lib/sessionLabels";

/** One chip per label the new session will start with; a chip opens the editor. */
export function NewSessionLabelChips({
  labels,
  onEdit,
  onRemove,
}: {
  labels: Record<string, string>;
  onEdit: () => void;
  onRemove: (key: string) => void;
}) {
  const entries = userLabelEntries(labels);
  if (entries.length === 0) return null;
  return (
    <ComposerChipRow className="gap-1.5" data-testid="new-session-label-chips">
      {entries.map(([key, value]) => (
        <span
          key={key}
          className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-sm text-muted-foreground"
        >
          <button
            type="button"
            onClick={onEdit}
            className="flex min-w-0 items-center gap-1 hover:text-foreground"
            aria-label={`Edit labels (${key}: ${value})`}
          >
            <TagIcon className="size-3 shrink-0" />
            <span className="max-w-[200px] truncate font-mono" title={`${key}: ${value}`}>
              {key}: {value}
            </span>
          </button>
          <button
            type="button"
            onClick={() => onRemove(key)}
            className="ml-0.5 rounded-full hover:text-foreground"
            aria-label={`Remove label ${key}`}
          >
            <XIcon className="size-3" />
          </button>
        </span>
      ))}
    </ComposerChipRow>
  );
}

/** Edit the new session's labels; a valid label left in the add or edit row is kept on close. */
export function NewSessionLabelsDialog({
  open,
  onOpenChange,
  labels,
  projectDefaults,
  projectName,
  onSet,
  onRemove,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  labels: Record<string, string>;
  projectDefaults: Record<string, string>;
  projectName: string;
  onSet: (key: string, value: string) => void;
  onRemove: (key: string) => void;
}) {
  const editorRef = useRef<LabelEditorHandle>(null);
  const close = () => {
    const pending = editorRef.current?.takePendingLabel() ?? { status: "none" };
    if (pending.status === "invalid") return;
    if (pending.status === "valid") onSet(pending.key, pending.value);
    onOpenChange(false);
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (next ? onOpenChange(true) : close())}>
      <DialogContent
        className="flex max-h-[85vh] flex-col sm:max-w-md"
        data-testid="new-session-labels-dialog"
      >
        <DialogHeader>
          <DialogTitle>Labels</DialogTitle>
          <DialogDescription>
            {projectName
              ? `Added to the new session. ${projectName}'s default labels are filled in; removing one leaves it off this session only.`
              : "Added to the new session."}
          </DialogDescription>
        </DialogHeader>
        <div className="-mx-1 min-h-0 flex-1 overflow-y-auto px-1">
          <LabelEditor
            labels={labels}
            onSet={onSet}
            onRemove={onRemove}
            emptyText="No labels"
            testIdPrefix="new-session-labels-editor"
            handleRef={editorRef}
            rowNote={(key, value) => (projectDefaults[key] === value ? "from project" : null)}
          />
        </div>
        <DialogFooter className="border-t-0 bg-transparent">
          <Button type="button" onClick={close} data-testid="new-session-labels-done">
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
