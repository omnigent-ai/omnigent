// GithubPanel — the right-rail "GitHub" tab. Read-only view of the session
// branch's relationship to GitHub: the associated PR (number, title, state, CI
// summary, link out) and the branch-vs-base diff.
//
// Layout is GitHub's "Files changed": every file's diff stacked in one scroll
// view, with the sidebar as a jump-to-file navigator that also highlights the
// file currently in view. The whole PR is fetched as ONE unified-diff patch
// (/resources/github/diff) and parsed client-side into per-file diffs, each
// rendered with @pierre/diffs' FileDiff. Its `loadDiffFiles` loader lazily
// fetches a file's full content (/resources/github/diff/{path}) only when the
// reader expands unchanged context.
//
// Data comes from the runner's read-only GitHub resource API (see
// hooks/useGithub.ts), which shells out to `gh` + `git`. `deriveGithubPanelState`
// is the single switch that turns the info query into what the panel shows: an
// outdated host, a non-git workspace, a missing `gh` CLI, an unresolved
// upstream repo, or no open PR each render their own empty state, and only an
// open PR falls through to the header + stacked diff.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  ChevronsDownUpIcon,
  ChevronsUpDownIcon,
  AlertCircleIcon,
  CircleCheckIcon,
  CircleDotIcon,
  CircleXIcon,
  Columns2Icon,
  DownloadIcon,
  ExternalLinkIcon,
  FileDiffIcon,
  FileMinusIcon,
  FilePlusIcon,
  FileSymlinkIcon,
  FolderIcon,
  GitBranchIcon,
  GitPullRequestIcon,
  KeyRoundIcon,
  Loader2Icon,
  type LucideIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
  RefreshCwIcon,
  Rows2Icon,
  TerminalIcon,
} from "lucide-react";
import { FileDiff } from "@pierre/diffs/react";
import { parsePatchFiles, type FileDiffMetadata } from "@pierre/diffs";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useQueryClient } from "@tanstack/react-query";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { useResizableColumn } from "@/hooks/useResizableColumn";
import { RunnerOfflineError } from "@/hooks/useWorkspaceChangedFiles";
import { readFileViewPreferences, writeFileViewPreferences } from "@/lib/fileViewPreferences";
import {
  fetchGithubFileContents,
  useGithubChangedFiles,
  useGithubInfo,
  useGithubPrDiff,
  type GithubChangedFile,
  type GithubCheckRun,
  type GithubInfo,
} from "@/hooks/useGithub";

// Shiki bundled themes matching the app's editor look; the concrete side is
// chosen by `themeType` from the app's resolved light/dark mode.
const DIFF_THEME = { dark: "github-dark", light: "github-light" } as const;

/** Centered muted message filling the panel — the shared loading/transient shell. */
function PanelMessage({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-ui text-muted-foreground">
      {children}
    </div>
  );
}

/** Full-panel empty state: an icon, a title, and an optional hint line. Used
 *  for every "no GitHub content to show" reason so they read as one family. */
function GithubEmptyState({
  icon: Icon,
  title,
  hint,
}: {
  icon: LucideIcon;
  title: React.ReactNode;
  hint?: React.ReactNode;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
      <Icon className="size-8 text-muted-foreground/50" />
      <p className="text-ui font-medium text-foreground">{title}</p>
      {hint && <p className="max-w-xs text-ui text-muted-foreground">{hint}</p>}
    </div>
  );
}

/** The one thing the panel should show, derived from the info query. Every
 *  non-`ready` kind is a whole-panel state; `ready` renders the PR + diff. */
export type GithubPanelState =
  | { kind: "loading" }
  | { kind: "runner-offline" }
  | { kind: "error"; message: string }
  | { kind: "host-outdated" }
  | { kind: "unavailable" }
  | { kind: "not-a-git-repo" }
  | { kind: "no-gh-cli" }
  | { kind: "repo-unresolved" }
  | { kind: "no-pr"; branch: string | undefined }
  | { kind: "ready" };

/** Central switch turning the GitHub info query into the panel's state.
 *
 * Order matters: transient states (loading/offline/error) first, then the
 * git-first availability reasons, then the `gh` enhancement layer (CLI → auth
 * → repo → PR). `ready` is reached only with an open PR to render. */
export function deriveGithubPanelState(info: {
  isLoading: boolean;
  error: unknown;
  data: GithubInfo | undefined;
}): GithubPanelState {
  if (info.isLoading) return { kind: "loading" };
  if (info.error) {
    if (info.error instanceof RunnerOfflineError) return { kind: "runner-offline" };
    return { kind: "error", message: (info.error as Error).message };
  }
  const data = info.data;
  if (!data || !data.available) {
    if (data?.reason === "not_a_git_repo") return { kind: "not-a-git-repo" };
    if (data?.reason === "host_outdated") return { kind: "host-outdated" };
    return { kind: "unavailable" };
  }
  // Git repo present; `gh` layers PR/repo metadata on top of it.
  if (data.gh_available === false) return { kind: "no-gh-cli" };
  // Not signed in, or signed in but the upstream repo can't be resolved —
  // both point the user at `gh auth status`.
  if (data.authenticated === false) return { kind: "repo-unresolved" };
  if (!data.repo?.name_with_owner) return { kind: "repo-unresolved" };
  if (!data.pr) return { kind: "no-pr", branch: data.branch };
  return { kind: "ready" };
}

/** A ghost icon button with a tooltip; the toolbar's shared control element.
 *  No-delay is set by the surrounding TooltipProvider. */
function IconButton({
  label,
  onClick,
  className,
  children,
}: {
  label: string;
  onClick: () => void;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label={label}
          onClick={onClick}
          className={cn("shrink-0", className)}
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

// GitHub-style status icons (a file glyph carrying the change kind) instead of
// bare A/M/D/R letters — quicker to recognize at a glance.
const STATUS_META: Record<
  GithubChangedFile["status"],
  { label: string; className: string; Icon: LucideIcon }
> = {
  created: { label: "Added", className: "text-green-600 dark:text-green-400", Icon: FilePlusIcon },
  modified: {
    label: "Modified",
    className: "text-amber-600 dark:text-amber-400",
    Icon: FileDiffIcon,
  },
  deleted: { label: "Deleted", className: "text-red-600 dark:text-red-400", Icon: FileMinusIcon },
  renamed: {
    label: "Renamed",
    className: "text-blue-600 dark:text-blue-400",
    Icon: FileSymlinkIcon,
  },
};

/** Diffstat (+adds −removes) shared by the sidebar row and the section header. */
function DiffStat({ file }: { file: GithubChangedFile }) {
  if (file.lines_added === null && file.lines_removed === null) return null;
  return (
    <span className="shrink-0 font-mono text-xs tabular-nums">
      {file.lines_added !== null && (
        <span className="text-green-600 dark:text-green-400">+{file.lines_added}</span>
      )}{" "}
      {file.lines_removed !== null && (
        <span className="text-red-600 dark:text-red-400">−{file.lines_removed}</span>
      )}
    </span>
  );
}

/** Split a path into its directory prefix (with trailing slash) and basename. */
function splitPath(path: string): { dir: string; name: string } {
  const i = path.lastIndexOf("/");
  return i === -1
    ? { dir: "", name: path }
    : { dir: path.slice(0, i + 1), name: path.slice(i + 1) };
}

/** How @pierre/diffs' FileDiff is configured for this read-only stacked view. */
type DiffOptions = React.ComponentProps<typeof FileDiff>["options"];

/**
 * One file's section in the stacked diff: a sticky grey header (chevron + status
 * + path + diffstat) and the file's rendered diff. Clicking the header toggles
 * the diff open/closed. The diff mounts lazily once the section nears the
 * viewport (a big PR doesn't build every diff at once).
 */
function GithubFileSection({
  file,
  fileDiff,
  options,
  registerRef,
  collapsed,
  onToggleCollapsed,
}: {
  file: GithubChangedFile;
  /** Parsed per-file diff from the whole-PR patch; absent for binary/unparsed. */
  fileDiff: FileDiffMetadata | undefined;
  options: DiffOptions;
  registerRef: (path: string, el: HTMLElement | null) => void;
  /** Whether this file's diff is hidden. Lifted so the toolbar can drive all. */
  collapsed: boolean;
  onToggleCollapsed: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [seen, setSeen] = useState(false);

  useEffect(() => {
    registerRef(file.path, ref.current);
    return () => registerRef(file.path, null);
  }, [file.path, registerRef]);

  useEffect(() => {
    const el = ref.current;
    if (!el || seen) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) setSeen(true);
      },
      { rootMargin: "400px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [seen]);

  const meta = STATUS_META[file.status];
  const StatusIcon = meta.Icon;
  const { dir, name } = splitPath(file.path);
  const ToggleIcon = collapsed ? ChevronRightIcon : ChevronDownIcon;
  // A rename carries its old path in the parsed patch; a pure rename (100%
  // similarity) has no hunks, so show a note instead of an empty diff.
  const renamedFrom =
    fileDiff?.prevName && fileDiff.prevName !== file.path ? fileDiff.prevName : null;
  const pureRename = fileDiff?.type === "rename-pure";

  return (
    <div ref={ref} data-github-file={file.path}>
      <button
        type="button"
        onClick={onToggleCollapsed}
        aria-expanded={!collapsed}
        title={renamedFrom ? `${renamedFrom} → ${file.path}` : file.path}
        className="group sticky top-0 z-10 flex w-full cursor-pointer items-center gap-2 border-b border-border bg-secondary px-3 py-1.5 text-left dark:bg-muted"
      >
        <ToggleIcon
          aria-hidden="true"
          className="size-3.5 shrink-0 text-muted-foreground transition-colors group-hover:text-foreground"
        />
        <StatusIcon aria-hidden className={cn("size-3.5 shrink-0", meta.className)} />
        <span className="min-w-0 flex-1 truncate text-ui">
          {renamedFrom ? (
            <>
              <span className="text-muted-foreground">{renamedFrom}</span>
              <span className="text-muted-foreground"> → </span>
              <span className="font-medium">{file.path}</span>
            </>
          ) : (
            <>
              {dir && <span className="text-muted-foreground">{dir}</span>}
              <span className="font-medium">{name}</span>
            </>
          )}
        </span>
        <DiffStat file={file} />
      </button>
      {collapsed ? null : pureRename ? (
        <div className="p-4 text-ui text-muted-foreground">File renamed without changes.</div>
      ) : !seen ? (
        <div className="flex items-center justify-center gap-2 p-6 text-ui text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading diff…
        </div>
      ) : fileDiff ? (
        <FileDiff fileDiff={fileDiff} options={options} disableWorkerPool />
      ) : (
        <div className="p-4 text-ui text-muted-foreground">
          No text diff available for this file.
        </div>
      )}
    </div>
  );
}

/**
 * A CI-status pill (e.g. "✓ 66 passed"); hovering it reveals the individual job
 * names in that bucket, each with the status icon and a divider between rows.
 * Renders nothing when the bucket is empty. The full list shows — the card is
 * height-capped and scrolls.
 */
function CheckPill({
  label,
  count,
  runs,
  icon,
  className,
}: {
  label: string;
  count: number;
  /** All checks; filtered to this pill's bucket for the hover list. */
  runs: GithubCheckRun[];
  icon: React.ReactNode;
  /** Tint for the pill + icon. */
  className: string;
}) {
  if (count === 0) return null;
  const names = runs.filter((r) => r.name);
  return (
    <HoverCard openDelay={100} closeDelay={100}>
      <HoverCardTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex cursor-pointer items-center gap-0.5 rounded-full border px-1.5 py-px text-xs tabular-nums",
            className,
          )}
        >
          {icon}
          {count} {label}
        </button>
      </HoverCardTrigger>
      <HoverCardContent
        side="bottom"
        align="start"
        className="max-h-64 w-auto max-w-xs overflow-y-auto p-1"
      >
        {names.length === 0 ? (
          <p className="px-1.5 py-1 text-muted-foreground">No job details.</p>
        ) : (
          <ul className="divide-y divide-border">
            {names.map((r) => (
              <li key={r.url ?? r.name} className="flex items-center gap-2 px-1.5 py-1.5">
                <span className="shrink-0">{icon}</span>
                <span className="truncate" title={r.name}>
                  {r.name}
                </span>
              </li>
            ))}
          </ul>
        )}
      </HoverCardContent>
    </HoverCard>
  );
}

// ── Sidebar file tree ────────────────────────────────────────────────────
// The changed files group into a folder tree. A single-child directory chain
// collapses into one row (VS Code "compact folders"): a lone change under
// omnigent/runner/x.py shows an "omnigent/runner" row, not two nested folders.
// Files are leaves that jump the diff scroll to their section; folders toggle.

interface TreeFileNode {
  type: "file";
  name: string;
  file: GithubChangedFile;
}
interface TreeDirNode {
  type: "dir";
  /** Display name, possibly a compacted chain like "a/b/c". */
  name: string;
  /** Full path of the deepest folder in the chain — the collapse key. */
  path: string;
  children: SidebarTree[];
}
type SidebarTree = TreeFileNode | TreeDirNode;

/** Directories before files, each group kept in first-encounter order. */
function dirsFirst(nodes: SidebarTree[]): SidebarTree[] {
  return [...nodes.filter((n) => n.type === "dir"), ...nodes.filter((n) => n.type === "file")];
}

/** Fold single-dir-child chains into one node (children are compacted first,
 *  so one merge per level suffices). */
function compactNode(node: SidebarTree): SidebarTree {
  if (node.type === "file") return node;
  const children = node.children.map(compactNode);
  if (children.length === 1 && children[0].type === "dir") {
    const only = children[0];
    return {
      type: "dir",
      name: `${node.name}/${only.name}`,
      path: only.path,
      children: only.children,
    };
  }
  return { type: "dir", name: node.name, path: node.path, children: dirsFirst(children) };
}

/** Build the compacted folder tree for the changed-files sidebar. */
function buildSidebarTree(files: GithubChangedFile[]): SidebarTree[] {
  const root: TreeDirNode = { type: "dir", name: "", path: "", children: [] };
  for (const file of files) {
    const parts = file.path.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const part = parts[i];
      let dir = node.children.find((c): c is TreeDirNode => c.type === "dir" && c.name === part);
      if (!dir) {
        dir = { type: "dir", name: part, path: parts.slice(0, i + 1).join("/"), children: [] };
        node.children.push(dir);
      }
      node = dir;
    }
    node.children.push({ type: "file", name: parts[parts.length - 1], file });
  }
  return dirsFirst(root.children.map(compactNode));
}

// VS Code–style tree indentation, tuned for the narrow sidebar.
const TREE_INDENT_STEP = 10;
const TREE_BASE_PAD = 8;
const treeIndent = (depth: number) => depth * TREE_INDENT_STEP + TREE_BASE_PAD;

/**
 * One node in the sidebar file tree — a collapsible folder or a file leaf.
 * Names truncate on the LEFT (rtl) so the end of the path — the most specific
 * part — stays visible; the name isn't `flex-1`, so short names still sit
 * beside their icon rather than drifting right.
 */
function SidebarNode({
  node,
  depth,
  activePath,
  onSelectFile,
  collapsedDirs,
  onToggleDir,
}: {
  node: SidebarTree;
  depth: number;
  activePath: string | null;
  onSelectFile: (path: string) => void;
  collapsedDirs: ReadonlySet<string>;
  onToggleDir: (path: string) => void;
}) {
  if (node.type === "file") {
    const meta = STATUS_META[node.file.status];
    const StatusIcon = meta.Icon;
    return (
      <button
        type="button"
        onClick={() => onSelectFile(node.file.path)}
        title={node.file.path}
        style={{ paddingLeft: `${treeIndent(depth)}px` }}
        className={cn(
          "flex w-full items-center gap-1.5 py-1 pr-2 text-left text-ui hover:bg-muted/60",
          node.file.path === activePath && "bg-muted",
        )}
      >
        {/* Empty chevron column so the status icon lines up under a sibling
            folder's icon. */}
        <span className="size-3.5 shrink-0" aria-hidden />
        <StatusIcon aria-hidden className={cn("size-3.5 shrink-0", meta.className)} />
        <span className="min-w-0 truncate [direction:rtl]">
          <bdi>{node.name}</bdi>
        </span>
        <span className="ml-auto flex shrink-0 items-center">
          <DiffStat file={node.file} />
        </span>
      </button>
    );
  }

  const open = !collapsedDirs.has(node.path);
  return (
    <>
      <button
        type="button"
        onClick={() => onToggleDir(node.path)}
        aria-expanded={open}
        title={node.path}
        style={{ paddingLeft: `${treeIndent(depth)}px` }}
        className="flex w-full items-center gap-1.5 py-1 pr-2 text-left text-ui hover:bg-muted/60"
      >
        <ChevronRightIcon
          aria-hidden
          className={cn(
            "size-3.5 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90",
          )}
        />
        <FolderIcon aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0 truncate font-medium [direction:rtl]">
          <bdi>{node.name}</bdi>
        </span>
      </button>
      {open &&
        node.children.map((child) => (
          <SidebarNode
            key={child.type === "file" ? child.file.path : child.path}
            node={child}
            depth={depth + 1}
            activePath={activePath}
            onSelectFile={onSelectFile}
            collapsedDirs={collapsedDirs}
            onToggleDir={onToggleDir}
          />
        ))}
    </>
  );
}

export function GithubPanel({ conversationId }: { conversationId: string }) {
  const queryClient = useQueryClient();
  const info = useGithubInfo(conversationId);
  const baseRef = info.data?.base_ref ?? undefined;
  const changes = useGithubChangedFiles(conversationId, baseRef);
  const prDiff = useGithubPrDiff(conversationId, baseRef);

  const themeType = useResolvedThemeMode();
  // Diff layout is the app-global FileViewer preference (unified/split); seed
  // from the persisted value and write toggles back so the choice carries over.
  const [diffStyle, setDiffStyle] = useState<"unified" | "split">(() =>
    readFileViewPreferences().diffLayout === "split" ? "split" : "unified",
  );
  const toggleDiffStyle = useCallback(() => {
    setDiffStyle((prev) => {
      const next = prev === "split" ? "unified" : "split";
      writeFileViewPreferences({ ...readFileViewPreferences(), diffLayout: next });
      return next;
    });
  }, []);

  const files = useMemo<GithubChangedFile[]>(() => changes.data?.data ?? [], [changes.data]);
  // The sidebar groups the flat file list into a compacted folder tree (the
  // diff scroll below stays linear, in file order).
  const fileTree = useMemo(() => buildSidebarTree(files), [files]);

  // Per-file collapse is lifted here so the toolbar's "expand/collapse all" can
  // drive every section; a path in the set is collapsed.
  const [collapsedPaths, setCollapsedPaths] = useState<ReadonlySet<string>>(() => new Set());
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  // Collapsed folders in the sidebar tree (empty = all expanded).
  const [collapsedDirs, setCollapsedDirs] = useState<ReadonlySet<string>>(() => new Set());
  const toggleDir = useCallback((path: string) => {
    setCollapsedDirs((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  // Drag-to-resize the file sidebar via a handle on its right edge.
  const {
    width: sidebarWidth,
    containerRef: bodyRef,
    handleProps: sidebarHandleProps,
  } = useResizableColumn(192, 140, 560);

  const toggleOne = useCallback((path: string) => {
    setCollapsedPaths((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const allCollapsed = files.length > 0 && files.every((f) => collapsedPaths.has(f.path));
  const toggleAll = useCallback(() => {
    setCollapsedPaths((prev) => {
      const everyCollapsed = files.length > 0 && files.every((f) => prev.has(f.path));
      return everyCollapsed ? new Set<string>() : new Set(files.map((f) => f.path));
    });
  }, [files]);

  // Parse the one whole-PR patch into per-file diffs, keyed by path.
  const filesByPath = useMemo(() => {
    const map = new Map<string, FileDiffMetadata>();
    const patch = prDiff.data?.patch;
    if (patch) {
      try {
        for (const parsed of parsePatchFiles(patch)) {
          for (const f of parsed.files) map.set(f.name, f);
        }
      } catch {
        // A malformed patch just yields no rendered diffs (the sidebar and the
        // per-section "no diff" fallback still render).
      }
    }
    return map;
  }, [prDiff.data]);

  // Expand-context loader: fetch a file's full old/new content on demand when
  // the reader expands unchanged regions.
  const loadDiffFiles = useCallback(
    async (fd: FileDiffMetadata) => {
      const { before, after } = await fetchGithubFileContents(conversationId, fd.name, baseRef);
      return {
        oldFile: { name: fd.prevName ?? fd.name, contents: before ?? "" },
        newFile: { name: fd.name, contents: after ?? "" },
      };
    },
    [conversationId, baseRef],
  );

  const diffOptions = useMemo<DiffOptions>(
    () => ({
      theme: DIFF_THEME,
      themeType,
      diffStyle,
      // The section renders its own header; the diff body has none.
      disableFileHeader: true,
      // Expand unchanged context 10 lines at a time (default is 100). The
      // unchanged lines aren't in the patch, so expansion (and the exact
      // trailing-region count) is served by loadDiffFiles on demand.
      expansionLineCount: 10,
      loadDiffFiles,
    }),
    [themeType, diffStyle, loadDiffFiles],
  );

  // The stacked diff scrolls as one; the sidebar highlights the file at the top
  // of the viewport and jumps to a file on click.
  const scrollRef = useRef<HTMLDivElement>(null);
  const sectionEls = useRef<Map<string, HTMLElement>>(new Map());
  const [activePath, setActivePath] = useState<string | null>(null);

  const registerRef = useCallback((path: string, el: HTMLElement | null) => {
    if (el) sectionEls.current.set(path, el);
    else sectionEls.current.delete(path);
  }, []);

  const rafRef = useRef<number | null>(null);
  const recomputeActive = useCallback(() => {
    const container = scrollRef.current;
    if (!container) return;
    const top = container.getBoundingClientRect().top;
    let current: string | null = null;
    for (const [path, el] of sectionEls.current) {
      if (el.getBoundingClientRect().top - top <= 8) current = path;
    }
    setActivePath((prev) => current ?? prev);
  }, []);
  const onScroll = useCallback(() => {
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      recomputeActive();
    });
  }, [recomputeActive]);

  useEffect(() => {
    setActivePath((prev) =>
      prev && files.some((f) => f.path === prev) ? prev : (files[0]?.path ?? null),
    );
  }, [files]);

  const jumpTo = useCallback((path: string) => {
    setActivePath(path);
    sectionEls.current.get(path)?.scrollIntoView({ block: "start" });
  }, []);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["github-info", conversationId] });
    void queryClient.invalidateQueries({ queryKey: ["github-changed-files", conversationId] });
    void queryClient.invalidateQueries({ queryKey: ["github-pr-diff", conversationId] });
    void queryClient.invalidateQueries({ queryKey: ["github-file-diff", conversationId] });
  };

  // ── Whole-panel states (before the header + stacked diff) ───────────────
  // One central switch: every non-`ready` kind returns its own whole-panel
  // state, so the diff below renders only when there's an open PR.
  const panelState = deriveGithubPanelState({
    isLoading: info.isLoading,
    error: info.error,
    data: info.data,
  });
  switch (panelState.kind) {
    case "loading":
      return (
        <PanelMessage>
          <Loader2Icon className="size-5 animate-spin" />
          Loading GitHub…
        </PanelMessage>
      );
    case "runner-offline":
      return (
        <PanelMessage>The agent is asleep. Send a message to reconnect its runner.</PanelMessage>
      );
    case "error":
      return <PanelMessage>Couldn’t load GitHub info: {panelState.message}</PanelMessage>;
    case "host-outdated":
      return (
        <GithubEmptyState
          icon={DownloadIcon}
          title="Update your host to use GitHub"
          hint="The GitHub panel needs the host running Omnigent 0.13.0 or later. Update the host, then reconnect the session."
        />
      );
    case "not-a-git-repo":
      return (
        <GithubEmptyState
          icon={GitBranchIcon}
          title="Not a git repository"
          hint="This workspace isn’t a git checkout, so there’s no branch or PR to show."
        />
      );
    case "no-gh-cli":
      return (
        <GithubEmptyState
          icon={TerminalIcon}
          title="GitHub CLI not found"
          hint={
            <>
              Install the GitHub CLI (<span className="font-mono">gh</span>) on the host to see this
              branch’s pull request and CI status.
            </>
          }
        />
      );
    case "repo-unresolved":
      return (
        <GithubEmptyState
          icon={KeyRoundIcon}
          title="Can’t reach the upstream repo"
          hint={
            <>
              Run <span className="font-mono">gh auth status</span> on the host to confirm the
              GitHub CLI is signed in to the right account.
            </>
          }
        />
      );
    case "no-pr":
      // TODO: offer a "Create PR" action here once the panel can open PRs.
      return (
        <GithubEmptyState
          icon={GitPullRequestIcon}
          title={
            <>
              No open PR for <span className="font-mono">{panelState.branch ?? "this branch"}</span>
            </>
          }
          hint="When you open a pull request for this branch, it’ll show up here."
        />
      );
    case "unavailable":
      return (
        <GithubEmptyState
          icon={AlertCircleIcon}
          title="GitHub isn’t available"
          hint="There’s no GitHub information to show for this session."
        />
      );
  }

  // ── Ready: an open PR to render as its header + the stacked diff ─────────
  const data = info.data!;
  const pr = data.pr!;
  const checks = pr.checks;

  return (
    <TooltipProvider delayDuration={0}>
      <div className="flex h-full min-h-0 flex-col">
        {/* Header: repo + PR metadata + Refresh. */}
        <div className="shrink-0 border-b border-border p-2">
          <div className="flex items-center justify-between gap-2">
            <span className="min-w-0 truncate text-xs text-muted-foreground">
              {data.repo?.name_with_owner ?? "GitHub"}
              {data.branch && (
                <>
                  {" · "}
                  <span className="font-mono">{data.branch}</span>
                  {baseRef && <span className="text-muted-foreground"> → {baseRef}</span>}
                </>
              )}
            </span>
            <IconButton label="Refresh" onClick={refresh}>
              <RefreshCwIcon
                className={cn(
                  "size-3.5",
                  (info.isFetching || changes.isFetching || prDiff.isFetching) && "animate-spin",
                )}
              />
            </IconButton>
          </div>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <a
              href={pr.url}
              target="_blank"
              rel="noreferrer"
              className="group inline-flex min-w-0 items-center gap-1 text-ui font-medium hover:underline"
            >
              <span className="truncate">{pr.title}</span>
              <span className="shrink-0 text-muted-foreground">#{pr.number}</span>
              <ExternalLinkIcon className="size-3 shrink-0 text-muted-foreground" />
            </a>
          </div>
          {/* CI status checks (from the PR's statusCheckRollup), on their own
            line as pills; hover a pill to see the job names in that bucket. */}
          {checks.total > 0 && (
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-muted-foreground">
              <span className="text-ui font-medium">Checks</span>
              <CheckPill
                label="passed"
                count={checks.passing}
                runs={checks.runs.filter((r) => r.bucket === "passing")}
                icon={<CircleCheckIcon className="size-2.5 text-green-600 dark:text-green-400" />}
                className="border-green-500/25 bg-green-500/10 text-green-700 dark:text-green-400"
              />
              <CheckPill
                label="pending"
                count={checks.pending}
                runs={checks.runs.filter((r) => r.bucket === "pending")}
                icon={<CircleDotIcon className="size-2.5 text-amber-600 dark:text-amber-400" />}
                className="border-amber-500/25 bg-amber-500/10 text-amber-700 dark:text-amber-400"
              />
              <CheckPill
                label="failed"
                count={checks.failing}
                runs={checks.runs.filter((r) => r.bucket === "failing")}
                icon={<CircleXIcon className="size-2.5 text-red-600 dark:text-red-400" />}
                className="border-red-500/25 bg-red-500/10 text-red-700 dark:text-red-400"
              />
            </div>
          )}
          {/* Controls bar: hide the file list (left); toggle layout + expand/
            collapse every diff (right). */}
          {files.length > 0 && (
            <div className="mt-1.5 flex w-full items-center justify-between">
              <IconButton
                label={sidebarCollapsed ? "Show file list" : "Hide file list"}
                onClick={() => setSidebarCollapsed((v) => !v)}
                className="size-5 text-muted-foreground"
              >
                {sidebarCollapsed ? (
                  <PanelLeftOpenIcon className="size-3.5" />
                ) : (
                  <PanelLeftCloseIcon className="size-3.5" />
                )}
              </IconButton>
              <div className="flex items-center gap-0.5">
                <IconButton
                  label={diffStyle === "split" ? "Switch to unified view" : "Switch to split view"}
                  onClick={toggleDiffStyle}
                  className="size-5 text-muted-foreground"
                >
                  {diffStyle === "split" ? (
                    <Rows2Icon className="size-3.5" />
                  ) : (
                    <Columns2Icon className="size-3.5" />
                  )}
                </IconButton>
                <IconButton
                  label={allCollapsed ? "Expand all diffs" : "Collapse all diffs"}
                  onClick={toggleAll}
                  className="size-5 text-muted-foreground"
                >
                  {allCollapsed ? (
                    <ChevronsUpDownIcon className="size-3.5" />
                  ) : (
                    <ChevronsDownUpIcon className="size-3.5" />
                  )}
                </IconButton>
              </div>
            </div>
          )}
        </div>

        {/* Body: sidebar (jump-to-file) + one scroll of all files' diffs. */}
        <div ref={bodyRef as React.RefObject<HTMLDivElement>} className="flex min-h-0 flex-1">
          {!sidebarCollapsed && (
            <div
              style={{ width: `${sidebarWidth}px` }}
              className="relative shrink-0 overflow-y-auto border-r border-border pb-1"
            >
              {/* Drag the right edge to resize the file list. */}
              <div
                {...sidebarHandleProps}
                aria-label="Resize file list"
                className="absolute inset-y-0 right-0 z-10 w-1 cursor-col-resize transition-colors hover:bg-primary/30 active:bg-primary/50"
              />
              {changes.isLoading ? (
                <div className="flex items-center justify-center p-4 text-muted-foreground">
                  <Loader2Icon className="size-4 animate-spin" />
                </div>
              ) : changes.error ? (
                <p className="px-2 py-1 text-ui text-muted-foreground">
                  {changes.error instanceof RunnerOfflineError
                    ? "Runner offline."
                    : (changes.error as Error).message}
                </p>
              ) : files.length === 0 ? (
                <p className="px-2 py-1 text-ui text-muted-foreground">No changes vs base.</p>
              ) : (
                fileTree.map((node) => (
                  <SidebarNode
                    key={node.type === "file" ? node.file.path : node.path}
                    node={node}
                    depth={0}
                    activePath={activePath}
                    onSelectFile={jumpTo}
                    collapsedDirs={collapsedDirs}
                    onToggleDir={toggleDir}
                  />
                ))
              )}
            </div>
          )}
          <div ref={scrollRef} onScroll={onScroll} className="min-w-0 flex-1 overflow-y-auto">
            {files.length === 0 || prDiff.isLoading ? (
              <PanelMessage>
                {changes.isLoading || prDiff.isLoading ? (
                  <>
                    <Loader2Icon className="size-5 animate-spin" />
                    Loading changes…
                  </>
                ) : (
                  "No changes vs base."
                )}
              </PanelMessage>
            ) : (
              files.map((file) => (
                <GithubFileSection
                  key={file.path}
                  file={file}
                  fileDiff={filesByPath.get(file.path)}
                  options={diffOptions}
                  registerRef={registerRef}
                  collapsed={collapsedPaths.has(file.path)}
                  onToggleCollapsed={() => toggleOne(file.path)}
                />
              ))
            )}
          </div>
        </div>
      </div>
    </TooltipProvider>
  );
}
