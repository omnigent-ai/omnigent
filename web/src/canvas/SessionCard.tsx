// One session as a draggable card on the Canvas. Mirrors the sidebar row: the
// same state badge (approval tag / running spinner / unread dot), the display
// label, the working directory, the worktree branch, and the open PR.

import { memo, type KeyboardEvent, type MouseEvent } from "react";
import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { FolderIcon, GitBranchIcon, GitPullRequestIcon, UserPlusIcon } from "lucide-react";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import type { Conversation } from "@/hooks/useConversations";
import { getSessionState, type SessionState } from "@/hooks/useSessionState";
import { stateAsOf } from "@/hooks/useSessionPullRequests";
import { useConversationReadState } from "@/hooks/useUnseenConversations";
import { gitProviderCopy } from "@/lib/gitProviders";
import { useOptimisticTitle } from "@/lib/optimisticTitles";
import { cn } from "@/lib/utils";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import { isTeamLeadRole, teamRole } from "./canvasTeam";
import type { CanvasPullRequest } from "./pullRequests";

export type SessionCardData = {
  conversation: Conversation;
  pullRequest: CanvasPullRequest | null;
  onOpen: (sessionId: string) => void;
  onAddAgent: (conversation: Conversation) => void;
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

/** The PR link's tooltip: its title and last fetched state, with that state's age once stale. */
function pullRequestTooltip(pr: CanvasPullRequest): string {
  if (!pr.state) return pr.title;
  const asOf = stateAsOf(pr.state_checked_at);
  return `${pr.title} (${pr.state.toLowerCase()}${asOf ? `, as of ${asOf}` : ""})`;
}

function SessionCardComponent({ data, selected }: NodeProps<SessionCardNode>) {
  const { conversation, pullRequest, onOpen, onAddAgent } = data;
  const role = teamRole(conversation);
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
  const prCopy = gitProviderCopy(pullRequest?.provider, undefined, pullRequest?.provider_display);
  const prNumberPrefix = prCopy.prNumberPrefix;

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
      aria-pressed={selected ?? false}
      tabIndex={0}
      aria-label={`${title}. ${label}. ${workspace}`}
      onClick={(event) => {
        // React Flow distinguishes pointer clicks from drags; virtual clicks
        // activate here without also bubbling to the node's click handler.
        if (event.detail === 0) {
          event.stopPropagation();
          onOpen(conversation.id);
        }
      }}
      onKeyDown={openFromKeyboard}
    >
      <Handle type="target" position={Position.Top} isConnectable={false} className="invisible" />
      <Handle
        type="source"
        position={Position.Bottom}
        isConnectable={false}
        className="invisible"
      />
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
        <button
          type="button"
          // `nodrag` keeps React Flow from turning a click on the button into a card drag.
          className="nodrag ml-auto flex size-6 shrink-0 cursor-pointer items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
          aria-label={`Add agent to ${title}`}
          title="Add agent"
          data-testid="session-card-add-agent"
          onClick={(event) => {
            event.stopPropagation();
            onAddAgent(conversation);
          }}
          onDoubleClick={stopCardEvents}
          onKeyDown={(event) => event.stopPropagation()}
        >
          <UserPlusIcon aria-hidden className="size-3.5" />
        </button>
      </div>
      {role && (
        <span
          className={cn(
            "w-fit max-w-full truncate rounded-full border px-2 py-0.5 text-xs font-medium",
            isTeamLeadRole(role) && "border-brand-accent text-brand-accent",
          )}
          title={role}
          data-testid="session-card-role"
        >
          {role}
        </span>
      )}
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
          title={pullRequestTooltip(pullRequest)}
          aria-label={`Open ${prCopy.requestName} ${prNumberPrefix}${pullRequest.number}`}
          onClick={stopCardEvents}
          onDoubleClick={stopCardEvents}
        >
          <GitPullRequestIcon aria-hidden className="size-3.5 shrink-0" />
          <span className="truncate">
            {prNumberPrefix}
            {pullRequest.number} {pullRequest.title}
          </span>
        </a>
      )}
    </div>
  );
}

export const SessionCard = memo(SessionCardComponent);
