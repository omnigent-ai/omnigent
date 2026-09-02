// GithubPanel — the right-rail "GitHub" tab. Read-only view of the session
// branch's relationship to GitHub: the associated PR (number, title, state, CI
// summary, link out) and the branch-vs-base diff.
//
// Layout is GitHub's "Files changed": every file's diff stacked in one scroll
// view, with the sidebar as a jump-to-file navigator that also highlights the
// file currently in view. Each file's Monaco diff sizes to its content (no
// inner scroll) and mounts lazily as it nears the viewport, so a large PR
// doesn't spin up every editor at once.
//
// Data comes from the runner's read-only GitHub resource API (see
// hooks/useGithub.ts), which shells out to `gh` + `git`. When gh is missing /
// unauthenticated / the workspace isn't a repo, the panel renders a message
// rather than an error.

import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CircleCheckIcon,
  CircleDotIcon,
  CircleXIcon,
  ExternalLinkIcon,
  Loader2Icon,
  RefreshCwIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { useQueryClient } from "@tanstack/react-query";
import { RunnerOfflineError } from "@/hooks/useWorkspaceChangedFiles";
import { readFileViewPreferences } from "@/lib/fileViewPreferences";
import {
  useGithubChangedFiles,
  useGithubFileDiff,
  useGithubInfo,
  type GithubChangedFile,
} from "@/hooks/useGithub";

// Monaco is heavy — load it only once a diff actually renders (mirrors FileViewer).
const MonacoDiffViewer = lazy(() =>
  import("./MonacoDiffViewer").then((m) => ({ default: m.MonacoDiffViewer })),
);

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

/** One row in the changed-files list: status letter, path (basename bold), +/- counts. */
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

/**
 * One file's section in the stacked diff: a sticky header (status + path +
 * diffstat) and the file's Monaco diff, sized to its content. The diff (and its
 * fetch) mount lazily once the section nears the viewport.
 */
function GithubFileSection({
  conversationId,
  file,
  base,
  prefs,
  registerRef,
}: {
  conversationId: string;
  file: GithubChangedFile;
  base: string | undefined;
  prefs: ReturnType<typeof readFileViewPreferences>;
  registerRef: (path: string, el: HTMLElement | null) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // Mount the diff only once the section nears the viewport; keep it mounted
  // afterwards so scrolling back up doesn't re-fetch/re-mount.
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
      // Prefetch a little before the section is actually on screen.
      { rootMargin: "400px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [seen]);

  const diff = useGithubFileDiff(conversationId, seen ? file.path : null, base);
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
      {!seen || diff.isLoading || (!diff.data && !diff.error) ? (
        <div className="flex items-center justify-center gap-2 p-6 text-ui text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading diff…
        </div>
      ) : diff.error ? (
        <div className="p-4 text-ui text-muted-foreground">
          {diff.error instanceof RunnerOfflineError
            ? "Runner offline."
            : `Couldn’t load diff: ${(diff.error as Error).message}`}
        </div>
      ) : (
        <Suspense
          fallback={
            <div className="flex items-center justify-center gap-2 p-6 text-ui text-muted-foreground">
              <Loader2Icon className="size-4 animate-spin" />
              Loading diff…
            </div>
          }
        >
          <MonacoDiffViewer
            autoHeight
            before={diff.data!.before}
            after={diff.data!.after}
            path={file.path}
            layout={prefs.diffLayout}
            hideWhitespace={prefs.hideWhitespace}
            wrapLines={prefs.wrapLines}
            conversationId={conversationId}
            comments={[]}
            activeSelection={null}
            onSetActiveSelection={() => {}}
          />
        </Suspense>
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

export function GithubPanel({ conversationId }: { conversationId: string }) {
  const queryClient = useQueryClient();
  const info = useGithubInfo(conversationId);
  const baseRef = info.data?.base_ref ?? undefined;
  const changes = useGithubChangedFiles(conversationId, baseRef);

  // Diff rendering preferences are app-global (shared with the FileViewer);
  // no in-panel toggle in v1, so read the persisted values once on mount.
  const prefs = useMemo(() => readFileViewPreferences(), []);

  const files = useMemo<GithubChangedFile[]>(() => changes.data?.data ?? [], [changes.data]);

  // The stacked diff scrolls as one; the sidebar highlights the file currently
  // at the top of the viewport and jumps to a file on click.
  const scrollRef = useRef<HTMLDivElement>(null);
  const sectionEls = useRef<Map<string, HTMLElement>>(new Map());
  const [activePath, setActivePath] = useState<string | null>(null);

  const registerRef = useCallback((path: string, el: HTMLElement | null) => {
    if (el) sectionEls.current.set(path, el);
    else sectionEls.current.delete(path);
  }, []);

  // Highlight the last section whose top has scrolled to/above the pane's top
  // edge. rAF-throttled so the scroll stays smooth.
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

  // Default the highlight to the first file once the list arrives.
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
  // ``available`` is false only for a non-git workspace (or no os_env): the
  // branch-vs-base diff needs a git checkout, so there's nothing to show.
  if (!data || !data.available) {
    if (data?.reason === "not_a_git_repo") {
      return <PanelMessage>This workspace isn’t a git repository.</PanelMessage>;
    }
    return <PanelMessage>GitHub isn’t available for this session.</PanelMessage>;
  }

  const pr = data.pr ?? null;
  const checks = pr?.checks;
  // gh is an enhancement: without it (or its auth) the git diff still renders,
  // and we surface a one-line note explaining why PR info is missing. This is
  // also the host-fallback state when the runner is offline and its machine
  // has no gh.
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
              className={cn("size-3.5", (info.isFetching || changes.isFetching) && "animate-spin")}
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
          {files.length === 0 ? (
            <PanelMessage>
              {changes.isLoading ? (
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
                conversationId={conversationId}
                file={file}
                base={baseRef}
                prefs={prefs}
                registerRef={registerRef}
              />
            ))
          )}
        </div>
      </div>
    </div>
  );
}
