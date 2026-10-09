// Serves every git provider. Runtime names (query keys, routes, test ids) keep
// the `github` prefix because they are stable wire ids.
//
// PullRequestPanel — the right-rail "Pull Requests" tab. Read-only view of the
// session branch's relationship to its git provider: the associated PR (number,
// title, state, CI summary, link out) and the branch-vs-base diff.
// Provider-visible text comes from lib/gitProviders.ts.
//
// Layout is GitHub's "Files changed": every file's diff stacked in one scroll
// view, with the sidebar as a jump-to-file navigator that also highlights the
// file currently in view. The whole PR is fetched as ONE unified-diff patch
// (/resources/github/diff) and parsed client-side into per-file diffs, each
// rendered with @pierre/diffs' FileDiff. Its `loadDiffFiles` loader lazily
// fetches a file's full content (/resources/github/diff/{path}) only when the
// reader expands unchanged context.
//
// Data comes from the runner's read-only PR resource API (see
// hooks/usePullRequests.ts), which shells out to the provider CLI + `git`.
// `derivePullRequestPanelState` is the single switch that turns the info query
// into what the panel shows: an outdated host, a non-git workspace, an
// unsupported remote, a missing provider CLI, an unresolved upstream repo, or no
// PR each render their own empty state, and an associated PR falls through to
// the header + stacked diff.

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
  PlusIcon,
  Rows2Icon,
  ServerOffIcon,
  TerminalIcon,
  Trash2Icon,
} from "lucide-react";
import { FileDiff } from "@pierre/diffs/react";
import { parsePatchFiles, type FileDiffMetadata } from "@pierre/diffs";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { MessageResponse } from "@/components/ai-elements/message";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useResizableColumn } from "@/hooks/useResizableColumn";
import { RunnerOfflineError } from "@/hooks/useWorkspaceChangedFiles";
import { readFileViewPreferences, writeFileViewPreferences } from "@/lib/fileViewPreferences";
import { gitProviderCopy, type GitProviderCopy } from "@/lib/gitProviders";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import {
  fetchPullRequestFileContents,
  normalizePullRequestInfo,
  usePullRequestChangedFiles,
  usePullRequestDiff,
  usePullRequestInfo,
  useSetPullRequestPreference,
  useUpdateSessionPr,
  type NormalizedPullRequestInfo,
  type PullRequestAssociation,
  type PullRequestChangedFile,
  type PullRequestCheckRun,
  type PullRequestChecks,
  type PullRequestComment,
  type PullRequestInfo,
} from "@/hooks/usePullRequests";

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

/** Full-panel empty state: an icon, a title, optional hint and note lines, and
 *  optional children below (the account/remote selectors). Used for every "no PR
 *  content to show" reason so they read as one family. */
function PullRequestEmptyState({
  icon: Icon,
  title,
  hint,
  note,
  children,
}: {
  icon: LucideIcon;
  title: React.ReactNode;
  hint?: React.ReactNode;
  /** A second line under the hint. */
  note?: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
      <Icon className="size-8 text-muted-foreground/50" />
      <p className="text-ui font-medium text-foreground">{title}</p>
      {hint && <p className="max-w-xs text-ui text-muted-foreground">{hint}</p>}
      {note && <p className="max-w-xs text-ui text-muted-foreground">{note}</p>}
      {children}
    </div>
  );
}

/** Keep the provider name accessible without taking space from the PR picker. */
function PanelTitle({ copy }: { copy: GitProviderCopy }) {
  const Icon = copy.Icon;
  return (
    <h2 className="shrink-0" title={copy.label}>
      <Icon size={14} className="shrink-0" aria-hidden />
      <span className="sr-only">{copy.label}</span>
    </h2>
  );
}

/** Provider copy with its `backticked` commands set in the monospace font. */
function codeSpans(text: string): React.ReactNode {
  return text.split("`").map((part, i) => {
    // Static copy, so position keys stay stable across renders.
    const key = `${i}:${part}`;
    return i % 2 ? (
      <span key={key} className="font-mono">
        {part}
      </span>
    ) : (
      part
    );
  });
}

/**
 * Account switcher — the one PR-resolution lever that can't be inferred: it picks
 * which signed-in identity `gh` runs as, i.e. which account can even see the repo.
 * Shown ONLY when the upstream repo can't be reached (the `repo-unresolved` empty
 * state), since that's an access problem the account can fix. Once the repo
 * resolves, the account is correct — surfacing the knob then would just invite a
 * misconfiguration, so it's absent from the header and the `no-pr` state. Renders
 * nothing with a single account (nothing to choose) or when the provider can't
 * switch accounts.
 */
function GithubAccountSelector({
  conversationId,
  info,
}: {
  conversationId: string;
  info: NormalizedPullRequestInfo;
}) {
  const setPref = useSetPullRequestPreference(conversationId);
  const accounts = info.auth.accounts ?? [];
  if (!info.capabilities.account_switching || accounts.length <= 1) return null;

  const selectedAccount = info.auth.selected_account ?? undefined;

  return (
    <div className="flex w-full max-w-xs flex-col items-center gap-2 pt-2">
      <Select
        value={selectedAccount}
        onValueChange={(login) => setPref.mutate({ account: login, pr_url: info.selected_pr_url })}
        disabled={setPref.isPending}
      >
        <SelectTrigger
          aria-label={`${gitProviderCopy(info.provider, info.auth.hint, info.provider_display).label} account`}
          className="h-8 w-full text-ui"
        >
          <SelectValue placeholder="Account" />
        </SelectTrigger>
        <SelectContent>
          {accounts.map((a) => (
            <SelectItem key={a.login} value={a.login}>
              {a.login}
              {a.active ? " (active)" : ""}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {setPref.isError && (
        <p className="text-ui text-red-600 dark:text-red-400">
          Couldn’t apply: {(setPref.error as Error).message}
        </p>
      )}
    </div>
  );
}

/** The one thing the panel should show, derived from the info query. Every
 *  non-`ready` kind is a whole-panel state; `ready` renders the PR + diff. */
export type PullRequestPanelState =
  | { kind: "loading" }
  | { kind: "runner-offline" }
  | { kind: "error"; message: string }
  | { kind: "host-outdated" }
  | { kind: "unavailable" }
  | { kind: "not-a-git-repo" }
  | { kind: "unsupported-remote"; remoteHost: string | undefined }
  | { kind: "no-cli"; cli: string }
  | { kind: "repo-unresolved" }
  | { kind: "no-pr"; branch: string | undefined }
  | { kind: "ready" };

/** Central switch turning the PR info query into the panel's state.
 *
 * Order matters: transient states (loading/offline/error) first, then the
 * git-first availability reasons, then the provider layer (CLI → auth → repo
 * → PR). `ready` is reached only with an associated PR to render. */
export function derivePullRequestPanelState(info: {
  isLoading: boolean;
  error: unknown;
  data: PullRequestInfo | undefined;
}): PullRequestPanelState {
  if (info.isLoading) return { kind: "loading" };
  if (info.error) {
    if (info.error instanceof RunnerOfflineError) return { kind: "runner-offline" };
    return { kind: "error", message: (info.error as Error).message };
  }
  const data = info.data;
  if (!data || !data.available) {
    if (data?.reason === "not_a_git_repo") return { kind: "not-a-git-repo" };
    if (data?.reason === "host_outdated") return { kind: "host-outdated" };
    if (data?.reason === "unsupported_remote") {
      return { kind: "unsupported-remote", remoteHost: data.remote_host };
    }
    return { kind: "unavailable" };
  }
  // Git repo present; the provider CLI layers PR/repo metadata on top of it.
  const { auth, repo, pr, branch } = normalizePullRequestInfo(data);
  // A provider can authenticate with a token without installing its CLI.
  if (auth.cli?.available === false && !auth.authenticated) {
    return { kind: "no-cli", cli: auth.cli.name };
  }
  // Not signed in, or signed in but the upstream repo can't be resolved —
  // both point the user at the provider's sign-in check.
  if (!auth.authenticated) return { kind: "repo-unresolved" };
  if (!repo?.name_with_owner) return { kind: "repo-unresolved" };
  if (!pr && data.warnings?.length) return { kind: "unavailable" };
  if (!pr) return { kind: "no-pr", branch };
  return { kind: "ready" };
}

/** A ghost icon button with a tooltip; the toolbar's shared control element.
 *  No-delay is set by the surrounding TooltipProvider. */
function IconButton({
  label,
  onClick,
  disabled,
  className,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
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
          disabled={disabled}
          className={cn("shrink-0", className)}
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

/** Compact PR state shown beside the title in the shared panel header. */
function PullRequestStatus({ state }: { state: string }) {
  const normalized = state.toUpperCase();
  const visual =
    normalized === "OPEN"
      ? {
          label: "Open",
          className: "border-green-500/25 bg-green-500/10 text-green-700 dark:text-green-400",
        }
      : normalized === "MERGED"
        ? {
            label: "Merged",
            className: "border-purple-500/25 bg-purple-500/10 text-purple-700 dark:text-purple-400",
          }
        : normalized === "CLOSED"
          ? {
              label: "Closed",
              className: "border-red-500/25 bg-red-500/10 text-red-700 dark:text-red-400",
            }
          : { label: state, className: "border-border bg-muted text-muted-foreground" };

  return (
    <Badge
      aria-label={`Pull request status: ${visual.label}`}
      className={cn("h-5 rounded-full border px-2 py-px text-xs leading-none", visual.className)}
    >
      {visual.label}
    </Badge>
  );
}

// GitHub-style status icons (a file glyph carrying the change kind) instead of
// bare A/M/D/R letters — quicker to recognize at a glance.
const STATUS_META: Record<
  PullRequestChangedFile["status"],
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
function DiffStat({ file }: { file: PullRequestChangedFile }) {
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
 * Files with more unified diff lines than this wait for "Show diff": FileDiff
 * tokenizes on the main thread (disableWorkerPool), so a huge file freezes the app.
 */
export const LARGE_DIFF_THRESHOLD = 2000;

/**
 * One file's section in the stacked diff: a sticky grey header (chevron + status
 * + path + diffstat) and the file's rendered diff. Clicking the header toggles
 * the diff open/closed. The diff mounts lazily once the section nears the
 * viewport (a big PR doesn't build every diff at once).
 */
function PullRequestFileSection({
  file,
  fileDiff,
  options,
  registerRef,
  collapsed,
  onToggleCollapsed,
}: {
  file: PullRequestChangedFile;
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
  const [showLargeDiff, setShowLargeDiff] = useState(false);

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
      ) : fileDiff && fileDiff.unifiedLineCount > LARGE_DIFF_THRESHOLD && !showLargeDiff ? (
        <div className="flex items-center gap-3 p-4 text-ui text-muted-foreground">
          Large diff — {fileDiff.unifiedLineCount.toLocaleString()} lines
          <button
            type="button"
            className="text-foreground underline-offset-2 hover:underline"
            onClick={() => setShowLargeDiff(true)}
          >
            Show diff
          </button>
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

/** React keys for items that can match on every identifying field (ADO replies share their
 *  thread's URL): the fields plus a repeat count stay unique and keep their value across polls. */
function listKeys<T>(items: readonly T[], identity: (item: T) => readonly unknown[]): string[] {
  const seen = new Map<string, number>();
  return items.map((item) => {
    const base = JSON.stringify(identity(item));
    const repeat = seen.get(base) ?? 0;
    seen.set(base, repeat + 1);
    return `${base}#${repeat}`;
  });
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
  runs: PullRequestCheckRun[];
  icon: React.ReactNode;
  /** Tint for the pill + icon. */
  className: string;
}) {
  if (count === 0) return null;
  const names = runs.filter((r) => r.name);
  const keys = listKeys(names, (r) => [r.url, r.name]);
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
            {names.map((r, i) => (
              <li key={keys[i]} className="flex items-center gap-2 px-1.5 py-1.5">
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

// ── Summary tab ──────────────────────────────────────────────────────────
// The PR's description and its conversation comments (from `gh pr view`), both
// rendered as GitHub-flavored markdown via the shared MessageResponse.

/** One comment card: an author + relative-time header over the markdown body. */
function PullRequestCommentCard({
  comment,
  providerLabel,
}: {
  comment: PullRequestComment;
  providerLabel: string;
}) {
  const ts = comment.created_at ? Date.parse(comment.created_at) : NaN;
  const rel = relativeTime(ts);
  const initial = comment.author?.[0]?.toUpperCase() ?? "?";
  return (
    <li className="rounded-lg border border-border bg-muted/20 p-3">
      <div className="mb-1.5 flex items-center gap-2 text-xs">
        <span
          aria-hidden
          className="inline-flex size-4 shrink-0 items-center justify-center rounded-full bg-muted text-[9px] font-semibold text-muted-foreground"
        >
          {initial}
        </span>
        <span className="min-w-0 truncate font-medium text-foreground">
          {comment.author ?? "unknown"}
        </span>
        {rel && (
          <span className="shrink-0 text-muted-foreground/70" title={absoluteTime(ts)}>
            {rel}
          </span>
        )}
        {comment.url && (
          <a
            href={comment.url}
            target="_blank"
            rel="noreferrer"
            aria-label={`Open comment on ${providerLabel}`}
            className="ml-auto shrink-0 text-muted-foreground hover:text-foreground"
          >
            <ExternalLinkIcon className="size-3" />
          </a>
        )}
      </div>
      <div className="text-ui break-words">
        <MessageResponse>{comment.body}</MessageResponse>
      </div>
    </li>
  );
}

/** The Summary tab body: CI checks, the PR description, then its comments. */
function PullRequestSummaryTab({
  checks,
  body,
  comments,
  commentsPartial,
  providerLabel,
}: {
  checks: PullRequestChecks;
  body: string | null | undefined;
  comments: PullRequestComment[];
  commentsPartial?: boolean;
  providerLabel: string;
}) {
  const commentKeys = listKeys(comments, (c) => [c.url, c.created_at, c.author_id ?? c.author]);
  return (
    // Extra bottom padding so the last comment can scroll clear of the very
    // bottom edge, where it's awkward to read.
    <div className="space-y-4 p-3 pb-16">
      {/* CI status checks (from the PR's statusCheckRollup) as pills; hover a
          pill to see the job names in that bucket. */}
      {(checks.total > 0 || checks.partial) && (
        <section className="space-y-1.5">
          <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
            Checks
          </h3>
          {checks.partial && (
            <p role="status" className="text-ui text-muted-foreground">
              Some checks are unavailable. Counts include only the checks loaded.
            </p>
          )}
          <div className="flex flex-wrap items-center gap-1.5">
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
        </section>
      )}
      <section className="space-y-1.5">
        <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
          Description
        </h3>
        {body ? (
          <div className="text-ui break-words">
            <MessageResponse>{body}</MessageResponse>
          </div>
        ) : (
          <p className="text-ui text-muted-foreground italic">No description provided.</p>
        )}
      </section>
      <section className="space-y-2">
        <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
          {comments.length > 0
            ? `Comments (${comments.length}${commentsPartial ? "+" : ""})`
            : "Comments"}
        </h3>
        {commentsPartial && (
          <p role="status" className="text-ui text-muted-foreground">
            Some comments are unavailable.
          </p>
        )}
        {comments.length === 0 ? (
          !commentsPartial && <p className="text-ui text-muted-foreground">No comments yet.</p>
        ) : (
          <ul className="space-y-2">
            {comments.map((c, i) => (
              <PullRequestCommentCard
                key={commentKeys[i]}
                comment={c}
                providerLabel={providerLabel}
              />
            ))}
          </ul>
        )}
      </section>
    </div>
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
  file: PullRequestChangedFile;
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
function buildSidebarTree(files: PullRequestChangedFile[]): SidebarTree[] {
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

/** A PR's picker label, in its own provider's copy (else the session's). */
function pullRequestLabel(pr: PullRequestAssociation, sessionProvider?: string | null): string {
  const copy = gitProviderCopy(pr.provider ?? sessionProvider, undefined, pr.provider_display);
  const host = pr.host === copy.defaultHost ? "" : `${pr.host}/`;
  const inferred = pr.relationship === "inferred" ? " (from branch)" : "";
  const identity = `${host}${pr.repository} ${copy.prNumberPrefix}${pr.number}${inferred}`;
  const title = pr.title?.trim();
  return title ? `${identity} — ${title}` : identity;
}

export function PullRequestPanel({ conversationId }: { conversationId: string }) {
  const isMobileViewport = useIsMobileViewport();
  const [selection, setSelection] = useState<{ sessionId: string; url?: string }>();
  const [prPickerOpen, setPrPickerOpen] = useState(false);
  const [prPickerTooltipOpen, setPrPickerTooltipOpen] = useState(false);
  // Select focuses rows on both pointer hover and keyboard navigation.
  const [focusedPrUrl, setFocusedPrUrl] = useState<string>();
  const [linking, setLinking] = useState(false);
  const [url, setUrl] = useState("");
  const selected = selection?.sessionId === conversationId ? selection.url : undefined;
  const info = usePullRequestInfo(conversationId, { poll: true, prUrl: selected });
  const [knownAssociations, setKnownAssociations] = useState<{
    sessionId: string;
    data: Pick<
      PullRequestInfo,
      "prs" | "tracking_available" | "selected_pr_url" | "provider" | "provider_display"
    >;
  }>();
  useEffect(() => {
    if (info.data) {
      const { prs, tracking_available, selected_pr_url, provider, provider_display } = info.data;
      setKnownAssociations({
        sessionId: conversationId,
        data: { prs, tracking_available, selected_pr_url, provider, provider_display },
      });
    }
  }, [conversationId, info.data]);
  // Switching the metadata query must not unmount the session's PR controls.
  const associations =
    info.data ??
    (knownAssociations?.sessionId === conversationId ? knownAssociations.data : undefined);
  const panelState = derivePullRequestPanelState(info);
  // No provider is known without a payload, or in a non-git workspace (older hosts omit it).
  const copy = gitProviderCopy(
    associations && panelState.kind !== "not-a-git-repo" ? associations.provider : null,
    undefined,
    associations?.provider_display,
  );
  const update = useUpdateSessionPr(conversationId);
  useEffect(() => {
    if (!selected && info.data?.selected_pr_url) {
      setSelection({ sessionId: conversationId, url: info.data.selected_pr_url });
    }
  }, [conversationId, selected, info.data?.selected_pr_url]);
  const changeSelection = (next?: string) => setSelection({ sessionId: conversationId, url: next });
  const prs = associations?.prs ?? [];
  const selectedPr = prs.find((pr) => pr.url === (selected ?? associations?.selected_pr_url));
  const linkInEmptyState = prs.length === 0 && panelState.kind === "no-pr";
  const linkControls = (
    <>
      {linking && (
        <form
          className="mt-2 flex gap-2"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              setLinking(false);
            }
          }}
          onSubmit={(event) => {
            event.preventDefault();
            update.mutate(
              { url, action: "attach" },
              {
                onSuccess: (data) => {
                  changeSelection(data.selected_pr_url);
                  setLinking(false);
                  setUrl("");
                },
              },
            );
          }}
        >
          <Input
            aria-label="Pull request URL"
            type="url"
            required
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder={copy.prUrlPlaceholder}
            className="flex-1 focus-visible:ring-0"
          />
          <Button type="submit" disabled={update.isPending}>
            Link
          </Button>
          <Button type="button" variant="outline" onClick={() => setLinking(false)}>
            Cancel
          </Button>
        </form>
      )}
      {update.isError && (
        <p role="alert" className="mt-1 text-ui text-destructive">
          {update.error.message}
        </p>
      )}
    </>
  );
  const fallbackCopy = gitProviderCopy(
    selectedPr?.provider ?? associations?.provider,
    undefined,
    selectedPr?.provider_display ?? associations?.provider_display,
  );
  const openPrFallback =
    associations?.tracking_available && selected && !info.isLoading && !info.data?.pr ? (
      <div className="mt-2 flex flex-col items-center gap-2 text-ui">
        <span className="text-muted-foreground">or</span>
        <a
          href={selected}
          target="_blank"
          rel="noreferrer"
          className="text-foreground underline underline-offset-4"
        >
          Open the PR on {fallbackCopy.label}
        </a>
      </div>
    ) : undefined;
  const showTrackingControls = !!associations?.tracking_available && !linkInEmptyState;
  return (
    <div className="flex h-full min-h-0 flex-col">
      {showTrackingControls ? (
        <div className="flex h-11 shrink-0 items-center gap-2 border-b border-border px-2">
          <PanelTitle copy={copy} />
          <div className="ml-auto flex min-w-0 flex-1 items-center gap-2">
            {prs.length > 0 && (
              <TooltipProvider>
                <Select
                  open={prPickerOpen}
                  onOpenChange={(open) => {
                    setPrPickerOpen(open);
                    setPrPickerTooltipOpen(false);
                    setFocusedPrUrl(undefined);
                  }}
                  value={selected ?? associations.selected_pr_url ?? ""}
                  onValueChange={changeSelection}
                >
                  <Tooltip
                    open={prPickerTooltipOpen && !prPickerOpen}
                    onOpenChange={(open) => setPrPickerTooltipOpen(open && !prPickerOpen)}
                  >
                    <TooltipTrigger asChild>
                      <SelectTrigger
                        aria-label="Session pull request"
                        className="min-w-0 flex-1 *:data-[slot=select-value]:block *:data-[slot=select-value]:truncate"
                      >
                        <SelectValue />
                      </SelectTrigger>
                    </TooltipTrigger>
                    {selectedPr && (
                      <TooltipContent className="wrap-anywhere">
                        {pullRequestLabel(selectedPr, associations.provider)}
                      </TooltipContent>
                    )}
                  </Tooltip>
                  <SelectContent
                    position="popper"
                    align="start"
                    className="w-(--radix-select-trigger-width)"
                    onKeyDown={(event) => {
                      if (event.key === "Escape") setPrPickerOpen(false);
                    }}
                  >
                    {prs.map((pr) => (
                      <Tooltip key={pr.url} open={prPickerOpen && focusedPrUrl === pr.url}>
                        <TooltipTrigger asChild>
                          <SelectItem
                            value={pr.url}
                            onFocus={() => setFocusedPrUrl(pr.url)}
                            onBlur={() =>
                              setFocusedPrUrl((current) =>
                                current === pr.url ? undefined : current,
                              )
                            }
                            className="*:[span]:last:block *:[span]:last:min-w-0 *:[span]:last:truncate"
                          >
                            {pullRequestLabel(pr, associations.provider)}
                          </SelectItem>
                        </TooltipTrigger>
                        <TooltipContent
                          side={isMobileViewport ? "bottom" : "left"}
                          className="wrap-anywhere"
                          style={isMobileViewport ? { pointerEvents: "none" } : undefined}
                        >
                          {pullRequestLabel(pr, associations.provider)}
                        </TooltipContent>
                      </Tooltip>
                    ))}
                  </SelectContent>
                </Select>
              </TooltipProvider>
            )}
            <div className="ml-auto flex shrink-0 items-center gap-1">
              <TooltipProvider delayDuration={0}>
                <IconButton label="Link a PR" onClick={() => setLinking(!linking)}>
                  <PlusIcon className="size-3.5" aria-hidden="true" />
                </IconButton>
                {selected && (
                  <IconButton
                    label="Unlink PR"
                    disabled={update.isPending}
                    onClick={() =>
                      update.mutate(
                        { url: selected, action: "remove" },
                        {
                          onSuccess: (data) => changeSelection(data.selected_pr_url),
                        },
                      )
                    }
                  >
                    <Trash2Icon className="size-3.5" aria-hidden="true" />
                  </IconButton>
                )}
              </TooltipProvider>
            </div>
          </div>
        </div>
      ) : panelState.kind !== "ready" ? (
        <div className="flex h-11 shrink-0 items-center gap-2 border-b border-border px-2">
          <PanelTitle copy={copy} />
        </div>
      ) : null}
      {showTrackingControls && (linking || update.isError) && (
        <div className="shrink-0 border-b border-border p-2">{linkControls}</div>
      )}
      {info.data?.warnings?.length || info.data?.discovery_warnings?.length ? (
        <p
          role="status"
          className="shrink-0 border-b border-border p-2 text-ui text-muted-foreground"
        >
          {[...(info.data?.warnings ?? []), ...(info.data?.discovery_warnings ?? [])].join(" ")}
        </p>
      ) : null}
      <div className="min-h-0 flex-1">
        <PullRequestPanelDetails
          key={`${conversationId}:${selected ?? ""}`}
          conversationId={conversationId}
          info={info}
          showProviderIcon={!showTrackingControls}
          emptyStateAction={
            associations?.tracking_available && linkInEmptyState ? (
              <div className="mt-2 w-full max-w-sm">
                <Button onClick={() => setLinking(!linking)}>Link a PR</Button>
                {linkControls}
              </div>
            ) : (
              openPrFallback
            )
          }
        />
      </div>
    </div>
  );
}

function PullRequestPanelDetails({
  conversationId,
  info,
  showProviderIcon,
  emptyStateAction,
}: {
  conversationId: string;
  info: ReturnType<typeof usePullRequestInfo>;
  showProviderIcon: boolean;
  emptyStateAction?: React.ReactNode;
}) {
  const baseRef = info.data?.base_ref ?? undefined;
  const prUrl = info.data?.selected_pr_url;
  const headSha = info.data?.pr?.head_sha;
  const baseSha = info.data?.pr?.base_sha;
  const revision = `${baseSha ?? ""}:${headSha ?? ""}`;
  const hasPr = !!info.data?.pr;
  const changes = usePullRequestChangedFiles(conversationId, hasPr, prUrl, revision);
  const prDiff = usePullRequestDiff(conversationId, hasPr, prUrl, revision);

  // Summary (PR body + comments) vs Changes (the stacked diff). Summary is the
  // landing tab — like GitHub's PR page opening on the Conversation view.
  const [activeTab, setActiveTab] = useState<"summary" | "changes">("summary");

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

  const files = useMemo<PullRequestChangedFile[]>(() => changes.data?.data ?? [], [changes.data]);
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
      const { before, after } = prUrl
        ? await fetchPullRequestFileContents(conversationId, fd.name, baseRef, {
            pr_url: prUrl,
            previous_path: fd.prevName,
            head_sha: headSha,
            base_sha: baseSha,
          })
        : await fetchPullRequestFileContents(conversationId, fd.name, baseRef);
      return {
        oldFile: { name: fd.prevName ?? fd.name, contents: before ?? "" },
        newFile: { name: fd.name, contents: after ?? "" },
      };
    },
    [conversationId, baseRef, prUrl, headSha, baseSha],
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

  // Mutation responses seed the query cache raw, so normalize before reading.
  const normalized = info.data && normalizePullRequestInfo(info.data);
  const copy = gitProviderCopy(
    normalized?.provider ?? null,
    normalized?.auth.hint,
    normalized?.provider_display,
  );

  // ── Whole-panel states (before the header + stacked diff) ───────────────
  // One central switch: every non-`ready` kind returns its own whole-panel
  // state, so the diff below renders only when there's an open PR.
  const panelState = derivePullRequestPanelState({
    isLoading: info.isLoading,
    error: info.error,
    data: info.data,
  });
  switch (panelState.kind) {
    case "loading":
      return (
        <PanelMessage>
          <Loader2Icon className="size-5 animate-spin" />
          Loading pull requests…
        </PanelMessage>
      );
    case "runner-offline":
      return (
        <PanelMessage>
          <p>The agent is asleep. Send a message to reconnect its runner.</p>
          {emptyStateAction}
        </PanelMessage>
      );
    case "error":
      return (
        <PanelMessage>
          <p>Couldn’t load pull request info: {panelState.message}</p>
          {emptyStateAction}
        </PanelMessage>
      );
    case "host-outdated":
      return (
        <PullRequestEmptyState
          icon={DownloadIcon}
          title="Update your host to use the Pull Requests tab"
          hint="The Pull Requests tab needs the host running Omnigent 0.13.0 or later. Update the host, then reconnect the session."
        >
          {emptyStateAction}
        </PullRequestEmptyState>
      );
    case "not-a-git-repo":
      return (
        <PullRequestEmptyState
          icon={GitBranchIcon}
          title="Not a git repository"
          hint="This workspace isn’t a git checkout, so there’s no branch or PR to show."
        >
          {emptyStateAction}
        </PullRequestEmptyState>
      );
    case "unsupported-remote":
      return (
        <PullRequestEmptyState
          icon={ServerOffIcon}
          title="No supported remote"
          hint={
            panelState.remoteHost ? (
              <>
                The workspace’s remote is on{" "}
                <span className="font-mono">{panelState.remoteHost}</span>, which isn’t a supported
                git provider.
              </>
            ) : (
              "This workspace’s remote isn’t on a supported git provider."
            )
          }
          // A GitHub Enterprise host the user never signed in to lands here.
          note={
            panelState.remoteHost && (
              <>
                For a GitHub Enterprise host, run{" "}
                <span className="font-mono">gh auth login --hostname {panelState.remoteHost}</span>{" "}
                on the host.
              </>
            )
          }
        >
          {emptyStateAction}
        </PullRequestEmptyState>
      );
    case "no-cli":
      return (
        <PullRequestEmptyState
          icon={TerminalIcon}
          title={`${copy.cliLabel} not found`}
          hint={
            <>
              Install the {copy.cliLabel} (<span className="font-mono">{panelState.cli}</span>) on
              the host to see this branch’s pull request and CI status.
              {copy.signInWithoutCli && <> {codeSpans(copy.authHint)}</>}
            </>
          }
        >
          {emptyStateAction}
        </PullRequestEmptyState>
      );
    case "repo-unresolved":
      return (
        <PullRequestEmptyState
          icon={KeyRoundIcon}
          title="Can’t reach the upstream repo"
          hint={codeSpans(copy.repoUnresolvedHint ?? copy.authHint)}
        >
          {normalized && (
            <GithubAccountSelector conversationId={conversationId} info={normalized} />
          )}
          {emptyStateAction}
        </PullRequestEmptyState>
      );
    case "no-pr":
      // TODO: offer a "Create PR" action here once the panel can open PRs.
      // No account selector here: the repo resolved, so the account is correct —
      // this is a genuine "no PR yet", not a misconfiguration to fix.
      return (
        <PullRequestEmptyState
          icon={GitPullRequestIcon}
          title={
            <>
              No open PR for <span className="font-mono">{panelState.branch ?? "this branch"}</span>
            </>
          }
          hint="Pull requests created in this session appear here. You can also link an existing PR."
        >
          {emptyStateAction}
        </PullRequestEmptyState>
      );
    case "unavailable":
      return (
        <PullRequestEmptyState
          icon={AlertCircleIcon}
          title="Pull requests aren’t available"
          hint="There’s no pull request information to show for this session."
        >
          {emptyStateAction}
        </PullRequestEmptyState>
      );
  }

  // ── Ready: an associated PR to render as its header + stacked diff ───────
  const data = normalized!;
  const pr = data.pr!;
  const checks = pr.checks;
  const comments = pr.comments ?? [];

  return (
    <TooltipProvider delayDuration={0}>
      <Tabs
        value={activeTab}
        onValueChange={(v) => setActiveTab(v === "changes" ? "changes" : "summary")}
        componentId="github.panel.tabs"
        className="flex h-full min-h-0 flex-col gap-0"
      >
        {/* Header: repo + PR metadata + the Summary/Changes tabs. Refreshes on
            its own — via the git-activity SSE signal and the panel's CI poll.
            Padding lives on the title block (not the outer container) so the
            tabs align to the gutter and the underline row spans full width. */}
        <div className="shrink-0 border-b border-border pb-0.5">
          <div className="px-3 pt-2">
            <div className="flex items-center gap-2">
              {showProviderIcon && <PanelTitle copy={copy} />}
              <span className="block min-w-0 truncate text-xs text-muted-foreground">
                {data.repo?.name_with_owner ?? copy.label}
                {data.branch && (
                  <>
                    {" · "}
                    <span className="font-mono">{data.branch}</span>
                    {baseRef && <span className="text-muted-foreground"> → {baseRef}</span>}
                  </>
                )}
              </span>
            </div>
            <div className="mt-1 flex flex-nowrap items-center gap-2">
              <a
                href={pr.url}
                target="_blank"
                rel="noreferrer"
                className="group flex min-w-0 items-center gap-1 text-ui font-medium hover:underline"
              >
                <span className="truncate">{pr.title}</span>
                <span className="shrink-0 text-muted-foreground">
                  {copy.prNumberPrefix}
                  {pr.number}
                </span>
                <ExternalLinkIcon className="size-3 shrink-0 text-muted-foreground" />
              </a>
              <PullRequestStatus state={pr.state} />
            </div>
          </div>
          {/* Tab bar (Summary | Changes); the diff controls live inside the
            Changes tab on their own line, so they don't crowd the tabs. gap-0 +
            flex-none keep the two labels close and content-sized (the default
            flex-1 equalizes and spreads them). */}
          <TabsList variant="line" aria-label="Pull request" className="h-auto gap-0 p-0">
            <TabsTrigger value="summary" className="flex-none border-0 px-3 leading-none">
              Summary
            </TabsTrigger>
            <TabsTrigger value="changes" className="flex-none border-0 px-3 leading-none">
              Changes
            </TabsTrigger>
          </TabsList>
        </div>

        {/* Summary: CI checks + the PR description + its conversation comments. */}
        <TabsContent value="summary" className="min-h-0 flex-1 overflow-y-auto">
          <PullRequestSummaryTab
            checks={checks}
            body={pr.body}
            comments={comments}
            commentsPartial={pr.comments_partial}
            providerLabel={copy.label}
          />
        </TabsContent>

        {/* Changes: a controls row, then the sidebar (jump-to-file) + one scroll
            of all files' diffs. */}
        <TabsContent value="changes" className="flex min-h-0 flex-1 flex-col">
          {(changes.data?.has_more || changes.data?.warning) && (
            <p
              role="status"
              className="shrink-0 border-b border-border p-2 text-ui text-muted-foreground"
            >
              {changes.data.warning || "The changed-file list is incomplete."}
            </p>
          )}
          {files.length > 0 && (
            <div className="flex w-full shrink-0 items-center justify-between gap-2 border-b border-border px-2 py-1">
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
                  <p className="px-2 py-1 text-ui text-muted-foreground">
                    {changes.data?.has_more || changes.data?.warning
                      ? "Changed files are unavailable."
                      : "No changes vs base."}
                  </p>
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
            <div
              ref={scrollRef}
              onScroll={onScroll}
              className="github-diff-stack min-w-0 flex-1 overflow-y-auto"
            >
              {prDiff.error ? (
                <PanelMessage>{(prDiff.error as Error).message}</PanelMessage>
              ) : prDiff.data?.unavailable_reason ? (
                <PanelMessage>
                  {prDiff.data.message ||
                    (prDiff.data.unavailable_reason === "pr_outside_workspace"
                      ? "Diff unavailable for a PR outside this workspace's repository"
                      : "Diff unavailable. Refresh or open the request on its provider.")}
                </PanelMessage>
              ) : files.length === 0 || prDiff.isLoading ? (
                <PanelMessage>
                  {changes.isLoading || prDiff.isLoading ? (
                    <>
                      <Loader2Icon className="size-5 animate-spin" />
                      Loading changes…
                    </>
                  ) : changes.data?.has_more || changes.data?.warning ? (
                    "Changed files are unavailable."
                  ) : (
                    "No changes vs base."
                  )}
                </PanelMessage>
              ) : (
                files.map((file) => (
                  <PullRequestFileSection
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
        </TabsContent>
      </Tabs>
    </TooltipProvider>
  );
}
