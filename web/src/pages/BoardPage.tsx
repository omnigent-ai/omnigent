/**
 * Board page (`/board`, behind the `session_board` release flag): the viewer's
 * own sessions as cards in work-state columns — Running, Needs you, Needs
 * review and Done.
 *
 * Columns derive from the shared sidebar rows (kept live by the
 * `WS /v1/sessions/updates` stream), so the board needs no extra list fetch.
 * A session reaches Done only when the user accepts it: Accept sends the
 * reviewed `board_revision`, the server stores the `omnigent.board.accepted`
 * label if it is still current, and any later activity deletes the label
 * (see `@/lib/sessionBoard`).
 */

import { useMemo } from "react";
import {
  CheckIcon,
  GitBranchIcon,
  GitPullRequestIcon,
  Loader2Icon,
  RotateCcwIcon,
  SquareKanbanIcon,
} from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { Button } from "@/components/ui/button";
import { useBoardAcceptPending, useSetBoardAccepted } from "@/hooks/useBoardAcceptance";
import type { Conversation } from "@/hooks/useConversations";
import type { SessionState } from "@/hooks/useSessionState";
import { useSidebarData } from "@/hooks/useSidebarData";
import { useConversationReadState } from "@/hooks/useUnseenConversations";
import { useViewerId } from "@/hooks/useViewerId";
import { usePullRequests, type CanvasPullRequest } from "@/canvas/pullRequests";
import { gitProviderCopy } from "@/lib/gitProviders";
import { relativeTime } from "@/lib/relativeTime";
import { Link } from "@/lib/routing";
import {
  BOARD_COLUMNS,
  boardSessionState,
  canAcceptOnBoard,
  groupBoardColumns,
  type BoardColumnId,
} from "@/lib/sessionBoard";
import { sessionVisibility } from "@/lib/sessionVisibility";
import { conversationDisplayLabel, getConversationAgentType } from "@/shell/sidebarNav";

function statusLine(state: SessionState | null, column: BoardColumnId, unseen: boolean): string {
  if (state?.kind === "awaiting") {
    if (state.count === 0) return "Waiting in the agent's terminal";
    return state.count === 1 ? "1 approval waiting" : `${state.count} approvals waiting`;
  }
  if (state?.kind === "running") return state.background ? "Working in the background" : "Working";
  if (column === "done") return "Accepted";
  if (state?.kind === "error") return "Last turn failed";
  if (state?.kind === "disconnected") return "Host disconnected";
  return unseen ? "New messages" : "Ready for review";
}

function BoardCard({
  conversation,
  column,
  pullRequest,
}: {
  conversation: Conversation;
  column: BoardColumnId;
  pullRequest: CanvasPullRequest | null;
}) {
  const { unseen } = useConversationReadState(
    conversation.id,
    conversation.updated_at,
    conversation.status,
  );
  const setAccepted = useSetBoardAccepted();
  const acceptPending = useBoardAcceptPending(conversation.id);
  const sessionState = boardSessionState(conversation);
  // The column and status line already say a card is waiting or accepted, so
  // skip the wide "Needs response" tag and a stale failure icon on Done cards.
  const badgeState: SessionState | null =
    sessionState?.kind === "awaiting" || column === "done"
      ? null
      : (sessionState ?? (unseen ? { kind: "unseen" } : null));
  const title = conversationDisplayLabel(conversation);
  const prCopy = gitProviderCopy(pullRequest?.provider, undefined, pullRequest?.provider_display);
  const reviewedRevision = conversation.board_revision ?? null;
  // A server without revisions can't check the review, so it offers no Accept.
  const canAccept = reviewedRevision !== null && canAcceptOnBoard(conversation);

  return (
    <li
      className="flex flex-col gap-1.5 rounded-lg border bg-card px-3 py-2.5 text-ui shadow-sm"
      data-testid="board-card"
      data-session-id={conversation.id}
    >
      <div className="flex min-w-0 items-center gap-2">
        {badgeState && <SessionStateBadge state={badgeState} />}
        <Link
          to={`/c/${encodeURIComponent(conversation.id)}`}
          componentId="board.open_session"
          className="min-w-0 flex-1 truncate font-medium hover:underline"
          title={title}
        >
          {title}
        </Link>
        <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
          {relativeTime(conversation.updated_at * 1000)}
        </span>
      </div>
      <div className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
        <span className="min-w-0 max-w-[50%] shrink-0 truncate">
          {getConversationAgentType(conversation)}
        </span>
        <span aria-hidden>·</span>
        <span className="min-w-0 truncate">{statusLine(sessionState, column, unseen)}</span>
      </div>
      {conversation.git_branch && (
        <span
          className="flex min-w-0 items-center gap-1.5 font-mono text-xs text-muted-foreground"
          title={conversation.git_branch}
        >
          <GitBranchIcon aria-hidden className="size-3.5 shrink-0" />
          <span className="truncate">{conversation.git_branch}</span>
        </span>
      )}
      {pullRequest && (
        <a
          href={pullRequest.url}
          target="_blank"
          rel="noopener noreferrer"
          className="flex min-w-0 items-center gap-1.5 text-xs font-medium text-brand-accent hover:underline"
          title={pullRequest.title}
        >
          <GitPullRequestIcon aria-hidden className="size-3.5 shrink-0" />
          <span className="truncate">
            {prCopy.prNumberPrefix}
            {pullRequest.number} {pullRequest.title}
          </span>
        </a>
      )}
      {(canAccept || column === "done") && (
        <div className="flex justify-end">
          {canAccept ? (
            <Button
              size="sm"
              variant="outline"
              disabled={acceptPending}
              onClick={() => setAccepted.mutate({ row: conversation, reviewedRevision })}
              componentId="board.accept"
              aria-label={`Accept ${title}`}
            >
              <CheckIcon aria-hidden className="size-3.5" />
              Accept
            </Button>
          ) : (
            <Button
              size="sm"
              variant="ghost"
              disabled={acceptPending}
              onClick={() => setAccepted.mutate({ row: conversation, reviewedRevision: null })}
              componentId="board.reopen"
              aria-label={`Reopen ${title}`}
            >
              <RotateCcwIcon aria-hidden className="size-3.5" />
              Reopen
            </Button>
          )}
        </div>
      )}
    </li>
  );
}

export function BoardPage() {
  const { loadedRows, mine } = useSidebarData();
  const viewerId = useViewerId();
  const ownRows = useMemo(
    () => loadedRows.filter((row) => sessionVisibility(row, viewerId) === "mine"),
    [loadedRows, viewerId],
  );
  const columns = useMemo(() => groupBoardColumns(ownRows), [ownRows]);
  const pullRequests = usePullRequests(ownRows);
  const loading = mine.data === undefined && mine.isLoading;

  return (
    <PageScroll maxWidthClassName="max-w-[1400px]" contentClassName="px-4 md:px-10">
      <div className="mb-1 flex items-center gap-3">
        <h1 className="text-2xl font-normal">Board</h1>
      </div>
      <p className="mb-5 text-sm text-muted-foreground">
        Your sessions by work state. Accept a session once you have reviewed its work to move it to
        Done; new activity moves it back.
      </p>

      {mine.isError && (
        <div role="alert" className="mb-4 flex items-center gap-2 text-ui text-destructive">
          Couldn’t load your sessions.
          <Button
            variant="outline"
            size="sm"
            onClick={() => void mine.refetch?.()}
            componentId="board.retry"
          >
            Retry
          </Button>
        </div>
      )}

      {loading ? (
        <div className="flex items-center gap-2 py-12 text-ui text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading sessions…
        </div>
      ) : ownRows.length === 0 && !mine.isError ? (
        <div className="flex flex-col items-center gap-2 py-12 text-center">
          <SquareKanbanIcon className="size-8 text-muted-foreground/50" />
          <p className="text-ui font-medium">No sessions yet</p>
          <p className="text-sm text-muted-foreground">
            Start a session and it appears here while it runs.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
          {BOARD_COLUMNS.map((column) => {
            const cards = columns[column.id];
            const headingId = `board-column-${column.id}`;
            return (
              <section
                key={column.id}
                aria-labelledby={headingId}
                data-testid={`board-column-${column.id}`}
                className="flex min-w-0 flex-col gap-2 rounded-xl bg-muted/40 p-2"
              >
                <h2
                  id={headingId}
                  className="flex items-center gap-2 px-1.5 py-1 text-sm font-medium"
                >
                  {column.title}
                  <span
                    className="text-xs font-normal text-muted-foreground tabular-nums"
                    aria-label={`${cards.length} ${cards.length === 1 ? "session" : "sessions"}`}
                  >
                    {cards.length}
                  </span>
                </h2>
                {cards.length === 0 ? (
                  <p className="px-1.5 pb-2 text-xs text-muted-foreground">{column.empty}</p>
                ) : (
                  <ul className="flex flex-col gap-2">
                    {cards.map((conversation) => (
                      <BoardCard
                        key={conversation.id}
                        conversation={conversation}
                        column={column.id}
                        pullRequest={pullRequests[conversation.id] ?? null}
                      />
                    ))}
                  </ul>
                )}
              </section>
            );
          })}
        </div>
      )}

      {mine.hasNextPage && (
        <Button
          variant="outline"
          disabled={mine.isFetching}
          onClick={() => void mine.fetchNextPage()}
          componentId="board.load_more"
          className="mt-4"
        >
          {mine.isFetchingNextPage ? "Loading…" : "Load more sessions"}
        </Button>
      )}
    </PageScroll>
  );
}
