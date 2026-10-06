/**
 * Inbox page (``/inbox``) — every approval prompt waiting on the user,
 * across loaded sessions, rendered as actionable cards.
 *
 * Built entirely from existing primitives:
 *
 * - The shared sidebar caches carry
 *   `pending_elicitations_count` per row, kept live by the
 *   `WS /v1/sessions/updates` stream. Manual pagination grows the
 *   same caches used by the sidebar and its badge.
 * - Each session's snapshot (`GET /v1/sessions/{id}`) already replays
 *   the full pending `response.elicitation_request` event dicts; the
 *   per-session query key includes the row's count so a count change
 *   pushed over the socket refetches exactly that session.
 * - Cards are the same `ApprovalCard` the chat renders, with a local
 *   submit handler (the chat store is single-conversation, so the
 *   inbox posts the verdict itself via `approve()` — same endpoint).
 *
 * Only the first (newest) card is expanded by default; the rest
 * collapse to a one-line summary row so a long backlog stays
 * scannable. Clicking a row toggles it; manual toggles stick even
 * as new items arrive (overrides are keyed by elicitation id).
 *
 * Below the approvals, the inbox lists unseen file comments — draft
 * comments other users left on session files (`useCommentInbox`),
 * each iconed with the author's avatar pill. A comment clears when
 * it's actually opened in the file browser — the FileViewer records
 * it in the client-side seen registry (`useSeenComments`) while the
 * comments panel is open on its file; the "Open file" link deep-links
 * to exactly that (`?file=` + `?comment=` auto-opens the panel).
 *
 * After those come sessions with unseen agent output — the same
 * read-state behind the sidebar's unread dot — as compact rows previewing
 * the latest reply. Opening the session or "Mark as read" clears a row.
 * Tabs narrow the list to "Unread" (comments + unread sessions) or
 * "Awaiting response" (approvals); the pick persists per device.
 *
 * Deliberately NOT here: dismissing approvals and mentions — neither
 * exists as a server concept. Resolving (or the prompt timing out) is
 * what clears an approval.
 */

import { useEffect, useRef, useState } from "react";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangleIcon,
  ArrowRightIcon,
  CheckIcon,
  ChevronDownIcon,
  CircleAlertIcon,
  CircleCheckIcon,
  InboxIcon,
  Loader2Icon,
} from "lucide-react";
import { ApprovalCard, type SubmitApprovalFn } from "@/components/blocks/ApprovalCard";
import { PageScroll } from "@/components/PageScroll";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useSidebarData } from "@/hooks/useSidebarData";
import {
  isConversationUnseen,
  markConversationRead,
  useUnseenTick,
} from "@/hooks/useUnseenConversations";
import {
  collectInboxItems,
  collectUnreadInboxItems,
  type InboxItem,
  type InboxSource,
  type UnreadInboxItem,
} from "@/lib/inbox";
import {
  isInboxFilter,
  readInboxFilter,
  writeInboxFilter,
  type InboxFilter,
} from "@/lib/inboxFilterPreferences";
import { latestOutputPreview } from "@/lib/lastAssistantText";
import { relativeTime } from "@/lib/relativeTime";
import { Link } from "@/lib/routing";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { approve, fetchSessionItemsPage, getSession } from "@/lib/sessionsApi";
import { userColor, userInitials } from "@/lib/userBadge";
import { cn } from "@/lib/utils";
import { conversationDisplayLabel, getConversationAgentType } from "@/shell/sidebarNav";

/** Optimistic verdicts keyed by elicitation id, mirroring the chat store's flip. */
type RespondedMap = Record<
  string,
  {
    action: "accept" | "decline";
    content?: Record<string, unknown>;
    _meta?: Record<string, unknown>;
  }
>;

const INBOX_TABS: { value: InboxFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "unread", label: "Unread" },
  { value: "awaiting", label: "Awaiting response" },
];

const EMPTY_STATES: Record<InboxFilter, { title: string; body: string }> = {
  all: {
    title: "Nothing waiting on you",
    body: "When an agent replies or needs your input, or someone comments on a file, it will show up here.",
  },
  unread: {
    title: "You’re all caught up",
    body: "New agent replies and unseen file comments will show up here.",
  },
  awaiting: {
    title: "No approvals waiting",
    body: "When an agent needs your input, it will show up here.",
  },
};

// Enough trailing items to step past a tool call or two after the final reply.
const PREVIEW_SCAN_ITEMS = 12;
const PREVIEW_MAX_CHARS = 280;

export function InboxPage() {
  const queryClient = useQueryClient();
  const { trackClick } = useOmnigentAnalytics();
  const {
    inbox: conversationsQuery,
    inboxRows: allRows,
    comments: commentInbox,
  } = useSidebarData();
  const [filter, setFilter] = useState<InboxFilter>(readInboxFilter);
  const [responded, setResponded] = useState<RespondedMap>({});
  // Manual expand/collapse toggles keyed by elicitation id. Anything
  // not in the map falls back to the default: expanded only for the
  // first (newest) item. Keying by id (not index) keeps a user's
  // explicit toggles stable when new items shift positions.
  const [expandedOverrides, setExpandedOverrides] = useState<Record<string, boolean>>({});
  // Unread rows start collapsed; toggles are keyed by session id.
  const [expandedUnread, setExpandedUnread] = useState<Record<string, boolean>>({});

  const { hasNextPage, isFetchingNextPage, fetchNextPage } = conversationsQuery;
  const rows = allRows.filter((c) => (c.pending_elicitations_count ?? 0) > 0);

  // One snapshot fetch per session that reports pending prompts. The
  // count rides in the query key, so the WS count patch (new prompt,
  // resolved-elsewhere prompt) naturally triggers a refetch; a row
  // dropping to zero falls out of `rows` and its query is dropped.
  // `retry: 1` absorbs a transient blip without hammering a down
  // server; persistent failures surface in the error banner below.
  const snapshotQueries = useQueries({
    queries: rows.map((row) => ({
      queryKey: ["inbox-elicitations", row.id, row.pending_elicitations_count, row.updated_at],
      queryFn: () => getSession(row.id),
      retry: 1,
    })),
  });

  const sources: InboxSource[] = [];
  rows.forEach((row, i) => {
    const snapshot = snapshotQueries[i]?.data;
    if (snapshot) sources.push({ row, pendingElicitations: snapshot.pendingElicitations ?? [] });
  });
  const items = collectInboxItems(sources);

  // Subscribed to the read-state mirror so "Mark as read" (here or in the
  // sidebar) drops the row immediately instead of on the next list poll.
  useUnseenTick();
  const unreadItems = collectUnreadInboxItems(allRows, isConversationUnseen);
  const showApprovals = filter !== "unread";
  const showUnread = filter !== "awaiting";

  // Latest-output preview per unread session. `updated_at` in the key
  // refetches after a new turn; a failed fetch just leaves the row bare.
  const previewQueries = useQueries({
    queries: unreadItems.map(({ row }) => ({
      queryKey: ["inbox-unread-preview", row.id, row.updated_at],
      queryFn: async () => {
        const page = await fetchSessionItemsPage(row.id, { limit: PREVIEW_SCAN_ITEMS });
        return latestOutputPreview(page.items, PREVIEW_MAX_CHARS) ?? null;
      },
      enabled: showUnread,
      staleTime: Infinity,
      retry: 1,
    })),
  });
  const previewBySession = new Map(
    unreadItems.map(({ row }, i) => [row.id, previewQueries[i]?.data ?? undefined]),
  );

  const visibleApprovals = showApprovals ? items : [];
  const visibleComments = showUnread ? commentInbox.items : [];
  const visibleUnread = showUnread ? unreadItems : [];
  const visibleCount = visibleApprovals.length + visibleComments.length + visibleUnread.length;
  const summary = [
    items.length > 0 && (items.length === 1 ? "1 approval" : `${items.length} approvals`),
    commentInbox.items.length > 0 &&
      (commentInbox.items.length === 1 ? "1 comment" : `${commentInbox.items.length} comments`),
    unreadItems.length > 0 && `${unreadItems.length} unread`,
  ].filter(Boolean);
  const emptyState = EMPTY_STATES[filter];

  // Clear stale optimistic verdicts when snapshot data refreshes.
  // If a hook retry re-parks the same elicitation id after the user
  // approved the previous attempt, the local `responded` entry would
  // otherwise keep the card stuck on "Approved" indefinitely. When
  // any snapshot query delivers fresh data (dataUpdatedAt advances),
  // sweep verdicts whose id is still pending on the server — those
  // approvals were consumed and the server re-parked the prompt.
  const snapshotVersionKey = snapshotQueries.map((q) => q.dataUpdatedAt ?? 0).join(",");
  const isFirstRender = useRef(true);
  useEffect(() => {
    // Skip the first render — there are no stale verdicts yet.
    if (isFirstRender.current) {
      isFirstRender.current = false;
      return;
    }
    setResponded((prev) => {
      if (Object.keys(prev).length === 0) return prev;
      const pendingIds = new Set(items.map((i) => i.elicitation.elicitationId));
      const stale = Object.keys(prev).filter((id) => pendingIds.has(id));
      if (stale.length === 0) return prev;
      return Object.fromEntries(Object.entries(prev).filter(([id]) => !pendingIds.has(id)));
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshotVersionKey]);

  // "Settled" gating for the empty state: while the session list is
  // still paging or ANY snapshot is in flight, an empty `items` only
  // means "not assembled yet" — showing "No approvals waiting" then
  // would be a lie. Failed snapshots also block the empty state (their
  // approvals exist, we just couldn't fetch them) and get a banner.
  const assembling =
    conversationsQuery.isLoading ||
    isFetchingNextPage ||
    snapshotQueries.some((q) => q.isLoading) ||
    commentInbox.isLoading;
  const failedSnapshots = snapshotQueries.filter((q) => q.isError);
  const failedSessionCount =
    failedSnapshots.length + commentInbox.failedCount + Number(Boolean(conversationsQuery.isError));

  // Mirrors `chatStore.submitApproval`: optimistic flip → resolve POST →
  // rollback on error. Success invalidates the session list so the row's
  // count (and the sidebar badge) drop without waiting for the socket.
  const makeSubmit = (item: InboxItem): SubmitApprovalFn => {
    return (elicitationId, action, content, meta) => {
      setResponded((prev) => ({
        ...prev,
        [elicitationId]: {
          action,
          ...(content === undefined ? {} : { content }),
          ...(meta === undefined ? {} : { _meta: meta }),
        },
      }));
      void approve(item.resolveSessionId, elicitationId, {
        action,
        ...(content === undefined ? {} : { content }),
        ...(meta === undefined ? {} : { _meta: meta }),
      }).then(
        () => {
          void queryClient.invalidateQueries({ queryKey: ["conversations"] });
        },
        () => {
          // Roll back to pending so the buttons reappear and the user
          // can retry — same recovery the chat store uses.
          setResponded((prev) => {
            const { [elicitationId]: _respondedVerdict, ...pendingVerdicts } = prev;
            return pendingVerdicts;
          });
        },
      );
    };
  };

  return (
    <PageScroll contentClassName="px-4 md:px-6">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Inbox</h1>
        {summary.length > 0 && (
          <span className="text-ui text-muted-foreground">{summary.join(" · ")}</span>
        )}
      </div>

      <Tabs
        value={filter}
        onValueChange={(value) => {
          if (!isInboxFilter(value)) return;
          setFilter(value);
          writeInboxFilter(value);
        }}
        componentId="inbox.filter"
        className="mb-4"
      >
        <TabsList variant="pill" aria-label="Inbox filter" className="gap-1">
          {INBOX_TABS.map(({ value, label }) => (
            <TabsTrigger key={value} value={value} className="h-7 flex-none px-2.5">
              {label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {failedSessionCount > 0 && (
        <div
          data-testid="inbox-load-error"
          className="mb-4 flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-ui"
        >
          <AlertTriangleIcon className="size-4 shrink-0 text-destructive" />
          <span className="flex-1">
            Couldn’t load inbox items from {failedSessionCount}{" "}
            {failedSessionCount === 1 ? "session" : "sessions"}.
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              failedSnapshots.forEach((q) => void q.refetch());
              commentInbox.retryFailed();
              if (conversationsQuery.isError) void conversationsQuery.refetch?.();
            }}
            componentId="inbox.retry"
          >
            Retry
          </Button>
        </div>
      )}

      {assembling && visibleCount === 0 && (
        <div className="flex items-center gap-2 py-12 text-ui text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading inbox…
        </div>
      )}

      {!assembling && failedSessionCount === 0 && visibleCount === 0 && (
        <div className="flex flex-col items-center gap-2 py-16 text-center">
          <InboxIcon className="size-8 text-muted-foreground/50" />
          <p className="text-ui font-medium">
            {hasNextPage ? "Nothing waiting in these sessions" : emptyState.title}
          </p>
          <p className="text-sm text-muted-foreground">{emptyState.body}</p>
        </div>
      )}

      <div className="flex flex-col gap-4">
        {visibleApprovals.map((item, index) => {
          const elicitationId = item.elicitation.elicitationId;
          const verdict = responded[elicitationId];
          const expanded = expandedOverrides[elicitationId] ?? index === 0;
          // Same display mapping the sidebar uses: native-wrapper
          // sessions read "Claude Code" / "Codex", never the internal
          // agent name ("claude-native-ui"). The agent chip is hidden
          // when it would just repeat the title (untitled native
          // sessions, where the wrapper label IS the display label).
          const title = conversationDisplayLabel(item.row);
          const agentLabel = getConversationAgentType(item.row);
          return (
            <div
              key={elicitationId}
              data-testid="inbox-item"
              data-expanded={expanded}
              className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4"
            >
              <div className="flex items-center gap-2">
                {/* The toggle is a sibling of the Open-session link (not a
                    parent) — nesting a link inside a button is invalid HTML
                    and breaks middle-click/new-tab behavior. */}
                <button
                  type="button"
                  aria-expanded={expanded}
                  onClick={() => {
                    trackClick("inbox.approval.toggle_expanded", "button");
                    setExpandedOverrides((prev) => ({ ...prev, [elicitationId]: !expanded }));
                  }}
                  className="flex min-w-0 flex-1 cursor-pointer items-center gap-2 text-left"
                >
                  <ChevronDownIcon
                    className={cn(
                      "size-4 shrink-0 text-muted-foreground transition-transform",
                      !expanded && "-rotate-90",
                    )}
                  />
                  <span className="min-w-0 shrink-0 truncate text-ui font-medium">
                    {title}
                    {agentLabel !== title && (
                      <span className="ml-2 text-sm font-normal text-muted-foreground">
                        {agentLabel}
                      </span>
                    )}
                  </span>
                  {!expanded && (
                    <span className="min-w-0 truncate text-sm text-muted-foreground">
                      {item.elicitation.message}
                    </span>
                  )}
                </button>
                <span className="flex shrink-0 items-center gap-2">
                  <span className="text-sm text-muted-foreground">
                    {/* Server timestamps are epoch seconds; relativeTime takes ms. */}
                    {relativeTime(item.row.updated_at * 1000)}
                  </span>
                  <Button asChild variant="ghost" size="sm" className="text-sm">
                    <Link to={`/c/${item.row.id}`} componentId="inbox.approval.open_session">
                      Open session
                      <ArrowRightIcon className="ml-1 size-3.5" />
                    </Link>
                  </Button>
                </span>
              </div>
              {expanded && (
                <ApprovalCard
                  elicitationId={elicitationId}
                  message={item.elicitation.message}
                  phase={item.elicitation.phase}
                  policyName={item.elicitation.policyName}
                  contentPreview={item.elicitation.contentPreview}
                  requestedSchema={item.elicitation.requestedSchema}
                  url={item.elicitation.url}
                  status={verdict ? "responded" : "pending"}
                  response={verdict ?? null}
                  askUserQuestion={item.elicitation.askUserQuestion}
                  exitPlanMode={item.elicitation.exitPlanMode}
                  codexCommand={item.elicitation.codexCommand}
                  allowAllEdits={item.elicitation.allowAllEdits}
                  allowAutoMode={item.elicitation.allowAutoMode}
                  rememberScope={item.elicitation.rememberScope}
                  codexPersistModes={item.elicitation.codexPersistModes}
                  onSubmit={makeSubmit(item)}
                />
              )}
            </div>
          );
        })}
        {visibleComments.map((item) => {
          const comment = item.comment;
          // Single-user mode stores no author; mirror CommentsPanel's
          // "You" fallback (the only human in that mode is the viewer).
          const author = comment.created_by ?? "You";
          const sessionTitle = conversationDisplayLabel(item.row);
          return (
            <div
              key={comment.id}
              data-testid="inbox-comment"
              className="flex gap-3 rounded-xl border border-border bg-card p-4"
            >
              {/* The item's icon: the author's avatar pill (same
                  deterministic initials + color as presence circles). */}
              <Avatar size="sm" className="mt-0.5">
                <AvatarFallback
                  className="font-medium text-white"
                  style={{ backgroundColor: userColor(author) }}
                >
                  {userInitials(author)}
                </AvatarFallback>
              </Avatar>
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <div className="flex items-center gap-2">
                  <span className="min-w-0 truncate text-ui">
                    <span className="font-medium">{author}</span>
                    <span className="text-muted-foreground"> commented on </span>
                    <span className="font-mono text-sm">{comment.path}</span>
                  </span>
                  <span className="ml-auto flex shrink-0 items-center gap-2">
                    <span className="text-sm text-muted-foreground">
                      {/* created_at is epoch seconds; relativeTime takes ms. */}
                      {relativeTime(comment.created_at * 1000)}
                    </span>
                    <Button asChild variant="ghost" size="sm" className="text-sm">
                      {/* Deep-link into the file browser with this comment
                          selected — opening it there marks it seen, which
                          is what clears this inbox item. */}
                      <Link
                        to={`/c/${item.row.id}?file=${encodeURIComponent(comment.path)}&comment=${encodeURIComponent(comment.id)}`}
                        componentId="inbox.comment.open_file"
                      >
                        Open file
                        <ArrowRightIcon className="ml-1 size-3.5" />
                      </Link>
                    </Button>
                  </span>
                </div>
                {comment.anchor_content && (
                  <p className="truncate font-mono text-sm text-muted-foreground">
                    {comment.anchor_content.trim()}
                  </p>
                )}
                <p className="line-clamp-3 text-ui break-words whitespace-pre-wrap">
                  {comment.body}
                </p>
                <span className="text-sm text-muted-foreground">{sessionTitle}</span>
              </div>
            </div>
          );
        })}
        {visibleUnread.map((item) => {
          const sessionId = item.row.id;
          const expanded = expandedUnread[sessionId] ?? false;
          return (
            <UnreadInboxRow
              key={sessionId}
              item={item}
              preview={previewBySession.get(sessionId)}
              expanded={expanded}
              onToggle={() => {
                trackClick("inbox.unread.toggle_expanded", "button");
                setExpandedUnread((prev) => ({ ...prev, [sessionId]: !expanded }));
              }}
              onMarkRead={() => {
                // Drop the toggle so a later reply re-surfaces the row collapsed.
                setExpandedUnread(({ [sessionId]: _cleared, ...rest }) => rest);
                markConversationRead(sessionId, item.row.updated_at);
              }}
            />
          );
        })}
        {assembling && visibleCount > 0 && (
          <div className="flex items-center gap-2 py-2 text-sm text-muted-foreground">
            <Loader2Icon className="size-3.5 animate-spin" />
            Checking remaining sessions…
          </div>
        )}
      </div>
      {hasNextPage && (
        <Button
          variant="outline"
          disabled={conversationsQuery.isFetching}
          onClick={() => void fetchNextPage()}
          componentId="inbox.load_more"
        >
          {isFetchingNextPage ? "Loading…" : "Load more sessions"}
        </Button>
      )}
    </PageScroll>
  );
}

/**
 * One session with unseen agent output: a compact row (kind, title, a
 * one-line preview of the latest reply) that expands to the fuller
 * preview plus "Mark as read" / "Open session" — both mark the session read.
 */
function UnreadInboxRow({
  item,
  preview,
  expanded,
  onToggle,
  onMarkRead,
}: {
  item: UnreadInboxItem;
  preview: string | undefined;
  expanded: boolean;
  onToggle: () => void;
  onMarkRead: () => void;
}) {
  const isError = item.kind === "error";
  const KindIcon = isError ? CircleAlertIcon : CircleCheckIcon;
  return (
    <div
      data-testid="inbox-unread"
      data-kind={item.kind}
      data-expanded={expanded}
      className="flex flex-col gap-3 rounded-xl border border-border bg-card px-4 py-3"
    >
      <div className="flex items-center gap-3">
        <button
          type="button"
          aria-expanded={expanded}
          onClick={onToggle}
          className="flex min-w-0 flex-1 cursor-pointer items-center gap-3 text-left"
        >
          <span
            aria-hidden
            className={cn(
              "flex size-7 shrink-0 items-center justify-center rounded-md",
              isError ? "bg-destructive/10 text-destructive" : "bg-muted text-muted-foreground",
            )}
          >
            <KindIcon className="size-4" />
          </span>
          <span className="w-12 shrink-0 text-sm text-muted-foreground">
            {isError ? "Error" : "Done"}
          </span>
          <span className="min-w-0 truncate text-ui">
            <span className="font-medium">{conversationDisplayLabel(item.row)}</span>
            {!expanded && preview && <span className="text-muted-foreground"> — {preview}</span>}
          </span>
        </button>
        <span className="flex shrink-0 items-center gap-3">
          <span className="text-sm text-muted-foreground">
            {/* Server timestamps are epoch seconds; relativeTime takes ms. */}
            {relativeTime(item.row.updated_at * 1000)}
          </span>
          {/* Same brand-pink dot as the sidebar's unread indicator. */}
          <span role="img" aria-label="Unread" className="size-1.5 rounded-full bg-brand-accent" />
        </span>
      </div>
      {expanded && (
        <div className="flex flex-col gap-3 pl-10">
          {preview && <p className="text-ui break-words whitespace-pre-wrap">{preview}</p>}
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={onMarkRead}
              componentId="inbox.unread.mark_read"
            >
              <CheckIcon className="mr-1 size-3.5" />
              Mark as read
            </Button>
            <Button asChild variant="outline" size="sm">
              {/* Opening is reading. Mark it here: a freshly mounted chat keeps an
                  explicit "Mark as unread" override, so it wouldn't clear this row. */}
              <Link
                to={`/c/${item.row.id}`}
                onClick={onMarkRead}
                componentId="inbox.unread.open_session"
              >
                Open session
                <ArrowRightIcon className="ml-1 size-3.5" />
              </Link>
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
