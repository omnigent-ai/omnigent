// The Overview's Activity: the conversation, every review thread and the pushes in one
// timeline, narrowed by one kind at a time, with author, file, state, sort and search under
// the toolbar. Counts say what the reads can vouch for, and its states say what's loading,
// failed or unsupported. Anything on the page that opens an entry reveals it here.

import { type ReactNode, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  ArrowUpDownIcon,
  ChevronDownIcon,
  CircleCheckIcon,
  CircleSlashIcon,
  FileDiffIcon,
  GitBranchIcon,
  GitCommitHorizontalIcon,
  ListFilterIcon,
  MessageSquareIcon,
  MoreHorizontalIcon,
  RotateCwIcon,
  SearchIcon,
  XIcon,
} from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type {
  PullRequestComment,
  PullRequestReview,
  PullRequestReviewState,
  PullRequestTimelineEvent,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type { usePullRequestReviewThreads } from "@/newPullRequestPage/hooks/usePullRequestReviewThreads";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import type { ReviewThreadContext } from "@/newPullRequestPage/shell/PullRequestReviewThread";
import { PullRequestSectionHeader } from "@/shell/PullRequestSection";
import { useOwnsPaneCommands } from "@/store/PaletteCommandScope";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";
import { settingCommand } from "@/store/paletteSettings";
import { commentsOrderPreference, useCommentsOrder } from "@/pullRequestComments/commentsOrder";
import { ConversationEntry, ThreadEntry } from "@/pullRequestComments/PullRequestCommentsEntries";
import { useStableReviewThreadContext } from "@/pullRequestComments/useStableReviewThreadContext";
import {
  COMMENT_FILTER_TITLES,
  COMMENTS_SORTS,
  countLabel,
  filterValueTitle,
  searchCoverage,
  toggleFilter,
  type CommentFilter,
  type CommentFilterField,
  type CommentsSort,
  type CommentsView,
} from "@/pullRequestComments/pullRequestCommentsModel";
import "@/pullRequestComments/pullRequestCommentsPage.css";

import {
  ACTIVITY_FILTER_FIELDS,
  ACTIVITY_KIND_FILTERS,
  ACTIVITY_WINDOW,
  type ActivityCount,
  type ActivityEntry,
  type ActivityKindFilter,
  type ActivityReads,
  type ActivityRevealCleared,
  type ActivityTimelineState,
  activityFilterCounts,
  activityTimeline,
  activityWindow,
  DEFAULT_ACTIVITY_TIMELINE,
  postedActivityKey,
  REVIEW_VERDICTS,
  revealActivityEntry,
  windowIncluding,
} from "./activityTimeline";
import {
  ACTIVITY_HIGHLIGHT_MS,
  ACTIVITY_ID_ATTRIBUTE,
  type ActivityRevealRequest,
} from "./activityReveal";
import { INERT_SELECTOR } from "@/lib/inert";

const NO_COMMANDS: PaletteCommand[] = [];

/** The id of the Activity heading, which a reveal of the whole section focuses. */
export const ACTIVITY_HEADING_ID = "pull-request-activity";

const THREAD_VIEW_TITLES: Record<CommentsView, string> = {
  all: "All threads",
  unresolved: "Open",
  resolved: "Resolved",
};

export interface PullRequestActivityProps {
  /** False while Overview is hidden, so its `/` shortcut and ⌘K group stay out of the way. */
  active: boolean;
  comments: PullRequestComment[];
  /** The host returned only some conversation comments. */
  commentsPartial: boolean;
  /** Null where the read brings no push history, as this Mac's own read. */
  events: readonly PullRequestTimelineEvent[] | null;
  eventsPartial: boolean;
  /** Null where the read brings no reviews, as the host's; their bodies are comments then. */
  submittedReviews: readonly PullRequestReview[] | null;
  submittedReviewsPartial: boolean;
  reviews: ReturnType<typeof usePullRequestReviewThreads>;
  /** Review threads can be read here; without them only the conversation and pushes show. */
  threadsAvailable: boolean;
  context: ReviewThreadContext;
  providerLabel: string;
  pullRequestAuthor: string | null;
  pullRequestUrl: string;
  /** Opens a push's commit on Commits; absent where Commits can't show it. */
  openCommit?: (sha: string) => void;
  /** What the page asked Activity to show, for this pull request, reader and pane; settled once it's on screen. */
  revealRequest: ActivityRevealRequest | null;
  onRevealSettled: (id: number) => void;
}

const ACTIVITY_ATTRIBUTE = "data-pull-request-activity-section";

export function PullRequestActivity({
  active,
  comments,
  commentsPartial,
  events,
  eventsPartial,
  submittedReviews,
  submittedReviewsPartial,
  reviews,
  threadsAvailable,
  context: rebuiltContext,
  providerLabel,
  pullRequestAuthor,
  pullRequestUrl,
  openCommit,
  revealRequest,
  onRevealSettled,
}: PullRequestActivityProps) {
  const context = useStableReviewThreadContext(rebuiltContext);
  const sort = useCommentsOrder();
  // What shows is per pull request: another one starts with every kind, its newest entries
  // and its resolved threads folded.
  const [local, setLocal] = useState(() => freshLocal(pullRequestUrl));
  if (local.pullRequestUrl !== pullRequestUrl) setLocal(freshLocal(pullRequestUrl));
  // What shows: a kind or thread state the reads can't offer now, as threads while this pane
  // can't read them, falls back to all of it; the choice returns once they can again.
  const pushesRead = events !== null;
  const reviewsRead = submittedReviews !== null;
  const state = useMemo<ActivityTimelineState>(() => {
    const chosen = local.state;
    const unavailable =
      (chosen.kind === "thread" && !threadsAvailable) ||
      (chosen.kind === "review" && !reviewsRead) ||
      (chosen.kind === "push" && !pushesRead);
    return {
      ...chosen,
      kind: unavailable ? "all" : chosen.kind,
      threads: threadsAvailable ? chosen.threads : "all",
      sort,
    };
  }, [local.state, pushesRead, reviewsRead, sort, threadsAvailable]);
  const setState = useCallback(
    (update: (current: ActivityTimelineState) => ActivityTimelineState) =>
      setLocal((current) => ({ ...current, state: update(current.state), cleared: [] })),
    [],
  );
  const setExpanded = useCallback(
    (threadId: string, expanded: boolean) =>
      setLocal((current) => ({
        ...current,
        expanded: new Map(current.expanded).set(threadId, expanded),
      })),
    [],
  );
  const showHidden = useCallback(
    (commentId: string) =>
      setLocal((current) => ({
        ...current,
        revealed: new Set([...current.revealed, commentId]),
      })),
    [],
  );
  const searchRef = useRef<HTMLInputElement>(null);
  const rootRef = useRef<HTMLElement>(null);
  const host = useMemo(() => hostOf(pullRequestUrl), [pullRequestUrl]);

  const page = threadsAvailable ? reviews.page : undefined;
  const loadingThreads = threadsAvailable && !page && !reviews.error;
  const failedThreads = threadsAvailable && !page && !!reviews.error;
  const moreThreads = !!page?.has_more;
  const reads = useMemo<ActivityReads>(
    () => ({
      comments,
      commentsPartial,
      threads: page?.data ?? null,
      moreThreads,
      reviews: submittedReviews,
      reviewsPartial: submittedReviewsPartial,
      events,
      eventsPartial,
    }),
    [
      comments,
      commentsPartial,
      events,
      eventsPartial,
      moreThreads,
      page,
      submittedReviews,
      submittedReviewsPartial,
    ],
  );
  const timeline = useMemo(
    () => activityTimeline(reads, state, local.revealed),
    [local.revealed, reads, state],
  );
  const rows = activityWindow(timeline.entries, sort, local.windowSize);
  const valueCounts = useMemo(() => activityFilterCounts(reads), [reads]);
  // Search can miss later replies even when every thread is loaded, so it tracks its own coverage.
  const coverage = useMemo(
    () => searchCoverage({ moreThreads, commentsPartial, threads: page?.data ?? [] }),
    [commentsPartial, moreThreads, page],
  );
  const loadNextReplies = useCallback(() => {
    const next = coverage.threadsWithMoreReplies[0];
    if (next) context.loadComments(next);
  }, [context, coverage.threadsWithMoreReplies]);

  // A request for this pull request, reader and pane, once its target has been read.
  const [scrollTarget, setScrollTarget] = useState<string | null>(null);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  // A request is applied once, though it stays until the page settles it.
  const appliedRequest = useRef<number | null>(null);
  useEffect(() => {
    if (!revealRequest || appliedRequest.current === revealRequest.id) return;
    const { target } = revealRequest;
    if (target.kind === "section") {
      appliedRequest.current = revealRequest.id;
      setLocal((current) => ({ ...current, state: DEFAULT_ACTIVITY_TIMELINE, cleared: [] }));
      setScrollTarget(ACTIVITY_HEADING_ID);
      onRevealSettled(revealRequest.id);
      return;
    }
    const key = target.kind === "posted" ? postedActivityKey(reads, target.key) : target.key;
    const revealed = key && revealActivityEntry(reads, local.state, key, local.revealed);
    // Not read yet, as a posted comment before the refresh brings it: wait for the next read.
    if (!key || !revealed) return;
    appliedRequest.current = revealRequest.id;
    const entries = activityTimeline(reads, { ...revealed.state, sort }, local.revealed).entries;
    setLocal((current) => ({
      ...current,
      state: revealed.state,
      cleared: revealed.cleared,
      windowSize: windowIncluding(entries, sort, current.windowSize, key),
      expanded: key.startsWith("thread:")
        ? new Map(current.expanded).set(key.slice("thread:".length), true)
        : current.expanded,
    }));
    setScrollTarget(key);
    onRevealSettled(revealRequest.id);
  }, [local.revealed, local.state, onRevealSettled, reads, revealRequest, sort]);
  // Once the target renders: scroll to it, focus it and highlight it for a moment.
  useEffect(() => {
    if (!scrollTarget) return;
    const root = rootRef.current;
    const element =
      scrollTarget === ACTIVITY_HEADING_ID
        ? root
        : root?.querySelector<HTMLElement>(
            `[${ACTIVITY_ID_ATTRIBUTE}="${CSS.escape(scrollTarget)}"]`,
          );
    if (!element) return;
    element.scrollIntoView({ block: scrollTarget === ACTIVITY_HEADING_ID ? "start" : "center" });
    element.focus({ preventScroll: true });
    setScrollTarget(null);
    if (scrollTarget !== ACTIVITY_HEADING_ID) setHighlighted(scrollTarget);
    // The list it waits on changes with what shows and how much of it.
  }, [scrollTarget, timeline, rows.shown.length]);
  useEffect(() => {
    if (!highlighted) return;
    const timer = setTimeout(() => setHighlighted(null), ACTIVITY_HIGHLIGHT_MS);
    return () => clearTimeout(timer);
  }, [highlighted]);

  // `/` jumps to search from anywhere on the shown Overview, unless something else takes text.
  // Of two pages on screen, the focused pane's takes it.
  const ownsKeys = useOwnsPaneCommands();
  useEffect(() => {
    if (!active || !ownsKeys) return;
    const focusSearch = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      const root = rootRef.current;
      // A tab that isn't shown is hidden or inert; its page doesn't take the key.
      if (!root?.isConnected || root.closest(`[hidden], ${INERT_SELECTOR}`)) return;
      const target = event.target as HTMLElement | null;
      if (
        target?.closest("input, textarea, select, [contenteditable]:not([contenteditable='false'])")
      )
        return;
      // Pressed in another pull request's Activity or details, it's that one's.
      const owner = target?.closest(`[${ACTIVITY_ATTRIBUTE}], [data-pull-request-details]`);
      if (owner && owner !== root && owner !== root.closest("[data-pull-request-details]")) return;
      // A dialog or menu keeps the key, unless the page is in it, as in a sheet.
      const layer = target?.closest("[role='dialog'], [role='menu'], [role='listbox']");
      if (layer && !layer.contains(root)) return;
      event.preventDefault();
      searchRef.current?.focus();
    };
    document.addEventListener("keydown", focusSearch);
    return () => document.removeEventListener("keydown", focusSearch);
  }, [active, ownsKeys]);

  const setKind = useCallback(
    // A pill is one choice: picking one shows its kind whole, threads of every state included.
    (kind: ActivityKindFilter) => setState((current) => ({ ...current, kind, threads: "all" })),
    [setState],
  );
  const setThreadView = useCallback(
    (threads: CommentsView) => setState((current) => ({ ...current, kind: "thread", threads })),
    [setState],
  );
  const setQuery = useCallback(
    (query: string) => setState((current) => ({ ...current, query })),
    [setState],
  );
  const toggle = useCallback(
    (field: CommentFilterField, value: string) =>
      setState((current) => ({ ...current, filters: toggleFilter(current.filters, field, value) })),
    [setState],
  );
  const removeFilter = useCallback(
    (field: CommentFilterField) =>
      setState((current) => ({
        ...current,
        filters: current.filters.filter((filter) => filter.field !== field),
      })),
    [setState],
  );
  const clearAll = useCallback(
    () => setState((current) => ({ ...DEFAULT_ACTIVITY_TIMELINE, sort: current.sort })),
    [setState],
  );
  const focusSearch = useCallback(() => {
    searchRef.current?.focus();
    searchRef.current?.select();
  }, []);
  const filtersActive = state.filters.length > 0 || state.query.trim().length > 0;
  const narrowed = filtersActive || state.kind !== "all" || state.threads !== "all";
  const kinds = useMemo(
    () =>
      ACTIVITY_KIND_FILTERS.filter(
        (option) =>
          option.id === "all" ||
          option.id === "comment" ||
          (option.id === "thread" ? threadsAvailable : timeline.counts[option.id].known),
      ),
    [threadsAvailable, timeline.counts],
  );
  const kindLabel = useCallback(
    (kind: ActivityKindFilter) =>
      kind === "thread"
        ? threadPillCount(timeline.counts.thread, timeline.threadCounts.unresolved, loadingThreads)
        : formatCount(timeline.counts[kind], kind === "all" && loadingThreads),
    [loadingThreads, timeline.counts, timeline.threadCounts.unresolved],
  );

  // Every toolbar action, in ⌘K too, run by the same state the toolbar changes.
  const commands = useMemo<PaletteCommand[]>(() => {
    const owner = context.conversationId;
    const list: PaletteCommand[] = [
      {
        id: "comments.search",
        label: "Search comments",
        icon: SearchIcon,
        keywords: ["find", "comments", "activity", "review", "/"],
        movesFocus: true,
        run: focusSearch,
      },
      settingCommand({
        id: "comments.view",
        owner,
        label: "Show comments",
        icon: ListFilterIcon,
        keywords: [
          "comments",
          "activity",
          "threads",
          "reviews",
          "pushes",
          "resolved",
          "unresolved",
          "open",
        ],
        current: state.kind === "thread" && state.threads !== "all" ? state.threads : state.kind,
        choices: [
          ...kinds.map((option) => ({
            value: option.id,
            label: `${option.title} (${kindLabel(option.id)})`,
          })),
          ...(threadsAvailable
            ? (["unresolved", "resolved"] as const).map((view) => ({
                value: view,
                label: `Threads: ${THREAD_VIEW_TITLES[view]} (${formatCount(timeline.threadCounts[view], loadingThreads)})`,
              }))
            : []),
        ],
        choose: (value) => {
          if (value === "unresolved" || value === "resolved") setThreadView(value);
          else
            setState((current) => ({
              ...current,
              kind: value as ActivityKindFilter,
              threads: "all",
            }));
        },
      }),
      settingCommand({
        id: "comments.sort",
        owner,
        label: "Sort comments",
        icon: ArrowUpDownIcon,
        keywords: ["comments", "activity", "order", "newest", "oldest", "file"],
        current: sort,
        choices: COMMENTS_SORTS.map((option) => ({ value: option.id, label: option.title })),
        choose: (value) => {
          commentsOrderPreference.write(value as CommentsSort);
        },
      }),
    ];
    for (const field of ACTIVITY_FILTER_FIELDS) {
      const values = [...valueCounts[field]];
      if (values.length === 0) continue;
      const selected = state.filters.find((filter) => filter.field === field)?.values ?? [];
      list.push(
        settingCommand({
          id: `comments.filter.${field}`,
          owner,
          label: `Filter comments by ${COMMENT_FILTER_TITLES[field].toLowerCase()}`,
          icon: ListFilterIcon,
          keywords: ["comments", "activity", "filter"],
          current: selected[0] ?? "",
          choices: values.map(([value, count]) => ({
            value,
            label: `${selected.includes(value) ? "✓ " : ""}${filterValueTitle(field, value)} (${count})`,
          })),
          choose: (value) => toggle(field, value),
        }),
      );
    }
    if (narrowed) {
      list.push({
        id: "comments.clear",
        label: "Clear comment filters",
        icon: XIcon,
        keywords: ["comments", "activity", "filter", "search", "reset"],
        run: clearAll,
      });
    }
    if (moreThreads && !reviews.loadingMore) {
      list.push({
        id: "comments.load-more",
        label: "Load more conversations",
        icon: RotateCwIcon,
        keywords: ["comments", "review", "threads", "more"],
        run: reviews.loadMore,
      });
    }
    if (coverage.threadsWithMoreReplies.length > 0 && context.loadingComments === null) {
      list.push({
        id: "comments.load-more-replies",
        label: "Load more replies",
        icon: RotateCwIcon,
        keywords: ["comments", "review", "replies", "more"],
        run: loadNextReplies,
      });
    }
    return list;
  }, [
    clearAll,
    context.conversationId,
    context.loadingComments,
    coverage.threadsWithMoreReplies.length,
    focusSearch,
    kindLabel,
    kinds,
    loadNextReplies,
    loadingThreads,
    moreThreads,
    narrowed,
    reviews.loadMore,
    reviews.loadingMore,
    setState,
    setThreadView,
    sort,
    state.filters,
    state.kind,
    state.threads,
    threadsAvailable,
    timeline.threadCounts,
    toggle,
    valueCounts,
  ]);
  const paletteInstance = useId();
  usePaletteCommands(
    `pull-request-activity:${context.conversationId}:${paletteInstance}`,
    "Comments",
    active ? commands : NO_COMMANDS,
  );

  // A failed first read with nothing else read says so in place of the list, and only there.
  const failedAlone = failedThreads && reads.comments.length === 0 && !reads.events?.length;
  let body: ReactNode;
  if (failedAlone) {
    body = (
      <div className="comments-message" role="alert">
        <strong>Activity Unavailable</strong>
        <span>{reviews.error}</span>
        <button type="button" onClick={reviews.retry}>
          Try Again
        </button>
      </div>
    );
  } else if (rows.shown.length === 0) {
    body = loadingThreads ? (
      <div className="comments-message" aria-live="polite">
        <span className="comments-spinner" aria-hidden />
        <span>Loading activity…</span>
      </div>
    ) : (
      <ActivityEmptyState
        hasAny={
          reads.comments.length + (reads.threads?.length ?? 0) + (reads.events?.length ?? 0) > 0
        }
        // Only what was read is known to be empty: comments the host held back, or pushes
        // not read at all, could be there.
        unread={commentsPartial ? "comments" : events === null || eventsPartial ? "pushes" : null}
        narrowed={narrowed}
        partial={!coverage.complete}
        clearAll={clearAll}
      />
    );
  } else {
    const earlier = rows.hidden > 0 && (
      <div className="comments-more">
        <button
          type="button"
          onClick={() =>
            setLocal((current) => ({ ...current, windowSize: timeline.entries.length }))
          }
        >
          {sort === "file" ? `Show ${rows.hidden} more` : `Show ${rows.hidden} earlier`}
        </button>
      </div>
    );
    body = (
      <>
        {rows.hiddenAt === "start" && earlier}
        <ol className="comments-list" aria-label="Activity">
          {rows.shown.map((entry) => (
            <li
              key={entry.key}
              className={`comments-row${highlighted === entry.key ? " animate-message-highlight" : ""}`}
              {...{ [ACTIVITY_ID_ATTRIBUTE]: entry.key }}
              tabIndex={-1}
            >
              <ActivityEntryView
                entry={entry}
                context={context}
                host={host}
                pullRequestAuthor={pullRequestAuthor}
                pullRequestUrl={pullRequestUrl}
                expanded={
                  entry.kind === "thread"
                    ? (local.expanded.get(entry.thread.id) ?? !entry.thread.resolved)
                    : false
                }
                onExpandedChange={setExpanded}
                revealed={local.revealed}
                onReveal={showHidden}
                openCommit={openCommit}
              />
            </li>
          ))}
        </ol>
        {rows.hiddenAt === "end" && earlier}
      </>
    );
  }

  return (
    <section
      ref={rootRef}
      aria-labelledby={ACTIVITY_HEADING_ID}
      tabIndex={-1}
      className="comments-tab activity-section scroll-mt-4 outline-none"
      {...{ [ACTIVITY_ATTRIBUTE]: "" }}
    >
      <PullRequestSectionHeader id={ACTIVITY_HEADING_ID} title="Activity" />
      <div className="comments-toolbar">
        <nav className="comments-views" aria-label="Show">
          {kinds.map((option) =>
            option.id === "thread" ? (
              <ThreadsPill
                key={option.id}
                selected={state.kind === "thread"}
                view={state.threads}
                count={kindLabel("thread")}
                counts={timeline.threadCounts}
                loading={loadingThreads}
                onSelect={() => setKind("thread")}
                onView={setThreadView}
              />
            ) : (
              <button
                key={option.id}
                type="button"
                className={`comments-view${state.kind === option.id ? " selected" : ""}`}
                aria-pressed={state.kind === option.id}
                onClick={() => setKind(option.id)}
              >
                {option.title}
                <span className="comments-view-count">{kindLabel(option.id)}</span>
              </button>
            ),
          )}
        </nav>
        <span className="comments-toolbar-spacer" />
        <label className="comments-search">
          <SearchIcon aria-hidden />
          <input
            ref={searchRef}
            type="search"
            value={state.query}
            placeholder="Search activity"
            aria-label="Search comments"
            spellCheck={false}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Escape") return;
              event.preventDefault();
              event.stopPropagation();
              if (state.query) setQuery("");
              else event.currentTarget.blur();
            }}
          />
          {state.query ? (
            <button type="button" aria-label="Clear search" onClick={() => setQuery("")}>
              <XIcon aria-hidden />
            </button>
          ) : (
            <kbd aria-hidden>/</kbd>
          )}
        </label>
        <ActivityOptionsMenu
          filters={state.filters}
          counts={valueCounts}
          sort={sort}
          narrowed={narrowed}
          onToggle={toggle}
          onSort={commentsOrderPreference.write}
          onClear={clearAll}
        />
      </div>
      {(state.filters.length > 0 || local.cleared.length > 0) && (
        <div className="comments-toolbar activity-chips">
          {state.filters.map((filter) => (
            <FilterChip
              key={filter.field}
              filter={filter}
              onRemove={() => removeFilter(filter.field)}
            />
          ))}
          {local.cleared.length > 0 && (
            <span className="comments-notice-inline" role="status">
              {clearedText(local.cleared)}
            </span>
          )}
        </div>
      )}
      <Notices
        threadsAvailable={threadsAvailable}
        reviews={reviews}
        hasPage={!!page}
        showError={!failedAlone}
      />
      {body}
      {page && (moreThreads || (filtersActive && !coverage.complete)) && (
        <div className="comments-more">
          {filtersActive && !coverage.complete && (
            <span>Search and filters cover only the activity loaded so far.</span>
          )}
          {moreThreads && (
            <button type="button" disabled={reviews.loadingMore} onClick={reviews.loadMore}>
              {reviews.loadingMore ? "Loading conversations…" : "Load more conversations"}
            </button>
          )}
          {/* A search can't show a thread whose matching reply hasn't loaded, so offer it here. */}
          {filtersActive && coverage.threadsWithMoreReplies.length > 0 && (
            <button
              type="button"
              disabled={context.loadingComments !== null}
              onClick={loadNextReplies}
            >
              {context.loadingComments !== null
                ? "Loading replies…"
                : `Load more replies (${coverage.threadsWithMoreReplies.length} ${coverage.threadsWithMoreReplies.length === 1 ? "conversation" : "conversations"})`}
            </button>
          )}
        </div>
      )}
      {commentsPartial && (
        <p className="comments-truncated">
          This pull request has more comments than Omnigent shows.{" "}
          <a href={pullRequestUrl} target="_blank" rel="noreferrer">
            See them on {providerLabel}
          </a>
        </p>
      )}
    </section>
  );
}

interface LocalActivity {
  pullRequestUrl: string;
  /** Everything but the sort, which is a preference of its own. */
  state: ActivityTimelineState;
  /** What the last reveal cleared to show its entry, said until the person changes a filter. */
  cleared: ActivityRevealCleared[];
  windowSize: number;
  expanded: Map<string, boolean>;
  revealed: ReadonlySet<string>;
}

function freshLocal(pullRequestUrl: string): LocalActivity {
  return {
    pullRequestUrl,
    state: DEFAULT_ACTIVITY_TIMELINE,
    cleared: [],
    windowSize: ACTIVITY_WINDOW,
    expanded: new Map(),
    revealed: new Set(),
  };
}

function formatCount(count: ActivityCount, loading: boolean): string {
  if (loading) return "…";
  return count.known ? countLabel(count.count, count.lowerBound) : "…";
}

function threadPillCount(all: ActivityCount, open: ActivityCount, loading: boolean): string {
  if (loading || !all.known) return "…";
  const total = countLabel(all.count, all.lowerBound);
  if (!open.known || open.count === 0) return total;
  return `${total} · ${countLabel(open.count, open.lowerBound)} open`;
}

function clearedText(cleared: readonly ActivityRevealCleared[]): string {
  const names = cleared.map((field) =>
    field === "search" ? "the search" : `the ${COMMENT_FILTER_TITLES[field].toLowerCase()} filter`,
  );
  const list =
    names.length <= 1 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
  return `Cleared ${list} to show it.`;
}

function ActivityEntryView({
  entry,
  context,
  host,
  pullRequestAuthor,
  pullRequestUrl,
  expanded,
  onExpandedChange,
  revealed,
  onReveal,
  openCommit,
}: {
  entry: ActivityEntry;
  context: ReviewThreadContext;
  host: string | undefined;
  pullRequestAuthor: string | null;
  pullRequestUrl: string;
  expanded: boolean;
  onExpandedChange: (threadId: string, expanded: boolean) => void;
  revealed: ReadonlySet<string>;
  onReveal: (commentId: string) => void;
  openCommit?: (sha: string) => void;
}) {
  if (entry.kind === "thread") {
    return (
      <ThreadEntry
        thread={entry.thread}
        context={context}
        host={host}
        pullRequestAuthor={pullRequestAuthor}
        pullRequestUrl={pullRequestUrl}
        expanded={expanded}
        onExpandedChange={onExpandedChange}
        revealed={revealed}
        onReveal={onReveal}
      />
    );
  }
  if (entry.kind === "comment") {
    return (
      <ConversationEntry
        comment={entry.comment}
        host={host}
        pullRequestAuthor={pullRequestAuthor}
        pullRequestUrl={pullRequestUrl}
        context={context}
      />
    );
  }
  if (entry.kind === "review") {
    return (
      <ReviewEntry review={entry.review}>
        {entry.comment && (
          <ConversationEntry
            comment={entry.comment}
            host={host}
            pullRequestAuthor={pullRequestAuthor}
            pullRequestUrl={pullRequestUrl}
            context={context}
          />
        )}
      </ReviewEntry>
    );
  }
  return <PushEntry event={entry.event} openCommit={openCommit} />;
}

const REVIEW_ICONS: Record<PullRequestReviewState, typeof CircleCheckIcon> = {
  APPROVED: CircleCheckIcon,
  CHANGES_REQUESTED: FileDiffIcon,
  COMMENTED: MessageSquareIcon,
  DISMISSED: CircleSlashIcon,
};

/** A review: who gave which verdict and when, with its body under it where it has one. */
function ReviewEntry({ review, children }: { review: PullRequestReview; children: ReactNode }) {
  const timestamp = review.submitted_at ? Date.parse(review.submitted_at) : NaN;
  const time = relativeTime(timestamp);
  const { action } = REVIEW_VERDICTS[review.state];
  const Icon = REVIEW_ICONS[review.state];
  const author = review.author ?? "Someone";
  return (
    <div className="activity-review" data-verdict={review.state}>
      <div
        className="activity-event"
        role="group"
        aria-label={[author, action, time].filter(Boolean).join(" ")}
      >
        <Icon aria-hidden className="activity-event-icon" />
        <span className="activity-event-author" title={review.author ?? undefined}>
          {author}
        </span>
        <span className="activity-event-action">{action}</span>
        {time && (
          <time
            dateTime={review.submitted_at!}
            title={absoluteTime(timestamp)}
            className="activity-event-time"
          >
            {time}
          </time>
        )}
      </div>
      {children && <div className="activity-review-body">{children}</div>}
    </div>
  );
}

/** A push: who pushed which commit and when, opening the commit on Commits where it can. */
function PushEntry({
  event,
  openCommit,
}: {
  event: PullRequestTimelineEvent;
  openCommit?: (sha: string) => void;
}) {
  const timestamp = event.created_at ? Date.parse(event.created_at) : NaN;
  const time = relativeTime(timestamp);
  const sha = event.sha?.slice(0, 7);
  const action = event.kind === "force_push" ? "force-pushed" : "pushed";
  const Icon = event.kind === "force_push" ? GitBranchIcon : GitCommitHorizontalIcon;
  const fullSha = event.sha;
  const content = (
    <>
      <Icon aria-hidden className="activity-event-icon" />
      <span className="activity-event-author" title={event.author ?? undefined}>
        {event.author ?? "Someone"}
      </span>
      <span className="activity-event-action">{action}</span>
      {sha && <code className="activity-event-sha">{sha}</code>}
      {event.message && <span className="activity-event-message">{event.message}</span>}
      {time && (
        <time
          dateTime={event.created_at!}
          title={absoluteTime(timestamp)}
          className="activity-event-time"
        >
          {time}
        </time>
      )}
    </>
  );
  const label = [event.author ?? "Someone", action, sha, time].filter(Boolean).join(" ");
  return fullSha && openCommit ? (
    <button
      type="button"
      className="activity-event activity-push"
      aria-label={`${label}${event.message ? `: ${event.message}` : ""}, open on Commits`}
      onClick={() => openCommit(fullSha)}
    >
      {content}
    </button>
  ) : (
    <div className="activity-event activity-push" role="group" aria-label={label}>
      {content}
    </div>
  );
}

/** The Threads pill: picking it shows every thread; its menu narrows to open or resolved ones. */
function ThreadsPill({
  selected,
  view,
  count,
  counts,
  loading,
  onSelect,
  onView,
}: {
  selected: boolean;
  view: CommentsView;
  count: string;
  counts: Record<CommentsView, ActivityCount>;
  loading: boolean;
  onSelect: () => void;
  onView: (view: CommentsView) => void;
}) {
  const title = selected && view !== "all" ? `Threads · ${THREAD_VIEW_TITLES[view]}` : "Threads";
  return (
    <span className={`comments-view-split${selected ? " selected" : ""}`}>
      <button
        type="button"
        className={`comments-view${selected ? " selected" : ""}`}
        aria-pressed={selected}
        onClick={onSelect}
      >
        {title}
        <span className="comments-view-count">
          {selected && view !== "all" ? formatCount(counts[view], loading) : count}
        </span>
      </button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className={`comments-view comments-view-menu${selected ? " selected" : ""}`}
            aria-label="Thread state"
          >
            <ChevronDownIcon aria-hidden />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          <DropdownMenuRadioGroup
            value={selected ? view : ""}
            onValueChange={(value) => onView(value as CommentsView)}
          >
            {(["all", "unresolved", "resolved"] as const).map((option) => (
              <DropdownMenuRadioItem key={option} value={option}>
                <span className="min-w-0 flex-1">{THREAD_VIEW_TITLES[option]}</span>
                <span className="ml-3 text-xs tabular-nums text-muted-foreground">
                  {formatCount(counts[option], loading)}
                </span>
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </span>
  );
}

const FAILURE_TITLES = {
  read: "Couldn’t refresh review conversations",
  threads: "Couldn’t load more conversations",
  replies: "Couldn’t load more replies",
} as const;

/** What isn't shown, or may be out of date, above the list. */
function Notices({
  threadsAvailable,
  reviews,
  hasPage,
  showError,
}: {
  threadsAvailable: boolean;
  reviews: ReturnType<typeof usePullRequestReviewThreads>;
  hasPage: boolean;
  /** False where the list's own place already says the read failed. */
  showError: boolean;
}) {
  if (!threadsAvailable) {
    return (
      <p className="comments-notice" role="status">
        Review conversations aren’t available here, so only the conversation shows.
      </p>
    );
  }
  const warning = reviews.page?.warning;
  return (
    <>
      {showError && reviews.error && (
        <p className="comments-notice warning" role="alert">
          {FAILURE_TITLES[(hasPage && reviews.failure) || "read"]}: {reviews.error}{" "}
          <button type="button" onClick={reviews.retry}>
            Try Again
          </button>
        </p>
      )}
      {warning && (
        <p className="comments-notice" role="status">
          {warning}
        </p>
      )}
      {reviews.historyStale && (
        <p className="comments-notice" role="status">
          Comments changed on GitHub since these loaded.{" "}
          <button type="button" onClick={reviews.refreshHistory}>
            Refresh
          </button>
        </p>
      )}
    </>
  );
}

function ActivityEmptyState({
  hasAny,
  unread,
  narrowed,
  partial,
  clearAll,
}: {
  hasAny: boolean;
  /** What may exist though none was read: comments the host held back, or the pushes. */
  unread: "comments" | "pushes" | null;
  narrowed: boolean;
  /** More conversations are still on the host, so this covers only what's loaded. */
  partial: boolean;
  clearAll: () => void;
}) {
  if (!hasAny) {
    const title =
      unread === "comments"
        ? "No activity loaded"
        : unread === "pushes"
          ? "No comments yet"
          : "No activity yet";
    return (
      <div className="comments-empty" role="status">
        <strong>{title}</strong>
        {unread !== "comments" && (
          <span>Start the conversation below, or select lines in Changes to comment on them.</span>
        )}
      </div>
    );
  }
  return (
    <div className="comments-empty" role="status">
      <strong>{partial ? "No matches in the loaded activity" : "Nothing matches"}</strong>
      {narrowed && <span>Try other filters or search words.</span>}
      <div className="comments-empty-actions">
        <button type="button" onClick={clearAll}>
          Clear Filters
        </button>
      </div>
    </div>
  );
}

function FilterChip({ filter, onRemove }: { filter: CommentFilter; onRemove: () => void }) {
  const title = (value: string) => filterValueTitle(filter.field, value);
  const fieldTitle = COMMENT_FILTER_TITLES[filter.field];
  const value =
    filter.values.length === 1
      ? title(filter.values[0]!)
      : `${filter.values.length} ${fieldTitle.toLowerCase()}s`;
  return (
    <span className="comments-chip">
      <span className="comments-chip-field">{fieldTitle}</span>
      <span className="comments-chip-operator">
        {filter.values.length === 1 ? "is" : "is any of"}
      </span>
      <span className="comments-chip-value" title={filter.values.map(title).join(", ")}>
        {value}
      </span>
      <button type="button" aria-label={`Remove ${fieldTitle} filter`} onClick={onRemove}>
        <XIcon aria-hidden />
      </button>
    </span>
  );
}

/** ⋯: a submenu per filter field with each value's count, the sort, and clearing everything. */
function ActivityOptionsMenu({
  filters,
  counts,
  sort,
  narrowed,
  onToggle,
  onSort,
  onClear,
}: {
  filters: readonly CommentFilter[];
  counts: Record<CommentFilterField, Map<string, number>>;
  sort: CommentsSort;
  narrowed: boolean;
  onToggle: (field: CommentFilterField, value: string) => void;
  onSort: (sort: CommentsSort) => void;
  onClear: () => void;
}) {
  const checked = (field: CommentFilterField, value: string) =>
    filters.some((filter) => filter.field === field && filter.values.includes(value));
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className={`comments-toolbar-button icon${filters.length > 0 ? " active" : ""}`}
          aria-label="Activity options"
          title="Filter and sort"
        >
          <MoreHorizontalIcon aria-hidden />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-44">
        {ACTIVITY_FILTER_FIELDS.map((field) => {
          const values = [...counts[field]].sort(
            (left, right) => right[1] - left[1] || left[0].localeCompare(right[0]),
          );
          return (
            <DropdownMenuSub key={field}>
              <DropdownMenuSubTrigger disabled={values.length === 0}>
                {COMMENT_FILTER_TITLES[field]}
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent className="max-h-80 min-w-52 overflow-y-auto">
                {values.map(([value, count]) => (
                  <DropdownMenuCheckboxItem
                    key={value}
                    checked={checked(field, value)}
                    // The menu stays open, so several values can be picked in a row.
                    onSelect={(event) => event.preventDefault()}
                    onCheckedChange={() => onToggle(field, value)}
                    title={value}
                  >
                    <span className="min-w-0 flex-1 truncate">
                      {filterValueTitle(field, value)}
                    </span>
                    <span className="ml-3 text-xs tabular-nums text-muted-foreground">{count}</span>
                  </DropdownMenuCheckboxItem>
                ))}
              </DropdownMenuSubContent>
            </DropdownMenuSub>
          );
        })}
        <DropdownMenuSub>
          <DropdownMenuSubTrigger>Sort</DropdownMenuSubTrigger>
          <DropdownMenuSubContent>
            <DropdownMenuRadioGroup
              value={sort}
              onValueChange={(value) => onSort(value as CommentsSort)}
            >
              {COMMENTS_SORTS.map((option) => (
                <DropdownMenuRadioItem key={option.id} value={option.id}>
                  {option.title}
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuSubContent>
        </DropdownMenuSub>
        <DropdownMenuSeparator />
        <DropdownMenuItem disabled={!narrowed} onSelect={onClear}>
          Clear filters
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function hostOf(url: string): string | undefined {
  try {
    return new URL(url).host.toLowerCase();
  } catch {
    return undefined;
  }
}
