// One row of the new Pull Requests list, with its avatars, CI ring and hover actions. The
// row's menu and its hover buttons run the same shared actions as ⌘K.

import { memo, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";
import { LinkIcon, UsersIcon, XIcon } from "lucide-react";
import { PullRequestFreshness } from "@/components/PullRequestFreshness";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Checkbox } from "@/components/ui/checkbox";
import { ContextMenu, ContextMenuContent, ContextMenuTrigger } from "@/components/ui/context-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { PullRequestReviewer } from "@/newPullRequestPage/hooks/usePullRequests";
import { compactCount } from "@/lib/pullRequestListStats";
import { ROW_DETAILS_AGE_SHOWN_AFTER_MS } from "@/lib/pullRequestRowSnapshots";
import { PULL_REQUEST_STATUS_ICONS } from "@/lib/pullRequestStatusIcons";
import { useMenuFilter } from "@/components/ui/menu-filter";
import { useMenuCloseHandOff } from "@/hooks/useMenuCloseHandOff";
import { isCommandAvailable } from "@/store/paletteCommands";
import { CheckRunsPopover } from "@/shell/PullRequestChecks";
import { isFinishedState } from "@/shell/pullRequestListModel";
import {
  CONTEXT_ROW_MENU,
  type PullRequestListEntry,
  PullRequestStateIcon,
  type RowAction,
  RowActionMenuBody,
  rowIdentity,
} from "@/shell/PullRequestListRows";
import { REVIEWER_STATE_META } from "@/newPullRequestPage/shell/PullRequestReviewers";
import {
  PullRequestListStatsGapMark,
  pullRequestListEntryStatsCell,
} from "@/shell/PullRequestListStatsGap";
import type { OpenGesture } from "@/shell/pullRequestOpenIntent";
import {
  compactAge,
  fullDate,
  githubAvatarUrl,
  initials,
  pageCi,
  pageStatus,
  rowLinkTooltip,
  statusDescription,
  statusSaidByState,
  type PageCi,
  type PageStatus,
} from "./pullRequestListPageModel";
import { type ListStackRowView, StackRowNotes, StackTreeLead } from "./PullRequestListStackParts";
import { alsoInNote } from "./subAgentPullRequests";

const SHOWN_REVIEWERS = 3;

/** The link a click landed on, when it landed on a row's number or title. */
export function rowLink(target: EventTarget | null): HTMLAnchorElement | null {
  return target instanceof Element
    ? target.closest<HTMLAnchorElement>(".pr-list-row a.pr-list-link")
    : null;
}

export const PullRequestListPageRow = memo(function PullRequestListPageRow({
  entry,
  actions,
  now,
  repository,
  personHues,
  sandboxAsleep,
  mergeRequested,
  changeInFlight = null,
  managesSessionLinks,
  selectable,
  isSelected,
  isActive,
  isShown,
  domId,
  tree,
  stack,
  onActivate,
  onOpen,
  onToggle,
  onToggleStack,
}: {
  entry: PullRequestListEntry;
  actions: RowAction[];
  now: number;
  /** The short repository name, set only when the list spans several repositories. */
  repository: string | undefined;
  personHues: ReadonlyMap<string, number>;
  sandboxAsleep: boolean;
  mergeRequested: boolean;
  /** A state change GitHub is making to it now, e.g. "Closing…": the title pulses until it answers. */
  changeInFlight?: string | null;
  /** One session's list: the row selects, and its branch is that session's. */
  managesSessionLinks: boolean;
  /** The row has a checkbox and is part of the list's multiple selection. */
  selectable: boolean;
  isSelected: boolean;
  /** The row the keyboard acts on. */
  isActive: boolean;
  /** The pull request the panel last showed. */
  isShown: boolean;
  /** The row's element id, which the list names as its active descendant. */
  domId: string;
  /** In a list that shows stacks as a tree: the row's level, and for a parent, if it's open. */
  tree?: { level: number; expanded?: boolean };
  stack?: ListStackRowView;
  onToggleStack?: (url: string) => void;
  onActivate: (url: string, event: ReactMouseEvent) => void;
  onOpen: (url: string, gesture?: OpenGesture) => void;
  onToggle: (url: string, extend: boolean) => void;
}) {
  const { association, row, pr } = entry;
  const alsoIn = alsoInNote(row.sessionSource?.alsoIn ?? []);
  const { reference, label } = rowIdentity(entry);
  const menu = useMenuCloseHandOff();
  const filter = useMenuFilter();
  const status = pageStatus(entry, { sandboxAsleep, mergeRequested });
  const loading = status.kind === "loading" && row.title === undefined;
  const completed = isFinishedState(row.state);
  const ci = pageCi(row, entry.offline);
  const removal = actions.find((action) => action.id === "remove");
  // Only a Not Linked row shows its owner Link in the row; a loaded one offers it in its menus.
  const link =
    status.meaning === "not-linked" ? actions.find((action) => action.id === "link") : undefined;
  const labels = (pr?.labels ?? []).filter((item) => item.name);
  const host = association.host;
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          className={[
            "pr-list-row",
            `state-${row.state}`,
            isSelected && "selected",
            isActive && "active",
            isShown && "shown",
            completed && "completed",
          ]
            .filter(Boolean)
            .join(" ")}
          role="row"
          aria-current={isShown ? "true" : undefined}
          id={domId}
          aria-selected={selectable ? isSelected : undefined}
          aria-level={tree?.level}
          aria-expanded={tree?.expanded}
          aria-label={label}
          aria-busy={changeInFlight ? true : undefined}
          // Read with the row, as the label below shows only where motion is reduced.
          aria-describedby={changeInFlight ? `${domId}-change` : undefined}
          data-pull-request-row={row.url}
          onClick={(event) => onActivate(row.url, event)}
          onMouseDown={(event) => {
            // A middle click on a link opens it behind, without the browser's autoscroll.
            if (event.button === 1 && rowLink(event.target)) event.preventDefault();
          }}
          onAuxClick={(event) => {
            if (event.button !== 1 || !rowLink(event.target)) return;
            event.preventDefault();
            onOpen(row.url, event);
          }}
        >
          {selectable && (
            <span className="pr-list-cell-select" role="gridcell">
              <Checkbox
                className="pr-list-checkbox"
                checked={isSelected}
                aria-label={isSelected ? `Deselect ${reference}` : `Select ${reference}`}
                onClick={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  onToggle(row.url, event.shiftKey);
                }}
              />
            </span>
          )}
          <span className="pr-list-cell-state" role="gridcell">
            <PullRequestStateIcon state={row.state} className="pr-list-state" />
          </span>
          <span className="pr-list-cell-number" role="gridcell">
            <RowLink
              url={row.url}
              className="pr-list-number"
              title={`${row.repository} ${reference}`}
            >
              {reference}
            </RowLink>
          </span>
          <span className="pr-list-cell-title" role="gridcell">
            {stack && (
              <StackTreeLead
                stack={stack}
                reference={reference}
                onToggle={() => onToggleStack?.(row.url)}
              />
            )}
            {loading ? (
              <span className="pr-list-title pr-list-skeleton" aria-label="Loading pull request" />
            ) : (
              <RowLink
                url={row.url}
                className={changeInFlight ? "pr-list-title changing" : "pr-list-title"}
                title={rowLinkTooltip(row, reference)}
              >
                {row.title ?? `${row.repository} ${reference}`}
              </RowLink>
            )}
            {changeInFlight && (
              // Where motion is reduced the title holds still, and this says what's under way.
              <span id={`${domId}-change`} className="pr-list-changing-label">
                {changeInFlight}
              </span>
            )}
            {stack && <StackRowNotes stack={stack} />}
            {repository && (
              <span className="pr-list-repository pr-list-wide-only" title={row.repository}>
                {repository}
              </span>
            )}
            {row.inferred && (
              <span
                className="pr-list-repository"
                title={
                  managesSessionLinks
                    ? "Found from the session’s branch"
                    : "Found from a session’s branch"
                }
              >
                From branch
              </span>
            )}
            {row.sessionSource?.session && (
              <span
                className="pr-list-repository pr-list-session-chip pr-list-wide-only"
                title={`Linked by ${row.sessionSource.session.name}`}
              >
                {row.sessionSource.session.name}
              </span>
            )}
            {alsoIn && (
              <span className="pr-list-repository pr-list-also-in pr-list-wide-only">{alsoIn}</span>
            )}
            {labels.length > 0 && (
              <span className="pr-list-labels">
                {labels.slice(0, 2).map((item) => (
                  <span
                    key={item.name}
                    className="pr-list-label"
                    title={item.description ?? item.name}
                  >
                    <span style={{ background: labelColor(item.color) }} aria-hidden />
                    {item.name}
                  </span>
                ))}
                {labels.length > 2 && (
                  <span
                    className="pr-list-label more"
                    title={labels
                      .slice(2)
                      .map((item) => item.name)
                      .join(", ")}
                  >
                    +{labels.length - 2}
                  </span>
                )}
              </span>
            )}
          </span>
          <span className="pr-list-cell-status" role="gridcell">
            {status.kind !== "loading" && !statusSaidByState(status, row.state) && (
              <StatusGlyph status={status} />
            )}
          </span>
          <span className="pr-list-cell-checks" role="gridcell">
            {ci && (
              <Checks ci={ci} runs={row.ci?.source === "live" ? row.ci.checks.runs : undefined} />
            )}
          </span>
          <span className="pr-list-cell-diff" role="gridcell">
            <Diff entry={entry} />
          </span>
          <span className="pr-list-cell-reviewers" role="gridcell">
            <Reviewers reviewers={row.reviewers} host={host} personHues={personHues} />
            {entry.detailsReadAt !== undefined && (
              // The list's own columns stay as fresh as the list; only reviewers wait for the row.
              <PullRequestFreshness
                state={{ updatedAt: entry.detailsReadAt, refreshing: false, failed: false }}
                form="clock"
                showAfter={ROW_DETAILS_AGE_SHOWN_AFTER_MS}
                describe={(age) => `Reviewers as of ${age}`}
                className="pr-list-reviewers-age"
              />
            )}
          </span>
          <span className="pr-list-cell-author" role="gridcell">
            {row.author && (
              <ListAvatar
                login={row.author}
                host={host}
                hue={personHues.get(row.author.toLowerCase())}
                label={row.author}
              />
            )}
          </span>
          <Updated entry={entry} now={now} />
          <span className="pr-list-row-actions" onClick={(event) => event.stopPropagation()}>
            {link && (
              <button
                type="button"
                aria-label={`Link ${reference} to this session`}
                title={
                  link.availability?.available === false
                    ? link.availability.reason
                    : "Link to This Session"
                }
                disabled={!isCommandAvailable(link.availability)}
                onClick={link.run}
              >
                <LinkIcon aria-hidden />
              </button>
            )}
            {removal && (
              <button
                type="button"
                aria-label={`Remove ${reference} from this session`}
                title={
                  isCommandAvailable(removal.availability) ? "Remove from This Session" : undefined
                }
                disabled={!isCommandAvailable(removal.availability)}
                onClick={removal.run}
              >
                <XIcon aria-hidden />
              </button>
            )}
          </span>
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent {...filter.contentProps} onCloseAutoFocus={menu.onCloseAutoFocus}>
        <RowActionMenuBody actions={actions} parts={CONTEXT_ROW_MENU} menu={menu} filter={filter} />
      </ContextMenuContent>
    </ContextMenu>
  );
});

/**
 * A real link to the pull request, so screen readers announce it as one, kept out of the
 * Tab order because arrow keys move between rows. The row handles its clicks.
 */
function RowLink({
  url,
  className,
  title,
  children,
}: {
  url: string;
  className: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <a
      className={`pr-list-link ${className}`}
      href={url}
      tabIndex={-1}
      draggable={false}
      title={title}
    >
      {children}
    </a>
  );
}

/** The status as one glyph; its label is the accessible name, and its tooltip on hover or focus. */
function StatusGlyph({ status }: { status: PageStatus }) {
  const Icon = PULL_REQUEST_STATUS_ICONS[status.meaning];
  const description = statusDescription(status);
  return (
    <WithTip tip={description}>
      <span
        className={`pr-list-classification tone-${status.tone}${status.attention ? " attention" : ""}`}
        data-status-meaning={status.meaning}
        role="img"
        aria-label={description}
        tabIndex={0}
      >
        <Icon aria-hidden />
      </span>
    </WithTip>
  );
}

function WithTip({ tip, children }: { tip: string | undefined; children: ReactNode }) {
  if (!tip) return children;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent className="max-w-xs">{tip}</TooltipContent>
    </Tooltip>
  );
}

/** A face for a person: their GitHub picture for github.com logins, otherwise initials. */
export function ListAvatar({
  login,
  host,
  hue,
  size = "regular",
  ring,
  label,
}: {
  login: string;
  host: string | null | undefined;
  hue: number | undefined;
  size?: "regular" | "small";
  ring?: "positive" | "danger";
  /** The tooltip and accessible name; the login when absent. */
  label?: string;
}) {
  const source = githubAvatarUrl(login, host);
  const description = label ?? login;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Avatar
          className={`pr-list-avatar ${size}${ring ? ` ring-${ring}` : ""}`}
          role="img"
          aria-label={description}
        >
          {source && (
            <AvatarImage src={source} alt="" loading="lazy" decoding="async" draggable={false} />
          )}
          <AvatarFallback
            className="pr-list-avatar-initials"
            style={
              // The author palette keeps one color per person across the list.
              hue === undefined
                ? undefined
                : { backgroundColor: `oklch(0.48 0.13 ${hue})`, color: "#fff" }
            }
            aria-hidden
          >
            {initials(login)}
          </AvatarFallback>
        </Avatar>
      </TooltipTrigger>
      <TooltipContent>{description}</TooltipContent>
    </Tooltip>
  );
}

function Reviewers({
  reviewers,
  host,
  personHues,
}: {
  reviewers: readonly PullRequestReviewer[];
  host: string | null | undefined;
  personHues: ReadonlyMap<string, number>;
}) {
  if (reviewers.length === 0) return null;
  const describe = (reviewer: PullRequestReviewer) =>
    `${reviewer.login} · ${REVIEWER_STATE_META[reviewer.state].label}`;
  const shown = reviewers.slice(0, SHOWN_REVIEWERS);
  const hidden = reviewers.slice(SHOWN_REVIEWERS);
  return (
    <span className="pr-list-reviewers">
      {shown.map((reviewer) =>
        reviewer.kind === "team" ? (
          <Tooltip key={`team:${reviewer.login}`}>
            <TooltipTrigger asChild>
              <span
                className="pr-list-avatar small team"
                role="img"
                aria-label={describe(reviewer)}
              >
                <UsersIcon aria-hidden />
              </span>
            </TooltipTrigger>
            <TooltipContent>{describe(reviewer)}</TooltipContent>
          </Tooltip>
        ) : (
          <ListAvatar
            key={`user:${reviewer.login}`}
            login={reviewer.login}
            host={host}
            hue={personHues.get(reviewer.login.toLowerCase())}
            size="small"
            ring={reviewerRing(reviewer.state)}
            label={describe(reviewer)}
          />
        ),
      )}
      {hidden.length > 0 && (
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="pr-list-reviewers-more" tabIndex={0}>
              +{hidden.length}
            </span>
          </TooltipTrigger>
          <TooltipContent className="whitespace-pre-line">
            {hidden.map(describe).join("\n")}
          </TooltipContent>
        </Tooltip>
      )}
    </span>
  );
}

function reviewerRing(state: PullRequestReviewer["state"]): "positive" | "danger" | undefined {
  if (state === "APPROVED") return "positive";
  if (state === "CHANGES_REQUESTED") return "danger";
  return undefined;
}

function Updated({ entry, now }: { entry: PullRequestListEntry; now: number }) {
  const cell = pullRequestListEntryStatsCell(entry, "updated");
  const updatedAt = cell.kind === "value" ? cell.stats.updatedAt : undefined;
  return (
    <span
      className="pr-list-cell-updated"
      role="gridcell"
      title={updatedAt === undefined ? undefined : `Updated ${fullDate(updatedAt)}`}
    >
      {updatedAt !== undefined && (
        <time dateTime={new Date(updatedAt).toISOString()}>{compactAge(updatedAt, now)}</time>
      )}
      {cell.kind === "gap" && (
        <PullRequestListStatsGapMark label={cell.label} reason={cell.reason} />
      )}
    </span>
  );
}

function Diff({ entry }: { entry: PullRequestListEntry }) {
  const cell = pullRequestListEntryStatsCell(entry, "diff");
  if (cell.kind === "gap") {
    return <PullRequestListStatsGapMark label={cell.label} reason={cell.reason} />;
  }
  const stats = cell.kind === "value" ? cell.stats : undefined;
  if (stats?.additions === undefined || stats.deletions === undefined) return null;
  const files =
    stats.changedFiles === undefined
      ? ""
      : ` in ${stats.changedFiles.toLocaleString()} ${stats.changedFiles === 1 ? "file" : "files"}`;
  const description = `${stats.additions.toLocaleString()} additions, ${stats.deletions.toLocaleString()} deletions${files}`;
  return (
    <span className="pr-list-diff" role="img" aria-label={description} title={description}>
      <span className="additions">+{compactCount(stats.additions)}</span>
      <span className="deletions">−{compactCount(stats.deletions)}</span>
    </span>
  );
}

/** A live read's checks open their job list on a click; a rollup shows its glyph alone. */
function Checks({
  ci,
  runs,
}: {
  ci: PageCi;
  runs: Parameters<typeof CheckRunsPopover>[0]["runs"] | undefined;
}) {
  if (ci.tone === "none" || ci.tone === "skipped") {
    return (
      <span className="pr-list-checks none" role="img" aria-label={ci.title} title={ci.title}>
        —
      </span>
    );
  }
  const glyph = (
    <span className={`pr-list-checks ${ci.tone}`}>
      <CheckRing ci={ci} />
      {ci.count !== undefined && <span>{ci.count}</span>}
    </span>
  );
  if (!runs || runs.length === 0) {
    return (
      <span role="img" aria-label={ci.title} title={ci.title}>
        {glyph}
      </span>
    );
  }
  return (
    <CheckRunsPopover
      runs={runs}
      label={ci.title}
      trigger={
        <button type="button" className="pr-list-checks-button" aria-label={ci.title}>
          {glyph}
        </button>
      }
    />
  );
}

const RING_RADIUS = 5.25;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

/** Failing and passing read as glyphs; running checks show how far along they are, when known. */
function CheckRing({ ci }: { ci: PageCi }) {
  if (ci.tone === "failing" || ci.tone === "cancelled") {
    return (
      <svg viewBox="0 0 14 14" aria-hidden>
        <circle className="failed" cx="7" cy="7" r="6" />
        <path className="cross" d="m5 5 4 4m0-4-4 4" />
      </svg>
    );
  }
  if (ci.tone === "passing" || ci.tone === "skipped") {
    return (
      <svg viewBox="0 0 14 14" aria-hidden>
        <circle className="passed" cx="7" cy="7" r="6" />
        <path className="tick" d="m4.4 7.1 1.7 1.7 3.5-3.6" />
      </svg>
    );
  }
  // Without counts the ring is a plain track: there's no progress to claim.
  return (
    <svg viewBox="0 0 14 14" aria-hidden>
      <circle className="track" cx="7" cy="7" r={RING_RADIUS} />
      {ci.progress !== undefined && (
        <circle
          className="progress"
          cx="7"
          cy="7"
          r={RING_RADIUS}
          strokeDasharray={`${Math.max(1.5, ci.progress * RING_CIRCUMFERENCE)} ${RING_CIRCUMFERENCE}`}
          transform="rotate(-90 7 7)"
        />
      )}
    </svg>
  );
}

function labelColor(color: string | null): string {
  return color && /^[0-9a-f]{6}$/i.test(color) ? `#${color}` : "var(--pr-list-muted)";
}
