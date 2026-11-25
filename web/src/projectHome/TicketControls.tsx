import { useId } from "react";
import {
  BadgeCheckIcon,
  CircleAlertIcon,
  CircleCheckIcon,
  RefreshCwIcon,
  UserRoundIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { NeedsResponseBadge } from "@/components/NeedsResponseBadge";
import { getCurrentUserId } from "@/lib/identity";
import { isServerRestartError } from "@/lib/serverRestart";
import type { TicketLabel, TicketWaitingOn } from "@/lib/projectTicketsApi";
import { cn } from "@/lib/utils";

import { needsWhom, type NeedsWhom } from "./ticketWaitingOn";

export { TicketStageIcon } from "./TicketStageAppearance";

export function TicketLabels({
  labels: allLabels,
  compact = false,
  waitingOn,
}: {
  labels: TicketLabel[];
  compact?: boolean;
  /** Who the ask waits on; null hides an answered ask's tag, undefined keeps the generic one. */
  waitingOn?: TicketWaitingOn | null;
}) {
  const whom = needsWhom(waitingOn, getCurrentUserId());
  const labels = allLabels.filter(
    (label) =>
      !(whom === null && label.key === "needs-human") &&
      // A card leaves out the ticket's classification; the panel still lists it.
      !(compact && label.key === KIND_LABEL_KEY),
  );
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
  // Nothing left to show (a card whose only label is its kind): no empty row to leave a gap.
  if (visible.length === 0) return null;
  return (
    <div className={cn("flex min-w-0 gap-1.5", compact ? "items-center" : "flex-wrap")}>
      {visible.map((label) =>
        label.key === "needs-human" ? (
          <NeedsWhomBadge key={label.key} whom={whom} question={label.value || undefined} />
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
export const KIND_LABEL_KEY = "kind";
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
    : compact
      ? `${verified ? "Verified" : "Reviewed and approved"} at commit ${label.value.slice(0, 7)}`
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
      {compact ? (
        // On a card the sign-off reads in words; the commit is in the tooltip.
        verified ? (
          "Verified"
        ) : (
          "Reviewed"
        )
      ) : (
        <>
          {label.key}
          <span className="font-mono text-[0.6875rem] tracking-tight">
            {label.value.slice(0, 7)}
          </span>
          {stale && <span className="font-normal">≠ approved</span>}
        </>
      )}
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

/** Hides a restart's failure while earlier data is still on screen; the banner covers it. */
export function shownLoadError(error: Error | null, hasData: boolean): Error | null {
  return error && hasData && isServerRestartError(error) ? null : error;
}

/**
 * A calm notice for a view that failed to load. During a restart a background
 * load shows nothing over earlier data, or a quiet wait when there is none; the
 * banner says why and refetches once the app is back. A failed *action* still
 * shows its message. Other failures get a soft red.
 */
export function TicketLoadError({
  error,
  message,
  onRetry,
  className,
  hasData = false,
  action = false,
}: {
  error: Error | null;
  /** Shown instead of the error's own message. */
  message?: string;
  onRetry?: () => void;
  className?: string;
  /** The view still shows earlier data. */
  hasData?: boolean;
  /** The error came from something the user just did, such as a save. */
  action?: boolean;
}) {
  if (!error) return null;
  const restarting = isServerRestartError(error);
  if (restarting && !action) {
    if (hasData) return null;
    return (
      <div
        role="status"
        data-tone="restart"
        className={cn("flex items-center gap-2 py-2 text-sm text-muted-foreground", className)}
      >
        <Spinner aria-hidden className="size-3.5" />
        Waiting for Omnigent to come back…
      </div>
    );
  }
  const Icon = restarting ? RefreshCwIcon : CircleAlertIcon;
  return (
    <div
      role="alert"
      data-tone={restarting ? "restart" : "error"}
      className={cn(
        "flex items-start gap-2 rounded-lg border px-3 py-2 text-sm",
        restarting
          ? "border-border/60 bg-muted/40 text-muted-foreground"
          : "border-destructive/20 bg-destructive/5 text-destructive/90",
        className,
      )}
    >
      <Icon aria-hidden className="mt-0.5 size-4 shrink-0" />
      <span className="min-w-0 flex-1 wrap-anywhere">{message ?? error.message}</span>
      {onRetry && (
        <Button variant="ghost" size="xs" className="-my-0.5 shrink-0" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

/** "Needs you" in the strong accent, someone else's ask in quiet grey. */
function NeedsWhomBadge({ whom, question }: { whom: NeedsWhom; question?: string }) {
  if (whom?.kind === "you")
    return (
      <NeedsResponseBadge
        title={question}
        data-testid="needs-you"
        // The accent darkened per theme for AA contrast with white text; see index.css.
        className="needs-you-tag"
      >
        Needs you
      </NeedsResponseBadge>
    );
  if (whom?.kind === "other")
    return (
      <Badge
        title={question ? `${whom.title}: ${question}` : whom.title}
        data-testid="needs-other"
        className="border-transparent bg-muted font-normal text-muted-foreground"
      >
        {whom.label}
      </Badge>
    );
  return <NeedsResponseBadge title={question} />;
}
