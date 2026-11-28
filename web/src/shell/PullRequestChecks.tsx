// CI checks for the Pull Requests list and the PR Summary tab. A list's CI cell opens the jobs
// in a popover; the Summary tab lists the jobs that need attention and folds the rest.

import { useState, type ReactNode, type SyntheticEvent } from "react";
import {
  BanIcon,
  ChevronRightIcon,
  CircleCheckIcon,
  CircleDotIcon,
  CircleMinusIcon,
  CircleXIcon,
  ExternalLinkIcon,
  KeyRoundIcon,
  type LucideIcon,
} from "lucide-react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type {
  PullRequestCheckRun,
  PullRequestChecks,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { listKeys } from "@/lib/listKeys";
import { parseHttpUrl } from "@/lib/openLinkInApp";
import { cn } from "@/lib/utils";
import {
  type CheckSummaryTone,
  completedChecksLabel,
  orderCheckRuns,
  summarizeChecks,
  unlistedChecksLabel,
} from "./pullRequestCheckSummary";
import {
  usePullRequestSectionStyle,
  usePullRequestSectionTitleClass,
} from "@/shell/pullRequestSectionStyle";

type CheckBucket = PullRequestCheckRun["bucket"];

/** Each bucket's state word, glyph, and tint. */
const CHECK_BUCKETS: Record<CheckBucket, { state: string; Icon: LucideIcon; iconClass: string }> = {
  passing: {
    state: "successful",
    Icon: CircleCheckIcon,
    iconClass: "text-status-green",
  },
  pending: {
    state: "running",
    Icon: CircleDotIcon,
    iconClass: "text-status-yellow",
  },
  failing: {
    state: "failing",
    Icon: CircleXIcon,
    iconClass: "text-status-red",
  },
  cancelled: {
    state: "cancelled",
    Icon: BanIcon,
    iconClass: "text-muted-foreground",
  },
  skipped: {
    state: "skipped",
    Icon: CircleMinusIcon,
    iconClass: "text-muted-foreground",
  },
};

function CheckBucketIcon({ bucket }: { bucket: CheckBucket }) {
  const { Icon, iconClass } = CHECK_BUCKETS[bucket];
  return <Icon aria-hidden className={cn("size-2.5 shrink-0", iconClass)} />;
}

const stopPropagation = (event: SyntheticEvent) => event.stopPropagation();

/**
 * A list's CI cell: hovering `trigger` names the result, and clicking it (or Enter or Space)
 * opens the jobs. Its events, the portalled jobs' included, stay off the row it sits in.
 */
export function CheckRunsPopover({
  trigger,
  label,
  runs,
  titled = true,
}: {
  /** A button, named `label`. */
  trigger: ReactNode;
  /** The result in a sentence, e.g. "1 failing check · 12 of 13 passed". */
  label: string;
  runs: readonly PullRequestCheckRun[];
  /** Name the result on a plain hover; off for a trigger that has its own tooltip. */
  titled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ordered = orderCheckRuns(runs);
  return (
    <span
      className="contents"
      onClick={stopPropagation}
      onDoubleClick={stopPropagation}
      onAuxClick={stopPropagation}
      onMouseDown={stopPropagation}
      onPointerDown={stopPropagation}
      onContextMenu={stopPropagation}
      onKeyDown={(event) => {
        // The row's own keys still reach it from the closed trigger; Enter and Space are its.
        if (open || event.key === "Enter" || event.key === " ") event.stopPropagation();
      }}
    >
      <Popover open={open} onOpenChange={setOpen}>
        {/* A plain hover only names the result, as the cell's other glyphs do. */}
        <PopoverTrigger asChild title={open || !titled ? undefined : label}>
          {trigger}
        </PopoverTrigger>
        {/* Inside the span, so the portalled jobs' events stay off the row too. */}
        <PopoverContent
          side="bottom"
          align="start"
          collisionPadding={8}
          aria-label="Checks"
          className="max-h-80 w-auto max-w-sm min-w-64 gap-1.5 overflow-y-auto p-2"
        >
          <p className="px-1.5 text-xs text-muted-foreground">{label}</p>
          {ordered.length > 0 ? (
            <CheckRunRows runs={ordered} label="Checks" />
          ) : (
            <p className="px-1.5 py-1 text-xs text-muted-foreground">No job details.</p>
          )}
        </PopoverContent>
      </Popover>
    </span>
  );
}

const SUMMARY_TONES: Record<CheckSummaryTone, { Icon: LucideIcon; className: string }> = {
  failing: { Icon: CircleXIcon, className: "text-status-red" },
  pending: { Icon: CircleDotIcon, className: "text-status-yellow" },
  passing: { Icon: CircleCheckIcon, className: "text-status-green" },
  cancelled: { Icon: BanIcon, className: "text-muted-foreground" },
  skipped: { Icon: CircleMinusIcon, className: "text-muted-foreground" },
  none: { Icon: CircleMinusIcon, className: "text-muted-foreground" },
};

/** The headline row: a status glyph beside the result and its counts. */
export function CheckSummaryHeadline({
  Icon,
  iconClass,
  title,
  detail,
  large = false,
}: {
  Icon: LucideIcon;
  iconClass: string;
  title: string;
  detail: ReactNode;
  /** The headline of a page's checks card. */
  large?: boolean;
}) {
  return (
    <div role="status" className={cn("flex items-start", large ? "gap-3.5" : "gap-2.5")}>
      <Icon
        aria-hidden
        className={cn("shrink-0", large ? "size-8 stroke-[1.75]" : "mt-0.5 size-5", iconClass)}
      />
      <div className={cn("min-w-0", large && "pt-0.5")}>
        <p
          className={cn("font-medium text-foreground", large ? "text-sm font-semibold" : "text-ui")}
        >
          {title}
        </p>
        {detail && <p className="text-xs text-muted-foreground">{detail}</p>}
      </div>
    </div>
  );
}

/** One job: its glyph, name, and state; a job with a run URL opens it. */
function CheckRunRow({ run }: { run: PullRequestCheckRun }) {
  const href = run.url ? parseHttpUrl(run.url)?.href : undefined;
  const { state } = CHECK_BUCKETS[run.bucket];
  const content = (
    <>
      <CheckBucketIcon bucket={run.bucket} />
      <span className="min-w-0 flex-1 truncate text-foreground" title={run.name}>
        {run.name}
      </span>
      <span className="shrink-0 text-muted-foreground">{state}</span>
      {/* Every row keeps the link icon's slot, so each state ends at the same edge. */}
      <span aria-hidden className="flex size-3 shrink-0" data-check-link-slot="">
        {href && (
          <ExternalLinkIcon className="size-3 text-muted-foreground opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100" />
        )}
      </span>
    </>
  );
  const rowClass = "group flex items-center gap-2 rounded-md px-1.5 py-1 text-xs";
  return (
    <li>
      {href ? (
        <a
          href={href}
          target="_blank"
          rel="noreferrer"
          aria-label={`${run.name}: ${state}`}
          className={cn(
            rowClass,
            "hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
          )}
        >
          {content}
        </a>
      ) : (
        <div aria-label={`${run.name}: ${state}`} className={rowClass}>
          {content}
        </div>
      )}
    </li>
  );
}

function CheckRunRows({ runs, label }: { runs: readonly PullRequestCheckRun[]; label: string }) {
  const keys = listKeys(runs, (run) => [run.url, run.name]);
  return (
    <ul aria-label={label} className="-mx-1.5">
      {runs.map((run, index) => (
        <CheckRunRow key={keys[index]} run={run} />
      ))}
    </ul>
  );
}

/** Says which counted checks have no row and links to where they all are. */
function UnlistedChecksNote({
  unlistedAttention,
  unlistedCompleted,
  providerLabel,
  pullRequestUrl,
}: {
  unlistedAttention: number;
  unlistedCompleted: number;
  providerLabel: string;
  pullRequestUrl: string;
}) {
  const href = parseHttpUrl(pullRequestUrl)?.href;
  return (
    <p role="note" className="text-xs text-muted-foreground">
      {unlistedChecksLabel(unlistedAttention, unlistedCompleted)}{" "}
      {href && (
        <a
          href={href}
          target="_blank"
          rel="noreferrer"
          className="text-foreground underline underline-offset-2"
        >
          See all checks on {providerLabel}
        </a>
      )}
    </p>
  );
}

/**
 * The Summary tab's Checks: what the head commit's CI says, the failing and running jobs,
 * and the completed jobs folded below. Says so when the account can't read checks.
 */
export function PullRequestChecksSection({
  checks,
  supported,
  unavailableHint,
  providerLabel,
  pullRequestUrl,
  footer,
}: {
  checks: PullRequestChecks;
  supported: boolean;
  unavailableHint?: string;
  providerLabel: string;
  /** Where the checks this list leaves out can be seen. */
  pullRequestUrl: string;
  /** A page's merge controls, along the card's bottom edge. */
  footer?: ReactNode;
}) {
  const titleClass = usePullRequestSectionTitleClass();
  // A page shows its checks as a card under a heading.
  const card = usePullRequestSectionStyle() === "heading";
  const summary = summarizeChecks(checks, providerLabel);
  // Follows the results as they arrive until the user opens or closes the list themselves.
  const [chosenOpen, setChosenOpen] = useState<boolean>();
  const completedOpen = chosenOpen ?? summary.completedOpen;
  const tone = SUMMARY_TONES[summary.tone];
  const unlisted = (summary.unlistedAttention > 0 || summary.unlistedCompleted > 0) && (
    <UnlistedChecksNote
      unlistedAttention={summary.unlistedAttention}
      unlistedCompleted={summary.unlistedCompleted}
      providerLabel={providerLabel}
      pullRequestUrl={pullRequestUrl}
    />
  );
  const checksBody = !supported ? (
    <CheckSummaryHeadline
      Icon={KeyRoundIcon}
      iconClass="text-muted-foreground"
      title="Checks unavailable"
      detail={unavailableHint ?? "The signed-in account can’t read this pull request’s checks."}
    />
  ) : (
    <>
      <CheckSummaryHeadline
        Icon={tone.Icon}
        iconClass={tone.className}
        title={summary.title}
        detail={summary.detail}
        large={card}
      />
      {summary.attention.length > 0 && (
        <CheckRunRows runs={summary.attention} label="Checks needing attention" />
      )}
      {summary.unlistedAttention > 0 && unlisted}
      {summary.completed.length > 0 && (
        <Collapsible open={completedOpen} onOpenChange={setChosenOpen}>
          <CollapsibleTrigger className="-mx-1.5 flex cursor-pointer items-center gap-1 rounded-md px-1.5 py-1 text-xs text-muted-foreground hover:text-foreground">
            <ChevronRightIcon
              aria-hidden
              className={cn("size-3 transition-transform", completedOpen && "rotate-90")}
            />
            {completedChecksLabel(summary.completed.length)}
          </CollapsibleTrigger>
          <CollapsibleContent>
            <CheckRunRows runs={summary.completed} label="Completed checks" />
          </CollapsibleContent>
        </Collapsible>
      )}
      {summary.unlistedAttention === 0 && unlisted}
    </>
  );
  return (
    <section className="space-y-2" aria-labelledby="pull-request-checks-heading">
      <h3 id="pull-request-checks-heading" className={titleClass}>
        Checks
      </h3>
      {card ? (
        <div className="rounded-lg border border-border bg-card">
          <div className="space-y-2 p-4">{checksBody}</div>
          {footer && (
            <div className="rounded-b-lg border-t border-border bg-muted/40 px-4 py-3">
              {footer}
            </div>
          )}
        </div>
      ) : (
        <>
          {checksBody}
          {footer}
        </>
      )}
    </section>
  );
}
