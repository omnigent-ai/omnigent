// PullRequestStrip — the PR panel's picker for a session's tracked PRs: a
// horizontally scrolling row of cards (state glyph, number, CI counts, title),
// with chevrons that page through the row while it overflows. Each card reveals
// its own unlink button on hover, so it's clear which PR gets unlinked.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  CircleCheckIcon,
  CircleDotIcon,
  CircleXIcon,
  GitPullRequestIcon,
  Trash2Icon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  usePullRequestInfos,
  type PullRequest,
  type PullRequestAssociation,
} from "@/hooks/usePullRequests";
import { gitProviderCopy } from "@/lib/gitProviders";
import { pullRequestStatus } from "@/lib/pullRequestDisplay";
import { cn } from "@/lib/utils";

/** Share of the visible row one chevron click scrolls by. */
const PAGE_FRACTION = 0.8;

/** Failing, pending and passing counts, each shown only when non-zero. */
function CheckCounts({ checks }: { checks: PullRequest["checks"] }) {
  const counts = [
    {
      key: "failed",
      count: checks.failing,
      Icon: CircleXIcon,
      className: "text-red-600 dark:text-red-400",
    },
    {
      key: "pending",
      count: checks.pending,
      Icon: CircleDotIcon,
      className: "text-amber-600 dark:text-amber-400",
    },
    {
      key: "passed",
      count: checks.passing,
      Icon: CircleCheckIcon,
      className: "text-green-600 dark:text-green-400",
    },
  ].filter(({ count }) => count > 0);
  if (counts.length === 0) return null;
  return (
    <>
      <span aria-hidden className="text-muted-foreground">
        ·
      </span>
      {counts.map(({ key, count, Icon, className }) => (
        <span key={key} className={cn("inline-flex items-center gap-0.5", className)}>
          <Icon className="size-3 shrink-0" aria-hidden />
          {count}
        </span>
      ))}
    </>
  );
}

/** A PR's card label, in its own provider's copy (else the session's). */
function pullRequestLabel(pr: PullRequestAssociation, sessionProvider?: string | null): string {
  const copy = gitProviderCopy(pr.provider ?? sessionProvider, undefined, pr.provider_display);
  const host = pr.host === copy.defaultHost ? "" : `${pr.host}/`;
  const inferred = pr.relationship === "inferred" ? " (from branch)" : "";
  const identity = `${host}${pr.repository} ${copy.prNumberPrefix}${pr.number}${inferred}`;
  const title = pr.title?.trim();
  return title ? `${identity} — ${title}` : identity;
}

/** "Open · 1 failed · 100 passed" for the card's tooltip. */
function statusSummary(pr: PullRequest): string {
  const { checks } = pr;
  const parts = [pullRequestStatus(pr).label];
  if (checks.failing) parts.push(`${checks.failing} failed`);
  if (checks.pending) parts.push(`${checks.pending} pending`);
  if (checks.passing) parts.push(`${checks.passing} passed`);
  return parts.join(" · ");
}

function PullRequestCard({
  association,
  details,
  selected,
  sessionProvider,
  unlinkDisabled,
  onSelect,
  onUnlink,
}: {
  association: PullRequestAssociation;
  /** The PR's resolved state and checks; absent until its info loads. */
  details: PullRequest | null | undefined;
  selected: boolean;
  sessionProvider?: string | null;
  unlinkDisabled: boolean;
  onSelect: () => void;
  onUnlink: () => void;
}) {
  const copy = gitProviderCopy(
    association.provider ?? sessionProvider,
    undefined,
    association.provider_display,
  );
  const status = details ? pullRequestStatus(details) : null;
  const StateIcon = status?.icon ?? GitPullRequestIcon;
  const label = pullRequestLabel(association, sessionProvider);
  const title = association.title?.trim() || details?.title?.trim() || association.repository;
  return (
    // Buttons can't nest, so the unlink button overlays the card as a sibling.
    <div
      className={cn(
        "group/card relative w-48 shrink-0 rounded-md hover:bg-muted/60",
        selected && "bg-muted hover:bg-muted",
      )}
    >
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            aria-label={label}
            aria-current={selected ? "true" : undefined}
            onClick={onSelect}
            className="flex w-full cursor-pointer flex-col gap-0.5 rounded-md px-2 py-1.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {/* Right padding keeps the counts clear of the unlink button. */}
            <span className="flex min-w-0 items-center gap-1 pr-5 text-ui tabular-nums">
              <StateIcon
                className={cn("size-3.5 shrink-0", status?.className ?? "text-muted-foreground")}
                aria-hidden
              />
              <span className="font-medium">
                {copy.prNumberPrefix}
                {association.number}
              </span>
              {details && <CheckCounts checks={details.checks} />}
            </span>
            <span className="truncate text-xs text-muted-foreground">{title}</span>
          </button>
        </TooltipTrigger>
        <TooltipContent side="bottom" className="flex-col items-start gap-0.5 wrap-anywhere">
          <span>{label}</span>
          {details && <span className="opacity-70">{statusSummary(details)}</span>}
        </TooltipContent>
      </Tooltip>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`Unlink ${label}`}
            disabled={unlinkDisabled}
            onClick={onUnlink}
            // Shown on hover and keyboard focus only: a clicked card keeps focus, so
            // focus-within would pin it on the selected card. Touch can't hover.
            className="absolute top-1 right-1 size-5 text-muted-foreground opacity-0 group-hover/card:opacity-100 group-has-[:focus-visible]/card:opacity-100 pointer-coarse:opacity-100"
          >
            <Trash2Icon className="size-3" aria-hidden />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">Unlink PR</TooltipContent>
      </Tooltip>
    </div>
  );
}

export function PullRequestStrip({
  conversationId,
  prs,
  selectedUrl,
  sessionProvider,
  unlinkDisabled = false,
  onSelect,
  onUnlink,
}: {
  conversationId: string;
  prs: PullRequestAssociation[];
  selectedUrl: string | undefined;
  sessionProvider?: string | null;
  /** Disables every card's unlink button, e.g. while an unlink is in flight. */
  unlinkDisabled?: boolean;
  onSelect: (url: string) => void;
  onUnlink: (url: string) => void;
}) {
  const infos = usePullRequestInfos(
    conversationId,
    prs.map((pr) => pr.url),
  );
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState({ start: false, end: false });

  const measure = useCallback(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    const start = scroller.scrollLeft > 1;
    const end = scroller.scrollLeft + scroller.clientWidth < scroller.scrollWidth - 1;
    setOverflow((current) =>
      current.start === start && current.end === end ? current : { start, end },
    );
  }, []);

  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(scroller);
    return () => observer.disconnect();
  }, [measure, prs.length]);

  // Keep the selected card in view, e.g. after linking a PR that lands off-screen.
  useEffect(() => {
    const scroller = scrollerRef.current;
    const card = scroller?.querySelector<HTMLElement>('[aria-current="true"]');
    if (!scroller || !card) return;
    const cardBox = card.getBoundingClientRect();
    const rowBox = scroller.getBoundingClientRect();
    if (cardBox.left < rowBox.left) {
      scroller.scrollLeft += cardBox.left - rowBox.left;
    } else if (cardBox.right > rowBox.right) {
      scroller.scrollLeft += cardBox.right - rowBox.right;
    }
  }, [selectedUrl, prs.length]);

  const page = (direction: -1 | 1) => {
    const scroller = scrollerRef.current;
    if (scroller) scroller.scrollLeft += direction * scroller.clientWidth * PAGE_FRACTION;
  };
  const scrollable = overflow.start || overflow.end;

  return (
    <div className="flex shrink-0 items-center gap-0.5 border-b border-border px-1 py-1.5">
      {scrollable && (
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label="Scroll pull requests left"
          disabled={!overflow.start}
          onClick={() => page(-1)}
          className="shrink-0 text-muted-foreground"
        >
          <ChevronLeftIcon className="size-3.5" aria-hidden />
        </Button>
      )}
      <TooltipProvider>
        <div
          ref={scrollerRef}
          role="group"
          aria-label="Session pull requests"
          onScroll={measure}
          className="flex min-w-0 flex-1 gap-1 overflow-x-auto scroll-smooth [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        >
          {prs.map((pr, index) => (
            <PullRequestCard
              key={pr.url}
              association={pr}
              details={infos[index]?.data?.pr}
              selected={pr.url === selectedUrl}
              sessionProvider={sessionProvider}
              unlinkDisabled={unlinkDisabled}
              onSelect={() => onSelect(pr.url)}
              onUnlink={() => onUnlink(pr.url)}
            />
          ))}
        </div>
      </TooltipProvider>
      {scrollable && (
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label="Scroll pull requests right"
          disabled={!overflow.end}
          onClick={() => page(1)}
          className="shrink-0 text-muted-foreground"
        >
          <ChevronRightIcon className="size-3.5" aria-hidden />
        </Button>
      )}
    </div>
  );
}
