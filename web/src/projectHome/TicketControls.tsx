import { Badge } from "@/components/ui/badge";
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
          <Badge
            key={tag.key}
            variant="secondary"
            title={tag.value || undefined}
            className={
              compact
                ? "h-5 min-w-0 max-w-36 shrink rounded-md bg-muted/60 px-1.5 py-0 text-xs font-normal text-muted-foreground"
                : "h-auto min-h-5 max-w-full whitespace-normal text-left wrap-anywhere"
            }
          >
            <span className={compact ? "truncate" : undefined}>
              {`${tag.key}${tag.value ? `: ${tag.value}` : ""}`}
            </span>
          </Badge>
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

export function TicketError({ error }: { error: Error | null }) {
  return error ? (
    <p role="alert" className="text-sm text-destructive">
      {error.message}
    </p>
  ) : null;
}
