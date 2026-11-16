// One agent in the Tile view. A live tile streams the session's conversation;
// only the active tile has a composer. Cards show status without streaming.

import { memo, type KeyboardEvent, type MouseEvent } from "react";
import { ExternalLinkIcon, FolderIcon, GitBranchIcon, RadioTowerIcon } from "lucide-react";
import { SideChatPane } from "@/components/chat/SideChatPane";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { Button } from "@/components/ui/button";
import type { Conversation } from "@/hooks/useConversations";
import { getSessionState, type SessionState } from "@/hooks/useSessionState";
import { cn } from "@/lib/utils";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import { TileResizeHandles } from "./TileResizeHandles";
import type { TileResize } from "./TileGrid";
import { isInteractiveTarget, needsYou } from "./tileSelection";

export type AgentTileVariant = "tile" | "card" | "strip";

interface AgentTileProps {
  conversation: Conversation;
  active: boolean;
  live: boolean;
  variant: AgentTileVariant;
  onActivate: (sessionId: string) => void;
  onOpenSingle: (sessionId: string) => void;
  /** Gallery tiles can be resized in whole grid slots. */
  resize?: TileResize;
}

function projectName(workspace: string | null | undefined): string | null {
  const trimmed = workspace?.trim().replace(/\/+$/, "");
  if (!trimmed) return null;
  return trimmed.slice(trimmed.lastIndexOf("/") + 1) || trimmed;
}

function relativeAge(updatedAtSeconds: number, nowMs = Date.now()): string {
  const minutes = Math.max(0, Math.round((nowMs - updatedAtSeconds * 1000) / 60000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function statusLabel(state: SessionState | null, status: Conversation["status"]): string {
  switch (state?.kind) {
    case "awaiting":
      return "Needs your response";
    case "running":
    case "starting":
      return "Running";
    default:
      return status === "failed" ? "Failed" : "Idle";
  }
}

function AgentTileComponent({
  conversation,
  active,
  live,
  variant,
  onActivate,
  onOpenSingle,
  resize,
}: AgentTileProps) {
  const title = conversation.title || conversationDisplayLabel(conversation);
  const state = getSessionState(conversation);
  const waiting = needsYou(conversation);
  const project = projectName(conversation.workspace);
  const subline = [conversation.harness, project, relativeAge(conversation.updated_at)]
    .filter(Boolean)
    .join(" · ");

  const activateFromClick = (event: MouseEvent<HTMLElement>) => {
    if (active || isInteractiveTarget(event.target)) return;
    onActivate(conversation.id);
  };
  const activateFromKeyboard = (event: KeyboardEvent<HTMLElement>) => {
    if (event.target !== event.currentTarget) return;
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    onActivate(conversation.id);
  };

  const showConversation = live && variant !== "card";

  return (
    <section
      data-testid="agent-tile"
      data-session-id={conversation.id}
      data-active={active || undefined}
      data-live={live || undefined}
      aria-label={`${title}${active ? " (active agent)" : ""}`}
      tabIndex={active ? -1 : 0}
      onClick={activateFromClick}
      onKeyDown={activateFromKeyboard}
      className={cn(
        "relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-lg border bg-card text-[13px] shadow-sm",
        !active && "cursor-pointer",
        active && "border-brand-accent ring-2 ring-brand-accent/30",
        !active && waiting && "border-amber-500 ring-2 ring-amber-500/30",
      )}
    >
      <header className="flex h-9 shrink-0 items-center gap-2 border-b px-3">
        {state ? (
          <SessionStateBadge state={state} />
        ) : (
          <span aria-hidden className="size-2 shrink-0 rounded-full bg-muted-foreground" />
        )}
        <strong title={title} className="min-w-0 truncate font-semibold">
          {title}
        </strong>
        <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground" title={subline}>
          {subline}
        </span>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label={`Open ${title} in single agent view`}
          title="Open in single agent view"
          onClick={() => onOpenSingle(conversation.id)}
        >
          <ExternalLinkIcon className="size-3.5" />
        </Button>
      </header>

      {showConversation ? (
        <div className="relative min-h-0 flex-1 overflow-hidden">
          <RenderErrorBoundary name="tile-chat" resetKey={conversation.id}>
            <SideChatPane childId={conversation.id} fullSession readOnly={!active} />
          </RenderErrorBoundary>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col gap-1.5 px-3 py-2.5 text-xs text-muted-foreground">
          <span className="font-medium text-foreground/80" data-testid="agent-tile-status">
            {statusLabel(state, conversation.status)}
          </span>
          {!conversation.workspace && <span>No working directory</span>}
          {conversation.workspace && (
            <span
              className="flex min-w-0 items-center gap-1.5 font-mono"
              title={conversation.workspace}
            >
              <FolderIcon aria-hidden className="size-3.5 shrink-0" />
              <span className="truncate">{conversation.workspace}</span>
            </span>
          )}
          {conversation.git_branch && (
            <span
              className="flex min-w-0 items-center gap-1.5 font-mono"
              title={conversation.git_branch}
            >
              <GitBranchIcon aria-hidden className="size-3.5 shrink-0" />
              <span className="truncate">{conversation.git_branch}</span>
            </span>
          )}
          {variant !== "card" && !live && (
            <span className="my-auto flex flex-col items-center gap-2 self-center rounded-md px-4 py-3 text-center">
              <RadioTowerIcon aria-hidden className="size-5 opacity-60" />
              <span className="font-medium text-foreground/80">Not streaming</span>
              <span>Click to watch this agent live.</span>
            </span>
          )}
        </div>
      )}

      {!active && variant !== "card" && live && (
        <footer
          className={cn(
            "shrink-0 border-t px-3 py-1.5 text-center text-xs",
            waiting ? "text-amber-600 dark:text-amber-400" : "text-muted-foreground",
          )}
        >
          {waiting
            ? "Waiting for you. Click to answer."
            : "View only. Click to make this the active agent."}
        </footer>
      )}
      {resize && (
        <TileResizeHandles
          sessionId={conversation.id}
          span={resize.span}
          slotWidthPx={resize.slotWidthPx}
          visibleColumns={resize.visibleColumns}
        />
      )}
    </section>
  );
}

export const AgentTile = memo(AgentTileComponent);
