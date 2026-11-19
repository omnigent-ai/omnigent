import { useState } from "react";
import {
  CircleCheckIcon,
  CircleDashedIcon,
  CircleDotIcon,
  ClipboardListIcon,
  EyeIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
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
import type { TicketStatus, TicketTag } from "@/lib/projectTicketsApi";
import { cn } from "@/lib/utils";

export function TicketStatusIcon({ status }: { status: TicketStatus }) {
  const Icon = {
    open: CircleDashedIcon,
    planning: ClipboardListIcon,
    in_progress: CircleDotIcon,
    review: EyeIcon,
    done: CircleCheckIcon,
  }[status];
  return (
    <Icon
      aria-hidden
      className={cn(
        "size-3.5 shrink-0",
        status === "done"
          ? "text-success"
          : status === "review"
            ? "text-warning"
            : status === "in_progress" || status === "planning"
              ? "text-session-active"
              : "text-muted-foreground",
      )}
    />
  );
}

export function TicketTags({ tags }: { tags: TicketTag[] }) {
  return (
    <div className="flex min-w-0 flex-wrap gap-1.5">
      {tags.map((tag) => (
        <Badge
          key={tag.key}
          variant="secondary"
          title={tag.value || undefined}
          className={cn(
            "h-auto min-h-5 max-w-full whitespace-normal text-left wrap-anywhere",
            tag.key === "needs-human" && "bg-warning/15 text-warning",
          )}
        >
          {tag.key === "needs-human"
            ? "Needs human input"
            : `${tag.key}${tag.value ? `: ${tag.value}` : ""}`}
        </Badge>
      ))}
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
}: TicketDialogProps & { approving?: boolean }) {
  return (
    <TicketTextDialog
      title={title}
      onClose={onClose}
      onComplete={onComplete}
      pending={pending}
      error={error}
      initialSummary={initialSummary}
      heading={approving ? "Approve & mark done" : "Mark ticket done"}
      label="Completion summary"
      submitLabel={approving ? "Approve & mark done" : "Mark done"}
      placeholder="What was delivered and verified?"
    />
  );
}

interface TicketDialogProps {
  title: string;
  onClose: () => void;
  onComplete: (summary: string) => void;
  pending: boolean;
  error: Error | null;
  initialSummary?: string;
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
}: TicketDialogProps & {
  heading: string;
  label: string;
  submitLabel: string;
  placeholder: string;
  maxLength?: number;
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
            if (pending || !summary.trim()) return;
            onComplete(summary.trim());
          }}
          className="space-y-4"
        >
          <DialogHeader>
            <DialogTitle>{heading}</DialogTitle>
            <DialogDescription>{title}</DialogDescription>
          </DialogHeader>
          <label className="grid gap-2 text-sm">
            {label}
            <Textarea
              autoFocus
              required
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
            <Button type="submit" loading={pending} disabled={!summary.trim()}>
              {submitLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
