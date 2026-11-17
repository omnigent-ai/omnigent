// One agent in the Tile view. A live tile streams the session's conversation;
// only the active tile has a composer. Cards show status without streaming.

import {
  memo,
  useId,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import {
  AppWindowIcon,
  ChevronDownIcon,
  EllipsisIcon,
  ExternalLinkIcon,
  GripVerticalIcon,
  Maximize2Icon,
  Minimize2Icon,
  FolderIcon,
  GitBranchIcon,
  LayersIcon,
  RadioTowerIcon,
  StarIcon,
} from "lucide-react";
import { SideChatPane } from "@/components/chat/SideChatPane";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { Conversation } from "@/hooks/useConversations";
import { getSessionState } from "@/hooks/useSessionState";
import { cn } from "@/lib/utils";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import { TileColorPicker } from "./TileColorPicker";
import { TileDetails } from "./TileDetails";
import {
  DEFAULT_TILE_TEXT_SIZE,
  setTileTextSize,
  stepTextSize,
  TILE_TEXT_SIZES,
  useTileTextSize,
} from "./tileTextSize";
import { projectName, relativeAge, statusLabel } from "./tileSummary";
import { TileResizeHandles } from "./TileResizeHandles";
import { useTileColor } from "./tileColors";
import type { TileResize } from "./TileGrid";
import { isInteractiveTarget, needsYou } from "./tileSelection";

export type AgentTileVariant = "tile" | "card" | "strip";

const TILE_DRAG_TYPE = "application/x-omnigent-tile";

export interface TileReorder {
  /** Put `id` in `targetId`'s place. */
  onMove: (id: string, targetId: string) => void;
  /** Move `id` by `delta` places. */
  onMoveBy: (id: string, delta: number) => void;
}

interface AgentTileProps {
  conversation: Conversation;
  active: boolean;
  live: boolean;
  variant: AgentTileVariant;
  onActivate: (sessionId: string) => void;
  onOpenSingle: (sessionId: string) => void;
  /** Gallery tiles can be resized in whole grid slots. */
  resize?: TileResize;
  /** Pinned (starred) state; the star shows only when `onToggleStar` is set. */
  starred?: boolean;
  starDisabled?: boolean;
  onToggleStar?: (conversation: Conversation) => void;
  /** Moves the tile to the Stack. */
  onPark?: (sessionId: string) => void;
  fullScreen?: boolean;
  onToggleFullScreen?: (sessionId: string) => void;
  /** Opens the session in its own window, where the shell allows one. */
  onPopOut?: (sessionId: string) => void;
  reorder?: TileReorder;
}

function AgentTileComponent({
  conversation,
  active,
  live,
  variant,
  onActivate,
  onOpenSingle,
  resize,
  starred = false,
  starDisabled = false,
  onToggleStar,
  onPark,
  fullScreen = false,
  onToggleFullScreen,
  onPopOut,
  reorder,
}: AgentTileProps) {
  const title = conversation.title || conversationDisplayLabel(conversation);
  const color = useTileColor(conversation.id);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const detailsId = useId();
  const textSize = useTileTextSize(conversation.id);
  const bodyZoom = textSize === DEFAULT_TILE_TEXT_SIZE ? undefined : { zoom: textSize };
  const sectionRef = useRef<HTMLElement>(null);
  const [dropTarget, setDropTarget] = useState(false);
  const isTileDrag = (event: DragEvent) => event.dataTransfer.types.includes(TILE_DRAG_TYPE);
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
      ref={sectionRef}
      {...(reorder && {
        onDragOver: (event: DragEvent) => {
          if (!isTileDrag(event)) return;
          event.preventDefault();
          setDropTarget(true);
        },
        onDragLeave: () => setDropTarget(false),
        onDrop: (event: DragEvent) => {
          setDropTarget(false);
          const draggedId = event.dataTransfer.getData(TILE_DRAG_TYPE);
          if (draggedId && draggedId !== conversation.id)
            reorder.onMove(draggedId, conversation.id);
        },
      })}
      className={cn(
        "relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-lg border bg-card text-[13px] shadow-sm",
        !active && "cursor-pointer",
        active && "border-brand-accent ring-2 ring-brand-accent/30",
        !active && waiting && "border-amber-500 ring-2 ring-amber-500/30",
        dropTarget && "ring-2 ring-primary/50",
      )}
      // The tile's color borders it unless the active or waiting ring takes over.
      style={active || waiting ? undefined : { borderColor: color.border }}
    >
      <header
        className="@container flex h-10 shrink-0 items-center gap-2 border-b pr-2 pl-2"
        style={{ backgroundColor: color.tint }}
      >
        {reorder && (
          <button
            type="button"
            draggable
            aria-label={`Reorder ${title}. Drag, or use the arrow keys.`}
            title="Drag to reorder"
            data-testid="tile-reorder-handle"
            onDragStart={(event) => {
              event.dataTransfer.setData(TILE_DRAG_TYPE, conversation.id);
              event.dataTransfer.effectAllowed = "move";
              if (sectionRef.current) event.dataTransfer.setDragImage(sectionRef.current, 24, 20);
            }}
            onKeyDown={(event) => {
              const delta = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[event.key];
              if (delta === undefined) return;
              event.preventDefault();
              reorder.onMoveBy(conversation.id, delta);
            }}
            className="-mr-1 shrink-0 cursor-grab rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:cursor-grabbing"
          >
            <GripVerticalIcon aria-hidden className="size-3.5" />
          </button>
        )}
        <TileColorPicker sessionId={conversation.id} title={title} color={color} />
        {state ? (
          <SessionStateBadge state={state} />
        ) : (
          <span aria-hidden className="size-2 shrink-0 rounded-full bg-muted-foreground" />
        )}
        <button
          type="button"
          data-testid="agent-tile-title"
          aria-expanded={detailsOpen}
          aria-controls={detailsOpen ? detailsId : undefined}
          title={`${title} · ${detailsOpen ? "Hide details" : "Show details"}`}
          onClick={() => setDetailsOpen((open) => !open)}
          onDoubleClick={() => onToggleFullScreen?.(conversation.id)}
          className="mr-auto flex min-w-0 cursor-pointer items-center gap-1 rounded-sm text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <strong className="min-w-0 truncate text-sm font-semibold tracking-tight text-foreground">
            {title}
          </strong>
          <ChevronDownIcon
            aria-hidden
            className={cn(
              "size-3.5 shrink-0 text-muted-foreground transition-transform",
              detailsOpen && "rotate-180",
            )}
          />
        </button>
        <span
          data-testid="agent-tile-subline"
          className="hidden min-w-0 flex-1 truncate text-xs text-muted-foreground @[34rem]:block"
          title={subline}
        >
          {subline}
        </span>
        {onToggleStar && (
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={starred ? `Unstar ${title}` : `Star ${title}`}
            aria-pressed={starred}
            title={starred ? "Unstar (unpin)" : "Star (pin to the top)"}
            disabled={starDisabled}
            onClick={() => onToggleStar(conversation)}
          >
            <StarIcon className={cn("size-3.5", starred && "fill-amber-400 text-amber-500")} />
          </Button>
        )}
        {onToggleFullScreen && (
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={fullScreen ? `Exit full screen for ${title}` : `Full screen ${title}`}
            aria-pressed={fullScreen}
            title={fullScreen ? "Exit full screen (Esc)" : "Full screen (F)"}
            onClick={() => onToggleFullScreen(conversation.id)}
          >
            {fullScreen ? (
              <Minimize2Icon className="size-3.5" />
            ) : (
              <Maximize2Icon className="size-3.5" />
            )}
          </Button>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon-xs" aria-label={`More for ${title}`} title="More">
              <EllipsisIcon className="size-3.5" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-48">
            <DropdownMenuItem onSelect={() => onOpenSingle(conversation.id)}>
              <ExternalLinkIcon className="size-4" />
              Open in single agent view
            </DropdownMenuItem>
            {onPopOut && (
              <DropdownMenuItem onSelect={() => onPopOut(conversation.id)}>
                <AppWindowIcon className="size-4" />
                Pop out to a window
              </DropdownMenuItem>
            )}
            {onPark && (
              <DropdownMenuItem onSelect={() => onPark(conversation.id)}>
                <LayersIcon className="size-4" />
                Move to the Stack
              </DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
            <DropdownMenuLabel className="text-xs text-muted-foreground">
              Text size · {Math.round(textSize * 100)}%
            </DropdownMenuLabel>
            {/* Size steps keep the menu open, so several can be applied in a row. */}
            <DropdownMenuItem
              disabled={textSize === TILE_TEXT_SIZES[0]}
              onSelect={(event) => {
                event.preventDefault();
                setTileTextSize(conversation.id, stepTextSize(textSize, -1));
              }}
            >
              Smaller text
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={textSize === TILE_TEXT_SIZES[TILE_TEXT_SIZES.length - 1]}
              onSelect={(event) => {
                event.preventDefault();
                setTileTextSize(conversation.id, stepTextSize(textSize, 1));
              }}
            >
              Larger text
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={textSize === DEFAULT_TILE_TEXT_SIZE}
              onSelect={() => setTileTextSize(conversation.id, DEFAULT_TILE_TEXT_SIZE)}
            >
              Reset text size
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </header>

      {detailsOpen ? (
        <TileDetails
          id={detailsId}
          conversation={conversation}
          title={title}
          accent={color.solid}
        />
      ) : showConversation ? (
        <div className="relative min-h-0 flex-1 overflow-hidden" style={bodyZoom}>
          <RenderErrorBoundary name="tile-chat" resetKey={conversation.id}>
            <SideChatPane childId={conversation.id} fullSession readOnly={!active} />
          </RenderErrorBoundary>
        </div>
      ) : (
        <div
          className="flex min-h-0 flex-1 flex-col gap-1.5 px-3 py-2.5 text-xs text-muted-foreground"
          style={bodyZoom}
        >
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

      {!detailsOpen && !active && variant !== "card" && live && (
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
