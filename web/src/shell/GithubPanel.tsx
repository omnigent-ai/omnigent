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
// hooks/useGithub.ts), which shells out to `gh` + `git`. When gh is missing /
// unauthenticated / the workspace isn't a repo, the panel renders a message
// rather than an error.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CircleCheckIcon,
  CircleDotIcon,
  CircleXIcon,
  ExternalLinkIcon,
  Loader2Icon,
  RefreshCwIcon,
} from "lucide-react";
import { FileDiff } from "@pierre/diffs/react";
import { parsePatchFiles, type FileDiffMetadata } from "@pierre/diffs";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { useQueryClient } from "@tanstack/react-query";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { RunnerOfflineError } from "@/hooks/useWorkspaceChangedFiles";
import { readFileViewPreferences } from "@/lib/fileViewPreferences";
import {
  fetchGithubFileContents,
  useGithubChangedFiles,
  useGithubInfo,
  useGithubPrDiff,
  type GithubChangedFile,
} from "@/hooks/useGithub";

// Shiki bundled themes matching the app's editor look; the concrete side is
// chosen by `themeType` from the app's resolved light/dark mode.
const DIFF_THEME = { dark: "github-dark", light: "github-light" } as const;

// Relabel the collapsed-region affordance to say what clicking does, rather
// than a line count. The library renders the count / "More unchanged context
// may be available" into a stable `[data-unmodified-lines]` element inside its
// shadow DOM; `unsafeCSS` is its supported hook to inject styles there. The
// step is fixed at expansionLineCount (10), so a static label is accurate for
// the usual case (a small final chunk may reveal fewer). font-size:0 collapses
// the original text node; ::after paints ours at the normal size.
const DIFF_UNSAFE_CSS =
  "[data-unmodified-lines]{font-size:0}" +
  '[data-unmodified-lines]::after{content:"Show 10 more lines";font-size:.75rem}';

/** Centered muted message filling the panel — the shared empty/error/loading shell. */
function PanelMessage({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-ui text-muted-foreground">
      {children}
    </div>
  );
}

const STATUS_META: Record<
  GithubChangedFile["status"],
  { letter: string; label: string; className: string }
> = {
  created: { letter: "A", label: "Added", className: "text-green-600 dark:text-green-400" },
  modified: { letter: "M", label: "Modified", className: "text-amber-600 dark:text-amber-400" },
  deleted: { letter: "D", label: "Deleted", className: "text-red-600 dark:text-red-400" },
  renamed: { letter: "R", label: "Renamed", className: "text-blue-600 dark:text-blue-400" },
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
 * One file's section in the stacked diff: a sticky header (status + path +
 * diffstat) and the file's rendered diff. The diff mounts lazily once the
 * section nears the viewport (a big PR doesn't build every diff at once).
 */
function GithubFileSection({
  file,
  fileDiff,
  options,
  registerRef,
}: {
  file: GithubChangedFile;
  /** Parsed per-file diff from the whole-PR patch; absent for binary/unparsed. */
  fileDiff: FileDiffMetadata | undefined;
  options: DiffOptions;
  registerRef: (path: string, el: HTMLElement | null) => void;
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
  const { dir, name } = splitPath(file.path);

  return (
    <div ref={ref} data-github-file={file.path}>
      <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-border bg-card px-3 py-1.5">
        <span
          className={cn("w-3 shrink-0 text-center font-mono text-xs", meta.className)}
          title={meta.label}
        >
          {meta.letter}
        </span>
        <span className="min-w-0 flex-1 truncate text-ui" title={file.path}>
          {dir && <span className="text-muted-foreground">{dir}</span>}
          <span className="font-medium">{name}</span>
        </span>
        <DiffStat file={file} />
      </div>
      {!seen ? (
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

/** A colored pill for the PR state (open / draft / merged / closed). */
function StateBadge({ state, isDraft }: { state: string; isDraft: boolean }) {
  const upper = state.toUpperCase();
  const label =
    isDraft && upper === "OPEN" ? "Draft" : upper.charAt(0) + upper.slice(1).toLowerCase();
  const className =
    upper === "MERGED"
      ? "bg-purple-500/15 text-purple-600 dark:text-purple-400"
      : upper === "CLOSED"
        ? "bg-red-500/15 text-red-600 dark:text-red-400"
        : isDraft
          ? "bg-muted text-muted-foreground"
          : "bg-green-500/15 text-green-600 dark:text-green-400";
  return (
    <span className={cn("rounded-full px-2 py-0.5 text-xs font-medium", className)}>{label}</span>
  );
}

/** One row in the jump-to-file sidebar. */
function FileRow({
  file,
  selected,
  onSelect,
}: {
  file: GithubChangedFile;
  selected: boolean;
  onSelect: () => void;
}) {
  const meta = STATUS_META[file.status];
  const { dir, name } = splitPath(file.path);
  return (
    <button
      type="button"
      onClick={onSelect}
      title={file.path}
      className={cn(
        "flex w-full items-center gap-2 px-2 py-1 text-left text-ui hover:bg-muted/60",
        selected && "bg-muted",
      )}
    >
      <span className={cn("w-3 shrink-0 text-center font-mono text-xs", meta.className)}>
        {meta.letter}
      </span>
      <span className="min-w-0 flex-1 truncate">
        {dir && <span className="text-muted-foreground">{dir}</span>}
        <span className="font-medium">{name}</span>
      </span>
      <DiffStat file={file} />
    </button>
  );
}

export function GithubPanel({ conversationId }: { conversationId: string }) {
  const queryClient = useQueryClient();
  const info = useGithubInfo(conversationId);
  const baseRef = info.data?.base_ref ?? undefined;
  const changes = useGithubChangedFiles(conversationId, baseRef);
  const prDiff = useGithubPrDiff(conversationId, baseRef);

  const themeType = useResolvedThemeMode();
  // Diff layout preference is app-global (shared with the FileViewer); no
  // in-panel toggle in v1, so read the persisted value once on mount.
  const diffStyle = useMemo(
    () => (readFileViewPreferences().diffLayout === "split" ? "split" : "unified"),
    [],
  );

  const files = useMemo<GithubChangedFile[]>(() => changes.data?.data ?? [], [changes.data]);

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
      // Relabel the expand affordance to an action ("Show 10 more lines").
      unsafeCSS: DIFF_UNSAFE_CSS,
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
  if (info.isLoading) {
    return (
      <PanelMessage>
        <Loader2Icon className="size-5 animate-spin" />
        Loading GitHub…
      </PanelMessage>
    );
  }
  if (info.error) {
    if (info.error instanceof RunnerOfflineError) {
      return (
        <PanelMessage>The agent is asleep. Send a message to reconnect its runner.</PanelMessage>
      );
    }
    return <PanelMessage>Couldn’t load GitHub info: {(info.error as Error).message}</PanelMessage>;
  }
  const data = info.data;
  if (!data || !data.available) {
    if (data?.reason === "not_a_git_repo") {
      return <PanelMessage>This workspace isn’t a git repository.</PanelMessage>;
    }
    return <PanelMessage>GitHub isn’t available for this session.</PanelMessage>;
  }

  const pr = data.pr ?? null;
  const checks = pr?.checks;
  const ghNote =
    data.gh_available === false
      ? "GitHub CLI (gh) not installed — showing local branch diff."
      : data.authenticated === false
        ? "Not signed in — run `gh auth login` for PR info."
        : null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header: repo + PR metadata + Refresh. */}
      <div className="shrink-0 space-y-1.5 border-b border-border px-3 py-2.5">
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
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="Refresh"
            onClick={refresh}
            className="shrink-0"
          >
            <RefreshCwIcon
              className={cn(
                "size-3.5",
                (info.isFetching || changes.isFetching || prDiff.isFetching) && "animate-spin",
              )}
            />
          </Button>
        </div>
        {pr ? (
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <StateBadge state={pr.state} isDraft={pr.is_draft} />
            <a
              href={pr.url}
              target="_blank"
              rel="noreferrer"
              className="group inline-flex min-w-0 items-center gap-1 text-ui font-medium hover:underline"
            >
              <span className="truncate">{pr.title}</span>
              <span className="shrink-0 text-muted-foreground">#{pr.number}</span>
              <ExternalLinkIcon className="size-3 shrink-0 text-muted-foreground opacity-0 group-hover:opacity-100" />
            </a>
            {/* CI status checks (from the PR's statusCheckRollup) — icons + a
                tooltip so this reads as "checks", not a line diffstat. */}
            {checks && checks.total > 0 && (
              <span
                className="flex shrink-0 items-center gap-1.5 text-xs tabular-nums"
                title={`Checks: ${checks.passing} passing, ${checks.failing} failing, ${checks.pending} pending`}
              >
                {checks.passing > 0 && (
                  <span className="flex items-center gap-0.5 text-green-600 dark:text-green-400">
                    <CircleCheckIcon className="size-3" />
                    {checks.passing}
                  </span>
                )}
                {checks.failing > 0 && (
                  <span className="flex items-center gap-0.5 text-red-600 dark:text-red-400">
                    <CircleXIcon className="size-3" />
                    {checks.failing}
                  </span>
                )}
                {checks.pending > 0 && (
                  <span className="flex items-center gap-0.5 text-amber-600 dark:text-amber-400">
                    <CircleDotIcon className="size-3" />
                    {checks.pending}
                  </span>
                )}
              </span>
            )}
          </div>
        ) : (
          <p className="text-ui text-muted-foreground">
            {ghNote ?? (
              <>
                No open PR for <span className="font-mono">{data.branch}</span>
                {baseRef && (
                  <>
                    {" "}
                    — showing changes vs <span className="font-mono">{baseRef}</span>
                  </>
                )}
                .
              </>
            )}
          </p>
        )}
      </div>

      {/* Body: sidebar (jump-to-file) + one scroll of all files' diffs. */}
      <div className="flex min-h-0 flex-1">
        <div className="w-48 shrink-0 overflow-y-auto border-r border-border py-1">
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
            files.map((file) => (
              <FileRow
                key={file.path}
                file={file}
                selected={file.path === activePath}
                onSelect={() => jumpTo(file.path)}
              />
            ))
          )}
        </div>
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
              />
            ))
          )}
        </div>
      </div>
    </div>
  );
}
