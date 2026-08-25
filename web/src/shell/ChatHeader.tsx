import {
  BotIcon,
  EllipsisVerticalIcon,
  FileIcon,
  GitCompareIcon,
  InfoIcon,
  ListIcon,
  PanelLeftIcon,
  PanelRightCloseIcon,
  PanelRightIcon,
  ShareIcon,
  TerminalIcon,
  UserPlusIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { AgentInfoButton } from "@/components/AgentInfo";
import { ConversationBreadcrumb } from "./ConversationBreadcrumb";
import { HeaderConversationMenu } from "./HeaderConversationMenu";
import { UNTITLED_CONVERSATION_LABEL } from "./sidebarNav";
import { PresenceAvatars } from "@/components/PresenceAvatars";
import type { Agent } from "@/hooks/useAgents";
import type { Conversation } from "@/hooks/useConversations";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { cn } from "@/lib/utils";
import { MOBILE_GLASS_PILL, MOBILE_GLASS_SURFACE } from "./mobileGlass";
import { TAB_BADGE_BASE } from "./railTabs";
import { ViewModeToggle } from "./ViewModeToggle";
import { useCallback, useEffect, useRef } from "react";

/**
 * Gating flags + handlers for the mobile workspace-rail entries (Files ·
 * Changes · Agents · Shells · Logs), which ride in the header's single kebab.
 * Folded into one object because the block is a self-contained unit never read
 * by the desktop action row — keeping it grouped halves ChatHeader's
 * top-level prop count.
 */
interface MobileSessionMenuProps {
  /** True while the desktop file viewer is open (suppresses the entries). */
  fileViewerOpen: boolean;
  /** True while a terminals/exec-logs push panel owns the right side. */
  panelOpen: boolean;
  /** Terminal-first session — terminal renders inline, entries stay available. */
  terminalFirst: boolean;
  /** True while the execution-logs push panel is open. */
  executionLogsOpen: boolean;
  /** True while the mobile files drawer is open. */
  filesPanelOpen: boolean;
  /** True while the mobile agents drawer is open. */
  subagentsPanelOpen: boolean;
  /** True while the mobile shells drawer is open. */
  shellsPanelOpen: boolean;
  /** Hide the Shells entry (claude-native sub-agents only). */
  hideTerminalsTab: boolean;
  /** Whether the Shells entry is available. */
  showShellsTab: boolean;
  /** Number of open terminals (entry badge). */
  terminalsLength: number;
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
  /** Open the mobile files drawer (full folder tree). */
  onOpenFiles: () => void;
  /** Open the mobile files drawer pinned to the changed-files list. */
  onOpenChanges: () => void;
  /** Open the mobile shells drawer. */
  onOpenShells: () => void;
  /** Open the mobile agents drawer. */
  onOpenSubagents: () => void;
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
  onOpenSidebar: (peek?: boolean) => void;
  /** Whether the active session is a sub-agent (appends its identity). */
  isChildSession: boolean;
  /** Active session id, or undefined on the landing composer. */
  conversationId: string | undefined;
  /** Owner-managed top-level row backing the title-adjacent action menu. */
  actionConversation?: Conversation | null;
  /**
   * Breadcrumb title: the active conversation's display name, or its
   * immediate parent's when viewing a sub-agent. ``null`` while unresolved.
   * A child still renders the breadcrumb (and the parent link) when
   * ``titleLinkTo`` is set — the title falls back to "New session".
   */
  conversationTitle: string | null;
  /**
   * Name of the project the breadcrumb conversation is filed under, shown as
   * a leading folder icon (project name in its tooltip). ``null`` when
   * unfiled — no folder renders.
   */
  projectName: string | null;
  /**
   * Route the title links to (the parent session, when inside a sub-agent),
   * making the title a way back out. ``undefined`` renders it as plain text.
   */
  titleLinkTo?: string;
  /** The bound agent (mcp_servers + policies) for the info popover. */
  boundAgent: Agent | undefined;
  /**
   * The session's ``omnigent.wrapper`` label, or ``null``. Names the vendor
   * in the sub-agent breadcrumb: a native sub-agent child reuses its
   * parent's ``<vendor>-native-ui`` agent row, whose name is an Omnigent
   * internal the user should never see.
   */
  wrapperLabel: string | null;
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
   * (files, terminals, or sub-agents). Gates both the mobile rail entries and
   * the desktop collapse toggle — with no rail content the panel doesn't mount
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
 * terminal-first via ``pt-14``). Left slot: open-sidebar + a conversation
 * breadcrumb (``[folder] / <title> [/ <sub-agent>]``, with the session-actions
 * kebab on desktop). Right slot: desktop action buttons (Agent info ·
 * Share · right-panel toggle) and, on mobile, a **single** kebab holding both
 * the session actions (pin/share/rename/project/archive/delete) and the
 * workspace-rail entries that open Files · Agents · Shells as full-screen
 * drawers — one trigger rather than two adjacent ones. It falls back to a
 * smaller Share · Agent info + rail menu when the session isn't
 * owner-managed. Stop session lives in the sidebar row's kebab menu; Clone
 * lives on each assistant message's "Fork from here" action (ChatPage).
 *
 * All state lives in AppShell — this is a pure presentational component.
 */
export function ChatHeader({
  sidebarOpen,
  onOpenSidebar,
  isChildSession,
  conversationId,
  actionConversation = null,
  conversationTitle,
  projectName,
  titleLinkTo,
  boundAgent,
  wrapperLabel,
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
  // Dwell on the toggle for 1s to peek the sidebar; leaving before then cancels
  // the pending peek so a quick pass-over never opens it. Peek is a desktop
  // hover affordance — on mobile the toggle just opens the full-screen overlay,
  // so a tap's synthetic pointerenter must not trigger it.
  const isMobile = useIsMobileViewport();
  const { trackClick } = useOmnigentAnalytics();
  const peekTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cancelPeek = useCallback(() => {
    if (peekTimer.current) {
      clearTimeout(peekTimer.current);
      peekTimer.current = null;
    }
  }, []);
  const onPeekSidebar = useCallback(() => {
    if (isMobile) return;
    cancelPeek();
    peekTimer.current = setTimeout(() => {
      onOpenSidebar(true);
    }, 400);
  }, [isMobile, onOpenSidebar, cancelPeek]);
  useEffect(() => cancelPeek, [cancelPeek]);
  // Workspace-rail entries (Files · Changes · Agents · Shells · Logs), each
  // opening the matching rail tab as a full-screen drawer. Mobile only: they
  // ride in the header's single kebab rather than a second trigger of their
  // own. Suppressed while a push panel already owns the right side — except in
  // a terminal-first session, where `panelOpen` means the terminal renders
  // inline in main and no drawer is mounted.
  const workspaceItems =
    conversationId &&
    !mobileMenu.fileViewerOpen &&
    (!mobileMenu.panelOpen || mobileMenu.terminalFirst) &&
    !mobileMenu.executionLogsOpen &&
    !mobileMenu.filesPanelOpen &&
    !mobileMenu.subagentsPanelOpen &&
    !mobileMenu.shellsPanelOpen &&
    (hasRailContent || mobileMenu.debugMode) ? (
      <>
        {showFilesPanel && (
          <DropdownMenuItem
            onSelect={mobileMenu.onOpenFiles}
            className="gap-2.5 px-2.5 py-2 text-ui"
          >
            <FileIcon className="size-4" />
            Files
          </DropdownMenuItem>
        )}
        {showFilesPanel && (
          <DropdownMenuItem
            onSelect={mobileMenu.onOpenChanges}
            className="gap-2.5 px-2.5 py-2 text-ui"
          >
            <GitCompareIcon className="size-4" />
            Changes
            {mobileMenu.changedCount > 0 && (
              <span className={cn(TAB_BADGE_BASE, "ml-auto bg-muted text-muted-foreground")}>
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
          className="gap-2.5 px-2.5 py-2 text-ui"
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
        {/* Shells — the mobile entry into the session's shells
            (desktop has no Shells tab; it opens shells as soft tabs):
            visible when a real shell exists, or when the agent spec
            declares shell access so the empty-state "+ New shell"
            affordance is reachable on mobile too. */}
        {!mobileMenu.hideTerminalsTab && mobileMenu.showShellsTab && (
          <DropdownMenuItem
            onSelect={mobileMenu.onOpenShells}
            className="gap-2.5 px-2.5 py-2 text-ui"
          >
            <TerminalIcon className="size-4" />
            Shells
            {mobileMenu.terminalsLength > 0 && (
              <span className={cn(TAB_BADGE_BASE, "ml-auto bg-muted text-muted-foreground")}>
                {mobileMenu.terminalsLength}
              </span>
            )}
          </DropdownMenuItem>
        )}
        {mobileMenu.debugMode && (
          <DropdownMenuItem
            onSelect={mobileMenu.onOpenMainExecutionLog}
            className="gap-2.5 px-2.5 py-2 text-ui"
          >
            <ListIcon className="size-4" />
            Logs
          </DropdownMenuItem>
        )}
      </>
    ) : null;
  // Session actions (pin/share/rename/project/archive/delete). Desktop hangs
  // them off the breadcrumb title; mobile puts the kebab in the right-hand
  // control cluster instead — the native iOS/Android shells hide the
  // breadcrumb entirely (their own chrome names the session), so a
  // title-adjacent trigger would be unreachable there.
  const conversationMenu = actionConversation ? (
    <HeaderConversationMenu
      conversation={actionConversation}
      currentProject={projectName}
      canShare={canShare}
      shareDisabled={shareDisabled}
      shareDisabledReason={shareDisabledReason}
      onShare={onShare}
      hasAgentInfo={isMobile && hasAgentInfo}
      onAgentInfo={onAgentInfo}
      workspaceItems={isMobile ? workspaceItems : null}
    />
  ) : null;
  return (
    <header
      className={cn(
        // h-14 fixes the bar at 56px: 12px symmetric vertical padding around
        // the 32px controls. No own background — the app canvas shows
        // through (a scrim can't track the canvas gradient).
        // Scrolled chat text can't render through the controls because the
        // conversation viewport fades its top edge instead (chat-scroll-fade
        // in index.css, applied in ChatPage).
        "chat-header absolute inset-x-0 top-0 z-30 flex h-14 md:h-12 items-center justify-between px-2 md:px-4 py-3 md:right-[var(--workspace-panel-offset,0px)]",
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
      <div
        className={cn(
          "flex min-w-0 items-center gap-1 md:gap-6",
          !sidebarOpen && "traffic-light-clearance",
        )}
      >
        {!sidebarOpen && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                // icon on <md (size-10, comfortable tap target), icon-xs on
                // desktop (md:size-6). size="icon" gives the base size-10; the
                // md:size-6 override replaces the variant's md:size-8.
                size="icon"
                aria-label="Open sidebar"
                componentId="chat.header.open_sidebar"
                onClick={() => {
                  cancelPeek();
                  onOpenSidebar(false);
                }}
                // chat-header-sidebar-toggle is hidden on the macOS shell, where
                // the title-bar cluster carries an always-present toggle (with
                // the same dwell-to-peek) and this would be a second, offset
                // copy of it. Kept everywhere else, where it is the ONLY way to
                // reopen a collapsed sidebar.
                className={cn(
                  "chat-header-sidebar-toggle text-muted-foreground hover:text-foreground md:size-6",
                  MOBILE_GLASS_PILL,
                )}
                onPointerEnter={onPeekSidebar}
                onPointerLeave={cancelPeek}
              >
                <PanelLeftIcon className="size-4" />
              </Button>
            </TooltipTrigger>
            {/* Bottom placement keeps the tooltip clear of the macOS
                Electron shell's traffic lights at the window's top edge. */}
            <TooltipContent side="bottom">Open sidebar</TooltipContent>
          </Tooltip>
        )}
        {/* Conversation breadcrumb (see ConversationBreadcrumb). Empty on the
            landing composer. A resolved title is enough; so is titleLinkTo —
            a child must keep its climb-out while the parent title loads.
            min-w-0 on this slot lets it truncate rather than push the
            right-hand action cluster. On the macOS shell with the sidebar
            collapsed, the slot's traffic-light-clearance pads it past the
            window controls + title-bar cluster (index.css). */}
        {conversationId && (conversationTitle || titleLinkTo) && (
          <ConversationBreadcrumb
            conversationTitle={conversationTitle ?? UNTITLED_CONVERSATION_LABEL}
            projectName={projectName}
            titleLinkTo={titleLinkTo}
            isChildSession={isChildSession}
            boundAgent={boundAgent}
            wrapperLabel={wrapperLabel}
            actions={isMobile ? undefined : (conversationMenu ?? undefined)}
            className="pr-1"
          />
        )}
      </div>

      <div
        className={cn(
          "flex items-center gap-2 max-md:gap-0 max-md:empty:hidden",
          MOBILE_GLASS_PILL,
        )}
      >
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
        {conversationId && <AgentInfoButton agent={boundAgent} sessionId={conversationId} />}
        {/* Chat/Terminal switcher for terminal-first sessions — self-gates to
            null otherwise (and in the iOS shell, where it's the native bar). */}
        {conversationId && <ViewModeToggle />}
        {/* Fallback mobile kebab for sessions with no owner-managed menu:
            the action buttons above (Share · Agent info) plus the same
            workspace-rail entries, so a phone still needs only one trigger. */}
        {(hasHeaderMenu || workspaceItems) && (!actionConversation || !isMobile) && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label="Session actions"
                data-testid="session-actions-menu"
                className="text-muted-foreground hover:text-foreground md:hidden max-md:rounded-full"
              >
                <EllipsisVerticalIcon className="size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className={cn("min-w-44", MOBILE_GLASS_SURFACE)}>
              {canShare && (
                <DropdownMenuItem
                  onSelect={
                    shareDisabled
                      ? undefined
                      : () => {
                          trackClick("chat.header.mobile_share", "button");
                          onShare();
                        }
                  }
                  disabled={shareDisabled}
                  data-testid="mobile-share-session"
                  title={shareDisabledReason}
                  className="gap-2.5 px-2.5 py-2 text-ui"
                >
                  <ShareIcon className="size-4" />
                  Share
                </DropdownMenuItem>
              )}
              {hasAgentInfo && (
                <DropdownMenuItem
                  onSelect={() => {
                    trackClick("chat.header.mobile_agent_info", "button");
                    onAgentInfo();
                  }}
                  data-testid="mobile-agent-info"
                  className="gap-2.5 px-2.5 py-2 text-ui"
                >
                  <InfoIcon className="size-4" />
                  Agent info
                </DropdownMenuItem>
              )}
              {hasHeaderMenu && workspaceItems && <DropdownMenuSeparator />}
              {workspaceItems}
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
                  className="share-button-glassy h-6 gap-1 rounded-[6px] px-2 text-ui font-normal text-white"
                >
                  <span className="flex size-4 shrink-0 items-center justify-center">
                    <UserPlusIcon />
                  </span>
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
            componentId="chat.header.share"
            // share-button-glassy (index.css) paints the pink gradient,
            // shadow, and white text in both light and dark mode.
            className="share-button-glassy hidden h-6 gap-1 rounded-[6px] px-2 text-ui font-normal text-white md:inline-flex"
          >
            <span className="flex size-4 shrink-0 items-center justify-center">
              <UserPlusIcon />
            </span>
            Share
          </Button>
        ) : null}
        {conversationId && hasRailContent && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                aria-label={rightPanelOpen ? "Collapse right panel" : "Expand right panel"}
                onClick={onToggleRightPanel}
                componentId="chat.header.toggle_right_panel"
                className="hidden md:inline-flex text-muted-foreground hover:text-foreground border-none"
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
        {/* Mobile-only session-actions kebab, rightmost in the cluster. Same
            menu the desktop breadcrumb hangs off its title. */}
        {isMobile && conversationMenu}
      </div>
    </header>
  );
}
