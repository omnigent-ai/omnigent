// Serves every git provider. Runtime names (query keys, routes, test ids) keep
// the `github` prefix because they are stable wire ids.
//
// PullRequestPanel — the right-rail "Pull Requests" tab: the session branch's PR
// (number, title, state, CI summary, reviewers, labels, link out) and the branch-vs-base diff.
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
// While the runner and host are offline, the server's stored PRs
// (hooks/useSessionPullRequests.ts) keep the list and the selected PR's last
// summary.
// `derivePullRequestPanelState` is the single switch that turns the info query
// into what the panel shows: an outdated host, a non-git workspace, an
// unsupported remote, a missing provider CLI, an unresolved upstream repo, or no
// PR each render their own empty state, and an associated PR falls through to
// the header + stacked diff.

import { createPortal } from "react-dom";
import { useMobilePanelHeaderTarget } from "./MobilePanelDrawer";
import { PullRequestCommentComposer } from "./PullRequestCommentComposer";
import { usePullRequestReviewThreads } from "@/hooks/usePullRequestReviewThreads";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { skipToken, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeftIcon,
  CopyIcon,
  EllipsisIcon,
  AlertCircleIcon,
  BanIcon,
  CircleCheckIcon,
  CircleDotIcon,
  CircleMinusIcon,
  CircleXIcon,
  DownloadIcon,
  ExternalLinkIcon,
  GitBranchIcon,
  GitCommitHorizontalIcon,
  GitPullRequestIcon,
  KeyRoundIcon,
  Loader2Icon,
  type LucideIcon,
  PlusIcon,
  RefreshCwIcon,
  ServerOffIcon,
  TerminalIcon,
  Trash2Icon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { copyText } from "@/lib/clipboard";
import { toast } from "sonner";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
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
import { Separator } from "@/components/ui/separator";
import { useCanEdit } from "@/hooks/usePermissions";
import {
  RunnerOfflineError,
  useWorkspaceAsleep,
  useWorkspaceServeable,
} from "@/hooks/useWorkspaceChangedFiles";
import {
  sessionPullRequestsKey,
  type StoredPullRequests,
  storedListToShow,
  storesSessionPullRequests,
  useSessionPullRequests,
  useUnlinkSessionPr,
} from "@/hooks/useSessionPullRequests";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { gitProviderCopy, type GitProviderCopy } from "@/lib/gitProviders";
import { parseHttpUrl } from "@/lib/openLinkInApp";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import {
  normalizePullRequestInfo,
  usePullRequestChangedFiles,
  usePullRequestDiff,
  usePullRequestInfo,
  useSetPullRequestPreference,
  useUpdateSessionPr,
  type NormalizedPullRequestInfo,
  type PullRequestAssociation,
  type PullRequestCheckRun,
  type PullRequestChecks,
  type PullRequestComment,
  type PullRequestInfo,
  type PullRequestTimelineEvent,
} from "@/hooks/usePullRequests";
import {
  PULL_REQUEST_MARKDOWN_COMPONENTS,
  PullRequestDescription,
  PullRequestTitle,
} from "./PullRequestDetails";
import { PullRequestLabels } from "./PullRequestLabels";
import { mergeButtonState, PullRequestMerge } from "./PullRequestMerge";
import { PullRequestReviewers } from "./PullRequestReviewers";
import { PanelMessage, PullRequestChangesTab } from "./PullRequestChangesTab";
import { IconButton } from "./PullRequestFileSection";

export { LARGE_DIFF_THRESHOLD } from "./PullRequestFileSection";

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

/** The panel's heading, hidden from sight (not from screen readers) where the PR
 *  list and its controls need the room. The provider stays out of it: it belongs to
 *  each request, so {@link PullRequestProviderIcon} carries it beside the title. */
function PanelTitle({ showTitle = false }: { showTitle?: boolean }) {
  return (
    <h2 className={cn("shrink-0 font-medium text-ui", !showTitle && "sr-only")}>Pull Requests</h2>
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
  | { kind: "sandbox-offline" }
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
  /** The session's managed sandbox is asleep (see `useWorkspaceAsleep`). */
  sandboxAsleep?: boolean;
}): PullRequestPanelState {
  // Nothing can answer until the sandbox starts again, so the query is skipped
  // or 503s. Say so instead of spinning or "not available"; info cached before
  // it stopped still renders.
  if (info.sandboxAsleep && info.data === undefined) return { kind: "sandbox-offline" };
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

/** The request's provider, shown beside its title: a glyph, named on hover and for
 *  screen readers. A session rarely spans providers, so it stays this quiet. */
function PullRequestProviderIcon({ copy }: { copy: GitProviderCopy }) {
  const Icon = copy.Icon;
  return (
    <span className="flex shrink-0 items-center text-muted-foreground" title={copy.label}>
      <Icon size={14} className="shrink-0" aria-hidden />
      <span className="sr-only">{copy.label}</span>
    </span>
  );
}

/** Compact PR state shown beside the title in the shared panel header. */
function PullRequestStatus({ state, isDraft = false }: { state: string; isDraft?: boolean }) {
  const normalized = state.toUpperCase();
  const visual =
    normalized === "OPEN" && isDraft
      ? { label: "Draft", className: "border-border bg-muted text-muted-foreground" }
      : normalized === "OPEN"
        ? {
            label: "Open",
            className: "border-green-500/25 bg-green-500/10 text-green-700 dark:text-green-400",
          }
        : normalized === "MERGED"
          ? {
              label: "Merged",
              className:
                "border-purple-500/25 bg-purple-500/10 text-purple-700 dark:text-purple-400",
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

const CHECK_ROW_CLASS = "flex items-center gap-2 px-1.5 py-1.5";

/**
 * A CI-status pill (e.g. "✓ 66 passed"); hovering it reveals the individual job
 * names in that bucket, each with the status icon and a divider between rows.
 * A job with an http(s) run URL links to it. Renders nothing when the bucket
 * is empty. The full list shows — the card is height-capped and scrolls.
 */
function CheckPill({
  label,
  count,
  runs,
  icon,
  className,
  compact = false,
}: {
  compact?: boolean;
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
          aria-label={`${count} ${label}`}
          className={cn(
            "inline-flex cursor-pointer items-center gap-0.5 rounded-full border px-1.5 py-px text-xs tabular-nums",
            className,
          )}
        >
          {icon}
          {count} {!compact && label}
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
            {names.map((r, i) => {
              const href = r.url ? parseHttpUrl(r.url)?.href : undefined;
              const row = (
                <>
                  <span className="shrink-0">{icon}</span>
                  <span className="truncate" title={r.name}>
                    {r.name}
                  </span>
                </>
              );
              return (
                <li key={keys[i]}>
                  {href ? (
                    <a
                      href={href}
                      target="_blank"
                      rel="noreferrer"
                      className={cn(CHECK_ROW_CLASS, "cursor-pointer hover:underline")}
                    >
                      {row}
                    </a>
                  ) : (
                    <div className={CHECK_ROW_CLASS}>{row}</div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </HoverCardContent>
    </HoverCard>
  );
}

// ── Summary and Comments tabs ────────────────────────────────────────────
// Reviewer assignments, CI checks, the PR description, and conversation comments.

/**
 * Space colors evenly and group shared suffixes without provider-specific rules.
 * The same author set keeps its colors; adding or removing authors can shift them.
 */
function pullRequestAuthorHues(comments: PullRequestComment[]): Map<string, number> {
  const identities = comments.flatMap(({ author }) => (author ? [author.toLowerCase()] : []));
  const authors = [...new Set(identities)].sort((a, b) => {
    const reversedA = [...a].reverse().join("");
    const reversedB = [...b].reverse().join("");
    return reversedA < reversedB ? -1 : reversedA > reversedB ? 1 : 0;
  });
  return new Map(authors.map((author, index) => [author, (index * 360) / authors.length]));
}

function PullRequestAuthorAvatar({
  author,
  hue,
}: {
  author: string | null | undefined;
  hue: number | undefined;
}) {
  return (
    <span
      aria-hidden
      className={cn(
        "inline-flex size-4 shrink-0 items-center justify-center rounded-full text-[9px] font-semibold",
        hue === undefined && "bg-muted text-muted-foreground",
      )}
      style={
        // Uniform perceptual lightness keeps white initials legible across the hue wheel.
        hue === undefined
          ? undefined
          : { backgroundColor: `oklch(0.48 0.13 ${hue})`, color: "#fff" }
      }
    >
      {author?.[0]?.toUpperCase() ?? "?"}
    </span>
  );
}

// The PR's description and its conversation comments (from `gh pr view`) are both
// rendered as GitHub-flavored markdown via the shared MessageResponse.
/** One comment card: an author + relative-time header over the markdown body. */
function PullRequestCommentCard({
  comment,
  providerLabel,
  authorHue,
}: {
  comment: PullRequestComment;
  providerLabel: string;
  authorHue: number | undefined;
}) {
  const ts = comment.created_at ? Date.parse(comment.created_at) : NaN;
  const rel = relativeTime(ts);
  return (
    <li className="pull-request-comment-surface rounded-lg border border-border p-3">
      <div className="mb-1.5 flex items-center gap-2 text-xs">
        <PullRequestAuthorAvatar author={comment.author} hue={authorHue} />
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
        <MessageResponse allowGitHubAttachmentImages components={PULL_REQUEST_MARKDOWN_COMPONENTS}>
          {comment.body}
        </MessageResponse>
      </div>
    </li>
  );
}

function PullRequestTimelineRow({
  event,
  providerLabel,
}: {
  event: PullRequestTimelineEvent;
  providerLabel: string;
}) {
  const timestamp = event.created_at ? Date.parse(event.created_at) : NaN;
  const time = relativeTime(timestamp);
  const forcePush = event.kind === "force_push";
  const Icon = forcePush ? GitBranchIcon : GitCommitHorizontalIcon;
  const url = event.url && parseHttpUrl(event.url);
  return (
    <li className="flex gap-2 px-3 py-2 text-xs text-muted-foreground">
      <Icon aria-hidden className="mt-0.5 size-4 shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-1">
          <span className="font-medium text-foreground">{event.author ?? "unknown"}</span>
          <span>{forcePush ? "force-pushed" : "committed"}</span>
          {forcePush && event.previous_sha && (
            <span>
              from <code title={event.previous_sha}>{event.previous_sha.slice(0, 7)}</code>
            </span>
          )}
          {forcePush && <span>{event.sha ? "to" : "the branch"}</span>}
          {url ? (
            <a
              href={url.href}
              target="_blank"
              rel="noreferrer"
              aria-label={`Open commit ${event.sha?.slice(0, 7) ?? ""} on ${providerLabel}`}
              className="font-mono text-foreground hover:underline"
              title={event.sha ?? undefined}
            >
              {event.sha?.slice(0, 7) ?? "View event"}
            </a>
          ) : (
            event.sha && <code title={event.sha}>{event.sha.slice(0, 7)}</code>
          )}
          {time && (
            <time dateTime={event.created_at!} title={absoluteTime(timestamp)} className="ml-auto">
              {time}
            </time>
          )}
        </div>
        {event.message && (
          <p className="mt-1 truncate" title={event.message}>
            {event.message}
          </p>
        )}
      </div>
    </li>
  );
}

function PullRequestCheckCounts({
  checks,
  compact = false,
}: {
  checks: PullRequestChecks;
  compact?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <CheckPill
        compact={compact}
        label="passed"
        count={checks.passing}
        runs={checks.runs.filter((r) => r.bucket === "passing")}
        icon={<CircleCheckIcon className="size-2.5 text-green-600 dark:text-green-400" />}
        className="border-green-500/25 bg-green-500/10 text-green-700 dark:text-green-400"
      />
      <CheckPill
        compact={compact}
        label="pending"
        count={checks.pending}
        runs={checks.runs.filter((r) => r.bucket === "pending")}
        icon={<CircleDotIcon className="size-2.5 text-amber-600 dark:text-amber-400" />}
        className="border-amber-500/25 bg-amber-500/10 text-amber-700 dark:text-amber-400"
      />
      <CheckPill
        compact={compact}
        label="failed"
        count={checks.failing}
        runs={checks.runs.filter((r) => r.bucket === "failing")}
        icon={<CircleXIcon className="size-2.5 text-red-600 dark:text-red-400" />}
        className="border-red-500/25 bg-red-500/10 text-red-700 dark:text-red-400"
      />
      <CheckPill
        compact={compact}
        label="cancelled"
        count={compact ? 0 : (checks.cancelled ?? 0)}
        runs={checks.runs.filter((r) => r.bucket === "cancelled")}
        icon={<BanIcon className="size-2.5 text-muted-foreground" />}
        className="border-border bg-muted text-muted-foreground"
      />
      <CheckPill
        compact={compact}
        label="skipped"
        count={compact ? 0 : (checks.skipped ?? 0)}
        runs={checks.runs.filter((r) => r.bucket === "skipped")}
        icon={<CircleMinusIcon className="size-2.5 text-muted-foreground" />}
        className="border-border bg-muted text-muted-foreground"
      />
    </div>
  );
}

/** The Summary tab body: reviewers beside labels, then CI checks with the Merge button, then
 *  the PR description, with dividers between. When the token can't read check runs, the
 *  Checks area says so instead of counts. */
function PullRequestSummaryTab({
  reviewers,
  labels,
  merge,
  checks,
  checksSupported,
  checksUnavailableHint,
  description,
}: {
  reviewers: React.ReactNode;
  labels: React.ReactNode;
  merge: React.ReactNode;
  checks: PullRequestChecks;
  checksSupported: boolean;
  checksUnavailableHint?: string;
  description: React.ReactNode;
}) {
  const showChecks = checks.total > 0 || checks.partial || !checksSupported;
  const showReviewers = !!reviewers;
  const showPeople = showReviewers || !!labels;
  const showStatus = showChecks || !!merge;
  return (
    // Blocks carry their own padding so the dividers between them span the full width.
    <div>
      {showPeople && (
        <div className="flex">
          {showReviewers && <div className="min-w-0 flex-1 p-3">{reviewers}</div>}
          {showReviewers && labels && <Separator orientation="vertical" />}
          {labels && <div className="min-w-0 flex-1 p-3">{labels}</div>}
        </div>
      )}
      {showPeople && showStatus && <Separator />}
      {showStatus && (
        <div className="space-y-3 p-3">
          {showChecks && (
            <section className="space-y-1.5">
              <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                Checks
              </h3>
              {!checksSupported ? (
                <p role="status" className="text-ui text-muted-foreground">
                  {checksUnavailableHint ?? "Checks can’t be read with the signed-in account."}
                </p>
              ) : (
                checks.partial && (
                  <p role="status" className="text-ui text-muted-foreground">
                    Some checks are unavailable. Counts include only the checks loaded.
                  </p>
                )
              )}
              <PullRequestCheckCounts checks={checks} />
            </section>
          )}
          {merge}
        </div>
      )}
      {(showPeople || showStatus) && <Separator />}
      {/* Extra bottom padding so the end of a long description can scroll clear
          of the very bottom edge, where it's awkward to read. */}
      <div className="p-3 pb-16">{description}</div>
    </div>
  );
}

/** Comments and commit activity share a chronological feed. */
function PullRequestCommentsTab({
  comments,
  commentsPartial,
  events,
  timelinePartial,
  prUrl,
  providerLabel,
  hasComposer,
}: {
  comments: PullRequestComment[];
  hasComposer: boolean;
  commentsPartial?: boolean;
  events?: PullRequestTimelineEvent[];
  timelinePartial?: boolean;
  prUrl: string;
  providerLabel: string;
}) {
  const commentKeys = listKeys(comments, (c) => [c.url, c.created_at, c.author_id ?? c.author]);
  const authorHues = useMemo(() => pullRequestAuthorHues(comments), [comments]);
  const prHref = parseHttpUrl(prUrl)?.href;
  const entries = [
    ...comments.map((comment, index) => ({
      created_at: comment.created_at,
      element: (
        <PullRequestCommentCard
          key={commentKeys[index]}
          comment={comment}
          providerLabel={providerLabel}
          authorHue={authorHues.get(comment.author?.toLowerCase() ?? "")}
        />
      ),
    })),
    ...(events ?? []).map((event) => ({
      created_at: event.created_at,
      element: (
        <PullRequestTimelineRow key={event.id} event={event} providerLabel={providerLabel} />
      ),
    })),
  ];
  if (events?.length) {
    const timestamp = (value: string | null) => {
      const parsed = Date.parse(value ?? "");
      return Number.isFinite(parsed) ? parsed : Infinity;
    };
    entries.sort((left, right) => timestamp(left.created_at) - timestamp(right.created_at));
  }
  return (
    <div className={cn("space-y-2 p-3", !hasComposer && "pb-16")}>
      {commentsPartial && (
        <p role="status" className="text-ui text-muted-foreground">
          Some comments are unavailable.
        </p>
      )}
      {timelinePartial && (
        <p role="status" className="text-ui text-muted-foreground">
          Some commit and push history is unavailable.{" "}
          {prHref && (
            <a href={prHref} target="_blank" rel="noreferrer" className="underline">
              View on {providerLabel}
            </a>
          )}
        </p>
      )}
      {entries.length === 0 ? (
        !commentsPartial &&
        !timelinePartial && <p className="text-ui text-muted-foreground">No comments yet.</p>
      ) : (
        <ul className="space-y-2">{entries.map((entry) => entry.element)}</ul>
      )}
    </div>
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

type Associations = Pick<
  PullRequestInfo,
  "prs" | "tracking_available" | "selected_pr_url" | "provider" | "provider_display"
>;
type PullRequestSubtab = "summary" | "comments" | "changes";

function PullRequestListRow({
  conversationId,
  active,
  association,
  onSelect,
  onUnlink,
  unlinkPending,
  hostOffline,
  sandboxAsleep,
}: {
  conversationId: string;
  active: boolean;
  association: PullRequestAssociation;
  onSelect: () => void;
  onUnlink?: () => void;
  unlinkPending: boolean;
  hostOffline: boolean;
  sandboxAsleep: boolean;
}) {
  const info = usePullRequestInfo(conversationId, {
    poll: active ? "list" : false,
    prUrl: association.url,
  });
  const canEdit = useCanEdit(conversationId);
  const data = info.data ? normalizePullRequestInfo(info.data) : undefined;
  const offline = hostOffline || info.error instanceof RunnerOfflineError;
  const cachedPr = data?.pr?.url === association.url ? data.pr : undefined;
  const pr = !offline ? cachedPr : undefined;
  const summary = pr ?? association;
  const checks = pr?.checks;
  const mergeState = pr ? mergeButtonState(pr) : undefined;
  const ready =
    canEdit &&
    !info.error &&
    data?.capabilities.merging &&
    pr?.state === "OPEN" &&
    !pr.is_draft &&
    mergeState?.kind === "ready";
  const checksUnavailable = checks?.partial || pr?.checks_supported === false;
  const checkStatus = (
    <>
      {checks && <PullRequestCheckCounts checks={checks} compact />}
      {checksUnavailable && <span title="Some checks could not be loaded">Incomplete checks</span>}
    </>
  );
  const title = (pr?.title ?? association.title)?.trim();
  const identity = pullRequestLabel({ ...association, title: null });
  const label = title ? `${identity} — ${title}` : identity;
  const providerCopy = gitProviderCopy(
    association.provider,
    undefined,
    association.provider_display,
  );
  const href = /^https:\/\//.test(association.url)
    ? parseHttpUrl(association.url)?.href
    : undefined;
  return (
    <li className="flex min-w-0 items-center gap-2 rounded-md px-1.5 hover:bg-muted/50">
      <button
        type="button"
        onClick={onSelect}
        title={label}
        aria-label={label}
        className="flex min-w-0 flex-1 cursor-pointer items-center gap-2 py-2 text-left text-ui focus-visible:outline-ring"
      >
        <GitPullRequestIcon
          aria-hidden
          className={cn(
            "size-4 shrink-0",
            !summary.state || summary.is_draft
              ? "text-muted-foreground"
              : summary.state === "OPEN"
                ? "text-green-600"
                : summary.state === "MERGED"
                  ? "text-purple-500"
                  : "text-red-500",
          )}
        />
        <span className="truncate">
          <span className="text-muted-foreground">{identity}</span>
          {title && (
            <>
              {" "}
              · <span className="font-medium">{title}</span>
            </>
          )}
        </span>
      </button>
      <div className="flex max-w-[45%] shrink-0 flex-wrap items-center justify-end gap-1 text-xs text-muted-foreground">
        {offline ? (
          <>
            {association.state && (
              <PullRequestStatus
                state={association.state}
                isDraft={association.is_draft === true}
              />
            )}
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span tabIndex={0} role="status" className="inline-flex items-center gap-1">
                    <ServerOffIcon aria-hidden className="size-3" />
                    {sandboxAsleep ? "Sandbox asleep" : "Host offline"}
                  </span>
                </TooltipTrigger>
                <TooltipContent className="max-w-xs">
                  {storedPullRequestHint(association, sandboxAsleep)}
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          </>
        ) : ready && pr ? (
          (checksUnavailable || !!checks?.failing || !!checks?.pending) && checkStatus
        ) : info.error ? (
          <span title={String(info.error)}>Status unavailable</span>
        ) : !pr ? (
          <span>{info.isLoading ? "Loading…" : "Status unavailable"}</span>
        ) : pr.state !== "OPEN" ? (
          <PullRequestStatus state={pr.state} />
        ) : (
          <>
            {pr.is_draft && <span>Draft</span>}
            {checkStatus}
            {!checksUnavailable && checks?.total === 0 && !pr.is_draft && (
              <span title={mergeState && "hint" in mergeState ? mergeState.hint : undefined}>
                No checks
              </span>
            )}
          </>
        )}
        {cachedPr && (
          <PullRequestMerge
            compact
            showTrigger={!!ready}
            unavailable={offline || !!info.error || !canEdit || !data?.capabilities.merging}
            conversationId={conversationId}
            prUrl={association.url}
            pr={cachedPr}
          />
        )}
      </div>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`Actions for ${identity}`}
            className="shrink-0"
          >
            <EllipsisIcon aria-hidden className="size-3.5" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {canEdit && onUnlink && (
            <>
              <DropdownMenuItem disabled={unlinkPending} onSelect={onUnlink}>
                <Trash2Icon aria-hidden /> Unlink PR
              </DropdownMenuItem>
              <DropdownMenuSeparator />
            </>
          )}
          {href && (
            <DropdownMenuItem asChild>
              <a href={href} target="_blank" rel="noreferrer">
                <ExternalLinkIcon aria-hidden /> Open in {providerCopy.label}
              </a>
            </DropdownMenuItem>
          )}
          <DropdownMenuItem
            onSelect={() => {
              void copyText(association.url).then(
                () => toast.success("PR link copied"),
                () => toast.error("Couldn’t copy PR link"),
              );
            }}
          >
            <CopyIcon aria-hidden /> Copy PR link
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </li>
  );
}

interface PullRequestPanelView {
  selected?: string;
  showingDetails: boolean;
  subtab: PullRequestSubtab;
}

const DEFAULT_PANEL_VIEW: PullRequestPanelView = { showingDetails: false, subtab: "summary" };

export function PullRequestPanel({
  conversationId,
  active = true,
}: {
  conversationId: string;
  /** False while the panel is mounted but hidden (collapsed rail); pauses the poll. */
  active?: boolean;
}) {
  const mobileHeaderTarget = useMobilePanelHeaderTarget();
  const inMobileDrawer = mobileHeaderTarget !== undefined;
  const queryClient = useQueryClient();
  const viewKey = ["pull-request-panel-view", conversationId];
  const { data: cachedView = DEFAULT_PANEL_VIEW } = useQuery<PullRequestPanelView>({
    queryKey: viewKey,
    queryFn: skipToken,
    initialData: DEFAULT_PANEL_VIEW,
    gcTime: 30 * 60_000,
  });
  // Keep interactions synchronous; the shared entry restores the view on navigation.
  const [localView, setLocalView] = useState({ conversationId, view: cachedView });
  if (localView.conversationId !== conversationId) {
    setLocalView({ conversationId, view: cachedView });
  }
  const view = localView.conversationId === conversationId ? localView.view : cachedView;
  const selected = view.selected;
  const showingDetails = view.showingDetails;
  const activeTab = view.subtab;
  const updateView = useCallback(
    (update: Partial<typeof view>) => {
      const next = queryClient.setQueryData<typeof view>(
        ["pull-request-panel-view", conversationId],
        (current) => ({
          showingDetails: false,
          subtab: "summary",
          ...current,
          ...update,
        }),
      );
      setLocalView({ conversationId, view: next! });
    },
    [queryClient, conversationId],
  );
  const changeSelection = useCallback(
    (next?: string) => updateView({ selected: next, subtab: "summary" }),
    [updateView],
  );
  const setActiveTab = (value: PullRequestSubtab) => updateView({ subtab: value });
  const [linking, setLinking] = useState(false);
  const [url, setUrl] = useState("");
  const info = usePullRequestInfo(conversationId, {
    poll: active ? (selected && !showingDetails ? "list" : true) : false,
    prUrl: selected,
  });
  const showPullRequestList = () => {
    updateView({ showingDetails: false, subtab: "summary" });
  };
  // Re-enabling the poll only restarts its timer; refresh stale info as the rail reopens.
  const wasActive = useRef(active);
  useEffect(() => {
    if (active && !wasActive.current) {
      void queryClient.refetchQueries({ queryKey: ["github-info", conversationId], stale: true });
    }
    wasActive.current = active;
  }, [active, conversationId, queryClient]);
  const [refreshing, setRefreshing] = useState(false);
  const refresh = async () => {
    setRefreshing(true);
    try {
      await Promise.all(
        [
          "github-info",
          "github-review-threads",
          "github-changed-files",
          "github-pr-diff",
          "session-pull-requests",
        ].map((key) => queryClient.invalidateQueries({ queryKey: [key, conversationId] })),
      );
    } finally {
      setRefreshing(false);
    }
  };
  const refreshIcon = (
    <RefreshCwIcon className={cn("size-3.5", refreshing && "animate-spin")} aria-hidden="true" />
  );
  const refreshButton = (
    <Button size="sm" variant="ghost" disabled={refreshing} onClick={refresh}>
      {refreshIcon}
      Refresh
    </Button>
  );
  // Servers that store PRs decide which PRs the session links, so the controls
  // survive a selection switch and the runner and host being offline. Live info
  // then only supplies the selected PR's details.
  const storesPrs = storesSessionPullRequests(useServerInfo());
  const stored = useSessionPullRequests(conversationId, info.data);
  const hostOffline = useHostOffline(conversationId, info);
  const sandboxAsleep = useWorkspaceAsleep(conversationId);
  // Linking verifies the PR with the host's provider account.
  const linkNeedsHost = storesPrs && hostOffline;
  const [knownAssociations, setKnownAssociations] = useState<{
    sessionId: string;
    data: Associations;
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
  const live =
    info.data ??
    (knownAssociations?.sessionId === conversationId ? knownAssociations.data : undefined);
  // A host that doesn't track PRs shows only its branch PR.
  const linked =
    storesPrs && (!live || live.tracking_available === true)
      ? storedListToShow(stored.data, live !== undefined)
      : undefined;
  const associations: Associations | undefined =
    linked === undefined
      ? live
      : {
          prs: linked,
          tracking_available: live?.tracking_available ?? true,
          selected_pr_url: linked.some((pr) => pr.url === live?.selected_pr_url)
            ? live?.selected_pr_url
            : linked[0]?.url,
          provider: live?.provider ?? linked[0]?.provider,
          provider_display: live?.provider_display ?? linked[0]?.provider_display,
        };
  // Offline before the server imported the session's PRs: there may be some it can't show.
  const notSynced = storesPrs && hostOffline && stored.data?.synced === false;
  const panelState = derivePullRequestPanelState(info);
  // No provider is known without a payload, or in a non-git workspace (older hosts omit it).
  const copy = gitProviderCopy(
    associations && panelState.kind !== "not-a-git-repo" ? associations.provider : null,
    undefined,
    associations?.provider_display,
  );
  const update = useUpdateSessionPr(conversationId);
  const unlink = useUnlinkSessionPr(conversationId);
  useEffect(() => {
    if (linked !== undefined && selected && !linked.some((pr) => pr.url === selected)) {
      changeSelection(associations?.selected_pr_url);
    } else if (!selected && associations?.selected_pr_url) {
      changeSelection(associations.selected_pr_url);
    }
  }, [selected, associations?.selected_pr_url, linked, changeSelection]);
  const prs = associations?.prs ?? [];
  useEffect(() => {
    if (prs.length === 1) updateView({ showingDetails: true });
  }, [updateView, prs.length]);
  const selectedPr = prs.find((pr) => pr.url === (selected ?? associations?.selected_pr_url));
  const detailsInfo =
    linked?.length === 0 && info.data
      ? { ...info, data: { ...info.data, pr: null, selected_pr_url: undefined } }
      : info;
  const linkInEmptyState =
    prs.length === 0 && derivePullRequestPanelState(detailsInfo).kind === "no-pr";
  const unlinkPr = (prUrl: string) =>
    unlink.mutate(prUrl, {
      onSuccess: (remaining) => {
        if (prUrl === selected) changeSelection(remaining.prs[0]?.url);
      },
    });
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
            if (linkNeedsHost) return;
            update.mutate(
              { url, action: "attach" },
              {
                onSuccess: async (data) => {
                  if (storesPrs && data.prs) {
                    const key = sessionPullRequestsKey(conversationId);
                    await queryClient.cancelQueries({ queryKey: key });
                    queryClient.setQueryData<StoredPullRequests>(key, (previous) => ({
                      prs: data.prs!.map((pr) => ({
                        ...previous?.prs.find((storedPr) => storedPr.url === pr.url),
                        ...pr,
                      })),
                      synced: previous?.synced ?? false,
                    }));
                  }
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
            autoFocus
            type="url"
            required
            disabled={linkNeedsHost}
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder={copy.prUrlPlaceholder}
            className="flex-1 focus-visible:ring-0"
          />
          <Button type="submit" disabled={update.isPending || linkNeedsHost}>
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
  const headerActions = (
    <TooltipProvider delayDuration={0}>
      <IconButton label="Refresh" disabled={refreshing} onClick={refresh}>
        {refreshIcon}
      </IconButton>
      {showTrackingControls && (
        <IconButton
          label={linkNeedsHost ? "Link a PR (needs the host online)" : "Link a PR"}
          disabled={linkNeedsHost}
          onClick={() => setLinking(!linking)}
        >
          <PlusIcon className="size-3.5" aria-hidden="true" />
        </IconButton>
      )}
      {showTrackingControls && selectedPr && showingDetails && (
        <IconButton
          label="Unlink PR"
          disabled={unlink.isPending}
          onClick={() => unlinkPr(selectedPr.url)}
        >
          <Trash2Icon className="size-3.5" aria-hidden="true" />
        </IconButton>
      )}
    </TooltipProvider>
  );
  return (
    <div className="flex h-full min-h-0 flex-col">
      {mobileHeaderTarget?.actions && createPortal(headerActions, mobileHeaderTarget.actions)}
      {mobileHeaderTarget?.title &&
        showingDetails &&
        prs.length > 1 &&
        createPortal(
          <div data-mobile-panel-title>
            <Button
              variant="ghost"
              size="sm"
              className="-ml-2"
              aria-label={`All pull requests (${prs.length})`}
              onClick={showPullRequestList}
            >
              <ArrowLeftIcon className="size-3.5" /> All pull requests
            </Button>
          </div>,
          mobileHeaderTarget.title,
        )}
      {showTrackingControls ? (
        !inMobileDrawer && (
          <div className="flex h-11 shrink-0 items-center gap-2 border-b border-border px-2">
            {!inMobileDrawer && <PanelTitle showTitle={!showingDetails || prs.length <= 1} />}
            <div className="ml-auto flex min-w-0 flex-1 items-center gap-2">
              {prs.length > 1 && showingDetails && (
                <Button variant="ghost" size="sm" onClick={showPullRequestList}>
                  <ArrowLeftIcon className="size-3.5" /> All pull requests ({prs.length})
                </Button>
              )}
              <div className="ml-auto flex shrink-0 items-center gap-1">
                {!inMobileDrawer && headerActions}
              </div>
            </div>
          </div>
        )
      ) : !inMobileDrawer && panelState.kind !== "ready" ? (
        <div className="flex h-11 shrink-0 items-center justify-between gap-2 border-b border-border px-2">
          <PanelTitle showTitle />
          {refreshButton}
        </div>
      ) : null}
      {showTrackingControls && (linking || update.isError) && (
        <div className="shrink-0 border-b border-border p-2 [&>:first-child]:mt-0">
          {linkControls}
        </div>
      )}
      {unlink.isError && (
        <p role="alert" className="shrink-0 border-b border-border p-2 text-ui text-destructive">
          Couldn’t unlink: {unlink.error.message}
        </p>
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
        {prs.length > 1 && !showingDetails ? (
          <ul aria-label="Pull requests" className="h-full overflow-y-auto">
            {prs.map((pr) => (
              <PullRequestListRow
                key={pr.url}
                conversationId={conversationId}
                active={active}
                association={{
                  ...pr,
                  provider: pr.provider ?? associations?.provider ?? undefined,
                  provider_display:
                    pr.provider_display ?? associations?.provider_display ?? undefined,
                }}
                hostOffline={hostOffline}
                sandboxAsleep={sandboxAsleep}
                unlinkPending={unlink.isPending}
                onUnlink={associations?.tracking_available ? () => unlinkPr(pr.url) : undefined}
                onSelect={() => {
                  updateView({ selected: pr.url, showingDetails: true, subtab: "summary" });
                }}
              />
            ))}
          </ul>
        ) : (
          <PullRequestPanelDetails
            key={`${conversationId}:${selected ?? ""}`}
            conversationId={conversationId}
            info={detailsInfo}
            active={active}
            activeTab={activeTab}
            setActiveTab={setActiveTab}
            storedPr={storesPrs && hostOffline ? selectedPr : undefined}
            notSynced={notSynced && !selectedPr}
            showHeading={!inMobileDrawer && !showTrackingControls}
            refreshButton={!inMobileDrawer && !showTrackingControls ? refreshButton : undefined}
            emptyStateAction={
              associations?.tracking_available && linkInEmptyState ? (
                <div className="mt-2 w-full max-w-sm">
                  {linkNeedsHost ? (
                    <p role="status" className="text-ui text-muted-foreground">
                      {sandboxAsleep
                        ? "To link a PR, send a message to start the session’s sandbox."
                        : "To link a PR, please reconnect the session’s host."}
                    </p>
                  ) : (
                    <>
                      {!linking && <Button onClick={() => setLinking(true)}>Link a PR</Button>}
                      {linkControls}
                    </>
                  )}
                </div>
              ) : (
                openPrFallback
              )
            }
          />
        )}
      </div>
    </div>
  );
}

/**
 * Whether neither the runner nor the host can answer, so the panel falls back
 * to the server's stored PRs. Live info cached before they went away counts as
 * offline too, since its failed refetch would otherwise replace the PR.
 */
function useHostOffline(
  conversationId: string,
  info: Pick<ReturnType<typeof usePullRequestInfo>, "error">,
): boolean {
  const serveable = useWorkspaceServeable(conversationId);
  const sandboxAsleep = useWorkspaceAsleep(conversationId);
  return serveable === false || sandboxAsleep || info.error instanceof RunnerOfflineError;
}

function storedPullRequestHint(pr: PullRequestAssociation, sandboxAsleep: boolean): string {
  const lastSync = pr.state_checked_at
    ? `Last sync time: ${absoluteTime(pr.state_checked_at * 1000)}. `
    : "";
  return (
    lastSync +
    (sandboxAsleep
      ? "To load the PR details, send a message to start the session’s sandbox."
      : "To load the PR details, please reconnect the session’s host.")
  );
}

/**
 * The selected PR as the server last stored it, while the runner and host are
 * offline: its title, link and state. Everything else stays on the host, so the
 * panel says when it comes back.
 */
function StoredPullRequestDetails({
  pr,
  copy,
  sandboxAsleep,
  showHeading,
  refreshButton,
}: {
  pr: PullRequestAssociation;
  copy: GitProviderCopy;
  sandboxAsleep: boolean;
  showHeading: boolean;
  refreshButton?: React.ReactNode;
}) {
  const title = pr.title?.trim() || `${pr.repository} ${copy.prNumberPrefix}${pr.number}`;
  const href = /^https:\/\//.test(pr.url) ? pr.url : undefined;
  const label = (
    <>
      <span className="truncate">{title}</span>
      <span className="shrink-0 text-muted-foreground">
        {copy.prNumberPrefix}
        {pr.number}
      </span>
    </>
  );
  return (
    <TooltipProvider delayDuration={0}>
      <div className="flex h-full min-h-0 flex-col" data-testid="github-stored-pr">
        <div className="shrink-0 border-b border-border px-3 pt-2 pb-2">
          <div className="flex items-center gap-2">
            {showHeading && <PanelTitle />}
            <span className="block min-w-0 truncate text-xs text-muted-foreground">
              {pr.repository}
            </span>
            <div className="ml-auto shrink-0">{refreshButton}</div>
          </div>
          <div className="mt-1 flex flex-nowrap items-center gap-2">
            <PullRequestProviderIcon copy={copy} />
            {href ? (
              <a
                href={href}
                target="_blank"
                rel="noreferrer"
                className="group flex min-w-0 items-center gap-1 text-ui font-medium hover:underline"
              >
                {label}
                <ExternalLinkIcon className="size-3 shrink-0 text-muted-foreground" />
              </a>
            ) : (
              <span className="flex min-w-0 items-center gap-1 text-ui font-medium">{label}</span>
            )}
            {pr.state && <PullRequestStatus state={pr.state} isDraft={pr.is_draft === true} />}
          </div>
        </div>
        <p role="status" className="p-3 text-ui text-muted-foreground">
          {storedPullRequestHint(pr, sandboxAsleep)}
        </p>
      </div>
    </TooltipProvider>
  );
}

function PullRequestPanelDetails({
  active,
  conversationId,
  info,
  storedPr,
  notSynced,
  showHeading,
  refreshButton,
  emptyStateAction,
  activeTab,
  setActiveTab,
}: {
  conversationId: string;
  info: ReturnType<typeof usePullRequestInfo>;
  active: boolean;
  /** The selected PR as the server stored it, passed while the host is offline. */
  storedPr?: PullRequestAssociation;
  /** Offline, and the server hasn't imported the session's PRs yet. */
  notSynced?: boolean;
  showHeading: boolean;
  refreshButton?: React.ReactNode;
  emptyStateAction?: React.ReactNode;
  activeTab: PullRequestSubtab;
  setActiveTab: (value: PullRequestSubtab) => void;
}) {
  const baseRef = info.data?.base_ref ?? undefined;
  const prUrl = info.data?.selected_pr_url;
  const headSha = info.data?.pr?.head_sha;
  const baseSha = info.data?.pr?.base_sha;
  const revision = `${baseSha ?? ""}:${headSha ?? ""}`;
  const hasPr = !!info.data?.pr;
  const serveable = useWorkspaceServeable(conversationId);
  const changes = usePullRequestChangedFiles(conversationId, hasPr, prUrl, revision);
  const prDiff = usePullRequestDiff(conversationId, hasPr, prUrl, revision);
  const sandboxAsleep = useWorkspaceAsleep(conversationId);
  const canEdit = useCanEdit(conversationId);

  // Summary (checks + PR body), Comments, and Changes (the stacked diff).
  // Summary is the landing tab — like GitHub's PR page opening on the
  // Conversation view.
  const reviews = usePullRequestReviewThreads(
    conversationId,
    prUrl,
    info.data?.auth?.selected_account ?? info.data?.selected_account,
    revision,
    active &&
      activeTab === "changes" &&
      !sandboxAsleep &&
      serveable !== false &&
      !!info.data?.capabilities?.review_threads &&
      hasPr,
  );

  // Mutation responses seed the query cache raw, so normalize before reading.
  const normalized = info.data && normalizePullRequestInfo(info.data);
  const copy = gitProviderCopy(
    normalized?.provider ?? null,
    normalized?.auth.hint,
    normalized?.provider_display,
  );

  if (storedPr) {
    return (
      <StoredPullRequestDetails
        pr={storedPr}
        copy={gitProviderCopy(storedPr.provider, undefined, storedPr.provider_display)}
        sandboxAsleep={sandboxAsleep}
        showHeading={showHeading}
        refreshButton={refreshButton}
      />
    );
  }
  if (notSynced) {
    return (
      <p role="status" className="p-3 text-ui text-muted-foreground">
        {sandboxAsleep
          ? "This session’s PRs haven’t synced to the server yet. To load them, send a message to start the session’s sandbox."
          : "This session’s PRs haven’t synced to the server yet. To load them, please reconnect the session’s host."}
      </p>
    );
  }

  // ── Whole-panel states (before the header + stacked diff) ───────────────
  // One central switch: every non-`ready` kind returns its own whole-panel
  // state, so the diff below renders only when there's an open PR.
  const panelState = derivePullRequestPanelState({
    isLoading: info.isLoading,
    error: info.error,
    data: info.data,
    sandboxAsleep,
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
    case "sandbox-offline":
      return (
        <PanelMessage>
          <p>The sandbox is offline. Send a message in the chat to start it again.</p>
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
  const canPostComment = !!(canEdit && data.capabilities.comment_posting && data.selected_pr_url);

  return (
    <TooltipProvider delayDuration={0}>
      <Tabs
        value={activeTab}
        onValueChange={(v) => setActiveTab(v === "comments" || v === "changes" ? v : "summary")}
        componentId="github.panel.tabs"
        className="flex h-full min-h-0 flex-col gap-0"
      >
        {/* Header: repo + PR metadata + the Summary/Comments/Changes tabs. Refreshes on
            its own — via the git-activity SSE signal and the panel's CI poll.
            Padding lives on the title block (not the outer container) so the
            tabs align to the gutter and the underline row spans full width. */}
        <div className="shrink-0 border-b border-border pb-0.5">
          <div className="px-3 pt-2">
            <div className="flex items-center gap-2">
              {showHeading && <PanelTitle />}
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
              <div className="ml-auto shrink-0">{refreshButton}</div>
            </div>
            <div className="mt-1 flex flex-nowrap items-center gap-2">
              <PullRequestProviderIcon copy={copy} />
              <PullRequestTitle
                // A different PR starts fresh, so an open title edit can't outlive its PR.
                key={data.selected_pr_url ?? pr.url}
                conversationId={conversationId}
                prUrl={data.selected_pr_url ?? null}
                url={pr.url}
                title={pr.title}
                numberLabel={`${copy.prNumberPrefix}${pr.number}`}
                editable={canEdit && !!data.capabilities.details_editing}
              />
              <PullRequestStatus state={pr.state} isDraft={pr.is_draft === true} />
            </div>
          </div>
          {/* Tab bar (Summary | Comments | Changes); the diff controls live inside
            the Changes tab, and the Comments label carries the count. gap-0 +
            flex-none keep the labels content-sized (default flex-1 spreads them). */}
          <TabsList variant="line" aria-label="Pull request" className="h-auto gap-0 p-0">
            <TabsTrigger value="summary" className="flex-none border-0 px-3 leading-none">
              Summary
            </TabsTrigger>
            <TabsTrigger value="comments" className="flex-none border-0 px-3 leading-none">
              Comments{" "}
              {comments.length > 0 && (
                <span className="text-muted-foreground/70 tabular-nums">
                  {comments.length}
                  {pr.comments_partial ? "+" : ""}
                </span>
              )}
            </TabsTrigger>
            <TabsTrigger value="changes" className="flex-none border-0 px-3 leading-none">
              Changes
            </TabsTrigger>
          </TabsList>
        </div>

        {/* Summary: reviewers, CI checks, and the PR description. A size container,
            so the description editor can fit itself to the visible height (cqh). */}
        <TabsContent
          value="summary"
          className="min-h-0 flex-1 overflow-y-auto [container-type:size]"
        >
          <PullRequestSummaryTab
            reviewers={
              pr.reviewers !== undefined && (
                <PullRequestReviewers
                  conversationId={conversationId}
                  prUrl={data.selected_pr_url ?? null}
                  reviewers={pr.reviewers}
                  author={pr.author}
                  editable={canEdit && !!data.capabilities.reviewer_editing}
                />
              )
            }
            labels={
              pr.labels !== undefined &&
              data.selected_pr_url && (
                <PullRequestLabels
                  conversationId={conversationId}
                  prUrl={data.selected_pr_url}
                  labels={pr.labels}
                  editable={canEdit && !!data.capabilities.label_editing}
                />
              )
            }
            merge={
              canEdit &&
              data.capabilities.merging &&
              pr.state === "OPEN" &&
              pr.merge_state_status !== undefined &&
              data.selected_pr_url && (
                <PullRequestMerge
                  // A different PR starts fresh, so an open dialog can't outlive its PR.
                  key={data.selected_pr_url}
                  conversationId={conversationId}
                  prUrl={data.selected_pr_url}
                  pr={pr}
                />
              )
            }
            checks={checks}
            checksSupported={pr.checks_supported !== false}
            checksUnavailableHint={copy.checksUnavailableHint}
            description={
              <PullRequestDescription
                conversationId={conversationId}
                prUrl={data.selected_pr_url ?? null}
                body={pr.body}
                editable={canEdit && !!data.capabilities.details_editing}
                attachments={!!data.capabilities.attachment_uploads}
              />
            }
          />
        </TabsContent>

        <TabsContent value="comments" className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <div
            role="region"
            aria-label="PR comments"
            tabIndex={0}
            className="min-h-0 flex-1 overflow-y-auto"
          >
            <PullRequestCommentsTab
              comments={comments}
              hasComposer={canPostComment}
              commentsPartial={pr.comments_partial}
              events={pr.timeline_events}
              timelinePartial={pr.timeline_partial}
              prUrl={pr.url}
              providerLabel={copy.label}
            />
          </div>
          {canPostComment && data.selected_pr_url && (
            <PullRequestCommentComposer
              key={`${conversationId}:${data.selected_pr_url}`}
              conversationId={conversationId}
              prUrl={data.selected_pr_url}
              account={data.auth.selected_account}
            />
          )}
        </TabsContent>

        <PullRequestChangesTab
          conversationId={conversationId}
          data={data}
          pr={pr}
          prUrl={prUrl}
          canEdit={canEdit}
          selected={activeTab === "changes"}
          changes={changes}
          prDiff={prDiff}
          reviews={reviews}
        />
      </Tabs>
    </TooltipProvider>
  );
}
