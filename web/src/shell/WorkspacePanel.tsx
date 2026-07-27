import {
  BotIcon,
  FileIcon,
  FilesIcon,
  GlobeIcon,
  ListTodoIcon,
  MaximizeIcon,
  MinimizeIcon,
  PlusIcon,
  SquareTerminalIcon,
  TerminalIcon,
  XIcon,
} from "lucide-react";
import { type ReactElement, useCallback, useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import { isOwnerLevel } from "@/lib/permissionsApi";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { TerminalView } from "@/components/blocks/TerminalView";
import { BrowserPane } from "@/components/BrowserPane/BrowserPane";
import { useSessionAgent } from "@/hooks/useAgents";
import { terminalTabKey, useCreateTerminal, useTerminals } from "@/hooks/useTerminals";
import { FilesPanel } from "./FilesPanel";
import { FileViewer } from "./FileViewer";
import type { ChangedSort } from "./FlatFileList";
import { InlineTerminalsSection } from "./InlineTerminalsSection";
import { SubagentsPanel } from "./SubagentsPanel";
import { TodoPanel } from "./TodoPanel";
import { useTerminalStatuses } from "./useTerminalStatuses";
import { type RightRailTab, TAB_BADGE_BASE } from "./railTabs";

function WorkspaceTabTooltip({
  label,
  className,
  children,
}: {
  label: string;
  className?: string;
  children: ReactElement;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className={cn("inline-flex shrink-0", className)}>{children}</span>
      </TooltipTrigger>
      <TooltipContent side="bottom">{label}</TooltipContent>
    </Tooltip>
  );
}

// ---------------------------------------------------------------------------
// NewTabMenu — the "+" affordance in the tab strip. Opens a small dropdown
// ("Open new") offering the surfaces a user can spin up on demand: a Browser
// (switches the rail to the embedded browser tab) and a Shell (creates a
// terminal and opens it as a rail tab). Each entry self-gates — Browser only
// when the embedded browser is available, Shell only when the agent's spec
// declares terminal access — so the menu renders nothing when neither applies.
// ---------------------------------------------------------------------------

function NewTabMenu({
  conversationId,
  onOpenTerminal,
  triggerClassName,
}: {
  conversationId: string;
  /** Open a freshly-created terminal as a rail tab by its tab key. */
  onOpenTerminal: (key: string) => void;
  /** Extra classes on the trigger wrapper — used to cancel the open-tabs
   *  region's gap so the "+" hugs the last tab. */
  triggerClassName?: string;
}) {
  const { data: agent } = useSessionAgent(conversationId);
  const create = useCreateTerminal(conversationId);
  // Shell access mirrors NewTerminalButton's gate: the agent's spec must
  // declare a non-empty ``terminals:`` block. The first declared name is the
  // default we launch (native wrappers put ``$SHELL`` first).
  const declaredTerminals = agent?.terminals ?? [];
  const canOpenShell = declaredTerminals.length > 0;
  // Nothing to offer → no "+" button at all. (The embedded browser is one view
  // per conversation, reached via its own pinned tab, so it's not offered here.)
  if (!canOpenShell) return null;

  const launchShell = () => {
    create.mutate(declaredTerminals[0], {
      onSuccess: (info) => onOpenTerminal(terminalTabKey(info)),
    });
  };

  return (
    <DropdownMenu>
      <WorkspaceTabTooltip label="Open new" className={triggerClassName}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label="Open new"
            disabled={create.isPending}
            className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-default disabled:opacity-50"
          >
            <PlusIcon className="size-4" />
          </button>
        </DropdownMenuTrigger>
      </WorkspaceTabTooltip>
      <DropdownMenuContent align="start">
        <DropdownMenuLabel>Open new</DropdownMenuLabel>
        <DropdownMenuItem onSelect={launchShell} disabled={create.isPending}>
          <TerminalIcon className="size-4" />
          Shell
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// ---------------------------------------------------------------------------
// FileTabsStrip — open file tabs rendered in the top rail tab strip, as peers
// of the fixed Files/Terminals/Agents/Tasks tabs. Each tab is a cell with the
// file's basename and an "x" close button. Clicking the cell activates the
// tab (opening its viewer); clicking the x closes it. No own scroll container
// or flex-1: the parent strip's overflow-x-auto scrolls the whole row.
// ---------------------------------------------------------------------------

function FileTabsStrip({
  openFiles,
  activeFilePath,
  onFileSelect,
  onCloseFile,
}: {
  /** Ordered list of open file paths. */
  openFiles: string[];
  /** Currently active file path, or null when a scope/other tab is active. */
  activeFilePath: string | null;
  /** Activate a tab by path. */
  onFileSelect: (path: string) => void;
  /** Close a tab by path. */
  onCloseFile: (path: string) => void;
}) {
  // Scroll the active tab into view when it changes (e.g. a newly opened file
  // appended past the visible edge). `inline: "nearest"` scrolls whichever
  // ancestor is the scroller — the outer strip (<500px) or the file-tabs
  // region (≥500px) — without us hard-coding which one.
  const activeTabRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    activeTabRef.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [activeFilePath]);
  // Render nothing (not an empty flex row) when there are no files — an empty
  // wrapper would still consume a slot in the parent's gap-0.5 and leave a
  // phantom gap before the next element.
  if (openFiles.length === 0) return null;
  return (
    <div className="flex items-center gap-0.5">
      {openFiles.map((path) => {
        const name = path.split("/").pop() ?? path;
        const active = path === activeFilePath;
        return (
          <div
            key={path}
            ref={active ? activeTabRef : undefined}
            role="button"
            tabIndex={0}
            aria-current={active}
            title={path}
            onClick={() => onFileSelect(path)}
            onAuxClick={(e) => {
              // Middle click (button 1) closes the tab, matching browser /
              // editor tab conventions.
              if (e.button === 1) {
                e.preventDefault();
                onCloseFile(path);
              }
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onFileSelect(path);
              }
            }}
            className={cn(
              // Match the fixed TabsTrigger pill's box metrics (h-32 / px-12 /
              // rounded-8 / 13px medium) so file tabs and Files/Terminals tabs
              // are the same height and the active chip lines up across both
              // sets. `group/tab` drives the hover-revealed close overlay below.
              // `overflow-hidden` clips the hover-close gradient overlay to the
              // pill's rounded corners so its rectangular edges can't poke out.
              "group/tab relative flex h-[32px] min-w-0 max-w-[320px] shrink-0 cursor-pointer items-center justify-center gap-[6px] overflow-hidden rounded-[8px] px-[12px] text-[13px] font-medium leading-5 transition-colors",
              active
                ? "bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            <FileIcon className="size-4 shrink-0" />
            <span className="min-w-0 truncate">{name}</span>
            {/* Close button: hidden until hover, then revealed over a gradient
                that fades the truncated filename into the tab's background so
                the "x" never collides with the text. The fade color tracks the
                tab's own background — the gray chip when active, card otherwise
                — so the mask blends in instead of flashing a white patch. */}
            <span
              className={cn(
                "absolute inset-y-0 right-[2px] flex items-center pl-[12px] pr-[4px] opacity-0 transition-opacity group-hover/tab:opacity-100",
                active
                  ? "[background:linear-gradient(to_right,transparent,color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))_40%)]"
                  : "[background:linear-gradient(to_right,transparent,var(--card)_40%)]",
              )}
            >
              <button
                type="button"
                aria-label={`Close ${name}`}
                className="flex size-6 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                onClick={(e) => {
                  e.stopPropagation();
                  onCloseFile(path);
                }}
              >
                <XIcon className="size-4" />
              </button>
            </span>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// TerminalTabsStrip — open shell tabs rendered in the top rail strip, as peers
// of the open file tabs. Each tab is a cell with the shell's name and an "x"
// close button, mirroring FileTabsStrip. Clicking the cell activates the tab
// (surfacing its xterm in the content slot); clicking the x closes it. Keys are
// ``terminalTabKey`` values; the label falls back to the raw key when the
// terminal hasn't loaded into the query cache yet.
// ---------------------------------------------------------------------------

function TerminalTabsStrip({
  openTerminals,
  activeTerminalKey,
  labelFor,
  onSelect,
  onClose,
}: {
  /** Ordered list of open terminal tab keys. */
  openTerminals: string[];
  /** Currently active terminal key, or null when another tab is active. */
  activeTerminalKey: string | null;
  /** Resolve a tab key to its display label (shell name / session). */
  labelFor: (key: string) => string;
  /** Activate a terminal tab by key. */
  onSelect: (key: string) => void;
  /** Close a terminal tab by key. */
  onClose: (key: string) => void;
}) {
  const activeTabRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    activeTabRef.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [activeTerminalKey]);
  // Render nothing (not an empty flex row) when there are no shells — an empty
  // wrapper would still consume a slot in the parent's gap-0.5 and leave a
  // phantom gap before the next element (e.g. the trailing "+").
  if (openTerminals.length === 0) return null;
  return (
    <div className="flex items-center gap-0.5">
      {openTerminals.map((key) => {
        const name = labelFor(key);
        const active = key === activeTerminalKey;
        return (
          <div
            key={key}
            ref={active ? activeTabRef : undefined}
            role="button"
            tabIndex={0}
            aria-current={active}
            title={name}
            onClick={() => onSelect(key)}
            onAuxClick={(e) => {
              if (e.button === 1) {
                e.preventDefault();
                onClose(key);
              }
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelect(key);
              }
            }}
            className={cn(
              // Match FileTabsStrip's pill metrics so shell and file tabs line
              // up in the same strip.
              "group/tab relative flex h-[32px] min-w-0 max-w-[320px] shrink-0 cursor-pointer items-center justify-center gap-[6px] overflow-hidden rounded-[8px] px-[12px] text-[13px] font-medium leading-5 transition-colors",
              active
                ? "bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            <TerminalIcon className="size-4 shrink-0" />
            <span className="min-w-0 truncate text-[11px]">{name}</span>
            <span
              className={cn(
                "absolute inset-y-0 right-[2px] flex items-center pl-[12px] pr-[4px] opacity-0 transition-opacity group-hover/tab:opacity-100",
                active
                  ? "[background:linear-gradient(to_right,transparent,color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))_40%)]"
                  : "[background:linear-gradient(to_right,transparent,var(--card)_40%)]",
              )}
            >
              <button
                type="button"
                aria-label={`Close ${name}`}
                className="flex size-6 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                onClick={(e) => {
                  e.stopPropagation();
                  onClose(key);
                }}
              >
                <XIcon className="size-4" />
              </button>
            </span>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// RailTerminalView — the xterm for the active shell tab, mounted in the rail's
// content slot. A thin wrapper around TerminalView that resolves the terminal
// id from its tab key and tracks per-terminal connection/activity status.
// ---------------------------------------------------------------------------

function RailTerminalView({
  conversationId,
  terminalKey,
  readOnly,
}: {
  conversationId: string;
  terminalKey: string;
  readOnly: boolean;
}) {
  const { terminals } = useTerminals(conversationId);
  const { setTerminalConnectionState, markTerminalActive } = useTerminalStatuses(terminals);
  const terminal = terminals.find((t) => terminalTabKey(t) === terminalKey) ?? null;
  if (!terminal) {
    return (
      <div className="flex flex-1 items-center justify-center text-muted-foreground text-sm">
        Shell not available.
      </div>
    );
  }
  return (
    <div key={terminal.id} className="flex h-full min-h-0 flex-col p-2">
      <TerminalView
        sessionId={conversationId}
        terminalId={terminal.id}
        readOnly={readOnly}
        transport={terminal.transport}
        onStateChange={(state) => setTerminalConnectionState(terminal.id, state)}
        onActivity={() => markTerminalActive(terminal.id)}
      />
    </div>
  );
}

/**
 * Props for {@link WorkspacePanel}. All state lives in AppShell; this
 * component is a pure view. Handlers wrap the AppShell setters so the
 * shell keeps single-source-of-truth over file/terminal/panel state.
 */
interface WorkspacePanelProps {
  /** Active session id — panels read the workspace against it. */
  conversationId: string;
  /** Current rail width (px), driven by the resize handle. */
  width: number;
  /** Whether the panel is closed/collapsed (hides it from keyboard nav + assistive tech). */
  inert?: boolean;
  /**
   * Props for the left-edge resize handle (onMouseDown/onKeyDown + ARIA),
   * from ``useResizableInlinePanel().handleProps``.
   */
  handleProps: React.HTMLAttributes<HTMLDivElement> & { tabIndex: number };
  /** Selected rail tab, e.g. ``"files"``. */
  rightRailTab: RightRailTab;
  /**
   * Switch rail tabs. AppShell owns the side effects (clearing any open
   * file + its comments + URL) so they can't drift from the tab state.
   */
  onRightRailTabChange: (next: RightRailTab) => void;
  /** Whether the Files tab is available (agent spec exposes an os_env). */
  showFilesPanel: boolean;
  /** Whether the Browser tab is available — Electron shell only (hidden in a
   *  plain web build, which has no embedded WebContentsView). */
  showBrowserTab: boolean;
  /** Count of changed files, shown as the Files tab badge. */
  changedCount: number;
  /**
   * Whether the Shells tab is available — AppShell's combined gate
   * (not a native wrapper, AND either a shell exists or the agent's
   * spec declares shell access, which makes the tab show by default
   * with its "+ New shell" empty state).
   */
  showShellsTab: boolean;
  /** Number of open shells, shown as the Shells tab badge when > 0. */
  terminalsLength: number;
  /** How many child agents are actively working (Agents tab badge). */
  subagentsWorking: number;
  /**
   * Total agents in the session tree, main agent included (Agents tab
   * badge denominator) — starts at 1 for a lone agent.
   */
  agentCount: number;
  /** Whether the session publishes a todo list (gates the Tasks tab). */
  todosSupported: boolean;
  /** Number of completed todos (Tasks tab badge numerator). */
  todosCompleted: number;
  /** Total todo count (Tasks tab badge denominator + visibility gate). */
  todosTotal: number;
  /**
   * The "root" session id for the Agents tab — the active session's
   * parent when inside a child, else the active id. May be null while
   * the session snapshot loads.
   */
  rootSessionId: string | null;
  /** Active file path, or null when the Files tab shows a scope view. */
  selectedFilePath: string | null;
  /** Ordered list of open file tabs, shown as a strip in the Files panel. */
  openFiles: string[];
  /** Open a file in the inline viewer (adds/activates its tab). */
  openFileViewer: (path: string) => void;
  /** Close a single open file tab by path. */
  onCloseFile: (path: string) => void;
  /** Deselect the active file tab to reveal the scope view (Changed/All). */
  onShowScopeView: () => void;
  /** Surface the file viewer's comments-open state up to AppShell (it
   *  widens the rail to fit the comments column). */
  onCommentsOpenChange: (open: boolean) => void;
  /** Open a shell as a rail tab (adds/activates its tab), surfacing its
   *  xterm in the content slot. */
  openTerminalTab: (key: string) => void;
  /** Ordered list of open shell tab keys, shown as a strip beside the file
   *  tabs. */
  openTerminals: string[];
  /** Active shell tab key, or null when no shell tab is selected. */
  selectedTerminalKey: string | null;
  /** Close a single open shell tab by key. */
  onCloseTerminal: (key: string) => void;
  /** Whether the rail is maximized (occupies the full content area). */
  maximized: boolean;
  /** Toggle the rail's maximized state. */
  onToggleMaximized: () => void;
  /** Viewer's permission level (gates edit affordances). */
  permissionLevel: number | null;
  /** Changed-files sort order, shared with the viewer's prev/next order. */
  filesPanelSort: ChangedSort;
  /** Change the changed-files sort order. */
  onSortChange: (sort: ChangedSort) => void;
  /** Files view scope: false = full tree, true = changed-only flat list. */
  filesPanelFlatView: boolean;
  /** Toggle the Files view scope (persisted by AppShell). */
  onFlatViewChange: (flat: boolean) => void;
  /** Whether the Files panel shows dotfiles/hidden entries. */
  filesPanelShowHidden: boolean;
  /** Toggle hidden-file visibility in the Files panel. */
  onShowHiddenChange: (show: boolean) => void;
}

/**
 * WorkspacePanel — the desktop right "Workspace" rail, rendered as a
 * floating card (bg-card, rounded, bordered, shadowed) sitting below the
 * full-width chat header band. Internally tabbed between Files,
 * Terminals, Agents and Tasks so each can claim the full rail height
 * instead of competing for a vertically-split slot.
 *
 * Desktop-only (``hidden md:flex``): on mobile the rail's contents are
 * reached via the header's session-menu FAB → full-screen drawers. The
 * card is drag-resizable via a handle on its left edge.
 *
 * Render gating (default-open, hidden while a push panel owns the
 * right side) lives in AppShell — this component assumes it should
 * render when mounted.
 */
export function WorkspacePanel({
  conversationId,
  width,
  handleProps,
  inert,
  rightRailTab,
  onRightRailTabChange,
  showFilesPanel,
  showBrowserTab,
  changedCount,
  showShellsTab,
  terminalsLength,
  subagentsWorking,
  agentCount,
  todosSupported,
  todosCompleted,
  todosTotal,
  rootSessionId,
  selectedFilePath,
  openFiles,
  openFileViewer,
  onCloseFile,
  onShowScopeView,
  onCommentsOpenChange,
  openTerminalTab,
  openTerminals,
  selectedTerminalKey,
  onCloseTerminal,
  maximized,
  onToggleMaximized,
  permissionLevel,
  filesPanelSort,
  onSortChange,
  filesPanelFlatView,
  onFlatViewChange,
  filesPanelShowHidden,
  onShowHiddenChange,
}: WorkspacePanelProps) {
  // Memoized so FileViewer's Escape-to-close effect doesn't re-subscribe its
  // window keydown listener on every render — an inline arrow would change
  // identity each render and thrash the effect's add/remove cycle.
  const handleCloseTab = useCallback(() => {
    if (selectedFilePath !== null) onCloseFile(selectedFilePath);
  }, [onCloseFile, selectedFilePath]);
  // Resolve shell tab keys to display labels. The list is already fetched for
  // the Shells tab / count badge, so this shares the same query cache.
  const { terminals } = useTerminals(conversationId);
  const terminalLabelFor = useCallback(
    (key: string) => {
      const t = terminals.find((term) => terminalTabKey(term) === key);
      if (!t) return key.replace(/^terminal:/, "");
      return t.session ? `${t.name} · ${t.session}` : t.name;
    },
    [terminals],
  );
  return (
    <aside
      aria-label="Workspace"
      inert={inert}
      // Floating desktop surface: 8px from every edge. AppShell reserves the
      // panel width from ChatHeader, so the pane can extend to the top without
      // sitting underneath the existing session action cluster.
      // ``@container/rail`` makes the rail a named container-query context so
      // the tab strip can switch scroll behavior on the rail's own width
      // (see the strip below) without a JS width listener.
      //
      // Maximized: break out of the flex row and stretch across the content
      // region (absolute inset-0) so the rail owns the full width. It keeps the
      // same m-2 / rounded-lg / bordered card styling as when docked — only the
      // width changes, the 8px inset (and thus the height) stays identical. The
      // resize handle is suppressed in that state — there's no neighbor to
      // resize against.
      className={cn(
        "@container/rail relative z-40 hidden md:m-2 md:flex md:min-h-0 md:flex-col md:overflow-hidden md:rounded-lg md:border md:border-border md:bg-card md:shadow-lg",
        maximized ? "md:absolute md:inset-0" : "md:shrink-0",
      )}
      // Width is fixed by the resize handle normally; maximized ignores it and
      // stretches to the absolute inset instead.
      style={maximized ? undefined : { width }}
    >
      {/* Left-edge horizontal resize handle — suppressed while maximized. */}
      {!maximized && (
        <div
          {...handleProps}
          className="absolute inset-y-0 left-0 z-10 w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 transition-colors"
        />
      )}
      {/* Tab strip, in display order Files · Agents · Shells · Tasks.
          Files and Agents are always present (the Agents panel lists at
          least the main agent). Shells shows whenever AppShell's gate
          allows it (the agent declares shell access, or a shell already
          exists) — the empty state carries the "+ New shell"
          affordance, so an empty tab is an entry point, not a dead end.
          The Agents tab keys off ``rootSessionId``, so inside a child
          it lists the siblings + a "main" link back to the parent. */}
      {/* Tab strip scroll behavior is rail-width-driven (container query):
          - ≥500px: the static tabs stay put and ONLY the file tabs scroll —
            so the outer row is overflow-x-hidden and the file-tabs region owns
            the scroller (see below).
          - <500px: there isn't room to keep the static tabs anchored, so the
            WHOLE row scrolls — the outer row is the scroller (base
            overflow-x-auto) and the file region just overflows into it.
          overflow-y stays hidden so overflow-x:auto can't spawn a vertical
          scrollbar that eats horizontal space. */}
      <div className="shrink-0 flex items-center overflow-x-auto overflow-y-hidden border-b border-border px-2 py-2 [scrollbar-width:thin] @min-[500px]/rail:overflow-x-hidden [&::-webkit-scrollbar]:h-1 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [&::-webkit-scrollbar-track]:bg-transparent">
        {(openFiles.length > 0 || openTerminals.length > 0) && (
          <>
            {/* Open-tabs region (file tabs + shell tabs). ≥500px (rail container
                query): the ONLY horizontal scroller (flex-1 + overflow-x-auto),
                so the static tabs stay anchored to the right. <500px: shrink-0
                with NO overflow set — it keeps its natural width and the whole
                row overflows into the outer scroller, so the strip scrolls as
                one. (overflow-y-hidden must stay scoped to the ≥500px case:
                setting it while overflow-x is `visible` would force overflow-x
                to `auto`, turning this into its own scroller and defeating the
                <500px whole-strip scroll.) */}
            <div className="flex shrink-0 items-center gap-0.5 [scrollbar-width:thin] @min-[500px]/rail:min-w-0 @min-[500px]/rail:flex-1 @min-[500px]/rail:overflow-x-auto @min-[500px]/rail:overflow-y-hidden [&::-webkit-scrollbar]:h-1 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [&::-webkit-scrollbar-track]:bg-transparent">
              <FileTabsStrip
                openFiles={openFiles}
                activeFilePath={selectedFilePath}
                onFileSelect={openFileViewer}
                onCloseFile={onCloseFile}
              />
              <TerminalTabsStrip
                openTerminals={openTerminals}
                activeTerminalKey={selectedTerminalKey}
                labelFor={terminalLabelFor}
                onSelect={openTerminalTab}
                onClose={onCloseTerminal}
              />
              {/* With open tabs, the "+" trails the last tab so it reads as
                  "add another tab" rather than a nav control. -ml-0.5 cancels
                  the region's gap-0.5 so it hugs the last tab. */}
              <NewTabMenu
                conversationId={conversationId}
                onOpenTerminal={openTerminalTab}
                triggerClassName="-ml-0.5"
              />
            </div>
            {/* 1px divider separating the open tabs from the static tabs. It's
                the leftmost element of the right cluster (divider + nav tabs +
                maximize), so it carries the row's single ml-auto that pushes
                that whole cluster flush right — keeping the divider glued to the
                nav group instead of stranded by the auto-gap. */}
            <div
              aria-hidden
              className="mx-[4px] ml-auto h-[14px] w-px shrink-0 self-center bg-border-strong"
            />
          </>
        )}
        <Tabs
          // Static group — never compresses (shrink-0). When open tabs exist
          // the divider before it owns the row's ml-auto and drags this group
          // (and the trailing maximize) flush right; with no open tabs there's
          // no divider, so the maximize button owns ml-auto and this group
          // stays left. Exactly one ml-auto in the row either way.
          className="shrink-0"
          // When a file or shell tab is active no fixed trigger should
          // highlight, so feed the radix group a sentinel that matches none of
          // them. The active file/shell tab carries its own highlight.
          value={
            selectedFilePath !== null || selectedTerminalKey !== null ? "__tab__" : rightRailTab
          }
          onValueChange={(v) => onRightRailTabChange(v as RightRailTab)}
        >
          <TabsList variant="pill" className="gap-0">
            {showFilesPanel && (
              <WorkspaceTabTooltip label="Files">
                <TabsTrigger
                  value="files"
                  aria-label={changedCount > 0 ? `Files ${changedCount} changed` : "Files"}
                  className="size-8 shrink-0 rounded-md p-0 hover:bg-muted"
                >
                  <FilesIcon className="size-4" />
                  <span className="sr-only">Files</span>
                  {changedCount > 0 && <span className="sr-only">{changedCount}</span>}
                </TabsTrigger>
              </WorkspaceTabTooltip>
            )}
            <WorkspaceTabTooltip label="Agents">
              <TabsTrigger
                value="subagents"
                aria-label={
                  subagentsWorking > 0
                    ? `Agents ${subagentsWorking}/${agentCount}`
                    : `Agents ${agentCount}`
                }
                className="size-8 shrink-0 rounded-md p-0 hover:bg-muted"
              >
                <BotIcon className="size-4" />
                <span className="sr-only">Agents</span>
                <span
                  className={cn(
                    TAB_BADGE_BASE,
                    "sr-only",
                    subagentsWorking > 0 ? "text-success" : "text-muted-foreground",
                  )}
                >
                  {subagentsWorking > 0 ? `${subagentsWorking}/${agentCount}` : agentCount}
                </span>
              </TabsTrigger>
            </WorkspaceTabTooltip>
            {showShellsTab && (
              <WorkspaceTabTooltip label="Shells">
                <TabsTrigger
                  value="terminals"
                  aria-label={terminalsLength > 0 ? `Shells ${terminalsLength}` : "Shells"}
                  className="size-8 shrink-0 rounded-md p-0 hover:bg-muted"
                >
                  <SquareTerminalIcon className="size-4" />
                  <span className="sr-only">Shells</span>
                  {terminalsLength > 0 && (
                    <span className="sr-only text-muted-foreground">{terminalsLength}</span>
                  )}
                </TabsTrigger>
              </WorkspaceTabTooltip>
            )}
            {todosSupported && todosTotal > 0 && (
              <WorkspaceTabTooltip label="Tasks">
                <TabsTrigger
                  value="todos"
                  aria-label={`Tasks ${todosCompleted} of ${todosTotal} completed`}
                  className="size-8 shrink-0 rounded-md p-0 hover:bg-muted"
                >
                  <ListTodoIcon className="size-4" />
                  <span className="sr-only">Tasks</span>
                  <span className="sr-only">
                    {todosCompleted}/{todosTotal}
                  </span>
                </TabsTrigger>
              </WorkspaceTabTooltip>
            )}
            {showBrowserTab && (
              <WorkspaceTabTooltip label="Browser">
                <TabsTrigger
                  value="browser"
                  aria-label="Browser"
                  className="size-8 shrink-0 rounded-md p-0 hover:bg-muted"
                >
                  <GlobeIcon className="size-4" />
                  <span className="sr-only">Browser</span>
                </TabsTrigger>
              </WorkspaceTabTooltip>
            )}
          </TabsList>
        </Tabs>
        {/* "+" — open a new Shell tab. With no open tabs it sits here, right
            after the nav tabs (next to Shells); once tabs exist it moves into
            the open-tabs region to trail the last tab (see above). Self-gates
            to nothing when the agent has no terminal access. */}
        {openFiles.length === 0 && openTerminals.length === 0 && (
          <NewTabMenu conversationId={conversationId} onOpenTerminal={openTerminalTab} />
        )}
        {/* Maximize/minimize toggle, at the rightmost edge. It owns the row's
            single ml-auto ONLY when there are no open tabs (nav group stays
            left, this pins right). With open tabs the nav group carries ml-auto
            and this button just trails it — a second ml-auto here would split
            the free space and strand the nav group mid-strip. */}
        <WorkspaceTabTooltip
          label={maximized ? "Exit full screen" : "Full screen"}
          className={cn(openFiles.length === 0 && openTerminals.length === 0 && "ml-auto")}
        >
          <button
            type="button"
            aria-label={maximized ? "Exit full screen" : "Full screen"}
            aria-pressed={maximized}
            onClick={onToggleMaximized}
            className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            {maximized ? <MinimizeIcon className="size-4" /> : <MaximizeIcon className="size-4" />}
          </button>
        </WorkspaceTabTooltip>
      </div>
      {/* Tab content — single slot. An open shell tab holds its xterm; a
          file tab holds FileViewer; the Files tab shows FilesPanel; the
          Shells tab holds the list-only inline section (clicking a row
          opens the shell as a tab above, surfacing its xterm here);
          Subagents lists the root's children + a "main" link back to the
          parent. The Shells branch is unreachable when its tab is hidden —
          native wrappers, claude-native sub-agents, or no shell attached. */}
      <div data-workspace-panel-content className="flex min-h-0 flex-1 flex-col overflow-hidden">
        {selectedTerminalKey !== null ? (
          <RailTerminalView
            conversationId={conversationId}
            terminalKey={selectedTerminalKey}
            readOnly={!isOwnerLevel(permissionLevel)}
          />
        ) : selectedFilePath !== null ? (
          <FileViewer
            frameless
            open
            conversationId={conversationId}
            path={selectedFilePath}
            onClose={onShowScopeView}
            onCloseTab={handleCloseTab}
            onNavigateTo={openFileViewer}
            permissionLevel={permissionLevel}
            onCommentsOpenChange={onCommentsOpenChange}
            sort={filesPanelSort}
          />
        ) : rightRailTab === "browser" && showBrowserTab ? (
          // Embedded browser (Electron only) — BrowserPane self-gates and
          // measures this rail slot to position the native view over it.
          <BrowserPane conversationId={conversationId} className="min-h-0 flex-1" />
        ) : rightRailTab === "subagents" && rootSessionId ? (
          <SubagentsPanel conversationId={conversationId} rootSessionId={rootSessionId} />
        ) : rightRailTab === "todos" && todosSupported ? (
          <TodoPanel frameless />
        ) : rightRailTab === "terminals" && showShellsTab ? (
          <InlineTerminalsSection conversationId={conversationId} onExpand={openTerminalTab} />
        ) : (
          showFilesPanel && (
            <FilesPanel
              frameless
              onFileSelect={openFileViewer}
              flatView={filesPanelFlatView}
              onFlatViewChange={onFlatViewChange}
              showHidden={filesPanelShowHidden}
              onShowHiddenChange={onShowHiddenChange}
              sort={filesPanelSort}
              onSortChange={onSortChange}
            />
          )
        )}
      </div>
    </aside>
  );
}
