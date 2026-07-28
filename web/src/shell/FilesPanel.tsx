import {
  ArrowDownAZIcon,
  ArrowDownWideNarrowIcon,
  CheckIcon,
  ChevronRightIcon,
  EyeIcon,
  EyeOffIcon,
  FileClockIcon,
  FileTypeIcon,
  MoonIcon,
  PencilIcon,
  SearchIcon,
  SlidersHorizontalIcon,
  XIcon,
} from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useParams } from "@/lib/routing";
import { useSession } from "@/hooks/useSession";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { useSessionHostOnline, useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useChatStore } from "@/store/chatStore";
import {
  PathUnreachableError,
  joinBrowseLocation,
  relativizeToWorkspace,
  useWorkspaceAllFiles,
  useWorkspaceEnvironment,
  useWorkspaceFileSearch,
  useAllWorkspaceChangedFiles,
  useRenameWorkspaceEnvironment,
  type WorkspaceChangedFile,
  type WorkspaceEnvironment,
} from "@/hooks/useWorkspaceChangedFiles";
import { cn } from "@/lib/utils";
import { DEFAULT_WORKSPACE_ENVIRONMENT_ID } from "@/lib/workspaceFiles";
import { BrowseLocationBar } from "./BrowseLocationBar";
import { CopyPathButton } from "./CopyPathButton";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { type ChangedSort, FlatFileList } from "./FlatFileList";
import { FolderTree } from "./FolderTree";
import { useScrollRestore } from "./useScrollRestore";

interface FilesPanelProps {
  onFileSelect: (path: string, environmentId?: string) => void;
  /**
   * Which scope this panel renders: false = full folder tree, true =
   * changed-files-only flat list. Fixed by the caller (the Files vs Changes
   * rail tab / mobile drawer) rather than switched inside the panel.
   */
  flatView: boolean;
  /**
   * Whether hidden files (dot-prefixed paths) are visible. Lifted to
   * the parent so the state survives inline→drawer transitions.
   */
  showHidden: boolean;
  onShowHiddenChange: (showHidden: boolean) => void;
  /**
   * Lifted changed-files sort order. Lifted to AppShell so it survives
   * inline→drawer transitions and stays in sync with the FileViewer's
   * prev/next navigation order.
   */
  sort: ChangedSort;
  onSortChange: (sort: ChangedSort) => void;
  /**
   * When provided, the panel renders an X close button in the header and
   * fills its parent's height — dropping the rounded card chrome so it can
   * serve as the entire content of a full-screen drawer.
   */
  onClose?: () => void;
  /**
   * Frameless mode: drops the rounded card chrome and fills the parent
   * container's height (like the `onClose` drawer) — but without a close
   * button. Used by the inline right panel where the panel is embedded in a
   * split layout rather than a drawer.
   */
  frameless?: boolean;
}

// ---------------------------------------------------------------------------
// HiddenFilesToggle
// ---------------------------------------------------------------------------

function HiddenFilesToggle({
  showHidden,
  onToggle,
  size,
  hiddenCount,
}: {
  showHidden: boolean;
  onToggle: () => void;
  size: "4" | "3.5";
  hiddenCount: number;
}) {
  const hasHidden = hiddenCount > 0 && !showHidden;
  const ariaLabel = showHidden ? "Hide hidden files" : "Show hidden files";
  const tooltipLabel = showHidden
    ? "Hide hidden files"
    : hasHidden
      ? `${hiddenCount} file${hiddenCount === 1 ? "" : "s"} in hidden directories. Click to show.`
      : "Show hidden files";
  const iconSize = size === "4" ? "size-4" : "size-3.5";
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            aria-label={ariaLabel}
            className={cn(
              "cursor-pointer rounded p-1 hover:bg-muted",
              hasHidden
                ? "text-warning hover:text-warning/80"
                : "text-muted-foreground hover:text-foreground",
            )}
            onClick={onToggle}
          >
            {/* The icon shows the current state, not the action: a plain eye
                means hidden files are visible, a slashed eye means they are not. */}
            {showHidden ? <EyeIcon className={iconSize} /> : <EyeOffIcon className={iconSize} />}
          </button>
        </TooltipTrigger>
        <TooltipContent side="bottom">{tooltipLabel}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

// ---------------------------------------------------------------------------
// SortSelector
// ---------------------------------------------------------------------------

const SORT_OPTIONS: { value: ChangedSort; label: string; Icon: typeof ArrowDownAZIcon }[] = [
  { value: "alpha", label: "Filename", Icon: ArrowDownAZIcon },
  { value: "recent", label: "Last edited", Icon: FileClockIcon },
  { value: "size", label: "Size", Icon: ArrowDownWideNarrowIcon },
  { value: "type", label: "Type", Icon: FileTypeIcon },
];

function SortSelector({
  sort,
  onChange,
}: {
  sort: ChangedSort;
  onChange: (next: ChangedSort) => void;
}) {
  const active = SORT_OPTIONS.find((o) => o.value === sort) ?? SORT_OPTIONS[0];
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`Sort: ${active.label}`}
          className="flex shrink-0 cursor-pointer items-center gap-1 rounded-full px-2.5 py-[4px] text-muted-foreground text-sm hover:bg-muted hover:text-foreground"
        >
          <span>Sort:</span>
          <active.Icon className="size-3.5" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-40">
        <DropdownMenuRadioGroup value={sort} onValueChange={(v) => onChange(v as ChangedSort)}>
          {SORT_OPTIONS.map(({ value, label, Icon }) => (
            <DropdownMenuRadioItem key={value} value={value}>
              <Icon className="size-3.5" />
              {label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// ---------------------------------------------------------------------------
// SearchFilterInput — labeled glob input for "files to include" / "exclude"
// ---------------------------------------------------------------------------

function SearchFilterInput({
  label,
  placeholder,
  value,
  onChange,
}: {
  label: string;
  placeholder: string;
  value: string;
  onChange: (next: string) => void;
}) {
  return (
    <label className="flex flex-col gap-0.5">
      <span className="font-medium text-[10px] text-muted-foreground uppercase tracking-wide">
        {label}
      </span>
      <input
        aria-label={label}
        className="w-full rounded border border-border bg-transparent px-2 py-1 font-mono text-sm outline-none placeholder:text-muted-foreground focus:border-ring"
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        type="text"
        value={value}
      />
    </label>
  );
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

/**
 * Right-side Files card. Always visible on desktop.
 *
 * - Flat view: changed files only (registry-backed, any depth).
 * - Tree view: all on-disk files in the workspace root, expandable folders.
 *
 * Uploaded/attached files are rendered inline in the message thread and are
 * intentionally not listed here.
 */
export function FilesPanel({
  onFileSelect,
  flatView,
  showHidden,
  onShowHiddenChange,
  sort: changedSort,
  onSortChange,
  onClose,
  frameless,
}: FilesPanelProps) {
  const { conversationId } = useParams<{ conversationId: string }>();
  // The runner went offline (e.g. its host restarted): `sessionStatus`
  // is "failed", set by `_on_runner_disconnect` server-side when the
  // runner's tunnel drops (and also client-side in chatStore when the
  // SSE stream itself dies). Either way the session can't be reached and
  // a message reconnects it. A brand-new session whose runner just hasn't
  // started is never "failed", so this distinguishes "asleep, send a
  // message to reconnect" from a fresh session that should show the
  // normal empty state — a real liveness signal, not an inference from
  // chat history.
  const runnerWentOffline = useChatStore(
    (s) => s.conversationId === conversationId && s.sessionStatus === "failed",
  );
  // The runner is offline but the host still holds the workspace on disk,
  // so the server serves the panel by reading the workspace over the host
  // tunnel. Show a passive "served from host" badge — the panel keeps
  // working and no message/agent wake-up is triggered. Only when the host
  // is also down (or the session isn't host-bound) do the file queries
  // surface RunnerOfflineError and fall back to the reconnect hint.
  const runnerOnline = useSessionRunnerOnline(conversationId);
  const hostOnline = useSessionHostOnline(conversationId);
  const servedFromHost = runnerOnline === false && hostOnline === true;
  const [changedSearch, setChangedSearch] = useState("");
  const [treeSearch, setTreeSearch] = useState("");
  const [debouncedTreeSearch, setDebouncedTreeSearch] = useState("");
  // "files to include" / "files to exclude" glob filters (VSCode-style),
  // revealed by the filters toggle in the Explore search bar.
  const [treeInclude, setTreeInclude] = useState("");
  const [debouncedTreeInclude, setDebouncedTreeInclude] = useState("");
  const [treeExclude, setTreeExclude] = useState("");
  const [debouncedTreeExclude, setDebouncedTreeExclude] = useState("");
  const [showSearchFilters, setShowSearchFilters] = useState(false);
  const [collapsedEnvironmentIds, setCollapsedEnvironmentIds] = useState<Set<string>>(
    () => new Set(),
  );
  // The drawer (onClose) adds an X close button to the header. Both the drawer
  // and the inline rail (frameless) fill their parent's height and drop the
  // rounded card chrome; only the standalone card caps content at max-h.
  const isDrawer = onClose !== undefined;
  const fillHeight = isDrawer || frameless === true;
  const changedQuery = useAllWorkspaceChangedFiles(conversationId, {
    enabled: true,
  });
  const envQuery = useWorkspaceEnvironment(conversationId, {
    enabled: true,
  });
  const workspaceRoot = envQuery.data?.root ?? null;
  // The picker browses the host's filesystem, the same source the new-session
  // workspace chip uses.
  const { session } = useSession(conversationId);
  // Absolute path currently browsed. Null tracks the workspace root, so the
  // panel keeps opening there and a session switch never strands the user in
  // a directory belonging to the session they just left.
  const [browseLocation, setBrowseLocation] = useState<string | null>(null);
  const [browseError, setBrowseError] = useState<string | null>(null);
  const [locationQueryError, setLocationQueryError] = useState<string | null>(null);
  useEffect(() => {
    setBrowseLocation(null);
    setBrowseError(null);
    setLocationQueryError(null);
  }, [conversationId]);
  const workingDir = browseLocation ?? workspaceRoot;
  // The wire form: "" means the workspace root (the historical relative
  // contract). A location INSIDE the workspace is sent relative to it, and
  // only a genuinely outside one is sent absolute. That distinction is not
  // cosmetic: the server gates absolute paths at owner level, so sending an
  // absolute path for a subfolder would 403 every collaborator browsing the
  // workspace they can already read.
  const locationParam = relativizeToWorkspace(browseLocation, workspaceRoot);

  function navigateTo(absolutePath: string) {
    setBrowseError(null);
    setLocationQueryError(null);
    setBrowseLocation(absolutePath === workspaceRoot ? null : absolutePath);
  }

  /** Re-root the primary directory onto a child of the current tree. */
  function navigateToChild(relativePath: string) {
    if (!workingDir) return;
    navigateTo(`${workingDir.replace(/\/$/, "")}/${relativePath}`);
  }

  const renameEnvironment = useRenameWorkspaceEnvironment(conversationId);
  const environments = changedQuery.environments ?? [];
  const locationError = browseError ?? locationQueryError;
  const changedFiles = changedQuery.data?.data ?? [];
  const hiddenFilesCount = changedFiles.filter((f) =>
    f.path.split("/").some((seg) => seg.startsWith(".")),
  ).length;

  useEffect(() => {
    if (!flatView) setChangedSearch("");
    if (flatView) {
      setTreeSearch("");
      setDebouncedTreeSearch("");
      setTreeInclude("");
      setDebouncedTreeInclude("");
      setTreeExclude("");
      setDebouncedTreeExclude("");
    }
  }, [flatView]);

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedTreeSearch(treeSearch);
      setDebouncedTreeInclude(treeInclude);
      setDebouncedTreeExclude(treeExclude);
    }, 300);
    return () => clearTimeout(timer);
  }, [treeSearch, treeInclude, treeExclude]);

  useEffect(() => {
    setCollapsedEnvironmentIds(new Set());
  }, [conversationId]);

  function toggleEnvironment(environmentId: string) {
    setCollapsedEnvironmentIds((current) => {
      const next = new Set(current);
      if (next.has(environmentId)) next.delete(environmentId);
      else next.add(environmentId);
      return next;
    });
  }
  // Highlight the filters toggle when include/exclude carry a value.
  const treeFiltersActive = treeInclude.trim().length > 0 || treeExclude.trim().length > 0;

  // Persist/restore the list's scroll position across conversation and view
  // switches. Keyed per conversation + view (Changed vs All) since the two
  // lists have independent heights. Readiness is data presence rather than
  // `isLoading` — the files queries are disabled (not loading) until the
  // environment query resolves.
  const scrollRef = useRef<HTMLElement>(null);
  const scrollKey = conversationId
    ? `files:${conversationId}:${flatView ? "changed" : "all"}`
    : null;
  // The aggregate environment query is the shared readiness gate for both
  // views; each root owns its own lazy all-files query in `RootFolderTree`.
  const dataReady = changedQuery.data !== undefined;
  const handleScroll = useScrollRestore(scrollRef, scrollKey, dataReady);

  return (
    <div
      className={cn(
        "@container/filespanel overflow-hidden bg-card",
        fillHeight ? "flex h-full min-h-0 flex-col" : "flex min-h-0 flex-col",
      )}
    >
      {/* Header — single row: [title · root count] [eye] [close?] */}
      <div className="flex shrink-0 items-center gap-2 px-3 py-2">
        <span className="shrink-0 font-medium text-sm">Project folders</span>
        {environments.length > 0 && (
          <span className="shrink-0 text-[11px] text-muted-foreground">
            {environments.length} root{environments.length === 1 ? "" : "s"}
          </span>
        )}
        {workingDir && workspaceRoot && (
          <BrowseLocationBar
            current={workingDir}
            workspace={workspaceRoot}
            hostId={session?.hostId ?? null}
            canBrowseOutside={isOwnerLevel(session?.permissionLevel ?? null)}
            reach={envQuery.data?.reachable ?? null}
            onNavigate={navigateTo}
            error={locationError}
          />
        )}
        {servedFromHost && (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  data-testid="files-host-served-badge"
                  className="flex shrink-0 items-center gap-1 rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground"
                >
                  <MoonIcon className="size-3 shrink-0" />
                  Asleep
                </span>
              </TooltipTrigger>
              <TooltipContent>
                Agent is asleep — files shown live from the host. Send a message to wake it.
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
        <div className="ml-auto flex items-center gap-1">
          {workingDir && (
            // Its own provider: the header has no TooltipProvider ancestor
            // (each control here brings one), unlike the file rows.
            <TooltipProvider>
              <CopyPathButton path={workingDir} label="Copy folder path" />
            </TooltipProvider>
          )}
          <HiddenFilesToggle
            showHidden={showHidden}
            onToggle={() => onShowHiddenChange(!showHidden)}
            size={isDrawer ? "4" : "3.5"}
            hiddenCount={hiddenFilesCount}
          />
          {onClose && (
            <button
              type="button"
              aria-label="Close files"
              className="cursor-pointer rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
              onClick={onClose}
            >
              <XIcon className="size-4" />
            </button>
          )}
        </div>
      </div>
      {/* Content */}
      <div className="shrink-0 border-t border-border" />
      {/* Search toolbar — the Changed | All scope switch leads, then the
              search field, then the per-view trailing control (Sort for the
              changed list, glob filters for the tree). Lives outside the
              scroll container so negative margins aren't clipped. */}
      {flatView && (
        <div
          className="shrink-0 flex items-center gap-2 px-2 py-1.5 @max-[400px]/filespanel:flex-col @max-[400px]/filespanel:items-stretch"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex min-w-0 flex-1 items-center gap-2">
            <div className="flex min-w-0 flex-1 items-center gap-[6px] rounded-full border border-border px-[10px] py-[4px] transition-colors focus-within:border-border-strong">
              <SearchIcon className="size-4 shrink-0 text-muted-foreground" />
              <input
                aria-label="Search changed files"
                className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
                onChange={(event) => setChangedSearch(event.target.value)}
                placeholder="Search"
                type="search"
                value={changedSearch}
              />
            </div>
            <SortSelector sort={changedSort} onChange={onSortChange} />
          </div>
        </div>
      )}
      {!flatView && (
        <div className="shrink-0" onClick={(e) => e.stopPropagation()}>
          <div className="flex items-center gap-2 px-2 py-1.5 @max-[400px]/filespanel:flex-col @max-[400px]/filespanel:items-stretch">
            <div className="flex min-w-0 flex-1 items-center gap-2">
              <div className="flex min-w-0 flex-1 items-center gap-[6px] rounded-full border border-border px-[10px] py-[4px] transition-colors focus-within:border-border-strong">
                <SearchIcon className="size-4 shrink-0 text-muted-foreground" />
                <input
                  aria-label="Search all files"
                  className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
                  onChange={(event) => setTreeSearch(event.target.value)}
                  placeholder="Search"
                  type="search"
                  value={treeSearch}
                />
              </div>
              <button
                type="button"
                aria-label={showSearchFilters ? "Hide search filters" : "Show search filters"}
                aria-expanded={showSearchFilters}
                title="Files to include / exclude"
                className={cn(
                  "flex shrink-0 cursor-pointer items-center gap-1 rounded-full px-2.5 py-[4px] hover:bg-muted",
                  showSearchFilters || treeFiltersActive
                    ? "text-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
                onClick={() => setShowSearchFilters((v) => !v)}
              >
                <SlidersHorizontalIcon className="size-3.5" />
                {treeFiltersActive && !showSearchFilters && (
                  <span className="size-1.5 rounded-full bg-primary" aria-hidden />
                )}
              </button>
              <SortSelector sort={changedSort} onChange={onSortChange} />
            </div>
          </div>
          {showSearchFilters && (
            <div className="flex flex-col gap-1.5 border-border border-t px-3 py-2">
              <SearchFilterInput
                label="files to include"
                placeholder="e.g. *.ts, src/**"
                value={treeInclude}
                onChange={setTreeInclude}
              />
              <SearchFilterInput
                label="files to exclude"
                placeholder="e.g. **/node_modules, *.test.ts"
                value={treeExclude}
                onChange={setTreeExclude}
              />
            </div>
          )}
        </div>
      )}
      <section
        ref={scrollRef}
        className={cn(
          "overflow-y-auto px-2 pb-2",
          flatView ? "pt-1" : "pt-2",
          fillHeight ? "min-h-0 flex-1" : "max-h-72",
        )}
        onScroll={handleScroll}
      >
        {flatView ? (
          <div className="flex flex-col gap-3">
            {(environments.length > 0 ? environments : [undefined]).map((environment) => {
              const files = environment
                ? changedQuery.data?.data.filter(
                    (file) =>
                      (file.environment_id ?? DEFAULT_WORKSPACE_ENVIRONMENT_ID) === environment.id,
                  )
                : changedQuery.data?.data;
              const collapsed = environment ? collapsedEnvironmentIds.has(environment.id) : false;
              return (
                <div key={environment?.id ?? "loading"}>
                  {environment && (
                    <DirectoryGroupHeader
                      environment={environment}
                      collapsed={collapsed}
                      onToggle={() => toggleEnvironment(environment.id)}
                      onRename={(name) =>
                        renameEnvironment.mutateAsync({ environmentId: environment.id, name })
                      }
                    />
                  )}
                  {!collapsed && (
                    <div id={environment ? environmentContentId(environment.id) : undefined}>
                      <FlatFileList
                        files={files}
                        isLoading={changedQuery.isLoading}
                        isError={changedQuery.isError}
                        error={changedQuery.error}
                        onFileSelect={onFileSelect}
                        showHidden={showHidden}
                        onShowHidden={() => onShowHiddenChange(true)}
                        searchQuery={changedSearch}
                        sort={changedSort}
                        conversationId={conversationId}
                        runnerWentOffline={runnerWentOffline}
                      />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {environments.map((environment) => (
              <RootFolderTree
                key={environment.id}
                conversationId={conversationId}
                environment={environment}
                changedFiles={changedFiles.filter(
                  (file) =>
                    (file.environment_id ?? DEFAULT_WORKSPACE_ENVIRONMENT_ID) === environment.id,
                )}
                onFileSelect={onFileSelect}
                showHidden={showHidden}
                onShowHidden={() => onShowHiddenChange(true)}
                sort={changedSort}
                runnerWentOffline={runnerWentOffline}
                searchQuery={debouncedTreeSearch}
                include={debouncedTreeInclude}
                exclude={debouncedTreeExclude}
                collapsed={collapsedEnvironmentIds.has(environment.id)}
                onToggle={() => toggleEnvironment(environment.id)}
                onRename={(name) =>
                  renameEnvironment.mutateAsync({ environmentId: environment.id, name })
                }
                browseLocation={
                  environment.id === DEFAULT_WORKSPACE_ENVIRONMENT_ID ? locationParam : ""
                }
                onNavigateDir={
                  environment.id === DEFAULT_WORKSPACE_ENVIRONMENT_ID ? navigateToChild : undefined
                }
                onLocationErrorChange={
                  environment.id === DEFAULT_WORKSPACE_ENVIRONMENT_ID
                    ? setLocationQueryError
                    : undefined
                }
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function environmentContentId(environmentId: string): string {
  return `files-environment-${environmentId}`;
}

function DirectoryGroupHeader({
  environment,
  collapsed,
  onToggle,
  onRename,
}: {
  environment: WorkspaceEnvironment;
  collapsed: boolean;
  onToggle: () => void;
  onRename: (name: string | null) => Promise<unknown>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(environment.name);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (!editing) setDraft(environment.name);
  }, [editing, environment.name]);

  function cancelEditing() {
    setEditing(false);
    setDraft(environment.name);
    setSaveError(null);
  }

  async function saveName(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = draft.trim() || null;
    setSaving(true);
    setSaveError(null);
    try {
      await onRename(name);
      setEditing(false);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "Could not rename folder");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="group mb-1 flex min-h-8 w-full min-w-0 items-center rounded-lg border border-border/80 bg-muted/25 px-1 py-1 shadow-sm transition-colors hover:bg-muted/40">
      {editing ? (
        <form className="flex min-w-0 flex-1 items-center gap-1" onSubmit={saveName}>
          <input
            autoFocus
            aria-label={`Name for ${environment.name} folder`}
            className="min-w-0 flex-1 rounded-md border border-border-strong bg-background px-2 py-0.5 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring/40"
            disabled={saving}
            maxLength={80}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") cancelEditing();
            }}
            title={saveError ?? undefined}
            value={draft}
          />
          <button
            type="submit"
            aria-label={`Save name for ${environment.name} folder`}
            className="cursor-pointer rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground disabled:cursor-default disabled:opacity-50"
            disabled={saving}
          >
            <CheckIcon className="size-3.5" />
          </button>
          <button
            type="button"
            aria-label={`Cancel renaming ${environment.name} folder`}
            className="cursor-pointer rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground disabled:cursor-default disabled:opacity-50"
            disabled={saving}
            onClick={cancelEditing}
          >
            <XIcon className="size-3.5" />
          </button>
        </form>
      ) : (
        <>
          <button
            type="button"
            aria-controls={environmentContentId(environment.id)}
            aria-expanded={!collapsed}
            aria-label={`${collapsed ? "Expand" : "Collapse"} ${environment.name} folder`}
            className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 rounded-md px-1 py-0.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring/40"
            onClick={onToggle}
          >
            <ChevronRightIcon
              className={cn(
                "size-3.5 shrink-0 text-muted-foreground transition-transform",
                !collapsed && "rotate-90",
              )}
            />
            <span className="min-w-0 truncate font-semibold text-foreground text-xs tracking-tight">
              {environment.name}
            </span>
            {environment.root && <WorkingDirLabel dir={environment.root} />}
          </button>
          <button
            type="button"
            aria-label={`Rename ${environment.name} folder`}
            className="cursor-pointer rounded p-1 text-muted-foreground opacity-60 transition-opacity hover:bg-muted hover:text-foreground hover:opacity-100 focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring/40"
            onClick={() => {
              setDraft(environment.name);
              setSaveError(null);
              setEditing(true);
            }}
          >
            <PencilIcon className="size-3.5" />
          </button>
        </>
      )}
    </div>
  );
}

function RootFolderTree({
  conversationId,
  environment,
  changedFiles,
  onFileSelect,
  showHidden,
  onShowHidden,
  sort,
  runnerWentOffline,
  searchQuery,
  include,
  exclude,
  collapsed,
  onToggle,
  onRename,
  browseLocation,
  onNavigateDir,
  onLocationErrorChange,
}: {
  conversationId: string | undefined;
  environment: WorkspaceEnvironment;
  changedFiles: WorkspaceChangedFile[];
  onFileSelect: (path: string, environmentId?: string) => void;
  showHidden: boolean;
  onShowHidden: () => void;
  sort: ChangedSort;
  runnerWentOffline: boolean;
  searchQuery: string;
  include: string;
  exclude: string;
  collapsed: boolean;
  onToggle: () => void;
  onRename: (name: string | null) => Promise<unknown>;
  browseLocation: string;
  onNavigateDir?: (relativePath: string) => void;
  onLocationErrorChange?: (error: string | null) => void;
}) {
  const filesQuery = useWorkspaceAllFiles(
    conversationId,
    { enabled: !collapsed, environmentId: environment.id },
    browseLocation,
  );
  const searchQueryResult = useWorkspaceFileSearch(
    conversationId,
    searchQuery,
    include,
    exclude,
    {
      enabled: !collapsed && searchQuery.trim().length > 0,
      environmentId: environment.id,
    },
    browseLocation,
  );
  useEffect(() => {
    if (!onLocationErrorChange) return;
    const unreachable = filesQuery.error instanceof PathUnreachableError ? filesQuery.error : null;
    onLocationErrorChange(
      unreachable
        ? unreachable.reachableRoots.length > 0
          ? `${unreachable.message}. Reachable: ${unreachable.reachableRoots.join(", ")}`
          : unreachable.message
        : null,
    );
  }, [filesQuery.error, onLocationErrorChange]);

  function openTreeFile(path: string) {
    const resolvedPath = browseLocation.startsWith("/")
      ? path
      : joinBrowseLocation(browseLocation, path);
    onFileSelect(resolvedPath, environment.id);
  }
  return (
    <div>
      <DirectoryGroupHeader
        environment={environment}
        collapsed={collapsed}
        onToggle={onToggle}
        onRename={onRename}
      />
      {!collapsed && (
        <div id={environmentContentId(environment.id)}>
          <FolderTree
            files={filesQuery.data?.data}
            isLoading={filesQuery.isLoading}
            isError={filesQuery.isError}
            error={filesQuery.error}
            onFileSelect={openTreeFile}
            conversationId={conversationId}
            showHidden={showHidden}
            onShowHidden={onShowHidden}
            changedFiles={changedFiles}
            sort={sort}
            runnerWentOffline={runnerWentOffline}
            searchQuery={searchQuery}
            searchResults={searchQueryResult.data}
            isSearching={searchQueryResult.isFetching}
            isSearchError={searchQueryResult.isError}
            searchError={searchQueryResult.error instanceof Error ? searchQueryResult.error : null}
            environmentId={environment.id}
            browseLocation={browseLocation}
            onNavigateDir={onNavigateDir}
          />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// WorkingDirLabel
// ---------------------------------------------------------------------------

function WorkingDirLabel({ dir }: { dir: string }) {
  // Outer span participates in the flex row as flex-1 for layout/truncation.
  // Inner span is the actual tooltip trigger so Radix anchors the popup to
  // the text's bounding rect (not the full flex-1 width).
  return (
    <span className="ml-auto flex min-w-0 flex-1 items-center justify-end overflow-hidden pl-2">
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="inline-block max-w-full cursor-default truncate font-mono font-normal text-[10px] text-muted-foreground">
              {dirBasename(dir)}
            </span>
          </TooltipTrigger>
          <TooltipContent side="bottom">{dir}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    </span>
  );
}

/** Return the last path segment, handling both POSIX (/) and Windows (\) separators. */
function dirBasename(path: string): string {
  return path.split(/[/\\]/).filter(Boolean).pop() ?? path;
}
