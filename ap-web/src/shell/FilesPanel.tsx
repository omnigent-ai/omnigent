import {
  ArrowDownAZIcon,
  ChevronDownIcon,
  EyeIcon,
  EyeOffIcon,
  FileClockIcon,
  FolderTreeIcon,
  ListIcon,
  SearchIcon,
  SlidersHorizontalIcon,
  XIcon,
} from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "@/lib/routing";
import { useChatStore } from "@/store/chatStore";
import {
  useWorkspaceChangedFiles,
  useWorkspaceAllFiles,
  useWorkspaceEnvironment,
  useWorkspaceFileSearch,
} from "@/hooks/useWorkspaceChangedFiles";
import { cn } from "@/lib/utils";
import { readFilesPanelPreferences, writeFilesPanelPreferences } from "@/lib/filesPanelPreferences";
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

interface FilesPanelProps {
  onFileSelect: (path: string) => void;
  flatView: boolean;
  onFlatViewChange: (flatView: boolean) => void;
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
   * When provided, the panel renders an X close button in the header
   * (replacing the expand toggle) and fills its parent's height —
   * dropping the rounded card chrome so it can serve as the entire
   * content of a full-screen drawer. The collapse chevron and the
   * expand toggle are hidden too: a full-screen drawer already owns
   * the viewport, so those affordances would be a no-op or actively
   * confusing.
   */
  onClose?: () => void;
  /**
   * Frameless mode: drops the rounded card chrome and fills the parent
   * container's height (like the `onClose` drawer) — but without a close
   * button. Used by the inline right panel where the panel is embedded in a
   * split layout rather than a drawer. Unlike the drawer, it keeps the
   * collapsible "Working folder" button header (with its chevron and
   * `aria-expanded`), so the header stays a focusable, toggleable control.
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
  const { t } = useTranslation("common");
  const hasHidden = hiddenCount > 0 && !showHidden;
  const ariaLabel = showHidden ? t("hideHiddenFiles") : t("showHiddenFiles");
  const tooltipLabel = showHidden
    ? t("hideHiddenFiles")
    : hasHidden
      ? t("filesInHiddenDirsClickToShow", { count: hiddenCount })
      : t("showHiddenFiles");
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
            {showHidden ? <EyeOffIcon className={iconSize} /> : <EyeIcon className={iconSize} />}
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

const SORT_OPTIONS: { value: ChangedSort; labelKey: string; Icon: typeof ArrowDownAZIcon }[] = [
  { value: "alpha", labelKey: "sortFilename", Icon: ArrowDownAZIcon },
  { value: "recent", labelKey: "sortLastEdited", Icon: FileClockIcon },
];

function SortSelector({
  sort,
  onChange,
}: {
  sort: ChangedSort;
  onChange: (next: ChangedSort) => void;
}) {
  const { t } = useTranslation("nav");
  const active = SORT_OPTIONS.find((o) => o.value === sort) ?? SORT_OPTIONS[0];
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={t("sortBy", { label: t(active.labelKey) })}
          className="flex shrink-0 cursor-pointer items-center gap-1 rounded-full px-2.5 py-[4px] text-muted-foreground text-xs hover:bg-muted hover:text-foreground"
        >
          <span>{t("sortLabel")}</span>
          <active.Icon className="size-3.5" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-40">
        <DropdownMenuRadioGroup value={sort} onValueChange={(v) => onChange(v as ChangedSort)}>
          {SORT_OPTIONS.map(({ value, labelKey, Icon }) => (
            <DropdownMenuRadioItem key={value} value={value}>
              <Icon className="size-3.5" />
              {t(labelKey)}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// ---------------------------------------------------------------------------
// FileScopeSwitch — segmented Changed | All control that flips the whole Files
// view between the changed-files-only flat list (Changed) and the full folder
// tree (All). One control replaces the old separate Files / Changes rail tabs.
// ---------------------------------------------------------------------------

// Leading cell in the search toolbar. Rounded-full pills (matching the rail
// tabs' pill chip) — the active scope fills with the same muted/card mix.
function FileScopeSwitch({
  flatView,
  onChange,
  count,
}: {
  flatView: boolean;
  onChange: (flatView: boolean) => void;
  count: number;
}) {
  const { t } = useTranslation("nav");
  const changedSelected = flatView;
  const allSelected = !flatView;
  const pill =
    "flex cursor-pointer items-center gap-[6px] rounded-full px-[14px] py-[2px] text-[13px] font-medium leading-5 transition-colors";
  const activePill =
    "bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] text-foreground";
  const idlePill = "text-muted-foreground hover:text-foreground";
  return (
    <div role="radiogroup" aria-label={t("fileScope")} className="flex shrink-0 items-center gap-1">
      <button
        type="button"
        role="radio"
        aria-checked={changedSelected}
        aria-label={t("scopeChanged")}
        title={t("showChangedFilesOnly")}
        onClick={() => onChange(true)}
        className={cn(pill, changedSelected ? activePill : idlePill)}
      >
        <ListIcon className="size-3.5 shrink-0" />
        {t("scopeChanged")}
        {count > 0 && (
          <span className="shrink-0 font-normal text-[11px] text-muted-foreground tabular-nums">
            {count}
          </span>
        )}
      </button>
      <button
        type="button"
        role="radio"
        aria-checked={allSelected}
        aria-label={t("scopeAll")}
        title={t("showFullFolderTree")}
        onClick={() => onChange(false)}
        className={cn(pill, allSelected ? activePill : idlePill)}
      >
        <FolderTreeIcon className="size-3.5 shrink-0" />
        {t("scopeAll")}
      </button>
    </div>
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
        className="w-full rounded border border-border bg-transparent px-2 py-1 font-mono text-xs outline-none placeholder:text-muted-foreground focus:border-ring"
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
 * Right-side Files card. Always visible on desktop; collapses its content via
 * the chevron in the header.
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
  onFlatViewChange,
  showHidden,
  onShowHiddenChange,
  sort: changedSort,
  onSortChange,
  onClose,
  frameless,
}: FilesPanelProps) {
  const { t } = useTranslation("nav");
  const { t: tc } = useTranslation("common");
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
  const [collapsed, setCollapsed] = useState(() => readFilesPanelPreferences().collapsed);
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
  // The drawer (onClose) owns the full viewport, so it gets a static,
  // always-open header with its own X close button — a collapse chevron there
  // would be a no-op. The inline rail (frameless) and the standalone card keep
  // the collapsible "Working folder" *button* header (accessible name +
  // aria-expanded), which is what lets it be focused/toggled and asserted on.
  const isDrawer = onClose !== undefined;
  // Both the drawer and the inline rail fill their parent's height and drop the
  // rounded card chrome; only the standalone card caps content at max-h.
  const fillHeight = isDrawer || frameless === true;
  // The drawer is always open; everywhere else the header chevron toggles it.
  const contentVisible = isDrawer || !collapsed;
  const changedQuery = useWorkspaceChangedFiles(conversationId, {
    enabled: contentVisible,
  });
  const allFilesQuery = useWorkspaceAllFiles(conversationId, {
    enabled: contentVisible && !flatView,
  });
  const envQuery = useWorkspaceEnvironment(conversationId, {
    enabled: contentVisible,
  });
  const workingDir = envQuery.data?.root ?? null;
  const changedCount = changedQuery.data?.data.length ?? 0;
  const hiddenFilesCount = (changedQuery.data?.data ?? []).filter((f) =>
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

  // Only fire search queries on the Explore tab. The include/exclude globs
  // narrow an active text query; globs alone do not search.
  const treeSearchQuery = useWorkspaceFileSearch(
    conversationId,
    debouncedTreeSearch,
    debouncedTreeInclude,
    debouncedTreeExclude,
    {
      enabled: contentVisible && !flatView && debouncedTreeSearch.trim().length > 0,
    },
  );
  // Highlight the filters toggle when include/exclude carry a value.
  const treeFiltersActive = treeInclude.trim().length > 0 || treeExclude.trim().length > 0;

  return (
    <div
      className={cn(
        "@container/filespanel overflow-hidden bg-card",
        fillHeight ? "flex h-full min-h-0 flex-col" : "flex min-h-0 flex-col",
      )}
    >
      {/* Header — single row: [title · workingDir] [eye] [chevron / close] */}
      <div className="flex shrink-0 items-center gap-2 px-3 py-2">
        {isDrawer ? (
          <>
            <span className="shrink-0 font-medium text-sm">{t("workingFolder")}</span>
            {workingDir && <WorkingDirLabel dir={workingDir} />}
            <div className="ml-auto flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
              <HiddenFilesToggle
                showHidden={showHidden}
                onToggle={() => onShowHiddenChange(!showHidden)}
                size="4"
                hiddenCount={hiddenFilesCount}
              />
              {onClose && (
                <button
                  type="button"
                  aria-label={t("closeFiles")}
                  className="cursor-pointer rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                  onClick={onClose}
                >
                  <XIcon className="size-4" />
                </button>
              )}
            </div>
          </>
        ) : (
          <>
            <button
              type="button"
              className="flex min-w-0 flex-1 cursor-pointer items-center gap-2 text-left"
              onClick={() =>
                setCollapsed((v) => {
                  const next = !v;
                  writeFilesPanelPreferences({ ...readFilesPanelPreferences(), collapsed: next });
                  return next;
                })
              }
              aria-expanded={!collapsed}
            >
              <span className="shrink-0 font-medium text-sm">{t("workingFolder")}</span>
              {workingDir && <WorkingDirLabel dir={workingDir} />}
              <ChevronDownIcon
                className={cn(
                  "ml-auto size-4 shrink-0 text-muted-foreground transition-transform duration-150",
                  collapsed && "-rotate-90",
                )}
              />
            </button>
            {!collapsed && (
              <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                <HiddenFilesToggle
                  showHidden={showHidden}
                  onToggle={() => onShowHiddenChange(!showHidden)}
                  size="3.5"
                  hiddenCount={hiddenFilesCount}
                />
              </div>
            )}
          </>
        )}
      </div>
      {/* Content */}
      {contentVisible && (
        <>
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
              <FileScopeSwitch
                flatView={flatView}
                onChange={onFlatViewChange}
                count={changedCount}
              />
              <div className="flex min-w-0 flex-1 items-center gap-2">
                <div className="flex min-w-0 flex-1 items-center gap-[6px] rounded-full border border-border px-[10px] py-[4px] transition-colors focus-within:border-border-strong">
                  <SearchIcon className="size-4 shrink-0 text-muted-foreground" />
                  <input
                    aria-label={t("searchChangedFiles")}
                    className="min-w-0 flex-1 bg-transparent text-xs outline-none placeholder:text-muted-foreground"
                    onChange={(event) => setChangedSearch(event.target.value)}
                    placeholder={tc("search")}
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
                <FileScopeSwitch
                  flatView={flatView}
                  onChange={onFlatViewChange}
                  count={changedCount}
                />
                <div className="flex min-w-0 flex-1 items-center gap-2">
                  <div className="flex min-w-0 flex-1 items-center gap-[6px] rounded-full border border-border px-[10px] py-[4px] transition-colors focus-within:border-border-strong">
                    <SearchIcon className="size-4 shrink-0 text-muted-foreground" />
                    <input
                      aria-label={t("searchAllFiles")}
                      className="min-w-0 flex-1 bg-transparent text-xs outline-none placeholder:text-muted-foreground"
                      onChange={(event) => setTreeSearch(event.target.value)}
                      placeholder={tc("search")}
                      type="search"
                      value={treeSearch}
                    />
                  </div>
                  <button
                    type="button"
                    aria-label={showSearchFilters ? t("hideSearchFilters") : t("showSearchFilters")}
                    aria-expanded={showSearchFilters}
                    title={t("filesToIncludeExclude")}
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
                </div>
              </div>
              {showSearchFilters && (
                <div className="flex flex-col gap-1.5 border-border border-t px-3 py-2">
                  <SearchFilterInput
                    label={t("filesToInclude")}
                    placeholder="e.g. *.ts, src/**"
                    value={treeInclude}
                    onChange={setTreeInclude}
                  />
                  <SearchFilterInput
                    label={t("filesToExclude")}
                    placeholder="e.g. **/node_modules, *.test.ts"
                    value={treeExclude}
                    onChange={setTreeExclude}
                  />
                </div>
              )}
            </div>
          )}
          <section
            className={cn(
              "overflow-y-auto px-2 pb-2",
              flatView ? "pt-1" : "pt-2",
              fillHeight ? "min-h-0 flex-1" : "max-h-72",
            )}
          >
            {flatView ? (
              <FlatFileList
                files={changedQuery.data?.data}
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
            ) : (
              <FolderTree
                files={allFilesQuery.data?.data}
                isLoading={allFilesQuery.isLoading}
                isError={allFilesQuery.isError}
                error={allFilesQuery.error}
                onFileSelect={onFileSelect}
                conversationId={conversationId}
                showHidden={showHidden}
                onShowHidden={() => onShowHiddenChange(true)}
                changedFiles={changedQuery.data?.data}
                runnerWentOffline={runnerWentOffline}
                searchQuery={debouncedTreeSearch}
                searchResults={treeSearchQuery.data}
                isSearching={treeSearchQuery.isFetching}
                isSearchError={treeSearchQuery.isError}
                searchError={treeSearchQuery.error instanceof Error ? treeSearchQuery.error : null}
              />
            )}
          </section>
        </>
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
    <span className="min-w-0 flex-1 flex items-center overflow-hidden">
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="inline-block max-w-full truncate font-mono text-[11px] text-muted-foreground cursor-default">
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
