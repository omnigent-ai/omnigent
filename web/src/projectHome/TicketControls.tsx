import { useId } from "react";
import { Badge } from "@/components/ui/badge";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { NeedsResponseBadge } from "@/components/NeedsResponseBadge";
import type { TicketTag } from "@/lib/projectTicketsApi";
import { cn } from "@/lib/utils";

export { TicketStageIcon } from "./TicketStageAppearance";

export function TicketTags({ tags, compact = false }: { tags: TicketTag[]; compact?: boolean }) {
  const ordered = compact
    ? [
        ...tags.filter((tag) => tag.key === "needs-human"),
        ...tags.filter((tag) => tag.key !== "needs-human"),
      ]
    : tags;
  const visible = compact ? ordered.slice(0, 2) : ordered;
  const hiddenCount = tags.length - visible.length;
  return (
    <div className={cn("flex min-w-0 gap-1.5", compact ? "items-center" : "flex-wrap")}>
      {visible.map((tag) =>
        tag.key === "needs-human" ? (
          <NeedsResponseBadge key={tag.key} title={tag.value || undefined} />
        ) : (
          <TicketTagBadge key={tag.key} tag={tag} compact={compact} />
        ),
      )}
      {hiddenCount > 0 && (
        <span
          className="shrink-0 text-xs text-muted-foreground tabular-nums"
          aria-label={`${hiddenCount} more ${hiddenCount === 1 ? "tag" : "tags"}`}
        >
          +{hiddenCount}
        </span>
      )}
    </div>
  );
}

function TicketTagBadge({ tag, compact }: { tag: TicketTag; compact: boolean }) {
  const descriptionId = useId();
  const label = `${tag.key}${tag.value ? `: ${tag.value}` : ""}`;
  const badge = (
    <Badge
      variant="secondary"
      title={compact ? undefined : tag.value || undefined}
      tabIndex={compact ? 0 : undefined}
      aria-describedby={compact ? descriptionId : undefined}
      className={
        compact
          ? "h-5 min-w-0 max-w-36 shrink cursor-help rounded-md bg-muted/60 px-1.5 py-0 text-xs font-normal text-muted-foreground outline-none focus-visible:ring-1"
          : "h-auto min-h-5 max-w-full whitespace-normal text-left wrap-anywhere"
      }
    >
      <span className={compact ? "truncate" : undefined}>{label}</span>
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
        {label}
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
