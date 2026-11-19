import { useState } from "react";
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
import type { TicketTag } from "@/lib/projectTicketsApi";

export function TicketTags({ tags }: { tags: TicketTag[] }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {tags.map((tag) => (
        <Badge
          key={tag.key}
          variant="secondary"
          className={tag.key === "needs-human" ? "bg-warning/15 text-warning" : undefined}
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
