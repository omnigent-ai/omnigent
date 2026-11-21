import { useState, type ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { NeedsResponseBadge } from "@/components/NeedsResponseBadge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import type { TicketStage, TicketTag } from "@/lib/projectTicketsApi";

export { TicketStageIcon } from "./TicketStageAppearance";

export function TicketTags({ tags }: { tags: TicketTag[] }) {
  return (
    <div className="flex min-w-0 flex-wrap gap-1.5">
      {tags.map((tag) =>
        tag.key === "needs-human" ? (
          <NeedsResponseBadge key={tag.key} title={tag.value || undefined} />
        ) : (
          <Badge
            key={tag.key}
            variant="secondary"
            title={tag.value || undefined}
            className="h-auto min-h-5 max-w-full whitespace-normal text-left wrap-anywhere"
          >
            {`${tag.key}${tag.value ? `: ${tag.value}` : ""}`}
          </Badge>
        ),
      )}
    </div>
  );
}

export function TicketError({ error }: { error: Error | null }) {
  return error ? (
    <p role="alert" className="text-sm text-destructive">
      {error.message}
    </p>
  ) : null;
}

export function CompleteTicketDialog({
  title,
  onClose,
  onComplete,
  pending,
  error,
  approving = false,
  initialSummary = "",
  stages,
  initialStageId,
  required = true,
}: Omit<TicketDialogProps, "onComplete"> & {
  approving?: boolean;
  stages: TicketStage[];
  initialStageId?: string;
  onComplete: (summary: string, stageId: string) => void;
}) {
  const stage = stages.find(
    (candidate) =>
      candidate.is_complete && (initialStageId === undefined || candidate.id === initialStageId),
  );
  return (
    <TicketTextDialog
      title={title}
      onClose={onClose}
      onComplete={(summary) => {
        if (stage) onComplete(summary, stage.id);
      }}
      pending={pending}
      error={error}
      initialSummary={initialSummary}
      required={required}
      heading={approving ? "Approve & complete" : "Complete ticket"}
      label="Completion summary"
      submitLabel={approving ? "Approve & complete" : "Complete ticket"}
      placeholder={required ? "What was delivered and verified?" : "Optional completion summary…"}
      disabled={!stage}
    >
      <p className="text-xs text-muted-foreground">
        This updates the ticket only. Needs response stays unchanged; answer open agent prompts in
        their own chat.
      </p>
    </TicketTextDialog>
  );
}

interface TicketDialogProps {
  title: string;
  onClose: () => void;
  onComplete: (summary: string) => void;
  pending: boolean;
  error: Error | null;
  initialSummary?: string;
  required?: boolean;
}

export function ReviewTicketDialog(props: TicketDialogProps) {
  return (
    <TicketTextDialog
      {...props}
      heading="Ready for review"
      label="Review brief"
      submitLabel="Send for review"
      placeholder="What changed? What did you verify? Link evidence and explain exactly what the reviewer should check."
    />
  );
}

export function RequestTicketChangesDialog(props: TicketDialogProps) {
  return (
    <TicketTextDialog
      {...props}
      heading="Request changes"
      label="Feedback"
      submitLabel="Request changes"
      maxLength={2000}
      placeholder="What needs to change before this ticket can be accepted?"
    />
  );
}

function TicketTextDialog({
  title,
  onClose,
  onComplete,
  pending,
  error,
  initialSummary = "",
  heading,
  label,
  submitLabel,
  placeholder,
  maxLength = 1500,
  children,
  disabled = false,
  required = true,
}: TicketDialogProps & {
  heading: string;
  label: string;
  submitLabel: string;
  placeholder: string;
  maxLength?: number;
  children?: ReactNode;
  disabled?: boolean;
}) {
  const [summary, setSummary] = useState(initialSummary);
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !pending) onClose();
      }}
    >
      <DialogContent>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (pending || disabled || (required && !summary.trim())) return;
            onComplete(summary.trim());
          }}
          className="space-y-4"
        >
          <DialogHeader>
            <DialogTitle>{heading}</DialogTitle>
            <DialogDescription>{title}</DialogDescription>
          </DialogHeader>
          {children}
          <label className="grid gap-2 text-sm">
            {label}
            <Textarea
              autoFocus
              required={required}
              maxLength={maxLength}
              value={summary}
              disabled={pending}
              rows={5}
              placeholder={placeholder}
              onChange={(event) => setSummary(event.target.value)}
            />
          </label>
          <TicketError error={error} />
          <DialogFooter>
            <Button type="button" variant="ghost" disabled={pending} onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              loading={pending}
              disabled={disabled || (required && !summary.trim())}
            >
              {submitLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
