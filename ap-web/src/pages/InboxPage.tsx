/**
 * Inbox page (``/inbox``) — every approval prompt waiting on the user,
 * across all of their sessions, rendered as actionable cards.
 *
 * Built entirely from existing primitives:
 *
 * - The session list (`useConversations`) already carries
 *   `pending_elicitations_count` per row, kept live by the
 *   `WS /v1/sessions/updates` stream. The inbox drains all list
 *   pages while mounted, since an awaiting session may sit far
 *   below the sidebar's first page.
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
 * Deliberately NOT here for approvals: read/unread state, dismiss,
 * mentions — none of those exist as server concepts. Resolving (or
 * the prompt timing out) is what clears an approval.
 */

import { useEffect, useRef, useState } from "react";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangleIcon,
  ArrowRightIcon,
  ChevronDownIcon,
  InboxIcon,
  Loader2Icon,
} from "lucide-react";
import { ApprovalCard, type SubmitApprovalFn } from "@/components/blocks/ApprovalCard";
import { PageScroll } from "@/components/PageScroll";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { useCommentInbox } from "@/hooks/useCommentInbox";
import { useConversations } from "@/hooks/useConversations";
import { collectInboxItems, type InboxItem, type InboxSource } from "@/lib/inbox";
import { relativeTime } from "@/lib/relativeTime";
import { Link } from "@/lib/routing";
import { approve, getSession } from "@/lib/sessionsApi";
import { userColor, userInitials } from "@/lib/userBadge";
import { cn } from "@/lib/utils";
import { conversationDisplayLabel, getConversationAgentType } from "@/shell/sidebarNav";

/** Optimistic verdicts keyed by elicitation id, mirroring the chat store's flip. */
type RespondedMap = Record<
  string,
  { action: "accept" | "decline" | "cancel"; content?: Record<string, unknown> }
>;

export function InboxPage() {
  const queryClient = useQueryClient();
  const conversationsQuery = useConversations("", false, { reconcileWhileConnected: true });
  const [responded, setResponded] = useState<RespondedMap>({});
  // Manual expand/collapse toggles keyed by elicitation id. Anything
  // not in the map falls back to the default: expanded only for the
  // first (newest) item. Keying by id (not index) keeps a user's
  // explicit toggles stable when new items shift positions.
  const [expandedOverrides, setExpandedOverrides] = useState<Record<string, boolean>>({});

  // The sidebar pages lazily on scroll, but the inbox must consider
  // EVERY session — an approval can be pending in a session that 20+
  // newer sessions have since pushed off the first page. Drain the
  // remaining pages while the inbox is mounted (the query cache is
  // shared with the sidebar, so this also completes its badge).
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = conversationsQuery;
  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage) void fetchNextPage();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  const allRows = (conversationsQuery.data?.pages ?? []).flatMap((page) => page.data);
  const rows = allRows.filter((c) => !c.archived && (c.pending_elicitations_count ?? 0) > 0);

  // Unseen file comments across sessions — the hook filters to rows
  // that report comments and mounts one comments query per such row.
  const commentInbox = useCommentInbox(allRows);

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

  // Clear stale optimistic verdicts when snapshot data refreshes.
  // If a hook retry re-parks the same elicitation id after the user
  // approved the previous attempt, the local `responded` entry would
  // otherwise keep the card stuck on "Approved" indefinitely. When
  // any snapshot query delivers fresh data (dataUpdatedAt advances),
  // sweep verdicts whose id is still pending on the server — those
  // approvals were consumed and the server re-parked the prompt.
  // eslint-disable-next-line react-hooks/exhaustive-deps
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
      const next = { ...prev };
      for (const id of stale) delete next[id];
      return next;
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
    hasNextPage ||
    isFetchingNextPage ||
    snapshotQueries.some((q) => q.isLoading) ||
    commentInbox.isLoading;
  const failedSnapshots = snapshotQueries.filter((q) => q.isError);
  const failedSessionCount = failedSnapshots.length + commentInbox.failedCount;

  // Mirrors `chatStore.submitApproval`: optimistic flip → resolve POST →
  // rollback on error. Success invalidates the session list so the row's
  // count (and the sidebar badge) drop without waiting for the socket.
  const makeSubmit = (item: InboxItem): SubmitApprovalFn => {
    return (elicitationId, action, content) => {
      setResponded((prev) => ({
        ...prev,
        [elicitationId]: content === undefined ? { action } : { action, content },
      }));
      void approve(
        item.resolveSessionId,
        elicitationId,
        content === undefined ? { action } : { action, content },
      ).then(
        () => {
          void queryClient.invalidateQueries({ queryKey: ["conversations"] });
        },
        () => {
          // Roll back to pending so the buttons reappear and the user
          // can retry — same recovery the chat store uses.
          setResponded((prev) => {
            const next = { ...prev };
            delete next[elicitationId];
            return next;
          });
        },
      );
    };
  };

  return (
    <PageScroll contentClassName="px-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Inbox</h1>
        {(items.length > 0 || commentInbox.items.length > 0) && (
          <span className="text-sm text-muted-foreground">
            {[
              items.length > 0 && (items.length === 1 ? "1 approval" : `${items.length} approvals`),
              commentInbox.items.length > 0 &&
                (commentInbox.items.length === 1
                  ? "1 comment"
                  : `${commentInbox.items.length} comments`),
            ]
              .filter(Boolean)
              .join(" · ")}{" "}
            waiting
          </span>
        )}
      </div>

      {failedSessionCount > 0 && (
        <div
          data-testid="inbox-load-error"
          className="mb-4 flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm"
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
            }}
          >
            Retry
          </Button>
        </div>
      )}

      {assembling && items.length === 0 && commentInbox.items.length === 0 && (
        <div className="flex items-center gap-2 py-12 text-sm text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading inbox…
        </div>
      )}

      {!assembling &&
        failedSessionCount === 0 &&
        items.length === 0 &&
        commentInbox.items.length === 0 && (
          <div className="flex flex-col items-center gap-2 py-16 text-center">
            <InboxIcon className="size-8 text-muted-foreground/50" />
            <p className="text-sm font-medium">Nothing waiting on you</p>
            <p className="text-xs text-muted-foreground">
              When an agent needs your input or someone comments on a file, it will show up here.
            </p>
          </div>
        )}

      <div className="flex flex-col gap-4">
        {items.map((item, index) => {
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
                  onClick={() =>
                    setExpandedOverrides((prev) => ({ ...prev, [elicitationId]: !expanded }))
                  }
                  className="flex min-w-0 flex-1 items-center gap-2 text-left"
                >
                  <ChevronDownIcon
                    className={cn(
                      "size-4 shrink-0 text-muted-foreground transition-transform",
                      !expanded && "-rotate-90",
                    )}
                  />
                  <span className="min-w-0 shrink-0 truncate text-sm font-medium">
                    {title}
                    {agentLabel !== title && (
                      <span className="ml-2 text-xs font-normal text-muted-foreground">
                        {agentLabel}
                      </span>
                    )}
                  </span>
                  {!expanded && (
                    <span className="min-w-0 truncate text-xs text-muted-foreground">
                      {item.elicitation.message}
                    </span>
                  )}
                </button>
                <span className="flex shrink-0 items-center gap-2">
                  <span className="text-xs text-muted-foreground">
                    {/* Server timestamps are epoch seconds; relativeTime takes ms. */}
                    {relativeTime(item.row.updated_at * 1000)}
                  </span>
                  <Button asChild variant="ghost" size="sm" className="text-xs">
                    <Link to={`/c/${item.row.id}`}>
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
                  rememberScope={item.elicitation.rememberScope}
                  onSubmit={makeSubmit(item)}
                />
              )}
            </div>
          );
        })}
        {commentInbox.items.map((item) => {
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
                  <span className="min-w-0 truncate text-sm">
                    <span className="font-medium">{author}</span>
                    <span className="text-muted-foreground"> commented on </span>
                    <span className="font-mono text-xs">{comment.path}</span>
                  </span>
                  <span className="ml-auto flex shrink-0 items-center gap-2">
                    <span className="text-xs text-muted-foreground">
                      {/* created_at is epoch seconds; relativeTime takes ms. */}
                      {relativeTime(comment.created_at * 1000)}
                    </span>
                    <Button asChild variant="ghost" size="sm" className="text-xs">
                      {/* Deep-link into the file browser with this comment
                          selected — opening it there marks it seen, which
                          is what clears this inbox item. */}
                      <Link
                        to={`/c/${item.row.id}?file=${encodeURIComponent(comment.path)}&comment=${encodeURIComponent(comment.id)}`}
                      >
                        Open file
                        <ArrowRightIcon className="ml-1 size-3.5" />
                      </Link>
                    </Button>
                  </span>
                </div>
                {comment.anchor_content && (
                  <p className="truncate font-mono text-[11px] text-muted-foreground">
                    {comment.anchor_content.trim()}
                  </p>
                )}
                <p className="line-clamp-3 text-sm break-words whitespace-pre-wrap">
                  {comment.body}
                </p>
                <span className="text-xs text-muted-foreground">{sessionTitle}</span>
              </div>
            </div>
          );
        })}
        {assembling && (items.length > 0 || commentInbox.items.length > 0) && (
          <div className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
            <Loader2Icon className="size-3.5 animate-spin" />
            Checking remaining sessions…
          </div>
        )}
      </div>
    </PageScroll>
  );
}
