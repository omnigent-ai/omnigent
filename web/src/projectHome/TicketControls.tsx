import { useId } from "react";
import { BadgeCheckIcon, CircleCheckIcon, UserRoundIcon } from "lucide-react";
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
  const leading = (key: string) => key === "needs-human" || key === ASSIGNEE_LABEL_KEY;
  const hasVerified = labels.some(
    (label) => label.key === VERIFIED_LABEL_KEY && isGateLabel(label),
  );
  const ordered = keepGatesTogether(
    compact
      ? [
          ...labels.filter((label) => label.key === "needs-human"),
          ...labels.filter((label) => label.key === ASSIGNEE_LABEL_KEY),
          // A card's one gate chip: verified (green, or amber when stale) stands for both.
          ...labels.filter(
            (label) => !leading(label.key) && !(hasVerified && label.key === APPROVED_LABEL_KEY),
          ),
        ]
      : labels,
  );
  const approvedSha = labels.find((label) => label.key === APPROVED_LABEL_KEY)?.value ?? null;
  const visible = compact ? ordered.slice(0, 2) : ordered;
  const hiddenCount = ordered.length - visible.length;
  return (
    <div className={cn("flex min-w-0 gap-1.5", compact ? "items-center" : "flex-wrap")}>
      {visible.map((label) =>
        label.key === "needs-human" ? (
          <NeedsResponseBadge key={label.key} title={label.value || undefined} />
        ) : label.key === ASSIGNEE_LABEL_KEY && label.value ? (
          <AssigneeBadge key={label.key} name={label.value} />
        ) : isGateLabel(label) ? (
          <GateShaBadge key={label.key} label={label} approvedSha={approvedSha} compact={compact} />
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

export const APPROVED_LABEL_KEY = "approved";
export const VERIFIED_LABEL_KEY = "verified";
const FULL_SHA = /^[0-9a-f]{40}$/;

/** `approved` and `verified` name the exact commit they sign off. */
function isGateLabel(label: TicketLabel): label is TicketLabel & { value: string } {
  return (
    (label.key === APPROVED_LABEL_KEY || label.key === VERIFIED_LABEL_KEY) &&
    FULL_SHA.test(label.value ?? "")
  );
}

/** `verified` reads right after `approved`, wherever approved sits. */
function keepGatesTogether(labels: TicketLabel[]): TicketLabel[] {
  const verified = labels.find((label) => label.key === VERIFIED_LABEL_KEY);
  if (!verified || !labels.some((label) => label.key === APPROVED_LABEL_KEY)) return labels;
  return labels.flatMap((label) =>
    label === verified ? [] : label.key === APPROVED_LABEL_KEY ? [label, verified] : [label],
  );
}

/** A sign-off on one commit: its key and short SHA, the full SHA on hover. */
function GateShaBadge({
  label,
  approvedSha,
  compact,
}: {
  label: TicketLabel & { value: string };
  approvedSha: string | null;
  /** On a card the icon names the gate; the key and the mismatch note move to the tooltip. */
  compact: boolean;
}) {
  const verified = label.key === VERIFIED_LABEL_KEY;
  // Verification is for one commit; a newer approval means it no longer covers the head.
  const stale = verified && approvedSha !== null && approvedSha !== label.value;
  const Icon = verified ? BadgeCheckIcon : CircleCheckIcon;
  const description = stale
    ? `verified ${label.value}, not the approved ${approvedSha}`
    : `${label.key} ${label.value}`;
  return (
    <Badge
      variant="outline"
      title={description}
      aria-label={description}
      data-testid={`ticket-gate-${label.key}`}
      className={cn(
        "h-5 shrink-0 gap-1 rounded-md px-1.5 py-0 text-xs font-medium",
        !verified && "border-border bg-muted/60 text-muted-foreground",
        verified && !stale && "border-success/40 bg-success/10 text-success",
        stale && "border-warning/50 bg-warning/10 text-warning",
      )}
    >
      <Icon aria-hidden className="size-3 shrink-0" />
      {!compact && label.key}
      <span className="font-mono text-[0.6875rem] tracking-tight">{label.value.slice(0, 7)}</span>
      {stale && !compact && <span className="font-normal">≠ approved</span>}
    </Badge>
  );
}

/** Ticket label naming the project identity that owns the ticket, e.g. `assignee: PM`. */
export const ASSIGNEE_LABEL_KEY = "assignee";

/** The owning identity, shown as a person chip rather than a plain label. */
function AssigneeBadge({ name }: { name: string }) {
  return (
    <Badge
      variant="outline"
      title={`Assigned to ${name}`}
      aria-label={`Assigned to ${name}`}
      data-testid="ticket-assignee"
      className="h-5 min-w-0 max-w-36 shrink gap-1 rounded-full border-primary/40 bg-primary/10 px-1.5 py-0 text-xs font-medium text-primary"
    >
      <UserRoundIcon className="size-3 shrink-0" />
      <span className="truncate">{name}</span>
    </Badge>
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
