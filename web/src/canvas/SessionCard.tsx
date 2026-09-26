// One session as a draggable card on the Canvas. Mirrors the sidebar row: the
// same state badge (approval tag / running spinner / unread dot), the display
// label, the working directory, the worktree branch, and the open PR.

import { memo, type KeyboardEvent, type MouseEvent } from "react";
import type { Node, NodeProps } from "@xyflow/react";
import { FolderIcon, GitBranchIcon, GitPullRequestIcon } from "lucide-react";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import type { Conversation } from "@/hooks/useConversations";
import { getSessionState, type SessionState } from "@/hooks/useSessionState";
import { useConversationReadState } from "@/hooks/useUnseenConversations";
import { useOptimisticTitle } from "@/lib/optimisticTitles";
import { cn } from "@/lib/utils";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import type { CanvasPullRequest } from "./pullRequests";

export type SessionCardData = {
  conversation: Conversation;
  pullRequest: CanvasPullRequest | null;
  onOpen: (sessionId: string) => void;
} & Record<string, unknown>;

export type SessionCardNode = Node<SessionCardData, "session">;

function stateLabel(state: SessionState | null, status: Conversation["status"]): string {
  switch (state?.kind) {
    case "awaiting":
      return "Needs response";
    case "running":
    case "starting":
      return "Running";
    case "unseen":
      return "New messages";
    default:
      return status === "failed" ? "Failed" : "Idle";
  }
}

function SessionCardComponent({ data, selected }: NodeProps<SessionCardNode>) {
  const { conversation, pullRequest, onOpen } = data;
  const optimisticTitle = useOptimisticTitle(conversation.id);
  const readState = useConversationReadState(
    conversation.id,
    conversation.updated_at,
    conversation.status,
  );
  const state: SessionState | null =
    getSessionState(conversation) ?? (readState.unseen ? { kind: "unseen" } : null);
  const title = conversation.title || optimisticTitle || conversationDisplayLabel(conversation);
  const titleProvisional = !conversation.title && optimisticTitle !== undefined;
  const label = stateLabel(state, conversation.status);
  const workspace = conversation.workspace?.trim() || "No working directory";

  const openFromKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    event.stopPropagation();
    onOpen(conversation.id);
  };
  const stopCardEvents = (event: MouseEvent) => event.stopPropagation();

  return (
    <div
      className={cn(
        "flex w-[280px] min-h-[116px] cursor-grab select-none flex-col gap-1.5 rounded-lg border bg-card px-4 py-3.5 text-[13px] shadow-sm active:cursor-grabbing",
        selected && "border-brand-accent ring-2 ring-brand-accent/25",
      )}
      data-testid="session-card"
      data-state={state?.kind ?? "idle"}
      role="button"
      tabIndex={0}
      aria-label={`${title}. ${label}. ${workspace}`}
      onClick={(event) => {
        // Pointer clicks select (and start drags); only keyboard activation opens.
        if (event.detail === 0) onOpen(conversation.id);
      }}
      onKeyDown={openFromKeyboard}
    >
      <div className="flex min-w-0 items-center gap-2">
        {state ? (
          <SessionStateBadge state={state} />
        ) : (
          <span aria-hidden className="size-2 shrink-0 rounded-full bg-muted-foreground" />
        )}
        <strong
          title={title}
          className={cn(
            "truncate font-semibold",
            titleProvisional && "font-medium italic text-muted-foreground",
          )}
        >
          {title}
        </strong>
      </div>
      <span className="text-xs text-muted-foreground">{label}</span>
      <span
        className="flex min-w-0 items-center gap-1.5 font-mono text-xs text-muted-foreground"
        title={workspace}
      >
        <FolderIcon aria-hidden className="size-3.5 shrink-0" />
        <span className="truncate">{workspace}</span>
      </span>
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
          // `nodrag` keeps React Flow from turning a click on the link into a card drag.
          className="nodrag flex min-w-0 items-center gap-1.5 text-xs font-medium text-brand-accent hover:underline"
          title={`${pullRequest.title} (${pullRequest.state.toLowerCase()})`}
          aria-label={`Open pull request #${pullRequest.number}`}
          onClick={stopCardEvents}
          onDoubleClick={stopCardEvents}
        >
          <GitPullRequestIcon aria-hidden className="size-3.5 shrink-0" />
          <span className="truncate">
            #{pullRequest.number} {pullRequest.title}
          </span>
        </a>
      )}
    </div>
  );
}

export const SessionCard = memo(SessionCardComponent);
