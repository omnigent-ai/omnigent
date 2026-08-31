import {
  BotIcon,
  CheckIcon,
  FileIcon,
  FolderTreeIcon,
  FileDiffIcon,
  GlobeIcon,
  Loader2Icon,
  MaximizeIcon,
  MinimizeIcon,
  PlusIcon,
  TerminalIcon,
  XIcon,
} from "lucide-react";
import {
  type CSSProperties,
  type ReactElement,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { cn } from "@/lib/utils";
import { isEditorLevel, isOwnerLevel } from "@/lib/permissionsApi";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { TerminalView } from "@/components/blocks/TerminalView";
import { BrowserPane } from "@/components/BrowserPane/BrowserPane";
import { useSessionAgent } from "@/hooks/useAgents";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import { terminalTabKey, useCreateTerminal, useTerminals } from "@/hooks/useTerminals";
import { SuppressBrowserView } from "@/hooks/useSuppressBrowserView";
import { FilesPanel } from "./FilesPanel";
import { FileViewer } from "./FileViewer";
import type { ChangedSort } from "./FlatFileList";
import { SubagentsPanel } from "./SubagentsPanel";
import { useTerminalStatuses } from "./useTerminalStatuses";
import { type RightRailTab, TAB_BADGE_BASE } from "./railTabs";
import { Button } from "../components/ui/button";

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

// localStorage key for the last shell type launched from the "+" menu, so the
// choice is remembered across the menu's remounts (it renders in two spots) and
// reloads. App-global (not per-session): the user's preferred shell rarely
// varies by conversation.
const PREFERRED_SHELL_KEY = "omnigent:preferred-shell";

function readPreferredShell(): string | null {
  try {
    return window.localStorage.getItem(PREFERRED_SHELL_KEY);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// NewTabMenu — the "+" affordance in the tab strip. Opens a small dropdown
// ("Open new") to spin up a Shell as a rail tab. When the agent declares a
// single terminal, "Shell" launches it directly; when several are declared,
// "Shell" nests a submenu so the user picks which type to launch — the last
// pick is remembered (check-marked, and launched on a plain "Shell" click).
// Gated on the agent's spec declaring terminal access — renders nothing else.
//
// The "Shell" item also reflects the session's liveness so opening a shell on
// a disconnected session isn't a silent 502:
//   - online / wakeable (runner_asleep, host_asleep, starting): the item stays
//     enabled — the server transparently reconnects the runner on create (see
//     `ensure_runner_connected`). While the create is in flight on a wakeable
//     session it reads "Reconnecting…" with a spinner, since the cold wake can
//     take tens of seconds.
//   - offline (host_offline, local_stranded): the web can't reconnect from the
//     browser (a CLI `omnigent host` / `--resume` is required), so the item is
//     disabled and labeled "Offline" — the chat reconnect banner owns recovery.
// ---------------------------------------------------------------------------

/** How the "Shell" item should behave given the session's liveness. */
type ShellConnectState = "ready" | "wakeable" | "offline";

function shellConnectState(liveness: SessionLiveness | undefined): ShellConnectState {
  switch (liveness?.kind) {
    case "host_offline":
    case "local_stranded":
      return "offline";
    case "runner_asleep":
    case "host_asleep":
    case "starting":
      return "wakeable";
    // online, unknown, or absent: treat as ready (never block on an
    // unresolved poll — matches useSessionLiveness's "assume online").
    default:
      return "ready";
  }
}

function NewTabMenu({
  conversationId,
  onOpenTerminal,
  onCreateStart,
  onCreateError,
  triggerClassName,
  liveness,
}: {
  conversationId: string;
  /** Open a freshly-created terminal as a rail tab by its tab key. */
  onOpenTerminal: (key: string) => void;
  /** Called when a shell create is initiated (before the POST resolves), so
   *  the shell can be focused as soon as its tab appears in the list. */
  onCreateStart?: () => void;
  /** Called when the shell create POST fails, so the caller can disarm the
   *  focus snapshot armed by ``onCreateStart``. */
  onCreateError?: () => void;
  /** Extra classes on the trigger wrapper — used to cancel the open-tabs
   *  region's gap so the "+" hugs the last tab. */
  triggerClassName?: string;
  /** Open session's derived liveness — drives the "Shell" item's connect
   *  affordance. Absent is treated as ready. */
  liveness?: SessionLiveness;
}) {
  const { data: agent } = useSessionAgent(conversationId);
  const create = useCreateTerminal(conversationId);
  const connectState = shellConnectState(liveness);
  // Remembered shell type, persisted across remounts/reloads. Seeded from
  // localStorage so the "+" in either strip spot agrees on the current pick.
  const [preferred, setPreferred] = useState<string | null>(() => readPreferredShell());
  // Controlled so a launch can force the menu closed. The submenu "Shell"
  // trigger preventDefaults its click (to launch the default without toggling
  // the submenu), which also suppresses Radix's auto-close — leaving the menu
  // stuck open until a second click. Closing here fixes that.
  const [menuOpen, setMenuOpen] = useState(false);
  // Shell access mirrors NewTerminalButton's gate: the agent's spec must
  // declare a non-empty ``terminals:`` block.
  const declaredTerminals = agent?.terminals ?? [];
  const canOpenShell = declaredTerminals.length > 0;
  // Nothing to offer → no "+" button at all. (The embedded browser is one view
  // per conversation, reached via its own pinned tab, so it's not offered here.)
  if (!canOpenShell) return null;

  // The default launched on a plain "Shell" click: the remembered pick when it
  // is still a declared type, else the first declared name.
  const defaultShell =
    preferred !== null && declaredTerminals.includes(preferred) ? preferred : declaredTerminals[0];

  const launchShell = (name: string) => {
    setMenuOpen(false);
    // Signal the create is starting so the shell gets focused the moment its
    // tab lands in the list — not only when this POST resolves. On a waking
    // (runner-asleep) session the POST can lag the tab's arrival by seconds;
    // ``onOpenTerminal`` on success is a backstop for when it's already open.
    onCreateStart?.();
    create.mutate(name, {
      onSuccess: (info) => onOpenTerminal(terminalTabKey(info)),
      onError: () => onCreateError?.(),
    });
  };

  // Launch a type and remember it as the new default for next time.
  const pickShell = (name: string) => {
    setPreferred(name);
    try {
      window.localStorage.setItem(PREFERRED_SHELL_KEY, name);
    } catch {
      /* storage unavailable — the in-memory pick still holds for this mount */
    }
    launchShell(name);
  };

  // One declared shell → a direct "Shell" action. Several → a nested submenu
  // so the user picks which type to launch (mirrors NewTerminalButton's picker).
  const multipleShells = declaredTerminals.length > 1;

  // Liveness-derived affordance for the "Shell" item. A create in flight on a
  // wakeable session reads "Reconnecting…" (the server is waking the runner);
  // an offline session disables the item since the browser can't reconnect it.
  const isReconnecting = create.isPending && connectState === "wakeable";
  const shellDisabled = create.isPending || connectState === "offline";
  // Icon + label + trailing hint, shared by the single-item and submenu-trigger
  // renders so both reflect the same connect state.
  const shellItemContent = (
    <>
      {isReconnecting ? (
        <Loader2Icon className="size-4 animate-spin" />
      ) : (
        <TerminalIcon className="size-4" />
      )}
      <span className="whitespace-nowrap">{isReconnecting ? "Reconnecting…" : "Shell"}</span>
      {connectState === "offline" && (
        <span className="ml-auto pl-4 text-sm text-muted-foreground">Offline</span>
      )}
    </>
  );

  return (
    <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
      <WorkspaceTabTooltip label="Open new" className={triggerClassName}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label="Open new"
            disabled={create.isPending}
            className="cursor-pointer flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-default disabled:opacity-50"
          >
            <PlusIcon className="size-5" />
          </button>
        </DropdownMenuTrigger>
      </WorkspaceTabTooltip>
      {/* min-w-44 floors the content wide enough for the longest item label
          ("Reconnecting…" + spinner, and the sub-trigger's chevron) — the
          default min-w-32 tracks the 32px "+" trigger and clips it. */}
      <DropdownMenuContent
        align="start"
        className="min-w-44"
        // On close, Radix restores focus to the "+" trigger, which re-opens its
        // tooltip for a frame before blur dismisses it — a visible flash after a
        // shell launch. Suppress the focus restore to keep the tooltip closed.
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        {/* Hide the native browser view while this menu is open so it doesn't
            paint over the dropdown (#3980). Only this rail menu needs it. */}
        <SuppressBrowserView />
        <DropdownMenuLabel>Open new</DropdownMenuLabel>
        {multipleShells ? (
          <DropdownMenuSub>
            {/* Clicking "Shell" launches the remembered default immediately —
                the type selection is optional. Hover/right-arrow still opens the
                submenu to pick a specific type. onClick fires the default and
                lets the menu close on its own; preventDefault stops the click
                from only toggling the submenu open. */}
            <DropdownMenuSubTrigger
              disabled={shellDisabled}
              onClick={(e) => {
                e.preventDefault();
                launchShell(defaultShell);
              }}
            >
              {shellItemContent}
            </DropdownMenuSubTrigger>
            <DropdownMenuSubContent>
              {declaredTerminals.map((name) => (
                <DropdownMenuItem
                  key={name}
                  onSelect={() => pickShell(name)}
                  disabled={shellDisabled}
                >
                  <CheckIcon
                    className={cn("size-4", name === defaultShell ? "opacity-100" : "opacity-0")}
                  />
                  {name}
                </DropdownMenuItem>
              ))}
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        ) : (
          <DropdownMenuItem
            onSelect={() => launchShell(declaredTerminals[0])}
            disabled={shellDisabled}
          >
            {shellItemContent}
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// ---------------------------------------------------------------------------
// FileTabsStrip — open file tabs rendered in the top rail tab strip, as peers
// of the fixed Files/Terminals/Agents tabs. Each tab is a cell with the
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
              // Match the fixed TabsTrigger pill's box metrics (h-24 / px-8 /
              // rounded-8 / 13px medium) so file tabs and Files/Terminals tabs
              // are the same height and the active chip lines up across both
              // sets. `group/tab` drives the hover-revealed close overlay below.
              // `overflow-hidden` clips the hover-close gradient overlay to the
              // pill's rounded corners so its rectangular edges can't poke out.
              "group/tab relative flex h-[24px] min-w-0 max-w-[320px] shrink-0 cursor-pointer items-center justify-center gap-[6px] overflow-hidden rounded-md px-2 text-ui font-medium leading-5 transition-colors",
              active
                ? "bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] text-foreground"
                : "text-muted-foreground hover:bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] hover:text-foreground",
            )}
          >
            <FileIcon className="size-4 shrink-0" />
            <span className="min-w-0 truncate">{name}</span>
            {/* Close button: hidden until hover, then revealed over a gradient
                that fades the truncated filename into the tab's background so
                the "x" never collides with the text. The overlay only shows on
                hover, where both active and inactive tabs share the same OPAQUE
                selection surface — fade to that exact color. (A translucent
                fade like var(--muted) would stack over the hover background and
                darken the right edge into a visible gradient patch.) */}
            <span className="absolute inset-y-0 right-0 flex items-center pl-[12px] pr-[4px] opacity-0 transition-opacity group-hover/tab:opacity-100 [background:linear-gradient(to_right,transparent,color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))_40%)]">
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
  closingKey,
  canClose,
  labelFor,
  onSelect,
  onClose,
}: {
  /** Ordered list of open terminal tab keys. */
  openTerminals: string[];
  /** Currently active terminal key, or null when another tab is active. */
  activeTerminalKey: string | null;
  /** Tab key whose close (kill) is in flight — greyed + non-interactive. */
  closingKey: string | null;
  /** Whether the viewer may close (kill) a shell. Closing is server-gated on
   *  edit access, so a read-only viewer gets no close affordance. */
  canClose: boolean;
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
        const closing = key === closingKey;
        return (
          <div
            key={key}
            ref={active ? activeTabRef : undefined}
            role="button"
            tabIndex={closing ? -1 : 0}
            aria-current={active}
            aria-busy={closing}
            title={name}
            onClick={() => !closing && onSelect(key)}
            onAuxClick={(e) => {
              if (e.button === 1 && !closing && canClose) {
                e.preventDefault();
                onClose(key);
              }
            }}
            onKeyDown={(e) => {
              if (!closing && (e.key === "Enter" || e.key === " ")) {
                e.preventDefault();
                onSelect(key);
              }
            }}
            className={cn(
              // Match FileTabsStrip's pill metrics (h-24 / px-8 / rounded-md)
              // so shell and file tabs line up in the same strip.
              "group/tab relative flex h-[24px] min-w-0 max-w-[320px] shrink-0 cursor-pointer items-center justify-center gap-[6px] overflow-hidden rounded-md px-2 text-ui font-medium leading-5 transition-colors",
              active
                ? "bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] text-foreground"
                : "text-muted-foreground hover:bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] hover:text-foreground",
              // Closing: kill is in flight — dim and freeze the tab until it goes.
              closing && "pointer-events-none opacity-50",
            )}
          >
            <TerminalIcon className="size-4 shrink-0" />
            <span className="min-w-0 truncate text-sm">{name}</span>
            {canClose && (
              <span className="absolute inset-y-0 right-0 flex items-center pl-[12px] pr-[4px] opacity-0 transition-opacity group-hover/tab:opacity-100 [background:linear-gradient(to_right,transparent,color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))_40%)]">
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
            )}
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
      <div className="flex flex-1 items-center justify-center text-muted-foreground text-ui">
        Shell not available.
      </div>
    );
  }
  return (
    <div key={terminal.id} className="flex h-full min-h-0 flex-col">
      <TerminalView
        sessionId={conversationId}
        terminalId={terminal.id}
        readOnly={readOnly}
        directAttachUrl={terminal.directAttachUrl}
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
  /** Whether the Files/Changes tabs are available (agent spec exposes an os_env). */
  showFilesPanel: boolean;
  /** Whether the Browser tab is available — Electron shell only (hidden in a
   *  plain web build, which has no embedded WebContentsView). */
  showBrowserTab: boolean;
  /** Count of changed files, shown as the Changes tab badge. */
  changedCount: number;
  /** How many child agents are actively working (Agents tab badge). */
  subagentsWorking: number;
  /**
   * Total agents in the session tree, main agent included (Agents tab
   * badge denominator) — starts at 1 for a lone agent.
   */
  agentCount: number;
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
  /** Tab key whose close (terminal kill) is in flight — rendered greyed and
   *  non-interactive until it disappears. Null when no close is pending. */
  closingTerminalKey?: string | null;
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
  /** Whether the Files panel shows dotfiles/hidden entries. */
  filesPanelShowHidden: boolean;
  /** Toggle hidden-file visibility in the Files panel. */
  onShowHiddenChange: (show: boolean) => void;
  /** Open session's derived liveness — drives the "+ New shell" menu's
   *  connect affordance (Reconnecting… / Offline). Absent is treated as
   *  ready. */
  liveness?: SessionLiveness;
  /** Called when a shell create is initiated from the "+" menu, so the new
   *  shell is focused as soon as its tab lands (not only on the create POST). */
  onShellCreateStart?: () => void;
  /** Called when the shell create POST fails, so the focus snapshot armed by
   *  ``onShellCreateStart`` is disarmed and can't grab an unrelated shell. */
  onShellCreateFailed?: () => void;
}

/**
 * WorkspacePanel — the desktop right "Workspace" rail, rendered as a
 * floating card (bg-card, rounded, bordered, shadowed) sitting below the
 * full-width chat header band. Internally tabbed between Files, Changes,
 * Terminals and Agents so each can claim the full rail height
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
  subagentsWorking,
  agentCount,
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
  closingTerminalKey,
  onCloseTerminal,
  maximized,
  onToggleMaximized,
  permissionLevel,
  filesPanelSort,
  onSortChange,
  filesPanelShowHidden,
  onShowHiddenChange,
  liveness,
  onShellCreateStart,
  onShellCreateFailed,
}: WorkspacePanelProps) {
  // Memoized so FileViewer's Escape-to-close effect doesn't re-subscribe its
  // window keydown listener on every render — an inline arrow would change
  // identity each render and thrash the effect's add/remove cycle.
  const handleCloseTab = useCallback(() => {
    if (selectedFilePath !== null) onCloseFile(selectedFilePath);
  }, [onCloseFile, selectedFilePath]);
  // Resolve shell tab keys to display labels. The terminals list is already
  // fetched elsewhere for the session, so this shares the same query cache.
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
      // The resize hook can starve the rail to width 0 while it stays mounted;
      // marking it collapsed keeps index.css's safe-area padding off it so a
      // zero-width rail can't paint a ghost bg-card strip on native shells.
      data-collapsed={width === 0 || undefined}
      // Full-height desktop surface flush to the window edge, separated from
      // the main content by a left divider — no outer margin, rounding, or
      // shadow (mirrors the left sidebar). AppShell reserves the panel width
      // from ChatHeader, so the pane extends to the top without sitting under
      // the existing session action cluster.
      // ``@container/rail`` makes the rail a named container-query context so
      // the tab strip can switch scroll behavior on the rail's own width
      // (see the strip below) without a JS width listener.
      //
      // Maximized: break out of the flex row and stretch across the content
      // region (absolute inset-0) so the rail owns the full width, keeping the
      // same flush/bordered styling — only the width changes. The resize
      // handle is suppressed in that state — there's no neighbor to resize
      // against.
      data-maximized={maximized || undefined}
      className={cn(
        "@container/rail relative z-40 hidden md:flex md:min-h-0 md:flex-col md:overflow-hidden md:border-l md:border-border md:bg-card",
        maximized ? "md:absolute md:inset-0" : "md:shrink-0",
      )}
      // Width is fixed by the resize handle normally; maximized ignores it and
      // stretches to the absolute inset instead. The width doubles as the
      // reservation index.css caps the rail's lateral safe-area insets to.
      style={
        maximized
          ? undefined
          : ({ width, "--omnigent-reserved-width": `${width}px` } as CSSProperties)
      }
    >
      {/* Left-edge horizontal resize handle — suppressed while maximized. */}
      {!maximized && (
        <div
          {...handleProps}
          className="absolute inset-y-0 left-0 z-10 w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 transition-colors"
        />
      )}
      {/* Tab strip, in display order Files · Changes · Agents.
          Files (full folder tree) and Changes (changed-files-only list) are
          two peer tabs — same gate (an on-disk workspace), same FilesPanel,
          each pinned to one scope. Agents is always present (the Agents panel
          lists at least the main agent). Shells have no nav tab — they open as
          closable soft tabs (see the "+" NewTabMenu / TerminalTabsStrip below).
          The Agents tab keys off ``rootSessionId``, so inside a child
          it lists the siblings + a "main" link back to the parent. */}
      {/* Tab strip: the static nav tabs + divider stay pinned on the left at
          every rail width, and ONLY the file-tabs region scrolls (it owns the
          horizontal scroller — see below). The outer row never scrolls
          (overflow-x-hidden), so the divider is a fixed boundary that doesn't
          drift when the tabs scroll. */}
      <div className="workspace-tab-strip shrink-0 flex items-center overflow-x-hidden border-b border-border px-2 py-3">
        <Tabs
          // Static group — never compresses (shrink-0) and stays anchored on
          // the LEFT whether or not tabs are open. The open tabs render to its
          // right; the maximize button owns the row's single ml-auto and pins
          // to the right edge.
          className="shrink-0"
          // When a file or shell tab is active no fixed trigger should
          // highlight, so feed the radix group a sentinel that matches none of
          // them. The active file/shell tab carries its own highlight. Gate the
          // shell case on the terminal actually being present (same gate as the
          // content slot below): a sticky selection whose terminal is gone shows
          // the fallback nav view, so its nav tab must highlight, not "__tab__".
          value={
            selectedFilePath !== null ||
            (selectedTerminalKey !== null && openTerminals.includes(selectedTerminalKey))
              ? "__tab__"
              : rightRailTab
          }
          onValueChange={(v) => onRightRailTabChange(v as RightRailTab)}
          componentId="chat.right_rail.tabs"
        >
          <TabsList variant="pill" className="gap-1">
            {showFilesPanel && (
              <WorkspaceTabTooltip label="Files">
                <TabsTrigger
                  value="files"
                  aria-label="Files"
                  className="size-6 shrink-0 p-0 hover:border-1 hover:border-muted rounded-md!"
                >
                  <FolderTreeIcon />
                  <span className="sr-only">Files</span>
                </TabsTrigger>
              </WorkspaceTabTooltip>
            )}
            {showFilesPanel && (
              <WorkspaceTabTooltip label="Changes">
                <TabsTrigger
                  value="changes"
                  aria-label={changedCount > 0 ? `Changes ${changedCount} changed` : "Changes"}
                  className="size-6 shrink-0 p-0 hover:border-1 hover:border-muted rounded-md!"
                >
                  <FileDiffIcon />
                  <span className="sr-only">Changes</span>
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
                className="size-6 shrink-0 p-0 hover:border-1 hover:border-muted rounded-md!"
              >
                <BotIcon />
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
            {showBrowserTab && (
              <WorkspaceTabTooltip label="Browser">
                <TabsTrigger
                  value="browser"
                  aria-label="Browser"
                  className="size-6 shrink-0 p-0 hover:border-1 hover:border-muted rounded-md!"
                >
                  <GlobeIcon />
                  <span className="sr-only">Browser</span>
                </TabsTrigger>
              </WorkspaceTabTooltip>
            )}
          </TabsList>
        </Tabs>
        {/* 1px divider separating the static nav tabs from the open tabs.
                Pinned (outside the scrolling file-tabs region), so it stays put
                at every rail width while the tabs scroll past it. */}
        <div aria-hidden className="mx-[8px] h-[14px] w-px shrink-0 self-center bg-border-strong" />
        {(openFiles.length > 0 || openTerminals.length > 0) && (
          <>
            {/* Open-tabs region (file tabs + shell tabs) — the horizontal
                scroller. It sizes to its content and shrinks+scrolls only when
                the tabs would overflow (min-w-0, no flex-1), so the "+" outside
                it hugs the last tab when they fit and stays pinned when they
                don't. overflow-y-hidden stops overflow-x:auto from spawning a
                vertical scrollbar that eats horizontal space. */}
            <div className="flex min-w-0 items-center gap-0.5 overflow-x-auto overflow-y-hidden [scrollbar-width:thin] [&::-webkit-scrollbar]:h-1 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [&::-webkit-scrollbar-track]:bg-transparent">
              <FileTabsStrip
                openFiles={openFiles}
                activeFilePath={selectedFilePath}
                onFileSelect={openFileViewer}
                onCloseFile={onCloseFile}
              />
              <TerminalTabsStrip
                openTerminals={openTerminals}
                activeTerminalKey={selectedTerminalKey}
                closingKey={closingTerminalKey ?? null}
                canClose={isEditorLevel(permissionLevel)}
                labelFor={terminalLabelFor}
                onSelect={openTerminalTab}
                onClose={onCloseTerminal}
              />
            </div>
            {/* "+" trails the last tab but sits OUTSIDE the scroller, so it
                stays pinned (never scrolls under / overlaps the tabs) when they
                overflow, and hugs the last tab when they fit. ml-[2px] keeps the
                same gap the scroller's gap-0.5 gives between tabs. */}
            <NewTabMenu
              conversationId={conversationId}
              onCreateError={onShellCreateFailed}
              onOpenTerminal={openTerminalTab}
              onCreateStart={onShellCreateStart}
              triggerClassName="ml-[2px]"
              liveness={liveness}
            />
          </>
        )}
        {/* "+" — open a new Shell tab. With no open tabs it sits here, right
            after the nav tabs (next to Shells); once tabs exist it moves into
            the open-tabs region to trail the last tab (see above). Self-gates
            to nothing when the agent has no terminal access. */}
        {openFiles.length === 0 && openTerminals.length === 0 && (
          <NewTabMenu
            conversationId={conversationId}
            onOpenTerminal={openTerminalTab}
            onCreateStart={onShellCreateStart}
            onCreateError={onShellCreateFailed}
            liveness={liveness}
          />
        )}
        {/* Maximize/minimize toggle, pinned to the rightmost edge via ml-auto,
            which absorbs the free space before it. When open tabs exist their
            ≥500px flex-1 region absorbs the space instead, so the button still
            hugs the right. */}
        <WorkspaceTabTooltip
          label={maximized ? "Exit full screen" : "Full screen"}
          className="ml-auto"
        >
          <Button
            // type="button"
            variant="ghost"
            aria-label={maximized ? "Exit full screen" : "Full screen"}
            aria-pressed={maximized}
            onClick={onToggleMaximized}
            size="icon-xs"
            className="flex size-6"
          >
            {maximized ? <MinimizeIcon className="size-4" /> : <MaximizeIcon className="size-4" />}
          </Button>
        </WorkspaceTabTooltip>
      </div>
      {/* Tab content — single slot. An open shell tab holds its xterm; a
          file tab holds FileViewer; the Files/Changes tabs show FilesPanel
          (tree vs changed-only list); Subagents lists the root's children +
          a "main" link back to the parent. */}
      <div data-workspace-panel-content className="flex min-h-0 flex-1 flex-col overflow-hidden">
        {selectedTerminalKey !== null && openTerminals.includes(selectedTerminalKey) ? (
          // Show the selected shell's xterm only while its terminal is actually
          // present. The selection is sticky (AppShell never prunes it off the
          // list), so during a transient terminals-list churn this falls back to
          // the default view and the xterm reappears when the terminal returns.
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
        ) : (
          showFilesPanel && (
            <FilesPanel
              frameless
              onFileSelect={openFileViewer}
              flatView={rightRailTab === "changes"}
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
