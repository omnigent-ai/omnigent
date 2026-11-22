import { useId } from "react";
import { Badge } from "@/components/ui/badge";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { NeedsResponseBadge } from "@/components/NeedsResponseBadge";
import type { TicketLabel } from "@/lib/projectTicketsApi";
import { cn } from "@/lib/utils";

export { TicketStageIcon } from "./TicketStageAppearance";

export function TicketLabels({
  labels,
  compact = false,
}: {
  labels: TicketLabel[];
  compact?: boolean;
}) {
  const ordered = compact
    ? [
        ...labels.filter((label) => label.key === "needs-human"),
        ...labels.filter((label) => label.key !== "needs-human"),
      ]
    : labels;
  const visible = compact ? ordered.slice(0, 2) : ordered;
  const hiddenCount = labels.length - visible.length;
  return (
    <div className={cn("flex min-w-0 gap-1.5", compact ? "items-center" : "flex-wrap")}>
      {visible.map((label) =>
        label.key === "needs-human" ? (
          <NeedsResponseBadge key={label.key} title={label.value || undefined} />
        ) : (
          <TicketLabelBadge key={label.key} label={label} compact={compact} />
        ),
      )}
      {hiddenCount > 0 && (
        <span
          className="shrink-0 text-xs text-muted-foreground tabular-nums"
          aria-label={`${hiddenCount} more ${hiddenCount === 1 ? "label" : "labels"}`}
        >
          +{hiddenCount}
        </span>
      )}
    </div>
  );
}

function TicketLabelBadge({ label, compact }: { label: TicketLabel; compact: boolean }) {
  const descriptionId = useId();
  const labelText = `${label.key}${label.value ? `: ${label.value}` : ""}`;
  const badge = (
    <Badge
      variant="secondary"
      title={compact ? undefined : label.value || undefined}
      tabIndex={compact ? 0 : undefined}
      aria-describedby={compact ? descriptionId : undefined}
      className={
        compact
          ? "h-5 min-w-0 max-w-36 shrink cursor-help rounded-md bg-muted/60 px-1.5 py-0 text-xs font-normal text-muted-foreground outline-none focus-visible:ring-1"
          : "h-auto min-h-5 max-w-full whitespace-normal text-left wrap-anywhere"
      }
    >
      <span className={compact ? "truncate" : undefined}>{labelText}</span>
    </Badge>
  );
  if (!compact) return badge;
  return (
    <HoverCard openDelay={150} closeDelay={100}>
      <HoverCardTrigger asChild>{badge}</HoverCardTrigger>
      <HoverCardContent
        id={descriptionId}
        side="bottom"
        align="start"
        sideOffset={8}
        collisionPadding={8}
        className="max-h-(--radix-hover-card-content-available-height) w-fit max-w-[min(18rem,var(--radix-hover-card-content-available-width))] overflow-y-auto whitespace-pre-wrap wrap-anywhere text-sm"
        onClick={(event) => event.stopPropagation()}
        onMouseDown={(event) => event.stopPropagation()}
        onTouchStart={(event) => event.stopPropagation()}
      >
        {labelText}
      </HoverCardContent>
    </HoverCard>
  );
}

export function TicketError({ error }: { error: Error | null }) {
  return error ? (
    <p role="alert" className="text-sm text-destructive">
      {error.message}
    </p>
  ) : null;
}
