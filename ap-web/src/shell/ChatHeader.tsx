import {
  BotIcon,
  ChevronLeftIcon,
  EllipsisVerticalIcon,
  FileIcon,
  InfoIcon,
  ListIcon,
  ListTodoIcon,
  PanelLeftIcon,
  PanelRightCloseIcon,
  PanelRightIcon,
  ShareIcon,
  TerminalIcon,
} from "lucide-react";
import { Link } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { AgentInfoButton } from "@/components/AgentInfo";
import { PresenceAvatars } from "@/components/PresenceAvatars";
import type { Agent } from "@/hooks/useAgents";
import { cn } from "@/lib/utils";
import { TAB_BADGE_BASE } from "./railTabs";

/**
 * Gating flags + handlers for the mobile-only session-menu FAB (the
 * three-dot → rail-entries dropdown). Folded into one object because the
 * block is a self-contained unit never read by the desktop action row —
 * keeping it grouped halves ChatHeader's top-level prop count.
 */
interface MobileSessionMenuProps {
  /** True while the desktop file viewer is open (suppresses the FAB). */
  fileViewerOpen: boolean;
  /** True while a terminals/exec-logs push panel owns the right side. */
  panelOpen: boolean;
  /** Terminal-first session — terminal renders inline, FAB stays available. */
  terminalFirst: boolean;
  /** True while the execution-logs push panel is open. */
  executionLogsOpen: boolean;
  /** True while the mobile files drawer is open. */
  filesPanelOpen: boolean;
  /** True while the mobile agents drawer is open. */
  subagentsPanelOpen: boolean;
  /** True while the mobile tasks drawer is open. */
  todosPanelOpen: boolean;
  /** True while the mobile shells drawer is open. */
  terminalsListOpen: boolean;
  /**
   * Whether the Shells entry should render — mirrors the desktop rail's
   * Shells tab gate (`railTabsAvailable.terminals`): shown when the
   * agent declares shell access (its empty state is the "+ New shell"
   * entry point) or once a shell exists, and hidden for claude-native
   * sub-agents.
   */
  showShellsTab: boolean;
  /** Number of open terminals (entry badge). */
  terminalsLength: number;
  /** Whether this is a claude-native session (gates the Tasks entry). */
  isClaudeNative: boolean;
  /** Completed todo count (Tasks entry badge numerator). */
  todosCompleted: number;
  /** Total todo count (Tasks entry badge denominator + visibility). */
  todosTotal: number;
  /** Debug mode — surfaces the Logs entry. */
  debugMode: boolean;
  /** Changed-file count (Files entry badge). */
  changedCount: number;
  /** Working child-agent count (Agents entry badge). */
  subagentsWorking: number;
  /**
   * Total agents in the session tree, main agent included (Agents
   * entry badge) — starts at 1 for a lone agent.
   */
  agentCount: number;
  /** Open the mobile files drawer. */
  onOpenFiles: () => void;
  /** Open the mobile shells drawer (the InlineTerminalsSection list). */
  onOpenShells: () => void;
  /** Open the mobile agents drawer. */
  onOpenSubagents: () => void;
  /** Open the mobile tasks drawer. */
  onOpenTodos: () => void;
  /** Open the main execution-log push panel. */
  onOpenMainExecutionLog: () => void;
}

/**
 * Props for {@link ChatHeader}. All state lives in AppShell; action
 * callbacks wrap the shell's dialog/panel setters so state ownership
 * stays in one place.
 */
interface ChatHeaderProps {
  /** Whether the left sidebar is open (hides the open-sidebar button). */
  sidebarOpen: boolean;
  /** Open the left sidebar. */
  onOpenSidebar: () => void;
  /** Whether the active session is a sub-agent (shows the back link). */
  isChildSession: boolean;
  /** Parent session id for the back link's destination (when a child). */
  parentSessionId: string | null | undefined;
  /** Active session id, or undefined on the landing composer. */
  conversationId: string | undefined;
  /** The bound agent (mcp_servers + policies) for the info popover. */
  boundAgent: Agent | undefined;
  /** Whether the Share button/menu entry should render. */
  canShare: boolean;
  /** Whether the rendered Share controls should be disabled. */
  shareDisabled?: boolean;
  /** User-facing reason for the disabled Share controls. */
  shareDisabledReason?: string;
  /** Open the share dialog. */
  onShare: () => void;
  /** Whether the agent has tools/policies worth surfacing. */
  hasAgentInfo: boolean;
  /** Open the mobile agent-info dialog. */
  onAgentInfo: () => void;
  /** Whether the mobile three-dot menu has any entry to offer. */
  hasHeaderMenu: boolean;
  /** Whether the Files tab/right panel is available for this session. */
  showFilesPanel: boolean;
  /**
   * Whether the right workspace rail has at least one available tab
   * (files, terminals, sub-agents, or todos). Gates the desktop
   * collapse toggle — with no rail content the panel doesn't mount
   * (see AppShell), so a toggle would flip an invisible card.
   */
  hasRailContent: boolean;
  /** Whether the right workspace panel is currently open. */
  rightPanelOpen: boolean;
  /** Toggle the right workspace panel. */
  onToggleRightPanel: () => void;
  /** Gating + handlers for the mobile session-menu FAB. */
  mobileMenu: MobileSessionMenuProps;
}

/**
 * ChatHeader — the top action bar for the conversation region.
 *
 * Rendered as an **absolute overlay** (``z-30``) spanning the full width
 * of the chat + workspace group. The bar paints no background — the app
 * canvas shows through, and chat content dissolves before it slides
 * under the controls (the conversation viewport's ``chat-scroll-fade``
 * mask, index.css; chat reserves clearance via ``pt-20``,
 * terminal-first via ``pt-16``). Left slot: open-sidebar +
 * back-to-parent. Right slot: desktop action buttons (Agent info ·
 * Share · right-panel toggle), a mobile three-dot menu mirroring the
 * same actions, and a mobile FAB that opens the rail tabs as
 * full-screen drawers. Stop session lives in the sidebar row's kebab
 * menu; Clone lives on each assistant message's "Fork from here"
 * action (ChatPage), not here.
 *
 * All state lives in AppShell — this is a pure presentational component.
 */
export function ChatHeader({
  sidebarOpen,
  onOpenSidebar,
  isChildSession,
  parentSessionId,
  conversationId,
  boundAgent,
  canShare,
  shareDisabled = false,
  shareDisabledReason,
  onShare,
  hasAgentInfo,
  onAgentInfo,
  hasHeaderMenu,
  showFilesPanel,
  hasRailContent,
  rightPanelOpen,
  onToggleRightPanel,
  mobileMenu,
}: ChatHeaderProps) {
  return (
    <header
      className={cn(
        // h-14 fixes the bar at 56px: 12px symmetric vertical padding around
        // the 32px controls. No own background — the app canvas shows
        // through (a scrim can't track the canvas gradient; see #3010).
        // Scrolled chat text can't render through the controls because the
        // conversation viewport fades its top edge instead (chat-scroll-fade
        // in index.css, applied in ChatPage).
        "chat-header absolute inset-x-0 top-0 z-30 flex h-14 items-center justify-between px-2 py-3",
      )}
    >
      {/* Left slot: sidebar toggle (when sidebar is closed) and a
          back-to-parent link (when this is a sub-agent session). The
          back link is the mobile-friendly counterpart of the nested
          sidebar row — on a phone the sidebar is collapsed and the
          nesting is invisible, so this affordance is the only way
          out of a child session without opening the sidebar. */}
      {/* With the sidebar closed this slot reaches the window corner,
          where the macOS Electron shell's traffic lights float — drop
          just this slot below them (the right action cluster stays up
          in the title-bar strip). Inert outside the shell (index.css). */}
      <div className={cn("flex items-center gap-1", !sidebarOpen && "traffic-light-clearance")}>
        {!sidebarOpen && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label="Open sidebar"
                onClick={onOpenSidebar}
                className="text-muted-foreground hover:text-foreground"
              >
                <PanelLeftIcon className="size-4" />
              </Button>
            </TooltipTrigger>
            {/* Bottom placement keeps the tooltip clear of the macOS
                Electron shell's traffic lights at the window's top edge. */}
            <TooltipContent side="bottom">Open sidebar</TooltipContent>
          </Tooltip>
        )}
        {isChildSession && parentSessionId && (
          <>
            {/* Back affordance. Ghost (not a filled pill) so it sits on the
                header's transparent overlay like the sidebar/panel toggles —
                an opaque fill would read as a hard tile over the glass canvas
                (the bar paints no background; see the header comment above).
                The chevron + label still make it read as a way out, paired
                with the sub-agent identity block beside it. */}
            <Button
              asChild
              type="button"
              variant="ghost"
              size="sm"
              className="gap-0.5 pl-1.5 pr-2 text-muted-foreground hover:text-foreground"
            >
              <Link to={`/c/${parentSessionId}`} aria-label="Back to parent session">
                <ChevronLeftIcon className="size-4" />
                <span>Back</span>
              </Link>
            </Button>
            {/* Divider + sub-agent identity. The agent name (from the bound
                agent) plus the "Sub-agent" caption make the nesting obvious
                on a phone, where the sidebar — and the tree it shows — is
                collapsed. Falls back to a plain "Sub-agent" label until the
                agent snapshot resolves, so the two lines never both read
                "Sub-agent". */}
            <span aria-hidden className="mx-1 h-5 w-px bg-border" />
            <div className="flex min-w-0 items-center gap-2">
              <BotIcon className="size-4 shrink-0 text-muted-foreground" />
              {boundAgent?.name ? (
                <div className="flex min-w-0 flex-col leading-tight">
                  <span className="truncate text-sm font-semibold text-foreground">
                    {boundAgent.name}
                  </span>
                  <span className="text-xs text-muted-foreground">Sub-agent</span>
                </div>
              ) : (
                <span className="text-sm font-semibold text-foreground">Sub-agent</span>
              )}
            </div>
          </>
        )}
      </div>

      <div className="flex items-center gap-1">
        {/* Other users currently viewing this session (presence).
            Self-contained — reads the chat store directly, renders
            nothing when the user is alone. */}
        {conversationId && <PresenceAvatars />}
        {/* Desktop (md+) action buttons. On mobile these collapse into
            the three-dot "Session actions" menu below, which renders
            the same set off the same gating booleans. Clone has no
            header presence at all — it's reached via the per-message
            "Fork from here" action on assistant bubbles (ChatPage). */}
        {/* Agent info: tools & policies for the bound agent. Desktop-only
            popover; self-hides when the agent has neither configured. */}
        {conversationId && (
          <AgentInfoButton agent={boundAgent} sessionId={conversationId} showIntelligentRouting />
        )}
        {/* Mobile-only three-dot menu folding the action buttons above
            (Share · Agent info) so the header stays
            uncluttered on a phone. The right-panel/rail control is
            deliberately left out — it has its own affordance below. */}
        {hasHeaderMenu && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label="Session actions"
                data-testid="session-actions-menu"
                className="text-muted-foreground hover:text-foreground md:hidden"
              >
                <EllipsisVerticalIcon className="size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-44">
              {canShare && (
                <DropdownMenuItem
                  onSelect={shareDisabled ? undefined : onShare}
                  disabled={shareDisabled}
                  data-testid="mobile-share-session"
                  title={shareDisabledReason}
                  className="gap-2.5 px-2.5 py-2 text-base"
                >
                  <ShareIcon className="size-4" />
                  Share
                </DropdownMenuItem>
              )}
              {hasAgentInfo && (
                <DropdownMenuItem
                  onSelect={onAgentInfo}
                  data-testid="mobile-agent-info"
                  className="gap-2.5 px-2.5 py-2 text-base"
                >
                  <InfoIcon className="size-4" />
                  Agent info
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
        {canShare && shareDisabled && shareDisabledReason ? (
          <Tooltip>
            <TooltipTrigger asChild>
              {/* Disabled buttons don't receive pointer events, so the wrapper
                  owns hover/focus for the explanatory tooltip. */}
              <span
                tabIndex={0}
                aria-label={`Share session disabled: ${shareDisabledReason}`}
                className="hidden md:inline-flex"
              >
                <Button
                  type="button"
                  aria-label="Share session"
                  disabled
                  title={shareDisabledReason}
                  // share-button-glassy (index.css) paints the pink gradient,
                  // shadow, and white text in both light and dark mode.
                  className="share-button-glassy h-8 rounded-full px-6 text-13 font-normal text-white"
                >
                  <ShareIcon className="size-4" />
                  Share
                </Button>
              </span>
            </TooltipTrigger>
            <TooltipContent side="bottom">{shareDisabledReason}</TooltipContent>
          </Tooltip>
        ) : canShare ? (
          <Button
            type="button"
            aria-label="Share session"
            onClick={onShare}
            // share-button-glassy (index.css) paints the pink gradient,
            // shadow, and white text in both light and dark mode.
            className="share-button-glassy hidden h-8 rounded-full px-6 text-13 font-normal text-white md:inline-flex"
          >
            <ShareIcon className="size-4" />
            Share
          </Button>
        ) : null}
        {conversationId && hasRailContent && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={rightPanelOpen ? "Collapse right panel" : "Expand right panel"}
                onClick={onToggleRightPanel}
                className="hidden md:inline-flex text-muted-foreground hover:text-foreground"
              >
                {rightPanelOpen ? (
                  <PanelRightCloseIcon className="size-4" />
                ) : (
                  <PanelRightIcon className="size-4" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              {rightPanelOpen ? "Collapse right panel" : "Expand right panel"}
            </TooltipContent>
          </Tooltip>
        )}
        {/* Mobile-only FAB → dropdown of rail entries. Each entry opens
            the matching rail tab's content as a full-screen drawer,
            mirroring the desktop rail's tab strip (Files · Agents ·
            Shells · Tasks). Hidden when a push panel is
            already taking up the right side, and suppressed entirely
            when there's nothing to show.
            In terminal-first sessions, `panelOpen` is true when the
            user picks Terminal view, but no drawer is mounted — the
            terminal renders inline in main — so the FAB stays
            available there. */}
        {conversationId &&
          !mobileMenu.fileViewerOpen &&
          (!mobileMenu.panelOpen || mobileMenu.terminalFirst) &&
          !mobileMenu.executionLogsOpen &&
          !mobileMenu.filesPanelOpen &&
          !mobileMenu.subagentsPanelOpen &&
          !mobileMenu.todosPanelOpen &&
          !mobileMenu.terminalsListOpen &&
          (hasRailContent || mobileMenu.debugMode) && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label="Open session menu"
                  className="text-muted-foreground hover:text-foreground md:hidden"
                >
                  <PanelRightIcon className="size-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-44">
                {showFilesPanel && (
                  <DropdownMenuItem
                    onSelect={mobileMenu.onOpenFiles}
                    className="gap-2.5 px-2.5 py-2 text-base"
                  >
                    <FileIcon className="size-4" />
                    Files
                    {mobileMenu.changedCount > 0 && (
                      <span
                        className={cn(TAB_BADGE_BASE, "ml-auto bg-muted text-muted-foreground")}
                      >
                        {mobileMenu.changedCount}
                      </span>
                    )}
                  </DropdownMenuItem>
                )}
                {/* Agents — always present (the panel lists at least
                    the main agent); the badge counts the whole tree,
                    main agent included. */}
                <DropdownMenuItem
                  onSelect={mobileMenu.onOpenSubagents}
                  className="gap-2.5 px-2.5 py-2 text-base"
                >
                  <BotIcon className="size-4" />
                  Agents
                  <span
                    className={cn(
                      TAB_BADGE_BASE,
                      "ml-auto",
                      mobileMenu.subagentsWorking > 0
                        ? "bg-success/15 text-success"
                        : "bg-muted text-muted-foreground",
                    )}
                  >
                    {mobileMenu.subagentsWorking > 0
                      ? `${mobileMenu.subagentsWorking}/${mobileMenu.agentCount}`
                      : mobileMenu.agentCount}
                  </span>
                </DropdownMenuItem>
                {/* Shells — same gate as the desktop rail's Shells tab
                    (``showShellsTab``): shown when the agent declares
                    shell access or a shell exists, hidden for
                    claude-native sub-agents. Opens the same
                    InlineTerminalsSection list as the desktop tab in a
                    drawer (where "+ New shell" is the empty-state entry
                    point), rather than jumping straight into a terminal.
                    The agent's own terminal (SDK REPL / native vendor
                    pane) is excluded from the count, so the badge — like
                    the desktop tab's — appears only once a real shell
                    exists; a "0" next to a default-visible entry reads as
                    an error state. */}
                {mobileMenu.showShellsTab && (
                  <DropdownMenuItem
                    onSelect={mobileMenu.onOpenShells}
                    className="gap-2.5 px-2.5 py-2 text-base"
                  >
                    <TerminalIcon className="size-4" />
                    Shells
                    {mobileMenu.terminalsLength > 0 && (
                      <span
                        className={cn(TAB_BADGE_BASE, "ml-auto bg-muted text-muted-foreground")}
                      >
                        {mobileMenu.terminalsLength}
                      </span>
                    )}
                  </DropdownMenuItem>
                )}
                {mobileMenu.isClaudeNative && mobileMenu.todosTotal > 0 && (
                  <DropdownMenuItem
                    onSelect={mobileMenu.onOpenTodos}
                    className="gap-2.5 px-2.5 py-2 text-base"
                  >
                    <ListTodoIcon className="size-4" />
                    Tasks
                    <span className={cn(TAB_BADGE_BASE, "ml-auto bg-muted text-muted-foreground")}>
                      {mobileMenu.todosCompleted}/{mobileMenu.todosTotal}
                    </span>
                  </DropdownMenuItem>
                )}
                {mobileMenu.debugMode && (
                  <DropdownMenuItem
                    onSelect={mobileMenu.onOpenMainExecutionLog}
                    className="gap-2.5 px-2.5 py-2 text-base"
                  >
                    <ListIcon className="size-4" />
                    Logs
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
      </div>
    </header>
  );
}
