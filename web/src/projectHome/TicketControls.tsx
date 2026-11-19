import { useState } from "react";
import { CircleCheckIcon, CircleDashedIcon, CircleDotIcon } from "lucide-react";
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
  const Icon =
    status === "done"
      ? CircleCheckIcon
      : status === "in_progress"
        ? CircleDotIcon
        : CircleDashedIcon;
  return (
    <Icon
      aria-hidden
      className={cn(
        "size-3.5 shrink-0",
        status === "done"
          ? "text-success"
          : status === "in_progress"
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
}: {
  title: string;
  onClose: () => void;
  onComplete: (summary: string) => void;
  pending: boolean;
  error: Error | null;
}) {
  const [summary, setSummary] = useState("");
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
            onComplete(summary.trim());
          }}
          className="space-y-4"
        >
          <DialogHeader>
            <DialogTitle>Mark ticket done</DialogTitle>
            <DialogDescription>{title}</DialogDescription>
          </DialogHeader>
          <label className="grid gap-2 text-sm">
            Completion summary
            <Textarea
              autoFocus
              required
              maxLength={1500}
              value={summary}
              rows={5}
              placeholder="What was delivered and verified?"
              onChange={(event) => setSummary(event.target.value)}
            />
          </label>
          <TicketError error={error} />
          <DialogFooter>
            <Button type="button" variant="ghost" disabled={pending} onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={pending} disabled={!summary.trim()}>
              Mark done
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
