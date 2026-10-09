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
 * Alongside approvals the inbox lists unseen file comments — draft
 * comments other users left on session files (`useCommentInbox`),
 * each iconed with the author's avatar pill. A comment clears when
 * it's actually opened in the file browser — the FileViewer records
 * it in the client-side seen registry (`useSeenComments`) while the
 * comments panel is open on its file; the "Open file" link deep-links
 * to exactly that (`?file=` + `?comment=` auto-opens the panel).
 *
 * It also lists sessions with unseen agent output — the same read-state
 * behind the sidebar's unread dot — previewing the latest reply. Opening
 * the session or "Mark as read" clears a row.
 *
 * Every kind renders as the same card: a one-line row (kind tile, label,
 * session title — preview, time, unread dot) that expands in place. All
 * items sort newest first under recency headers ("Just now", "Earlier
 * today", …). Only the newest approval starts expanded; manual toggles are
 * keyed by item, so they stick as new items arrive. Tabs narrow the list
 * to "Unread" (comments + unread sessions) or "Awaiting response"
 * (approvals); the pick persists per device.
 *
 * Deliberately NOT here: dismissing approvals and mentions — neither
 * exists as a server concept. Resolving (or the prompt timing out) is
 * what clears an approval.
 */

import { useEffect, useRef, useState, type ReactNode } from "react";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangleIcon,
  ArrowRightIcon,
  CheckIcon,
  CircleAlertIcon,
  CircleCheckIcon,
  HandIcon,
  InboxIcon,
  Loader2Icon,
  SquarePenIcon,
  type LucideIcon,
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
  groupInboxEntries,
  type CommentInboxItem,
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

/** One inbox card, tagged by kind, with the epoch-seconds time it sorts and groups by. */
type InboxEntry =
  | { kind: "approval"; key: string; at: number; item: InboxItem }
  | { kind: "comment"; key: string; at: number; item: CommentInboxItem }
  | { kind: "unread"; key: string; at: number; item: UnreadInboxItem };

const TILE_TONES = {
  yellow: "bg-status-yellow/12 text-status-yellow",
  gray: "bg-status-gray/12 text-status-gray",
  red: "bg-status-red/12 text-status-red",
} as const;

// Compact action buttons inside an expanded card.
const OUTLINE_ACTION =
  "rounded-md border-button-border bg-transparent text-ui text-muted-foreground hover:text-foreground";
const PRIMARY_ACTION =
  "rounded-md bg-foreground text-ui text-card hover:bg-foreground/80 [&_svg]:text-card";

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
  // Manual expand/collapse toggles keyed by entry key. Anything not in
  // the map falls back to the default: expanded only for the newest
  // approval. Keying by id (not index) keeps a user's explicit toggles
  // stable when new items shift positions.
  const [expandedOverrides, setExpandedOverrides] = useState<Record<string, boolean>>({});

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
  const totalCount = items.length + commentInbox.items.length + unreadItems.length;
  const summary = [
    items.length > 0 && (items.length === 1 ? "1 approval" : `${items.length} approvals`),
    commentInbox.items.length > 0 &&
      (commentInbox.items.length === 1 ? "1 comment" : `${commentInbox.items.length} comments`),
    unreadItems.length > 0 && `${unreadItems.length} unread`,
  ]
    .filter(Boolean)
    .join(" · ");
  const emptyState = EMPTY_STATES[filter];

  const groups = groupInboxEntries<InboxEntry>([
    ...visibleApprovals.map((item) => ({
      kind: "approval" as const,
      key: `approval:${item.elicitation.elicitationId}`,
      at: item.row.updated_at,
      item,
    })),
    ...visibleComments.map((item) => ({
      kind: "comment" as const,
      key: `comment:${item.comment.id}`,
      at: item.comment.created_at,
      item,
    })),
    ...visibleUnread.map((item) => ({
      kind: "unread" as const,
      key: `unread:${item.row.id}`,
      at: item.row.updated_at,
      item,
    })),
  ]);
  const defaultExpandedKey = items[0] && `approval:${items[0].elicitation.elicitationId}`;

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

  const renderEntry = (entry: InboxEntry) => {
    const expanded = expandedOverrides[entry.key] ?? entry.key === defaultExpandedKey;
    const onToggle = () => {
      trackClick(`inbox.${entry.kind}.toggle_expanded`, "button");
      setExpandedOverrides((prev) => ({ ...prev, [entry.key]: !expanded }));
    };
    const title = conversationDisplayLabel(entry.item.row);

    if (entry.kind === "approval") {
      const { item } = entry;
      const elicitationId = item.elicitation.elicitationId;
      const verdict = responded[elicitationId];
      // Same display mapping the sidebar uses: native-wrapper sessions read
      // "Claude Code" / "Codex", never the internal agent name. Hidden when
      // it would just repeat the title (untitled native sessions).
      const agentLabel = getConversationAgentType(item.row);
      return (
        <InboxCard
          key={entry.key}
          testId="inbox-item"
          tile={<KindTile icon={HandIcon} tone="yellow" />}
          kindLabel={item.elicitation.askUserQuestion ? "Question" : "Approval needed"}
          title={title}
          preview={item.elicitation.message}
          at={entry.at}
          unread={false}
          expanded={expanded}
          onToggle={onToggle}
        >
          <SessionLine title={title} detail={agentLabel !== title ? agentLabel : undefined} />
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
            className="border-0 bg-transparent p-0"
          />
          <InboxActions>
            <Button asChild variant="outline" size="xs" className={OUTLINE_ACTION}>
              <Link to={`/c/${item.row.id}`} componentId="inbox.approval.open_session">
                Open session
                <ArrowRightIcon className="size-3.5" />
              </Link>
            </Button>
          </InboxActions>
        </InboxCard>
      );
    }

    if (entry.kind === "comment") {
      const { comment } = entry.item;
      // Single-user mode stores no author; mirror CommentsPanel's "You"
      // fallback (the only human in that mode is the viewer).
      const author = comment.created_by ?? "You";
      return (
        <InboxCard
          key={entry.key}
          testId="inbox-comment"
          tile={
            // The author's avatar pill (same deterministic initials + color
            // as presence circles) says who left it at a glance.
            <Avatar>
              <AvatarFallback
                className="font-medium text-white"
                style={{ backgroundColor: userColor(author) }}
              >
                {userInitials(author)}
              </AvatarFallback>
            </Avatar>
          }
          kindLabel="Comment"
          title={title}
          preview={`${author}: ${comment.body}`}
          at={entry.at}
          unread
          expanded={expanded}
          onToggle={onToggle}
        >
          <div>
            <SessionLine title={title} />
            <p className="mt-2 text-sm text-muted-foreground">
              <span className="text-foreground">{author}</span> commented on{" "}
              <span className="font-mono">{comment.path}</span>
            </p>
            {comment.anchor_content && (
              <p className="mt-1 truncate font-mono text-sm text-muted-foreground">
                {comment.anchor_content.trim()}
              </p>
            )}
            <p className="mt-2 text-ui font-medium break-words whitespace-pre-wrap">
              {comment.body}
            </p>
          </div>
          <InboxActions>
            <Button asChild size="xs" className={PRIMARY_ACTION}>
              {/* Deep-link into the file browser with this comment selected —
                  opening it there marks it seen, which clears this item. */}
              <Link
                to={`/c/${entry.item.row.id}?file=${encodeURIComponent(comment.path)}&comment=${encodeURIComponent(comment.id)}`}
                componentId="inbox.comment.open_file"
              >
                Open file
                <ArrowRightIcon className="size-3.5" />
              </Link>
            </Button>
          </InboxActions>
        </InboxCard>
      );
    }

    const { item } = entry;
    const sessionId = item.row.id;
    const isError = item.kind === "error";
    const preview = previewBySession.get(sessionId);
    const markRead = () => {
      // Drop the toggle so a later reply re-surfaces the row collapsed.
      setExpandedOverrides(({ [entry.key]: _cleared, ...rest }) => rest);
      markConversationRead(sessionId, item.row.updated_at);
    };
    return (
      <InboxCard
        key={entry.key}
        testId="inbox-unread"
        dataKind={item.kind}
        tile={
          <KindTile
            icon={isError ? CircleAlertIcon : CircleCheckIcon}
            tone={isError ? "red" : "gray"}
          />
        }
        kindLabel={isError ? "Error" : "Done"}
        title={title}
        preview={preview}
        at={entry.at}
        unread
        expanded={expanded}
        onToggle={onToggle}
      >
        <div>
          <SessionLine title={title} />
          {preview && (
            <p className="mt-2 text-ui font-medium break-words whitespace-pre-wrap">{preview}</p>
          )}
        </div>
        <InboxActions>
          <Button
            variant="outline"
            size="xs"
            className={OUTLINE_ACTION}
            onClick={markRead}
            componentId="inbox.unread.mark_read"
          >
            <CheckIcon className="size-3.5" />
            Mark as read
          </Button>
          <Button asChild size="xs" className={PRIMARY_ACTION}>
            {/* Opening is reading. Mark it here: a freshly mounted chat keeps an
                explicit "Mark as unread" override, so it wouldn't clear this row. */}
            <Link to={`/c/${sessionId}`} onClick={markRead} componentId="inbox.unread.open_session">
              Open session
              <ArrowRightIcon className="size-3.5" />
            </Link>
          </Button>
        </InboxActions>
      </InboxCard>
    );
  };

  return (
    <PageScroll maxWidthClassName="max-w-[960px]" contentClassName="px-4 md:px-10">
      <div className="mb-4 flex items-center gap-3">
        <h1 className="text-2xl font-normal">Inbox</h1>
        {totalCount > 0 && (
          <span
            title={summary}
            className="rounded-full bg-foreground px-2 py-0.5 text-sm font-medium text-card"
          >
            <span aria-hidden>{totalCount}</span>
            <span className="sr-only">{summary}</span>
          </span>
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
            <TabsTrigger key={value} value={value} className="h-7 flex-none px-3">
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
        {groups.map((group) => (
          <section key={group.label} aria-label={group.label} className="flex flex-col gap-2">
            <h2 className="px-3 py-1.5 text-sm font-normal text-muted-foreground">{group.label}</h2>
            {group.entries.map(renderEntry)}
          </section>
        ))}
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
          className="mt-4"
        >
          {isFetchingNextPage ? "Loading…" : "Load more sessions"}
        </Button>
      )}
    </PageScroll>
  );
}

/**
 * The shared inbox card. Collapsed, it is one row: kind tile, kind label,
 * session title — preview, time, and the unread dot. The row toggles an
 * expanded card that drops the inline title and shows `children` below.
 */
function InboxCard({
  testId,
  dataKind,
  tile,
  kindLabel,
  title,
  preview,
  at,
  unread,
  expanded,
  onToggle,
  children,
}: {
  testId: string;
  dataKind?: string;
  tile: ReactNode;
  kindLabel: string;
  title: string;
  preview: string | undefined;
  /** Epoch seconds. */
  at: number;
  unread: boolean;
  expanded: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <div
      data-testid={testId}
      data-kind={dataKind}
      data-expanded={expanded}
      className={cn(
        "overflow-hidden border border-border transition-[border-radius]",
        expanded
          ? "rounded-2xl bg-card shadow-[0_12px_20px_-20px_rgba(0,0,0,0.14),0_20px_28px_-28px_rgba(0,0,0,0.1)]"
          : "rounded-xl bg-transparent",
      )}
    >
      <button
        type="button"
        aria-expanded={expanded}
        aria-label={`${kindLabel}: ${title}`}
        onClick={onToggle}
        className="flex w-full cursor-pointer items-center gap-3.5 p-2 text-left"
      >
        <span className="shrink-0">{tile}</span>
        <span
          className={cn(
            // The tile already signals the kind, so narrow screens drop the
            // label column to keep room for the title.
            "hidden w-[140px] shrink-0 truncate text-ui sm:block",
            expanded ? "text-foreground" : "text-muted-foreground",
          )}
        >
          {kindLabel}
        </span>
        {expanded ? (
          <span className="flex-1" />
        ) : (
          <span className="min-w-0 flex-1 truncate text-ui">
            <span className="font-medium">{title}</span>
            {preview && <span className="text-muted-foreground"> — {preview}</span>}
          </span>
        )}
        <span className="min-w-8 shrink-0 text-right text-sm text-muted-foreground">
          {/* Server timestamps are epoch seconds; relativeTime takes ms. */}
          {relativeTime(at * 1000)}
        </span>
        {unread ? (
          <span
            role="img"
            aria-label="Unread"
            className="size-1.5 shrink-0 rounded-full bg-foreground"
          />
        ) : (
          <span aria-hidden className="w-1.5 shrink-0" />
        )}
      </button>
      {expanded && <div className="flex flex-col gap-3 pr-4 pb-4 pl-[58px]">{children}</div>}
    </div>
  );
}

function KindTile({ icon: Icon, tone }: { icon: LucideIcon; tone: keyof typeof TILE_TONES }) {
  return (
    <span className={cn("flex size-8 items-center justify-center rounded-lg", TILE_TONES[tone])}>
      <Icon aria-hidden className="size-4" />
    </span>
  );
}

/** The expanded card's "which session" line. */
function SessionLine({ title, detail }: { title: string; detail?: string }) {
  return (
    <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
      <SquarePenIcon aria-hidden className="size-3 shrink-0" />
      <span className="truncate">{title}</span>
      {detail && <span className="shrink-0">· {detail}</span>}
    </p>
  );
}

function InboxActions({ children }: { children: ReactNode }) {
  return <div className="flex items-center justify-end gap-1 pt-0.5">{children}</div>;
}
