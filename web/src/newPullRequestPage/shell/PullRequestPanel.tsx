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

import {
  invalidateSessionPullRequestReads,
  isSessionPullRequestRead,
  sessionPullRequestReads,
} from "@/lib/pullRequestRecord";
import { removalIncludes, removalPendingReason } from "@/pullRequestList/removalProgress";
import { listRemovalOf, unlinkPlan } from "@/pullRequestList/subAgentRemoval";
import { useOwnsSessionTree } from "@/pullRequestList/useOwnsSessionTree";
import { toast } from "sonner";
import { VisibleMacAccountsProvider } from "../../shell/MacWriteNotices";
import { draftsNotPlaced } from "@/pullRequestReviews/pendingReview";
import { PendingReviewNotice } from "@/pullRequestReviews/PendingReviewNotice";
import {
  usePendingReview,
  useThreadsWithPendingDrafts,
} from "@/pullRequestReviews/usePendingReview";
import { refreshSessionPullRequests } from "@/hooks/refreshSessionPullRequests";
import { createPortal } from "react-dom";
import { DIFF_PALETTE_CSS, DIFF_SYNTAX_THEME } from "@/lib/diffTheme";
import { useMobilePanelHeaderTarget } from "../../shell/MobilePanelDrawer";
import {
  PullRequestCommentMenu,
  PullRequestCommentPaletteCommands,
  useConversationCommentCard,
} from "@/pullRequestComments/PullRequestCommentActions";
import { CommentReactionsBar } from "@/pullRequestComments/CommentReactionsBar";
import { PullRequestCommentEditSurface } from "@/pullRequestComments/PullRequestCommentEditSurface";
import { PullRequestCommentRecoveries } from "@/pullRequestComments/PullRequestCommentRecoveries";
import { PullRequestCommentComposer } from "./PullRequestCommentComposer";
import {
  EarlierConversations,
  PullRequestChangesReview,
  type PullRequestChangesReviewProps,
} from "../../shell/PullRequestChangesReview";
import { PullRequestCommitReview } from "../../shell/PullRequestCommitReview";
import {
  type PullRequestPatchState,
  useReviewThreadGroups,
} from "../../shell/pullRequestReviewSource";
import { LIGHTBOX_GALLERY_PROPS } from "@/lib/lightboxGallery";
import {
  conversationRange,
  NewConversationButton,
  NewConversationComposer,
  SELECTION_OUTSIDE_ONE_HUNK,
} from "../../shell/PullRequestNewConversation";
import { PullRequestReviewThread, type ReviewThreadContext } from "./PullRequestReviewThread";
import { usePullRequestReviewThreads } from "@/newPullRequestPage/hooks/usePullRequestReviewThreads";
import {
  reviewThreadAnnotations,
  reviewSelection,
  type ReviewThread,
} from "@/lib/pullRequestReviewThreads";
import { useCallback, useEffect, useMemo, useRef, useState, useId } from "react";
import { skipToken, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeftIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  ChevronRightIcon,
  ChevronsDownUpIcon,
  ChevronsUpDownIcon,
  AlertCircleIcon,
  Columns2Icon,
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
  Trash2Icon,
  ServerOffIcon,
  SquarePlusIcon,
  TerminalIcon,
  XIcon,
  FolderIcon,
  FileDiffIcon,
  MessageSquareIcon,
  Rows2Icon,
  PanelLeftCloseIcon,
  FileMinusIcon,
  FilePlusIcon,
  FileSymlinkIcon,
  PanelLeftOpenIcon,
} from "lucide-react";
import { FileDiff } from "@pierre/diffs/react";
import type { FileDiffMetadata, DiffLineAnnotation, SelectedLineRange } from "@pierre/diffs";
import { useResizableColumn } from "@/hooks/useResizableColumn";
import { cn } from "@/lib/utils";
import { useLocalReviewNavigation } from "@/lib/localReviewContext";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  DEFAULT_PANEL_VIEW,
  type PullRequestNewTabTarget,
  type PullRequestPanelView,
  type PullRequestSubtab,
  pullRequestPageViewKey,
  viewForChosenPullRequest,
} from "../../shell/pullRequestPanelView";
import {
  linkNeedsHostReason,
  useLinkPullRequestAvailability,
} from "../../shell/linkPullRequestAvailability";
import { IconButton, PanelMessage, PanelStateMessage } from "../../shell/PullRequestPanelControls";
import { COMMIT_OPERATIONS, PullRequestCommitsTab } from "../../shell/PullRequestCommits";
import {
  type CommitFocusTarget,
  useCommitCommands,
} from "../../shell/pullRequestCommits/useCommitCommands";
import { type PendingFocus, usePendingFocus } from "@/hooks/usePendingFocus";
import {
  ownedChangedPaths,
  refreshMergeReadiness,
  useMergeReadiness,
  useOwnedFiles,
} from "@/hooks/useMergeReadiness";
import { gitHubFastTrackStatusKey, useGitHubFastTrack } from "@/hooks/useGitHubFastTrack";
import {
  type PullRequestCommitsSource,
  useCommitListReconciliation,
  useCommitsTabStatus,
  usePullRequestCommits,
} from "@/hooks/usePullRequestCommits";
import type { MergeReadinessView } from "@/lib/mergeReadiness";
import { pullRequestFreshness } from "@/lib/pullRequestFreshness";
import { PULL_REQUEST_LIST_POLL_MS } from "@/lib/pullRequestPolling";
import { pullRequestFastTrackAccount } from "@/lib/githubFastTrack";
import {
  canonicalPullRequestUrl,
  githubPullRequestLocation,
  pullRequestKey,
} from "@/lib/pullRequestReference";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import {
  MarkdownCompletionProvider,
  type MarkdownCompletionSource,
  pullRequestParticipants,
} from "@/markdownEditor/markdownCompletionContext";
import { MessageResponse } from "@/components/ai-elements/message";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useCanChangePullRequests, usePullRequestChangeAccess } from "@/hooks/usePermissions";
import { PullRequestChangesHereContext } from "@/pullRequestPage/pullRequestChangesHere";
import { PullRequestOwnerOnlyNotice } from "@/newPullRequestPage/shell/PullRequestOwnerOnlyNotice";
import { useWorkspaceAsleep } from "@/hooks/useWorkspaceChangedFiles";
import { RunnerOfflineError } from "@/lib/workspaceReadErrors";
import {
  storedListToShow,
  storesSessionPullRequests,
  useSessionPullRequests,
  useSessionRemovalProgress,
  useSessionUnlinkPending,
  useUnlinkSessionPr,
  useUnlinkSessionPrs,
  useUnlinkSessionTree,
} from "@/newPullRequestPage/hooks/useSessionPullRequests";
import { useSessionHostOffline } from "@/hooks/useAttachSessionPullRequest";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  LINK_PULL_REQUEST_LABEL,
  openPullRequestAttach,
  usePullRequestAttachStore,
} from "@/store/pullRequestAttach";
import { readFileViewPreferences, writeFileViewPreferences } from "@/lib/fileViewPreferences";
import { type PullRequestTimingMark, usePullRequestTiming } from "@/hooks/usePullRequestTiming";
import { isElectronShell, isMacElectronShell } from "@/lib/nativeBridge";
import {
  accountCheckLabel,
  githubRecovery,
  githubRecoveryCopy,
  type RecoverySurface,
  unsupportedRemoteRecovery,
} from "@/lib/codeHostRecovery";
import { gitProviderCopy, type GitProviderCopy, hostedOnGitHub } from "@/lib/gitProviders";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import { parseHttpUrl } from "@/lib/openLinkInApp";
import {
  normalizePullRequestInfo,
  pullRequestPatchRevision,
  usePullRequestChangedFiles,
  usePullRequestDiff,
  usePullRequestInfo,
  pullRequestCommentControls,
  accountSwitchingOffered,
  mergeSectionShown,
  usePullRequestReviewThreadComposer,
  useSetPullRequestPreference,
  type NormalizedPullRequestInfo,
  type PullRequestAssociation,
  type PullRequestChangedFile,
  type PullRequestComment,
  type PullRequestInfo,
  type PullRequestTimelineEvent,
  fetchPullRequestFileContents,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { patchFileDiffs } from "@/lib/patchFileDiffs";
import { useWarmSharedDiffWorkers } from "@/components/diff-review/sharedDiffWorkers";
import { usePullRequestStackRead } from "@/pullRequestPage/usePullRequestStack";
import { PullRequestStackScope } from "@/pullRequestPage/PullRequestStack";
import { changedFilesTabCount } from "@/pullRequestPage/pullRequestSections";
import { DiffReviewBaseMenu } from "@/components/diff-review/DiffReviewBaseMenu";
import { useBaseMenuFocusHandoff } from "@/components/diff-review/useBaseMenuFocusHandoff";
import {
  reloadStackStatusCommand,
  stackDiffReviewBase,
  stackDiffStatusText,
} from "@/pullRequestPage/stackDiff/stackDiffCommands";
import {
  StackDiffPaletteCommands,
  StackStatusPaletteCommands,
} from "@/pullRequestPage/stackDiff/StackDiffPaletteCommands";
import { PullRequestStackOnlyReview } from "@/pullRequestPage/stackDiff/PullRequestStackOnlyReview";
import {
  type PullRequestStackDiff,
  usePullRequestStackDiff,
} from "@/pullRequestPage/stackDiff/usePullRequestStackDiff";
import {
  PullRequestStackLandingNotice,
  StackLandingPaletteCommands,
} from "@/pullRequestPage/stackLanding/PullRequestStackLanding";
import { stackLanding } from "@/pullRequestPage/stackLanding/stackLandingCommands";
import { nativeStackMergeHandoff } from "@/pullRequestPage/nativeStackMerge";
import { wantsLandingDiagnostic } from "@/lib/pullRequestStacks/stackLanding";
import {
  PULL_REQUEST_MARKDOWN_COMPONENTS,
  PullRequestDescription,
  PullRequestTitle,
} from "./PullRequestDetails";
import { PullRequestAuthorAvatar, pullRequestAuthorHues } from "../../shell/PullRequestAvatar";
import { PullRequestChecksSection } from "../../shell/PullRequestChecks";
import {
  MergeReadinessBadge,
  PullRequestMergeReadinessSection,
  hasMergeReadinessBadge,
} from "../../shell/PullRequestMergeReadiness";
import { listKeys } from "@/lib/listKeys";
import { PullRequestLabels } from "./PullRequestLabels";
import { withBranchOnStoredList } from "@/lib/branchPullRequest";
import { BranchPullRequestNotice } from "../../shell/BranchPullRequestNotice";
import { branchPullRequestQueryKey } from "@/hooks/useBranchPullRequest";
import { PullRequestList } from "../../shell/PullRequestList";
import { PullRequestActivity } from "@/pullRequestPage/PullRequestActivity";
import { useActivityRevealRequest } from "@/pullRequestPage/activityReveal";
import {
  PullRequestListPage,
  type PullRequestListFocus,
} from "@/pullRequestList/PullRequestListPage";
import {
  cachedSubAgentPullRequests,
  useSubAgentPullRequests,
} from "@/pullRequestList/useSubAgentPullRequests";
import { useIncludeSubAgentPullRequests } from "@/lib/pullRequestListPreferences";
import {
  PullRequestListToggle,
  type PullRequestListToggleState,
} from "../../shell/PullRequestListToggle";
import {
  type PullRequestOpenIntent,
  openByIntent,
  openGestureProps,
} from "../../shell/pullRequestOpenIntent";
import { usePullRequestInViewCommands } from "../../shell/pullRequestCommands/usePullRequestInViewCommands";
import { pullRequestFileCommands } from "../../shell/pullRequestFileCommands";
import { UnresolvedConversationPaletteCommands } from "../../shell/pullRequestCommands/UnresolvedConversationPaletteCommands";
import type { SectionCommandTarget } from "../../shell/pullRequestCommands/sectionCommands";
import { useSectionCommands } from "../../shell/pullRequestCommands/useSectionCommands";
import {
  AVAILABLE,
  isCommandAvailable,
  isCommandListed,
  type PaletteCommand,
  unavailableBecause,
  usePaletteCommands,
} from "@/store/paletteCommands";
import {
  isFinishedState,
  pullRequestListState,
  storedPullRequestHint,
  storedPullRequestLastSync,
  viewerLogin,
} from "../../shell/pullRequestListModel";
import {
  type PullRequestCommandFacts,
  REFRESHING_PULL_REQUEST_REASON,
  removeFinishedPullRequestsCommand,
} from "../../shell/pullRequestCommands/model";
import {
  PullRequestHeaderActions,
  PullRequestRefreshNotice,
} from "../../shell/PullRequestHeaderActions";
import { PullRequestLinkButton } from "../../shell/PullRequestLinkButton";
import { useLinkToSession } from "../../shell/pullRequestLinks/useLinkToSession";
import { usePullRequestRefresh } from "@/pullRequestPage/usePullRequestRefresh";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";
import {
  offeredEditCapabilities,
  usePullRequestEditsServed,
} from "@/newPullRequestPage/hooks/pullRequestEditOffers";
import { PullRequestRefreshOwnerContext } from "@/pullRequestPage/pullRequestRefreshOwner";
import { setPullRequestRefreshSurface } from "@/pullRequestPage/refreshPullRequest";
import { SandboxResumeNotice } from "@/components/SandboxResumeNotice";
import { useSandboxResume } from "@/hooks/useSandboxResume";
import { CONTEXT_LOAD_CSS } from "@/components/diff-review/contextLoadIndicator";
import { ContextLoadStatus } from "@/components/diff-review/ContextLoadStatus";
import {
  type ContextLoads,
  useContextLoads,
  useFileContextLoad,
} from "@/components/diff-review/useContextLoads";
import {
  clearSessionHostTransportRecovery,
  isHostTransportError,
} from "@/lib/hostTransportRecovery";
import { useRecentHostReturn } from "@/store/hostReturns";
import { usePullRequestListCounts } from "@/store/pullRequestListCounts";
import { PullRequestMerge } from "./PullRequestMerge";
import { PullRequestReviewers } from "./PullRequestReviewers";
import {
  type ReviewerPingPullRequest,
  ReviewerPingPaletteCommands,
} from "@/hooks/useReviewerPings";
import { PullRequestLabelPaletteCommands } from "../../shell/pullRequestLabelCommands";
import { TabStripScroller } from "../../shell/TabStripScroller";
import { reviewThreadsWanted as isReviewThreadsWanted } from "@/pullRequestPage/reviewThreadReads";
import type {
  PullRequestDetailsLayout,
  PullRequestDetailsState,
  PullRequestPreview,
} from "@/pullRequestPage/detailsLayout";
import {
  PullRequestHeaderSlot,
  PullRequestSummarySlot,
  createPullRequestPageLayout,
} from "@/pullRequestPage/pullRequestPageLayout";
import { focusSectionControl } from "@/pullRequestPage/PullRequestPageHeader";
import {
  PullRequestDestinationContext,
  PullRequestRowDestinationContext,
  useControlDestination,
} from "@/pullRequestPage/pullRequestDestination";
import { postedCommentKey } from "@/pullRequestPage/activityKeys";
import {
  type PullRequestAttachmentsAvailability,
  PullRequestAttachmentsProvider,
} from "@/pullRequestPage/PullRequestAttachmentsAvailability";
import { PullRequestPageScope } from "@/pullRequestPage/PullRequestPageScope";
import { pullRequestStatusDisplay } from "@/lib/pullRequestStatusDisplay";
import { PullRequestLifecycleBadge } from "@/shell/PullRequestLifecycleBadge";
import { usePullRequestsV2 } from "@/lib/workspaceSurfaces";
import { PullRequestReferenceScope } from "@/shell/pullRequestReferences/PullRequestReferenceScope";
import type { PullRequestSuggestion } from "@/lib/pullRequestSuggestions";
import { GITHUB_MARKDOWN_REHYPE_PLUGINS } from "@/lib/githubMarkdownPlugins";
import { useSessionFreePullRequestScope } from "@/hooks/sessionFreePullRequestScope";
import { SessionFreePullRequestState } from "./SessionFreePullRequestState";
import { usePullRequestViewServeable } from "@/hooks/usePullRequestViewServeable";

// Upstream's pull request page imports these from the panel.
export { refreshSessionPullRequests, useSessionHostOffline as useHostOffline };
export type { PullRequestSubtab };

/** ⌘K's row for the header's Refresh button, named for what it refreshes. */
export const REFRESH_PULL_REQUESTS_LABEL = "Refresh pull requests";
export const REFRESHING_PULL_REQUESTS_REASON = "Pull requests are refreshing…";

/** Centered muted message filling the panel — the shared loading/transient shell. */

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
function PanelTitle({ showTitle = false, count }: { showTitle?: boolean; count?: number }) {
  return (
    <h2
      className={cn(
        "flex shrink-0 items-baseline gap-1.5 font-medium text-ui",
        !showTitle && "sr-only",
      )}
    >
      Pull Requests
      {count !== undefined && (
        // The list announces its own size; the count is a visual summary.
        <span aria-hidden className="font-normal text-muted-foreground tabular-nums">
          {count}
        </span>
      )}
    </h2>
  );
}

/** Provider copy with its `backticked` commands set in the monospace font. */
function codeSpans(text: string): React.ReactNode {
  return text.split("`").map((part, i) => {
    // Static copy, so position keys stay stable across renders.
    const key = `${i}:${part}`;
    return i % 2 ? (
      <span key={key} className="font-mono whitespace-nowrap">
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
  const changeServed = usePullRequestMethodServed(conversationId, "setPullRequestAccount");
  if (!accountSwitchingOffered(info, changeServed)) return null;
  const accounts = info.auth.accounts ?? [];

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
        <p className="text-ui text-destructive">
          Couldn’t apply: {(setPref.error as Error).message}
        </p>
      )}
    </div>
  );
}

/** Where recovery copy points people: desktop reads with the computer's own account. */
function recoverySurface(): RecoverySurface {
  if (!isElectronShell()) return { kind: "web" };
  return { kind: "desktop", device: isMacElectronShell() ? "this Mac" : "this computer" };
}

/** The accounts the merge dialog offers, when the host merges as the one it sends. */
function mergeAccounts(info: NormalizedPullRequestInfo | undefined) {
  return info?.capabilities.account_switching && info.capabilities.merge_as_account
    ? info.auth
    : undefined;
}

/** The one thing the panel should show, derived from the info query. Every
 *  non-`ready` kind is a whole-panel state; `ready` renders the PR + diff. */
export type PullRequestPanelState =
  | { kind: "loading" }
  /** The host hasn't answered yet; the read is retrying. */
  | { kind: "host-reconnecting" }
  /** The host didn't answer any retry; `returned` when it came back online moments ago. */
  | { kind: "host-unreachable"; returned: boolean }
  /** The host came back moments ago and couldn't yet confirm the repository. */
  | { kind: "host-starting" }
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
  /** `updateFailed`: a read after the details loaded failed; they keep showing, and say so. */
  | { kind: "ready"; updateFailed?: string };

/** Central switch turning the PR info query into the panel's state.
 *
 * Order matters: transient states (loading/offline/error) first, then the
 * git-first availability reasons, then the provider layer (CLI → auth → repo
 * → PR). `ready` is reached only with an associated PR to render. */
export function derivePullRequestPanelState(info: {
  isLoading: boolean;
  error: unknown;
  data: PullRequestInfo | undefined;
  /**
   * What serves this info is the session's sandbox, and it's asleep (see
   * `useWorkspaceAsleep`). False for a PR this Mac reads itself, whose own
   * failures Resume can't fix.
   */
  sourceAsleep?: boolean;
  /** Why the current read failed while it retries. */
  failureReason?: unknown;
  /**
   * The session's host came back online moments ago, so it may still be
   * starting. False for a PR this Mac reads itself.
   */
  hostReturned?: boolean;
}): PullRequestPanelState {
  // Nothing can answer until the sandbox starts again, so the query is skipped
  // or 503s. Say so instead of spinning or "not available"; info cached before
  // it stopped still renders.
  if (info.sourceAsleep && info.data === undefined) return { kind: "sandbox-offline" };
  if (info.isLoading) {
    return isHostTransportError(info.failureReason)
      ? { kind: "host-reconnecting" }
      : { kind: "loading" };
  }
  // A refetch an asleep sandbox can't answer leaves the cached info showing.
  if (info.error && !info.sourceAsleep) {
    if (info.error instanceof RunnerOfflineError) return { kind: "runner-offline" };
    if (isHostTransportError(info.error)) {
      // Info loaded earlier keeps showing, with a notice, while the host can't answer.
      if (info.data === undefined)
        return { kind: "host-unreachable", returned: !!info.hostReturned };
    } else if (info.data === undefined) {
      return { kind: "error", message: (info.error as Error).message };
    }
    // A read that failed behind loaded details leaves them showing; a refused account clears its
    // data first, so it never lands here.
  }
  const updateFailed =
    info.error && !info.sourceAsleep && !isHostTransportError(info.error)
      ? (info.error as Error).message
      : undefined;
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
  if (!auth.authenticated || !repo?.name_with_owner) {
    // Right after the host returns, a failed or timed-out check is most likely the machine starting.
    const recovery = info.hostReturned ? githubRecovery(normalizePullRequestInfo(data)) : null;
    if (recovery?.kind === "repo-check-failed" || recovery?.kind === "check-timed-out") {
      return { kind: "host-starting" };
    }
    return { kind: "repo-unresolved" };
  }
  if (!pr && data.warnings?.length) return { kind: "unavailable" };
  if (!pr) return { kind: "no-pr", branch };
  return updateFailed ? { kind: "ready", updateFailed } : { kind: "ready" };
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

/** The PR's lifecycle badge, or for an open PR its merge status, which covers open and draft. */
function PullRequestStateBadge({
  state,
  isDraft,
  mergeReadiness,
}: {
  state: string | null | undefined;
  isDraft: boolean;
  mergeReadiness?: MergeReadinessView;
}) {
  if (state?.toUpperCase() === "OPEN" && hasMergeReadinessBadge(mergeReadiness)) {
    return <MergeReadinessBadge view={mergeReadiness} />;
  }
  return state ? <PullRequestLifecycleBadge state={state} isDraft={isDraft} /> : null;
}

/** Lines added and removed across the changed files, when every file reported them. */
export function changedLineTotals(
  files: readonly PullRequestChangedFile[],
): { added: number; removed: number } | undefined {
  let added = 0;
  let removed = 0;
  for (const file of files) {
    if (file.lines_added === null || file.lines_removed === null) return undefined;
    added += file.lines_added;
    removed += file.lines_removed;
  }
  return { added, removed };
}

/**
 * The PR header above the tabs: the repository link with the PR's state, the title, who
 * opened it, and its branches with the size of the change.
 */
function PullRequestHeader({
  copy,
  repository,
  number,
  url,
  state,
  isDraft,
  title,
  author,
  headRef,
  baseRef,
  files,
  showHeading,
  refreshButton,
  mergeReadiness,
}: {
  copy: GitProviderCopy;
  repository: string;
  number: number;
  url: string;
  state: string | null | undefined;
  isDraft: boolean;
  title: React.ReactNode;
  author?: string | null;
  headRef?: string | null;
  baseRef?: string | null;
  /** The complete changed-file list, or undefined while it loads or when it's partial. */
  files?: readonly PullRequestChangedFile[];
  showHeading: boolean;
  refreshButton?: React.ReactNode;
  /** Merge readiness from a registered source, shown beside the lifecycle badge for open PRs. */
  mergeReadiness?: MergeReadinessView;
}) {
  const name = `${repository} ${copy.prNumberPrefix}${number}`;
  const href = /^https:\/\//.test(url) ? url : undefined;
  const totals = files && changedLineTotals(files);
  return (
    <div className="space-y-1 px-3 pt-2 pb-1.5">
      <div className="flex items-center gap-2">
        {showHeading && <PanelTitle />}
        <PullRequestProviderIcon copy={copy} />
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noreferrer"
            aria-label={`Open ${name} on ${copy.label}`}
            title={`Open on ${copy.label}`}
            className="flex min-w-0 items-center gap-1 text-xs text-muted-foreground hover:text-foreground hover:underline"
          >
            <span className="truncate">{name}</span>
            <ExternalLinkIcon aria-hidden className="size-3 shrink-0" />
          </a>
        ) : (
          <span className="min-w-0 truncate text-xs text-muted-foreground">{name}</span>
        )}
        <PullRequestStateBadge state={state} isDraft={isDraft} mergeReadiness={mergeReadiness} />
        <div className="ml-auto shrink-0">{refreshButton}</div>
      </div>
      {title}
      {author && <p className="text-xs text-muted-foreground">Opened by {author}</p>}
      {(headRef || files) && (
        <p className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground">
          {headRef && (
            // Each branch keeps its own line before truncating, so a narrow pane shows both.
            <span className="flex max-w-full min-w-0 flex-wrap items-center gap-x-1 gap-y-0.5">
              <code
                title={headRef}
                className="max-w-full min-w-0 truncate rounded bg-muted px-1 py-px font-mono text-foreground"
              >
                {headRef}
              </code>
              <span className="flex max-w-full min-w-0 items-center gap-1">
                <span aria-label="into">→</span>
                <code title={baseRef ?? undefined} className="min-w-0 truncate font-mono">
                  {baseRef ?? "its base branch"}
                </code>
              </span>
            </span>
          )}
          {files && (
            <span className="flex shrink-0 items-center gap-1.5 tabular-nums">
              <span>{files.length === 1 ? "1 file" : `${files.length} files`}</span>
              {totals && (
                <>
                  <span className="text-status-green">+{totals.added}</span>
                  <span className="text-status-red">−{totals.removed}</span>
                </>
              )}
            </span>
          )}
        </p>
      )}
    </div>
  );
}

// GitHub-style status icons (a file glyph carrying the change kind) instead of
// bare A/M/D/R letters — quicker to recognize at a glance.
const STATUS_META: Record<
  PullRequestChangedFile["status"],
  { label: string; className: string; Icon: LucideIcon }
> = {
  created: { label: "Added", className: "text-success", Icon: FilePlusIcon },
  modified: {
    label: "Modified",
    className: "text-warning",
    Icon: FileDiffIcon,
  },
  deleted: { label: "Deleted", className: "text-destructive", Icon: FileMinusIcon },
  renamed: {
    label: "Renamed",
    className: "text-info",
    Icon: FileSymlinkIcon,
  },
};

/** Diffstat (+adds −removes) shared by the sidebar row and the section header. */
function DiffStat({ file }: { file: PullRequestChangedFile }) {
  if (file.lines_added === null && file.lines_removed === null) return null;
  return (
    <span className="shrink-0 font-mono text-xs tabular-nums">
      {file.lines_added !== null && <span className="text-status-green">+{file.lines_added}</span>}{" "}
      {file.lines_removed !== null && (
        <span className="text-status-red">−{file.lines_removed}</span>
      )}
    </span>
  );
}

/** The Changes toolbar's size of the change: the file count and lines added and removed. */
function ChangedFilesSummary({ files }: { files: readonly PullRequestChangedFile[] }) {
  const totals = changedLineTotals(files);
  return (
    <span className="ml-1 flex items-center gap-1.5 text-xs text-muted-foreground tabular-nums">
      <span>{files.length === 1 ? "1 file" : `${files.length} files`}</span>
      {totals && (
        <>
          <span className="text-status-green">+{totals.added}</span>
          <span className="text-status-red">−{totals.removed}</span>
        </>
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

/** Conversations and a draft can share the same diff annotation. */
interface ReviewAnnotation {
  threads: ReviewThread[];
  newThread?: boolean;
}
type DiffOptions = React.ComponentProps<typeof FileDiff<ReviewAnnotation>>["options"];

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
interface FileSectionProps {
  file: PullRequestChangedFile;
  /** Parsed per-file diff from the whole-PR patch; absent for binary/unparsed. */
  fileDiff: FileDiffMetadata | undefined;
  options: DiffOptions;
  registerRef: (path: string, el: HTMLElement | null) => void;
  /** Whether this file's diff is hidden. Lifted so the toolbar can drive all. */
  collapsed: boolean;
  onToggleCollapsed: () => void;
  threads: readonly ReviewThread[];
  threadContext: ReviewThreadContext;
  matchingRevision: boolean;
  /** Loads of the full text its unchanged lines expand from; absent where they can't expand. */
  contextLoads?: ContextLoads;
}

/** A PR file's diff section, with the file's review-conversation composer. */
function PullRequestFileSection(props: FileSectionProps) {
  const composer = usePullRequestReviewThreadComposer(
    props.threadContext.conversationId,
    props.threadContext.prUrl,
    props.file.path,
    props.threadContext.headSha ?? "",
    props.threadContext.baseSha ?? "",
  );
  return <FileSection {...props} composer={composer} />;
}

/**
 * One file's diff section. Without a composer it is read-only: no line selection,
 * no new conversations, and no saved review drafts.
 */
function FileSection({
  file,
  fileDiff,
  options,
  registerRef,
  collapsed,
  onToggleCollapsed,
  threads,
  threadContext,
  matchingRevision,
  composer,
  contextLoads,
}: FileSectionProps & {
  composer: ReturnType<typeof usePullRequestReviewThreadComposer> | null;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const contextLoad = useFileContextLoad(file.path, fileDiff, contextLoads, ref);
  const [seen, setSeen] = useState(false);
  const [showLargeDiff, setShowLargeDiff] = useState(false);
  const revision = `${threadContext.baseSha}:${threadContext.headSha}`;
  const [selection, setSelection] = useState<{
    revision: string;
    range: SelectedLineRange | null;
  }>();
  // Restore the committed range when a pointer gesture is canceled.
  const [selectionPreview, setSelectionPreview] = useState<typeof selection>();
  const selectedLines = selection?.revision === revision ? selection.range : null;
  const previewSelection = useCallback(
    (range: SelectedLineRange | null) => setSelectionPreview({ revision, range }),
    [revision],
  );
  const commitSelection = useCallback(
    (range: SelectedLineRange | null) => {
      setSelection({ revision, range });
      setSelectionPreview(undefined);
    },
    [revision],
  );
  const [selectionError, setSelectionError] = useState<string>();
  const startConversation = (range: SelectedLineRange) => {
    if (!composer || !threadContext.canCreate || !fileDiff || composer.draft.pending) return;
    const lines = reviewSelection(range, fileDiff);
    if (!lines) {
      setSelectionError(SELECTION_OUTSIDE_ONE_HUNK);
      return;
    }
    setSelectionError(undefined);
    composer.selectLines(lines);
  };

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
  const renderingDiff =
    !!fileDiff &&
    !pureRename &&
    seen &&
    (fileDiff.unifiedLineCount <= LARGE_DIFF_THRESHOLD || showLargeDiff);
  const anchor = composer?.draft.anchor;
  const showGutterUtility = !!composer && !!threadContext.canCreate && (!anchor || composer.stale);
  const showDraftInline = !!anchor && !composer?.stale && renderingDiff;
  const { annotations, unplaced } = useMemo(() => {
    const { annotations: threadAnnotations, unplaced: unplacedThreads } = reviewThreadAnnotations(
      threads,
      renderingDiff ? fileDiff : undefined,
      matchingRevision,
    );
    const inlineAnnotations: DiffLineAnnotation<ReviewAnnotation>[] = threadAnnotations.map(
      (annotation) => ({ ...annotation, metadata: { threads: annotation.metadata } }),
    );
    if (showDraftInline) {
      const side = anchor.side === "LEFT" ? "deletions" : "additions";
      const existing = inlineAnnotations.find(
        (item) => item.side === side && item.lineNumber === anchor.line,
      );
      if (existing) existing.metadata.newThread = true;
      else
        inlineAnnotations.push({
          side,
          lineNumber: anchor.line,
          metadata: { threads: [], newThread: true },
        });
    }
    return { annotations: inlineAnnotations, unplaced: unplacedThreads };
  }, [threads, renderingDiff, fileDiff, matchingRevision, anchor, showDraftInline]);
  const newConversation = anchor && composer && (
    <NewConversationComposer composer={composer} context={threadContext} />
  );

  return (
    <div ref={ref} data-github-file={file.path}>
      {/* One sticky header: the file's toggle, and beside it the Retry a failed expansion needs. */}
      <div className="sticky top-0 z-10 flex items-center border-b border-border bg-secondary dark:bg-muted">
        <button
          type="button"
          onClick={onToggleCollapsed}
          aria-expanded={!collapsed}
          title={renamedFrom ? `${renamedFrom} → ${file.path}` : file.path}
          className="group flex min-w-0 flex-1 cursor-pointer items-center gap-2 px-3 py-1.5 text-left"
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
          {threads.length > 0 && (
            <span
              className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground"
              title={`${threads.length} conversation${threads.length === 1 ? "" : "s"}`}
            >
              <MessageSquareIcon className="size-3" />
              {threads.length}
            </span>
          )}
        </button>
        {!collapsed && contextLoad.state && (
          <span className="shrink-0 pe-2 text-xs">
            <ContextLoadStatus
              path={file.path}
              state={contextLoad.state}
              onRetry={contextLoad.retry}
            />
          </span>
        )}
      </div>
      {!collapsed && (
        <>
          {selectionError && (
            <p role="alert" className="px-3 py-2 text-ui">
              {selectionError}
            </p>
          )}
          {!showDraftInline && newConversation && <div className="px-3">{newConversation}</div>}
        </>
      )}
      {!collapsed && unplaced.length > 0 && (
        <div className="px-3">
          {unplaced.map((thread) => (
            <PullRequestReviewThread
              key={thread.id}
              thread={thread}
              context={threadContext}
              fileDiff={matchingRevision ? fileDiff : undefined}
              showContext
            />
          ))}
        </div>
      )}
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
        <div className="overflow-x-auto" onPointerCancel={() => setSelectionPreview(undefined)}>
          <div
            style={{
              minWidth: annotations.length && options?.diffStyle === "split" ? 720 : undefined,
            }}
          >
            <FileDiff
              fileDiff={contextLoad.shownDiff ?? fileDiff}
              options={{
                ...options,
                onPostRender: contextLoad.mark,
                enableLineSelection: !!threadContext.canCreate,
                enableGutterUtility: showGutterUtility,
                onLineSelectionStart: previewSelection,
                onLineSelectionChange: previewSelection,
                onLineSelectionEnd: commitSelection,
              }}
              selectedLines={
                selectionPreview?.revision === revision ? selectionPreview.range : selectedLines
              }
              disableWorkerPool
              renderGutterUtility={
                showGutterUtility
                  ? (getHoveredLine) => (
                      <NewConversationButton
                        disabled={composer?.draft.pending}
                        onClick={() => {
                          const range = conversationRange(getHoveredLine(), selectedLines);
                          if (range) startConversation(range);
                        }}
                      />
                    )
                  : undefined
              }
              lineAnnotations={annotations}
              renderAnnotation={(annotation) => (
                <div className="min-w-0 px-3">
                  {annotation.metadata.newThread && newConversation}
                  {annotation.metadata.threads.map((thread) => (
                    <PullRequestReviewThread
                      key={thread.id}
                      thread={thread}
                      context={threadContext}
                      fileDiff={matchingRevision ? fileDiff : undefined}
                    />
                  ))}
                </div>
              )}
            />
          </div>
        </div>
      ) : (
        <div className="p-4 text-ui text-muted-foreground">
          No text diff available for this file.
        </div>
      )}
    </div>
  );
}

// ── Summary and Comments tabs ────────────────────────────────────────────
// Reviewer assignments, CI checks, the PR description, and conversation comments.

// The PR's description and its conversation comments (from `gh pr view`) are both
// rendered as GitHub-flavored markdown via the shared MessageResponse.
/** One comment card: an author + relative-time header over the markdown body. */
function PullRequestCommentCard({
  comment,
  context,
  pullRequestUrl,
  authorHue,
  byPullRequestAuthor,
}: {
  comment: PullRequestComment;
  context: ReviewThreadContext;
  pullRequestUrl: string;
  authorHue: number | undefined;
  /** The comment's author opened the pull request. */
  byPullRequestAuthor: boolean;
}) {
  const ts = comment.created_at ? Date.parse(comment.created_at) : NaN;
  const rel = relativeTime(ts);
  const card = useConversationCommentCard(comment, context, pullRequestUrl);
  const { actions } = card;
  return (
    <li
      ref={(element) => {
        card.cardRef.current = element;
      }}
      className="group/comment pull-request-comment-surface rounded-lg border border-border p-3"
      onFocus={actions.claim}
    >
      <div className="mb-1.5 flex items-center gap-2 text-xs">
        <PullRequestAuthorAvatar author={comment.author} hue={authorHue} />
        <span className="min-w-0 truncate font-semibold text-foreground">
          {comment.author ?? "unknown"}
        </span>
        {byPullRequestAuthor && (
          <Badge
            variant="outline"
            title="Opened this pull request"
            className="h-4 shrink-0 rounded-full px-1.5 text-[10px] font-normal text-muted-foreground"
          >
            Author
          </Badge>
        )}
        {rel && (
          <span
            className="shrink-0 text-muted-foreground/70"
            title={absoluteTime(ts)}
            onClick={actions.onTimeClick}
          >
            {rel}
          </span>
        )}
        <PullRequestCommentMenu
          actions={actions}
          className="ml-auto size-5 opacity-60 group-hover/comment:opacity-100 group-focus-within/comment:opacity-100 data-[state=open]:opacity-100"
        />
      </div>
      <PullRequestCommentEditSurface editing={card.editing} onOpenOnGitHub={card.openOnGitHub}>
        <div {...LIGHTBOX_GALLERY_PROPS} className="text-ui break-words">
          <MessageResponse
            allowGitHubAttachmentImages
            rehypePluginsAfterSanitizing={GITHUB_MARKDOWN_REHYPE_PLUGINS}
            components={PULL_REQUEST_MARKDOWN_COMPONENTS}
          >
            {comment.body}
          </MessageResponse>
        </div>
      </PullRequestCommentEditSurface>
      <CommentReactionsBar reactions={card.reactions} />
    </li>
  );
}

/** The Summary tab body: labels beside reviewers (stacked when narrow), then CI checks with the
 *  Merge button, then the PR description, with dividers between. */
function PullRequestSummaryTab({
  reviewers,
  labels,
  merge,
  checks,
  description,
}: {
  reviewers: React.ReactNode;
  labels: React.ReactNode;
  merge: React.ReactNode;
  checks: React.ReactNode;
  description: React.ReactNode;
}) {
  const showPeople = !!reviewers || !!labels;
  return (
    // Blocks carry their own padding so the dividers between them span the full width.
    <div>
      {showPeople && (
        <>
          <div className="flex @max-[480px]:flex-col">
            {labels && <div className="min-w-0 flex-1 p-3">{labels}</div>}
            {reviewers && (
              <div
                className={cn(
                  "min-w-0 flex-1 p-3",
                  labels && "border-l @max-[480px]:border-t @max-[480px]:border-l-0",
                )}
              >
                {reviewers}
              </div>
            )}
          </div>
          <Separator />
        </>
      )}
      <div className="space-y-3 p-3">
        {checks}
        {merge}
      </div>
      <Separator />
      {/* Extra bottom padding so the end of a long description can scroll clear
          of the very bottom edge, where it's awkward to read. */}
      <div className="p-3 pb-16">{description}</div>
    </div>
  );
}

/** One commit or force-push in the Comments tab's chronological feed. */
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

/** The Comments tab body: the PR's conversation comments and commit activity, oldest first.
 *  A partial list keeps what loaded and says the rest is on the provider. */
function PullRequestCommentsTab({
  comments,
  commentContext,
  commentsPartial,
  events,
  timelinePartial,
  providerLabel,
  pullRequestAuthor,
  pullRequestUrl,
  hasComposer,
}: {
  comments: PullRequestComment[];
  /** The session and PR the comments' actions belong to. */
  commentContext: ReviewThreadContext;
  hasComposer: boolean;
  commentsPartial?: boolean;
  events?: PullRequestTimelineEvent[];
  timelinePartial?: boolean;
  providerLabel: string;
  pullRequestAuthor: string | null;
  pullRequestUrl: string;
}) {
  const commentKeys = listKeys(comments, (c) => [c.url, c.created_at, c.author_id ?? c.author]);
  const authorHues = useMemo(
    () => pullRequestAuthorHues(comments.map((comment) => comment.author)),
    [comments],
  );
  const author = pullRequestAuthor?.toLowerCase();
  // Only an http(s) link leaves the app; anything else shows no link.
  const pullRequestHref = parseHttpUrl(pullRequestUrl)?.href;
  const entries = [
    ...comments.map((c, i) => ({
      created_at: c.created_at,
      element: (
        <PullRequestCommentCard
          key={commentKeys[i]}
          comment={c}
          context={commentContext}
          pullRequestUrl={pullRequestUrl}
          authorHue={authorHues.get(c.author?.toLowerCase() ?? "")}
          byPullRequestAuthor={!!author && c.author?.toLowerCase() === author}
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
      {entries.length === 0 && !commentsPartial && !timelinePartial ? (
        <div role="status" className="py-6 text-center">
          <p className="text-ui font-medium text-foreground">No comments yet</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {hasComposer
              ? "Start the conversation below."
              : "Comments on this pull request appear here."}
          </p>
        </div>
      ) : (
        <ul className="space-y-2">{entries.map((entry) => entry.element)}</ul>
      )}
      {commentsPartial && (
        <p role="status" className="text-xs text-muted-foreground">
          This pull request has more comments than Omnigent shows.{" "}
          <a
            href={pullRequestUrl}
            target="_blank"
            rel="noreferrer"
            className="text-foreground underline underline-offset-2"
          >
            See them on {providerLabel}
          </a>
        </p>
      )}
      {timelinePartial && (
        <p role="status" className="text-xs text-muted-foreground">
          Some commit and push history is unavailable.{" "}
          {pullRequestHref && (
            <a
              href={pullRequestHref}
              target="_blank"
              rel="noreferrer"
              className="text-foreground underline underline-offset-2"
            >
              View on {providerLabel}
            </a>
          )}
        </p>
      )}
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

const NO_PALETTE_COMMANDS: PaletteCommand[] = [];
const NO_REVIEW_THREADS: readonly ReviewThread[] = [];
const NO_THREADS_BY_FILE: ReadonlyMap<string, readonly ReviewThread[]> = new Map();

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
  onSelectFile: (path: string, intent: PullRequestOpenIntent) => void;
  collapsedDirs: ReadonlySet<string>;
  onToggleDir: (path: string) => void;
}) {
  if (node.type === "file") {
    const meta = STATUS_META[node.file.status];
    const StatusIcon = meta.Icon;
    return (
      <button
        type="button"
        {...openGestureProps((intent) => onSelectFile(node.file.path, intent))}
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

type Associations = Pick<
  PullRequestInfo,
  | "prs"
  | "tracking_available"
  | "selected_pr_url"
  | "provider"
  | "provider_display"
  | "branch_looked_up"
>;

const NO_LINKED_PULL_REQUESTS: readonly PullRequestAssociation[] = [];

/** The overview's row for a pull request a host reports only as its branch's, without a list. */
function overviewRowFor(pr: PullRequestInfo["pr"] | undefined): PullRequestAssociation | null {
  if (!pr?.url) return null;
  const location = githubPullRequestLocation(pr.url);
  return {
    url: pr.url,
    host: location?.host ?? "",
    repository: location?.repository ?? "",
    number: pr.number,
    title: pr.title,
    relationship: "created",
    state: pr.state,
  };
}

function samePanelView(left: PullRequestPanelView, right: PullRequestPanelView): boolean {
  return (
    left.selected === right.selected &&
    left.showingDetails === right.showingDetails &&
    left.subtab === right.subtab &&
    left.commit === right.commit &&
    left.file?.path === right.file?.path
  );
}

/**
 * How a tab in the tabs sidebar, or the Pull Requests page's detail, uses the panel: the overview tab is only the list,
 * and choosing a row opens that pull request's own tab; a pull request's tab is only
 * that pull request, with a way back to all of them. Neither ever switches itself.
 */
export type PullRequestTabsMode =
  | {
      kind: "overview";
      /**
       * `background`: behind the overview, as a browser's ⌘-click opens a link; `foreground`:
       * in its own tab in front, as ⇧⌘-click does. `linkedCount`: how many PRs the list shows
       * for the session, live or stored, or its branch's alone.
       */
      onOpenPullRequest: (
        url: string,
        options?: { background?: boolean; foreground?: true; linkedCount?: number },
      ) => void;
    }
  | {
      kind: "pull";
      /** The way back to all of them; unset where there's no list to go back to, as in a page's sidebar. */
      onShowAllPullRequests?: () => void;
      /** Set where the tab can show the list beside this pull request. */
      listToggle?: PullRequestListToggleState;
      /**
       * One commit or file of this pull request in its own tab; `background` keeps this tab in
       * front. Unset where there are no tabs, as on the Pull Requests page: nothing offers it.
       */
      onOpenInNewTab?: (target: PullRequestNewTabTarget, options: { background: boolean }) => void;
      /** Set where the host's own Refresh also refreshes this pull request, so it offers none. */
      refreshedByHost?: boolean;
      /**
       * False on the Pull Requests page: the PR shows whatever the session reading it links,
       * and offers no Remove from or Link to that session.
       */
      managesSessionLinks?: boolean;
      /** What the page's list knows of the shown PR, laid out in its place while it reads. */
      preview?: PullRequestPreview;
    };

/** Publishes how many pull requests the overview lists, for its tab's count pill. */
function usePublishedListCount(sessionId: string, count: number | undefined) {
  const publish = usePullRequestListCounts((state) => state.publish);
  const forget = usePullRequestListCounts((state) => state.forget);
  useEffect(() => {
    if (count !== undefined) publish(sessionId, count);
  }, [publish, sessionId, count]);
  const lists = count !== undefined;
  useEffect(() => {
    if (!lists) return;
    return () => forget(sessionId);
  }, [forget, sessionId, lists]);
}

export function PullRequestPanel({
  conversationId,
  active = true,
  view: controlledView,
  onViewChange,
  tabsMode,
  destination: destinationProp,
  rowDestination,
}: {
  conversationId: string;
  /** False while the panel is mounted but hidden (collapsed rail); pauses the poll. */
  active?: boolean;
  /** The id a list outside the panel aims its rows' requests at; its own otherwise. */
  destination?: string;
  /** Where its own list's rows open their PRs, when that's another panel; itself otherwise. */
  rowDestination?: string;
  /**
   * The tabs sidebar owns which pull request and sub-view show, as the tab's
   * place; the panel reports every change instead of keeping its own.
   */
  view?: PullRequestPanelView;
  /** `replace` marks a correction the panel made itself, not a step the user took. */
  onViewChange?: (view: PullRequestPanelView, options: { replace: boolean }) => void;
  /** Set by the tabs sidebar only; Classic and the mobile drawer keep switching in place. */
  tabsMode?: PullRequestTabsMode;
}) {
  const mobileHeaderTarget = useMobilePanelHeaderTarget();
  const inMobileDrawer = mobileHeaderTarget !== undefined;
  // Pull Requests v2's new page fills the details' slots, in the sidebar and the phone drawer alike.
  const pageV2 = usePullRequestsV2();
  // The new page's list replaces the classic one with it.
  const PullRequestListLayout = pageV2 ? PullRequestListPage : PullRequestList;
  const queryClient = useQueryClient();
  const viewKey = pullRequestPageViewKey(conversationId);
  const { data: cachedView = DEFAULT_PANEL_VIEW } = useQuery<PullRequestPanelView>({
    queryKey: viewKey,
    queryFn: skipToken,
    initialData: DEFAULT_PANEL_VIEW,
    gcTime: 30 * 60_000,
  });
  // Keep interactions synchronous; the shared entry restores the view on navigation.
  const [localView, setLocalView] = useState({ conversationId, view: cachedView });
  // Another surface can choose what the open panel shows by writing the shared entry.
  const [seenCachedView, setSeenCachedView] = useState(cachedView);
  if (localView.conversationId !== conversationId) {
    setLocalView({ conversationId, view: cachedView });
    setSeenCachedView(cachedView);
  } else if (cachedView !== seenCachedView) {
    setSeenCachedView(cachedView);
    if (cachedView !== localView.view) setLocalView({ conversationId, view: cachedView });
  }
  const view =
    controlledView ?? (localView.conversationId === conversationId ? localView.view : cachedView);
  const selected = view.selected;
  const showingDetails = view.showingDetails;
  const activeTab = view.subtab;
  // Read through refs so updateView stays stable, as the uncontrolled panel's is.
  const controlledViewRef = useRef(controlledView);
  controlledViewRef.current = controlledView;
  const onViewChangeRef = useRef(onViewChange);
  onViewChangeRef.current = onViewChange;
  const tabsKind = tabsMode?.kind;
  const tabsKindRef = useRef(tabsKind);
  tabsKindRef.current = tabsKind;
  // Filled in once the session's pull requests load, below.
  const shownDetailsRef = useRef<{ singlePullRequest: boolean; shownUrl?: string }>({
    singlePullRequest: false,
  });
  const updateView = useCallback(
    (update: Partial<typeof view>, options: { replace?: boolean } = {}) => {
      const controlled = controlledViewRef.current;
      const report = onViewChangeRef.current;
      if (controlled && report) {
        // Several corrections can land before the owner re-renders; each builds on the last.
        // Report what shows: one PR's details show without the list, for the PR on screen.
        const next = { ...controlled, ...update };
        // A tab's mode decides between the list and details; elsewhere one PR skips the list.
        if (
          tabsKindRef.current === undefined &&
          !next.showingDetails &&
          shownDetailsRef.current.singlePullRequest
        ) {
          next.showingDetails = true;
        }
        if (!next.selected && next.showingDetails && shownDetailsRef.current.shownUrl) {
          next.selected = shownDetailsRef.current.shownUrl;
        }
        if (samePanelView(next, controlled)) return;
        controlledViewRef.current = next;
        report(next, { replace: options.replace ?? false });
        return;
      }
      const next = queryClient.setQueryData<typeof view>(
        pullRequestPageViewKey(conversationId),
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
    (next?: string, options?: { replace?: boolean }) =>
      updateView({ selected: next, subtab: "summary", commit: undefined }, options),
    [updateView],
  );
  const setActiveTab = (value: PullRequestSubtab) => updateView({ subtab: value });
  // A review comment sent from a PR's Changes opens that PR there, wherever the panel is.
  const localReviewTarget = useLocalReviewNavigation(conversationId);
  useEffect(() => {
    const prUrl = localReviewTarget?.anchor_metadata?.pr_url;
    if (prUrl) updateView({ selected: prUrl, showingDetails: true, subtab: "changes" });
  }, [localReviewTarget, updateView]);
  // Requests to open a control here; a section's request brings its Summary tab forward.
  const {
    value: destinationValue,
    waiting: waitingControl,
    serial: controlRequests,
  } = useControlDestination(destinationProp, selected, active);
  useEffect(() => {
    if (waitingControl !== "reviewers" && waitingControl !== "labels") return;
    if (activeTab !== "summary") setActiveTab("summary");
    // Only a new request moves the tab; the person may switch away from it after.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [waitingControl, controlRequests]);
  const chooseFile = useCallback(
    (path: string | undefined, options: { replace: boolean }) =>
      updateView({ file: path === undefined ? undefined : { path } }, options),
    [updateView],
  );
  // A tab's place names what shows, so a sub-view this host can't show is corrected, not kept.
  const correctUnavailableTab = useCallback(
    (shown: PullRequestSubtab) =>
      updateView({ subtab: shown, commit: undefined }, { replace: true }),
    [updateView],
  );
  const info = usePullRequestInfo(conversationId, {
    poll: active ? (selected && !showingDetails ? "list" : true) : false,
    prUrl: selected,
    findBranch: true,
  });
  // Back to the list puts focus on the pull request it left, once the list's rows are there.
  const listFocus = useRef<PullRequestListFocus>(null);
  const focusListRowOnShow = useRef<string | null>(null);
  const showPullRequestList = () => {
    // One linked pull request has no list: back from an unlinked one shows that one.
    if (prs.length === 1) {
      focusDetailsOnOpen.current = true;
      updateView({
        selected: prs[0]!.url,
        showingDetails: true,
        subtab: "summary",
        commit: undefined,
      });
      return;
    }
    focusListRowOnShow.current = selected ?? null;
    // The section stays, for the next pull request chosen from the list.
    updateView({ showingDetails: false });
  };
  // Re-enabling the poll only restarts its timer; refresh stale info as the rail reopens.
  const wasActive = useRef(active);
  useEffect(() => {
    if (active && !wasActive.current) {
      void queryClient.refetchQueries({
        predicate: sessionPullRequestReads(conversationId, ["pr-page-github-info"]),
        stale: true,
      });
    }
    wasActive.current = active;
  }, [active, conversationId, queryClient]);
  const [refreshing, setRefreshing] = useState(false);
  const refresh = async () => {
    setRefreshing(true);
    try {
      await refreshSessionPullRequests(queryClient, conversationId);
    } finally {
      setRefreshing(false);
    }
  };
  const refreshIcon = (
    <RefreshCwIcon className={cn("size-3.5", refreshing && "animate-spin")} aria-hidden="true" />
  );
  // On the Pull Requests page, the page's Refresh refreshes the open PR too.
  const ownsRefresh = !(tabsMode?.kind === "pull" && tabsMode.refreshedByHost);
  const refreshButton = ownsRefresh && (
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
  const hostOffline = useSessionHostOffline(conversationId, info);
  const sandboxAsleep = useWorkspaceAsleep(conversationId);
  const sandboxResuming = useSandboxResume(conversationId).status === "resuming";
  // Linking verifies the PR with the host's provider account.
  const linkNeedsHost = storesPrs && hostOffline;
  const [knownAssociations, setKnownAssociations] = useState<{
    sessionId: string;
    data: Associations;
  }>();
  useEffect(() => {
    if (info.data) {
      const {
        prs,
        tracking_available,
        selected_pr_url,
        provider,
        provider_display,
        branch_looked_up,
      } = info.data;
      setKnownAssociations({
        sessionId: conversationId,
        data: {
          prs,
          tracking_available,
          selected_pr_url,
          provider,
          provider_display,
          branch_looked_up,
        },
      });
    }
  }, [conversationId, info.data]);
  // Switching the metadata query must not unmount the session's PR controls.
  const live =
    info.data ??
    (knownAssociations?.sessionId === conversationId ? knownAssociations.data : undefined);
  // A host that doesn't track PRs shows only its branch PR.
  const storedList =
    storesPrs && (!live || live.tracking_available === true)
      ? storedListToShow(stored.data, live !== undefined)
      : undefined;
  // The branch PR the host's lookup found shows first, in place of one the session info inferred.
  const linked = storedList && withBranchOnStoredList(storedList, live);
  // The page lists sub-agents' PRs beside the session's own, while the device's choice includes them.
  const includeSubAgents = useIncludeSubAgentPullRequests();
  // Sub-agent reads run while the list is on screen. A PR opened in a tab whose own links don't
  // include it reads the sub-agent tree once, so it can tell a sub-agent's PR from an unlinked one.
  const listSurfaceShown = tabsKind === "overview" || (tabsKind === undefined && !showingDetails);
  // A failed read of the session's own links still lets its sub-agents' known PRs show.
  const ownListFailed = storesPrs && stored.isError && linked === undefined;
  const joinedOwn = linked ?? (ownListFailed ? (live?.prs ?? NO_LINKED_PULL_REQUESTS) : undefined);
  const cachedSubAgents =
    joinedOwn !== undefined
      ? cachedSubAgentPullRequests(queryClient, conversationId, joinedOwn)
      : undefined;
  const openNeeded =
    pageV2 &&
    controlledView !== undefined &&
    tabsKind !== undefined &&
    tabsKind !== "overview" &&
    !!selected &&
    cachedSubAgents !== undefined &&
    !cachedSubAgents.prs.some(
      (pr) => canonicalPullRequestUrl(pr.url) === canonicalPullRequestUrl(selected),
    );
  const subAgentView = useSubAgentPullRequests(
    conversationId,
    joinedOwn,
    pageV2 && includeSubAgents && storesPrs && active && (listSurfaceShown || openNeeded),
    listSurfaceShown ? "list" : "open",
  );
  // The PRs this session's panel may show: its own links, and its sub-agents' on the new page.
  const openable = subAgentView?.merge.prs ?? cachedSubAgents?.prs ?? linked;
  // Resolving: the PR's sub-agent reads haven't answered, so it is neither linked nor unlinked yet.
  const openResolving = openNeeded && !!subAgentView && !subAgentView.settled;
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
  const markTiming = usePullRequestTiming({
    sessionId: conversationId,
    active,
    path: "host",
    stateKind: panelState.kind,
    cached: info.data !== undefined,
  });
  const unlink = useUnlinkSessionPr(conversationId);
  const unlinkMany = useUnlinkSessionPrs(conversationId);
  const unlinkAcrossTree = useUnlinkSessionTree(conversationId);
  // Read while the list shows: removal reaches sub-agents' links in a tree the user owns, and
  // only the owner may link a row the session doesn't.
  const ownsTree = useOwnsSessionTree(listSurfaceShown ? conversationId : null);
  const unlinkPending = useSessionUnlinkPending(conversationId);
  const removalProgress = useSessionRemovalProgress(conversationId);
  // The session the panel shows now, so a batch that ends after a switch moves no selection.
  const shownSessionRef = useRef<string | null>(conversationId);
  useEffect(() => {
    shownSessionRef.current = conversationId;
    return () => {
      shownSessionRef.current = null;
    };
  }, [conversationId]);
  const canEditSession = useCanChangePullRequests(conversationId);
  const managesSessionLinks = tabsMode?.kind !== "pull" || tabsMode.managesSessionLinks !== false;
  // A tab that asked for a pull request keeps it even when it isn't linked; it offers Link it.
  // Under Tabs the phone drawer stands in for that tab, so it keeps a chat link's PR while
  // showing it; going back to the list lets the list's own selection take over again.
  const controlled = controlledView !== undefined;
  const keepsRequested = controlled || (pageV2 && inMobileDrawer && showingDetails);
  const requestedUnlinked =
    managesSessionLinks &&
    keepsRequested &&
    tabsKind !== "overview" &&
    !openResolving &&
    openable !== undefined &&
    !!selected &&
    !openable.some((pr) => canonicalPullRequestUrl(pr.url) === canonicalPullRequestUrl(selected));
  // A PR this tab shows that the session doesn't link yet, or only infers from its branch,
  // offers Link beside its menu. The offer stays once made, so the button settles on Linked.
  const offersLinkNow =
    managesSessionLinks &&
    keepsRequested &&
    tabsKind !== "overview" &&
    !!selected &&
    (requestedUnlinked ||
      (joinedOwn?.some(
        (pr) =>
          pr.relationship === "inferred" && pullRequestKey(pr.url) === pullRequestKey(selected),
      ) ??
        false));
  const [linkOffer, setLinkOffer] = useState<string | null>(null);
  if (offersLinkNow && selected !== linkOffer) setLinkOffer(selected!);
  const linkOfferUrl =
    selected && linkOffer && pullRequestKey(linkOffer) === pullRequestKey(selected)
      ? linkOffer
      : null;
  const linkToSession = useLinkToSession(conversationId, linkOfferUrl);
  useEffect(() => {
    // A tab shows what its place names: the list, or its one pull request.
    if (requestedUnlinked || tabsKind !== undefined) return;
    // A tab's place carries the canonical URL; Classic compares the stored one as is.
    const sameUrl = (url: string) =>
      keepsRequested
        ? canonicalPullRequestUrl(url) === canonicalPullRequestUrl(selected!)
        : url === selected;
    if (
      !openResolving &&
      openable !== undefined &&
      selected &&
      !openable.some((pr) => sameUrl(pr.url))
    ) {
      changeSelection(associations?.selected_pr_url, { replace: true });
    } else if (!selected && associations?.selected_pr_url) {
      // A tab's place without a PR keeps its sub-view and commit for the PR shown.
      if (controlled) updateView({ selected: associations.selected_pr_url }, { replace: true });
      else changeSelection(associations.selected_pr_url, { replace: true });
    }
  }, [
    selected,
    associations?.selected_pr_url,
    openable,
    openResolving,
    changeSelection,
    updateView,
    requestedUnlinked,
    keepsRequested,
    controlled,
    tabsKind,
  ]);
  // Select a PR as soon as the shared link picker attaches it to this session.
  const lastAttached = usePullRequestAttachStore((state) => state.lastAttached);
  const handledAttach = useRef(lastAttached);
  // In the tabs sidebar the link's completion opens its own tab, so no mounted tab moves.
  useEffect(() => {
    if (lastAttached === handledAttach.current) return;
    handledAttach.current = lastAttached;
    if (tabsKind !== undefined) return;
    if (lastAttached?.sessionId === conversationId) changeSelection(lastAttached.url);
  }, [lastAttached, conversationId, changeSelection, tabsKind]);
  const prs = associations?.prs ?? NO_LINKED_PULL_REQUESTS;
  useEffect(() => {
    if (tabsKind === undefined && prs.length === 1) {
      updateView({ showingDetails: true }, { replace: true });
    }
  }, [updateView, prs.length, tabsKind]);
  const selectedPr = prs.find((pr) => pr.url === (selected ?? associations?.selected_pr_url));
  // A stack member shows here where this view shows any PR, or where it's linked to the session.
  const stackSelection = useRef({ prs, managesSessionLinks });
  stackSelection.current = { prs, managesSessionLinks };
  // Moving through a stack keeps the tab open, so Changes shows the next PR's changes.
  const selectStackPullRequest = useCallback(
    (url: string) => {
      const { prs: linkedPrs, managesSessionLinks: linkedOnly } = stackSelection.current;
      if (linkedOnly && !linkedPrs.some((pr) => pr.url === url)) return false;
      updateView({ selected: url, commit: undefined, file: undefined });
      return true;
    },
    [updateView],
  );
  shownDetailsRef.current = {
    singlePullRequest: prs.length <= 1,
    shownUrl: selected ?? associations?.selected_pr_url ?? info.data?.pr?.url,
  };
  // Only a host with no list at all (one that doesn't track PRs) stands for its branch PR;
  // an empty list from the server or a tracking host means none, whatever is still cached.
  const authoritativeList =
    linked !== undefined || live?.tracking_available === true || Array.isArray(live?.prs);
  // An empty list is the truth, so a pull request still cached from before isn't shown:
  // the panel's empty list, or the overview's, with any list the host or server answers.
  // A view that doesn't manage the session's links shows its PR whatever they say.
  const authoritativelyEmpty =
    managesSessionLinks &&
    !openResolving &&
    (openable?.length === 0 || (tabsKind === "overview" && authoritativeList && prs.length === 0));
  const detailsInfo =
    authoritativelyEmpty && info.data
      ? { ...info, data: { ...info.data, pr: null, selected_pr_url: undefined } }
      : info;
  const linkInEmptyState =
    !openResolving &&
    (openable ? openable.length === 0 : prs.length === 0) &&
    derivePullRequestPanelState(detailsInfo).kind === "no-pr";
  const unlinkPr = (prUrl: string) =>
    unlink.mutate(prUrl, {
      onSuccess: (remaining) => {
        if (prUrl === selected && tabsKind === undefined) {
          changeSelection(remaining.prs[0]?.url, { replace: true });
        }
      },
    });
  // A batch runs on its own, bound to this session, and finishes even if the panel switches.
  const unlinkPrs = (prUrls: string[]) => {
    // The list's own rule says which sessions' links go; a URL it no longer shows removes nothing.
    const plan = unlinkPlan(
      prUrls,
      listRemovalOf({
        shown: listSource.map((pr) => pr.url),
        sources: subAgentView?.merge.sources,
        viewedSessionId: conversationId,
        canEditViewed: canEditSession,
        ownsTree,
      }),
    );
    if (plan.kind === "none") return;
    if (plan.kind === "tree") {
      void unlinkAcrossTree(plan.removals).then(
        (remaining) => {
          if (shownSessionRef.current !== conversationId) return;
          if (selected && prUrls.includes(selected) && tabsKind === undefined) {
            changeSelection(remaining[0]?.url, { replace: true });
          }
        },
        // Its toast says what went wrong.
        () => {},
      );
      return;
    }
    if (plan.urls.length === 1) {
      unlinkPr(plan.urls[0]!);
      return;
    }
    void unlinkMany(plan.urls).then(
      (remaining) => {
        if (shownSessionRef.current !== conversationId) return;
        if (selected && prUrls.includes(selected) && tabsKind === undefined) {
          changeSelection(remaining[0]?.url, { replace: true });
        }
      },
      // Its toast says what went wrong.
      () => {},
    );
  };
  // A host that reports only its branch's pull request still gives the overview a row for it.
  const livePr = info.data?.pr;
  const branchRow = useMemo(
    () =>
      tabsKind === "overview" && prs.length === 0 && !authoritativeList
        ? overviewRowFor(livePr)
        : null,
    [tabsKind, prs.length, authoritativeList, livePr],
  );
  const listSource = useMemo(
    () => (branchRow ? [branchRow] : (subAgentView?.merge.prs ?? prs)),
    [branchRow, prs, subAgentView],
  );
  const listAssociations = useMemo(
    () =>
      listSource.map((pr) => ({
        ...pr,
        provider: pr.provider ?? associations?.provider ?? undefined,
        provider_display: pr.provider_display ?? associations?.provider_display ?? undefined,
      })),
    [listSource, associations?.provider, associations?.provider_display],
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
  // The overview tab lists even one pull request; Classic lists only several, until one is opened.
  const showsList =
    tabsKind === "overview"
      ? listSource.length > 0
      : tabsKind === undefined && (openable ? openable.length : prs.length) > 1 && !showingDetails;
  usePublishedListCount(conversationId, tabsKind === "overview" ? listSource.length : undefined);
  // Opening a PR from the list moves focus into its details, which replace the list
  // and whatever in it had focus: the selected tab, or the way back while they load.
  const focusDetailsOnOpen = useRef(false);
  const detailsRef = useRef<HTMLDivElement>(null);
  const backToListRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!showingDetails || !focusDetailsOnOpen.current) return;
    focusDetailsOnOpen.current = false;
    if (!focusSectionControl(detailsRef.current)) backToListRef.current?.focus();
  }, [showingDetails, selected]);
  // Waits for rows that load after the list mounts; opening details again cancels it.
  useEffect(() => {
    const url = focusListRowOnShow.current;
    if (url === null) return;
    if (showingDetails) {
      focusListRowOnShow.current = null;
      return;
    }
    if (listSource.length > 0 && listFocus.current?.focusRow(url)) {
      focusListRowOnShow.current = null;
    }
  }, [showingDetails, listSource]);
  const liveSelected = info.data?.pr?.url === selectedPr?.url ? info.data?.pr : undefined;
  // Removing needs PR tracking and edit access, as in the list.
  const removal: PullRequestCommandFacts["removal"] =
    managesSessionLinks && showTrackingControls && canEditSession && !!selectedPr
      ? unlinkPending
        ? "pending"
        : "available"
      : "hidden";
  // A PR the session doesn't link, as on the Pull Requests page, is known from its own read.
  const viewedPr =
    !selectedPr && info.data?.pr && info.data.pr.url === selected ? info.data.pr : undefined;
  const shownUrl = showingDetails ? (selectedPr?.url ?? viewedPr?.url ?? null) : null;
  const refreshOwner = useMemo(() => ({ id: Symbol("pull request panel") }), []);
  // What these details show, so a refresh can tell a reader the user left from one being replaced.
  useEffect(() => {
    setPullRequestRefreshSurface(refreshOwner, {
      prUrl: shownUrl,
      showingDetails,
      tab: activeTab ?? null,
    });
  }, [refreshOwner, shownUrl, showingDetails, activeTab]);
  useEffect(() => () => setPullRequestRefreshSurface(refreshOwner, null), [refreshOwner]);
  const pullRequestRefresh = usePullRequestRefresh(conversationId, shownUrl, refreshOwner);
  const inViewCommands = usePullRequestInViewCommands({
    conversationId,
    active: active && showingDetails,
    target:
      showingDetails && selectedPr
        ? {
            url: selectedPr.url,
            number: selectedPr.number,
            title: (liveSelected?.title ?? selectedPr.title)?.trim() || null,
            headRef: liveSelected?.head_ref ?? null,
            provider: selectedPr.provider ?? associations?.provider,
            providerDisplay: selectedPr.provider_display ?? associations?.provider_display,
          }
        : showingDetails && viewedPr
          ? {
              url: viewedPr.url,
              number: viewedPr.number,
              title: viewedPr.title?.trim() || null,
              headRef: viewedPr.head_ref ?? null,
              provider: associations?.provider,
              providerDisplay: associations?.provider_display,
            }
          : null,
    // Its other actions are the page's own rows'; offered here too, they'd show twice in ⌘K.
    // A session's own tab has no such rows, so it offers them all, with Link.
    only: selectedPr || linkOfferUrl ? undefined : ["refresh"],
    link: linkOfferUrl
      ? { run: linkToSession.link, availability: linkToSession.availability }
      : undefined,
    removal,
    removalPendingReason: removalPendingReason(
      removalProgress,
      removalIncludes(removalProgress, shownUrl ? [shownUrl] : []),
    ),
    onRemove: unlinkPr,
    refresh: shownUrl
      ? {
          run: pullRequestRefresh.refresh,
          availability: pullRequestRefresh.refreshing
            ? unavailableBecause(REFRESHING_PULL_REQUEST_REASON)
            : AVAILABLE,
        }
      : undefined,
  });
  // The session's merged and closed pull requests, whatever the list shows of them.
  // What each PR's own read says now, as the list's rows count them; a link's stored state only
  // where nothing has read it, so a PR reopened or closed since counts as it is now.
  const finishedUrls = prs
    .filter((pr) => {
      const shown = info.data?.pr?.url === pr.url ? info.data.pr : undefined;
      const read =
        shown ??
        queryClient
          .getQueriesData<PullRequestInfo>({
            predicate: ({ queryKey }) =>
              queryKey[0] === "pr-page-github-info" &&
              isSessionPullRequestRead(queryKey, conversationId, pr.url),
          })
          .map(([, data]) => data?.pr)
          .find((cached) => cached?.state);
      return isFinishedState(pullRequestListState(read?.state ? read : pr));
    })
    .map((pr) => pr.url);
  const removeFinished = removeFinishedPullRequestsCommand({
    finished: finishedUrls.length,
    removal: associations?.tracking_available ? removal : "hidden",
    removalPendingReason: removalPendingReason(
      removalProgress,
      removalIncludes(removalProgress, finishedUrls),
    ),
  });
  // The list offers it in ⌘K while it's on screen; a PR's own details offer it otherwise.
  const latestUnlinkFinished = useRef(() => unlinkPrs(finishedUrls));
  latestUnlinkFinished.current = () => unlinkPrs(finishedUrls);
  const removeFinishedPalette = useMemo<PaletteCommand[]>(
    () =>
      finishedUrls.length > 0 && isCommandListed(removeFinished.availability)
        ? [
            {
              id: "unlink-finished",
              label: removeFinished.paletteLabel,
              icon: Trash2Icon,
              keywords: [...removeFinished.keywords],
              availability: removeFinished.availability,
              run: () => latestUnlinkFinished.current(),
            },
          ]
        : [],
    [
      finishedUrls.length,
      removeFinished.availability,
      removeFinished.keywords,
      removeFinished.paletteLabel,
    ],
  );
  const cleanupPaletteInstance = useId();
  usePaletteCommands(
    `pull-request-details-cleanup:${conversationId}:${cleanupPaletteInstance}`,
    "Pull requests",
    active && showingDetails && !showsList ? removeFinishedPalette : NO_PALETTE_COMMANDS,
  );
  // ⌘K's refresh runs the header button's action, for the list or the PR on screen.
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const refreshPaletteCommands = useMemo<PaletteCommand[]>(
    () => [
      {
        id: "refresh-pull-requests",
        label: REFRESH_PULL_REQUESTS_LABEL,
        icon: RefreshCwIcon,
        keywords: ["pull requests", "pr", "reload", "update", "github"],
        availability: refreshing ? unavailableBecause(REFRESHING_PULL_REQUESTS_REASON) : AVAILABLE,
        run: () => void refreshRef.current(),
      },
    ],
    [refreshing],
  );
  // Each mounted panel owns its rows: a hidden tab's empty list never clears the shown one's.
  const paletteInstance = useId();
  usePaletteCommands(
    `pull-request-panel:${conversationId}:${paletteInstance}`,
    "Pull requests",
    active && ownsRefresh ? refreshPaletteCommands : [],
  );
  // Access comes first: without it, whether the host is online doesn't matter.
  const linkAccess = useLinkPullRequestAvailability(conversationId);
  const linkBlockedReason = linkAccess.available ? undefined : linkAccess.reason;
  const detailsStoredPr = storesPrs && hostOffline ? selectedPr : undefined;
  // In a PR's tab, the new page's header carries the way back and the actions while it's on
  // screen, which it reports itself; every other state keeps the panel's own row.
  const [pageHeaderShown, setPageHeaderShown] = useState(false);
  const pageHeaderOwnsBack = pageV2 && tabsMode?.kind === "pull" && pageHeaderShown;
  const headerActions = (
    <TooltipProvider delayDuration={0}>
      {ownsRefresh && (
        <IconButton label="Refresh" disabled={refreshing} onClick={refresh}>
          {refreshIcon}
        </IconButton>
      )}
      {/* A PR's own header leaves linking to the list beside it and the overview. */}
      {showTrackingControls && tabsMode?.kind !== "pull" && (
        <IconButton
          label={
            linkBlockedReason
              ? `${LINK_PULL_REQUEST_LABEL}: ${linkBlockedReason}`
              : linkNeedsHost
                ? `${LINK_PULL_REQUEST_LABEL} (needs the host online)`
                : LINK_PULL_REQUEST_LABEL
          }
          disabled={linkBlockedReason !== undefined || linkNeedsHost}
          onClick={() => openPullRequestAttach(conversationId)}
        >
          <PlusIcon className="size-3.5" aria-hidden="true" />
        </IconButton>
      )}
      {inViewCommands.remove && (
        <IconButton
          label={inViewCommands.remove.command.menuLabel}
          disabled={!isCommandAvailable(inViewCommands.remove.command.availability)}
          onClick={inViewCommands.remove.run}
        >
          <XIcon className="size-3.5" aria-hidden="true" />
        </IconButton>
      )}
    </TooltipProvider>
  );
  const showAllPullRequests =
    tabsMode?.kind === "pull" ? tabsMode.onShowAllPullRequests : undefined;
  // The new page's list draws one header line with the panel's actions in it; the phone
  // drawer's own header already carries its title and actions.
  const listOwnsHeader = pageV2 && showsList && tabsMode?.kind !== "pull" && !inMobileDrawer;
  // Rebuilt each render, so the header's actions are always the ones the panel shows now.
  const pageLayout = pageV2
    ? createPullRequestPageLayout({
        onShowAllPullRequests: showAllPullRequests,
        listToggle: tabsMode?.kind === "pull" ? tabsMode.listToggle : undefined,
        onHeaderShown: setPageHeaderShown,
        // A PR's own header keeps its refresh, links and removals in one menu, as ⌘K offers them.
        actions:
          tabsMode?.kind === "pull" ? (
            <TooltipProvider delayDuration={0}>
              {linkOfferUrl && <PullRequestLinkButton key={linkOfferUrl} link={linkToSession} />}
              <PullRequestHeaderActions
                commands={inViewCommands.all}
                refreshing={pullRequestRefresh.refreshing}
                removeFinished={{
                  menuLabel: removeFinished.menuLabel,
                  availability: removeFinished.availability,
                  run: () => unlinkPrs(finishedUrls),
                }}
              />
            </TooltipProvider>
          ) : undefined,
        notice:
          tabsMode?.kind === "pull" ? (
            <PullRequestRefreshNotice
              failure={pullRequestRefresh.failure}
              refreshing={pullRequestRefresh.refreshing}
              onTryAgain={pullRequestRefresh.refresh}
            />
          ) : undefined,
      })
    : undefined;
  const panel = (
    // Outside the tabs sidebar, a two-finger swipe here steps the app's history.
    <div className="flex h-full min-h-0 flex-col" data-swipe-history="router">
      {mobileHeaderTarget?.actions && createPortal(headerActions, mobileHeaderTarget.actions)}
      {mobileHeaderTarget?.title &&
        tabsKind === undefined &&
        showingDetails &&
        (prs.length > 1 || (requestedUnlinked && prs.length > 0)) &&
        createPortal(
          <div data-mobile-panel-title>
            <Button
              variant="ghost"
              size="sm"
              className="-ml-2"
              ref={backToListRef}
              aria-label={`All pull requests (${prs.length})`}
              onClick={showPullRequestList}
            >
              <ArrowLeftIcon className="size-3.5" /> All pull requests
            </Button>
          </div>,
          mobileHeaderTarget.title,
        )}
      {pageHeaderOwnsBack || listOwnsHeader ? null : tabsMode?.kind === "pull" ? (
        // A pull request's tab always offers the way back to all of them, whatever loads.
        <div className="flex h-11 shrink-0 items-center gap-2 border-b border-border px-2">
          {tabsMode.listToggle && <PullRequestListToggle toggle={tabsMode.listToggle} />}
          {tabsMode.onShowAllPullRequests && (
            <Button
              ref={backToListRef}
              variant="ghost"
              size="sm"
              className="min-w-0"
              onClick={tabsMode.onShowAllPullRequests}
            >
              <ArrowLeftIcon className="size-3.5" aria-hidden="true" /> Pull requests
            </Button>
          )}
          <div className="ml-auto flex shrink-0 items-center gap-1">
            {showTrackingControls ? headerActions : panelState.kind !== "ready" && refreshButton}
          </div>
        </div>
      ) : showTrackingControls ? (
        !inMobileDrawer && (
          <div className="flex h-11 shrink-0 items-center gap-2 border-b border-border px-2">
            {!inMobileDrawer && (
              <PanelTitle
                showTitle={showsList || !showingDetails || prs.length <= 1}
                count={showsList && prs.length > 1 ? prs.length : undefined}
              />
            )}
            <div className="ml-auto flex min-w-0 flex-1 items-center gap-2">
              {tabsKind === undefined && prs.length > 1 && showingDetails && (
                <Button ref={backToListRef} variant="ghost" size="sm" onClick={showPullRequestList}>
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
      {unlink.isError && (
        <p role="alert" className="shrink-0 border-b border-border p-2 text-ui text-destructive">
          Couldn’t unlink: {unlink.error.message}
        </p>
      )}
      {ownListFailed && !live && showsList && (
        <p role="alert" className="shrink-0 border-b border-border p-2 text-ui text-destructive">
          Couldn’t load this session’s own pull requests: {stored.error.message} Showing the ones
          its sub-agents linked.
        </p>
      )}
      {panelState.kind === "ready" && panelState.updateFailed && !pullRequestRefresh.failure && (
        <p role="alert" className="shrink-0 border-b border-border p-2 text-ui text-destructive">
          Couldn’t update this pull request: {panelState.updateFailed} Showing what was loaded
          before.
        </p>
      )}
      {info.data !== undefined && isHostTransportError(info.error ?? info.failureReason) && (
        <p
          role="status"
          className="shrink-0 border-b border-border p-2 text-ui text-muted-foreground"
        >
          Can’t reach this session’s machine right now. Showing what was loaded before.
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
      <BranchPullRequestNotice
        notice={info.data?.branch_discovery}
        onChanged={() =>
          void queryClient.invalidateQueries({
            queryKey: branchPullRequestQueryKey(conversationId),
          })
        }
      />
      <div ref={detailsRef} className="min-h-0 flex-1">
        {openResolving && selected ? (
          <PanelMessage>
            <Loader2Icon className="size-5 animate-spin" aria-hidden />
            Loading pull requests…
          </PanelMessage>
        ) : requestedUnlinked && selected ? (
          // Only the session's host reads it, and only once it's linked.
          <PanelMessage>
            <span className="font-medium text-foreground">
              This pull request isn&apos;t linked to this session
            </span>
            <span className="max-w-full truncate" title={selected}>
              {selected}
            </span>
            {canEditSession ? (
              <TooltipProvider delayDuration={0}>
                <PullRequestLinkButton link={linkToSession} appearance="notice" />
              </TooltipProvider>
            ) : (
              <span>You need edit access to link pull requests to this session.</span>
            )}
          </PanelMessage>
        ) : showsList ? (
          <PullRequestListLayout
            conversationId={conversationId}
            active={active}
            associations={listAssociations}
            sessionSources={subAgentView?.merge.sources}
            ownsSessionTree={ownsTree}
            subAgentPullRequests
            sessionSourcesTruncated={subAgentView?.truncated}
            hostOffline={hostOffline}
            sandboxAsleep={sandboxAsleep}
            selectedUrl={selected}
            unlinkPending={unlinkPending}
            removalProgress={removalProgress}
            onUnlink={associations?.tracking_available ? unlinkPrs : undefined}
            opensInNewTab={tabsMode?.kind === "overview"}
            headerActions={listOwnsHeader ? headerActions : undefined}
            // The phone drawer's header names the list; the new page's own heading would repeat it.
            {...(pageV2 ? { focusHandle: listFocus } : {})}
            {...(pageV2 && inMobileDrawer ? { showHeading: false } : {})}
            onSelect={(prUrl: string, intent: PullRequestOpenIntent) => {
              if (tabsMode?.kind === "overview") {
                tabsMode.onOpenPullRequest(prUrl, {
                  background: intent === "background",
                  ...(intent === "foreground" ? { foreground: true as const } : {}),
                  linkedCount: listSource.length,
                });
                return;
              }
              focusDetailsOnOpen.current = true;
              // On the section the reader was on, as moving through a stack keeps it.
              updateView(viewForChosenPullRequest(prUrl, activeTab));
            }}
          />
        ) : (
          <PullRequestPageScope
            enabled={pageV2}
            conversationId={conversationId}
            prUrl={selected}
            active={active}
            changesShown={active && activeTab === "changes"}
          >
            <PullRequestPanelDetails
              key={`${conversationId}:${selected ?? ""}`}
              requestedPrUrl={selected}
              onTimingMilestone={markTiming}
              layout={pageLayout}
              preview={
                tabsMode?.kind === "pull" &&
                tabsMode.preview &&
                selected &&
                pullRequestKey(tabsMode.preview.url) === pullRequestKey(selected)
                  ? tabsMode.preview
                  : undefined
              }
              conversationId={conversationId}
              info={detailsInfo}
              active={active}
              activeTab={activeTab}
              setActiveTab={setActiveTab}
              onTabUnavailable={correctUnavailableTab}
              requestedFile={controlled ? view.file : undefined}
              onFileChosen={controlled ? chooseFile : undefined}
              selectedCommit={view.commit}
              onSelectCommit={(commit) => updateView({ commit })}
              onOpenInNewTab={tabsMode?.kind === "pull" ? tabsMode.onOpenInNewTab : undefined}
              storedPr={detailsStoredPr}
              selectStackPullRequest={selectStackPullRequest}
              notSynced={notSynced && !selectedPr}
              showHeading={!inMobileDrawer && !showTrackingControls}
              refreshButton={!inMobileDrawer && !showTrackingControls ? refreshButton : undefined}
              emptyStateAction={
                managesSessionLinks && associations?.tracking_available && linkInEmptyState ? (
                  <div className="mt-2 w-full max-w-sm">
                    {linkBlockedReason ? (
                      <p role="status" className="text-ui text-muted-foreground">
                        {linkBlockedReason}
                      </p>
                    ) : linkNeedsHost ? (
                      <p role="status" className="text-ui text-muted-foreground">
                        {linkNeedsHostReason(sandboxAsleep, sandboxResuming)}
                      </p>
                    ) : (
                      <Button onClick={() => openPullRequestAttach(conversationId)}>
                        {LINK_PULL_REQUEST_LABEL}
                      </Button>
                    )}
                  </div>
                ) : (
                  openPrFallback
                )
              }
            />
          </PullRequestPageScope>
        )}
      </div>
    </div>
  );
  return (
    <PullRequestDestinationContext.Provider value={destinationValue}>
      <PullRequestRowDestinationContext.Provider value={rowDestination ?? destinationValue.id}>
        {/* This panel's refreshes reach its own parts, never another view's of the same PR. */}
        <PullRequestRefreshOwnerContext.Provider value={refreshOwner}>
          <PullRequestStackScope selectedUrl={selected ?? associations?.selected_pr_url ?? null}>
            {panel}
          </PullRequestStackScope>
        </PullRequestRefreshOwnerContext.Provider>
      </PullRequestRowDestinationContext.Provider>
    </PullRequestDestinationContext.Provider>
  );
}

/**
 * The selected PR as the server last stored it, while the runner and host are
 * offline: its title, link and state. Everything else stays on the host, so the
 * panel says when it comes back.
 */
function StoredPullRequestDetails({
  conversationId,
  pr,
  copy,
  sandboxAsleep,
  showHeading,
  refreshButton,
}: {
  conversationId: string;
  pr: PullRequestAssociation;
  copy: GitProviderCopy;
  sandboxAsleep: boolean;
  showHeading: boolean;
  refreshButton?: React.ReactNode;
}) {
  const title = pr.title?.trim() || `${pr.repository} ${copy.prNumberPrefix}${pr.number}`;
  return (
    <TooltipProvider delayDuration={0}>
      <div className="flex h-full min-h-0 flex-col" data-testid="github-stored-pr">
        <div className="shrink-0 border-b border-border pb-2">
          <PullRequestHeader
            copy={copy}
            repository={pr.repository}
            number={pr.number}
            url={pr.url}
            state={pr.state}
            isDraft={pr.is_draft === true}
            title={
              <h3 className="text-base leading-snug font-semibold break-words text-foreground">
                {title}
              </h3>
            }
            showHeading={showHeading}
            refreshButton={refreshButton}
          />
        </div>
        {sandboxAsleep ? (
          <div className="p-3">
            {pr.state_checked_at ? (
              <p className="text-ui text-muted-foreground">{storedPullRequestLastSync(pr)}</p>
            ) : null}
            <SandboxResumeNotice
              sessionId={conversationId}
              layout="inline"
              purpose="load the PR details"
              className="mt-1 px-0"
            />
          </div>
        ) : (
          <p role="status" className="p-3 text-ui text-muted-foreground">
            {storedPullRequestHint(pr, sandboxAsleep)}
          </p>
        )}
      </div>
    </TooltipProvider>
  );
}

/** A single commit's files, as the Changes tab's file sections without review threads. */
function CommitFileSections({
  files,
  filesByPath,
  options,
  context,
}: {
  files: PullRequestChangedFile[];
  filesByPath: ReadonlyMap<string, FileDiffMetadata>;
  options: DiffOptions;
  context: ReviewThreadContext;
}) {
  const [collapsedPaths, setCollapsedPaths] = useState<ReadonlySet<string>>(() => new Set());
  const registerRef = useCallback(() => {}, []);
  return files.map((file) => (
    <FileSection
      composer={null}
      key={file.path}
      file={file}
      fileDiff={filesByPath.get(file.path)}
      options={options}
      registerRef={registerRef}
      collapsed={collapsedPaths.has(file.path)}
      onToggleCollapsed={() =>
        setCollapsedPaths((previous) => {
          const next = new Set(previous);
          if (next.has(file.path)) next.delete(file.path);
          else next.add(file.path);
          return next;
        })
      }
      threads={[]}
      threadContext={context}
      matchingRevision={false}
    />
  ));
}

/** What every part of the details shares: tooltips, and whether attachments work here. */
function PullRequestDetailsProviders({
  changesHere,
  attachments,
  pullRequestUrl,
  references,
  completion,
  children,
}: {
  /** Whether the details may change the PR, on this Mac or through the session's machine. */
  changesHere: boolean;
  attachments: PullRequestAttachmentsAvailability;
  /** What the PR's Markdown editors complete `@` and `#` from. */
  completion: MarkdownCompletionSource | null;
  /** The PR the details show, which the PR references in them read against. */
  pullRequestUrl: string | null;
  /** What the details already read, which references to those PRs show without a lookup. */
  references: Omit<
    React.ComponentProps<typeof PullRequestReferenceScope>,
    "pullRequestUrl" | "children"
  >;
  children: React.ReactNode;
}) {
  return (
    <PullRequestChangesHereContext.Provider value={changesHere}>
      <TooltipProvider delayDuration={0}>
        {/* The accounts this PR's header names, so its pickers don't say them again. */}
        <VisibleMacAccountsProvider>
          <PullRequestAttachmentsProvider {...attachments}>
            <PullRequestReferenceScope pullRequestUrl={pullRequestUrl} {...references}>
              <MarkdownCompletionProvider source={completion}>
                {children}
              </MarkdownCompletionProvider>
            </PullRequestReferenceScope>
          </PullRequestAttachmentsProvider>
        </VisibleMacAccountsProvider>
      </TooltipProvider>
    </PullRequestChangesHereContext.Provider>
  );
}

const CANNOT_SELECT_IN_PLACE = () => false;

/** One pull request's details: its heading, tabs and their contents, from `info`. */
export function PullRequestPanelDetails({
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
  onTabUnavailable,
  requestedFile,
  onFileChosen,
  selectedCommit,
  onSelectCommit,
  onOpenInNewTab,
  onTimingMilestone,
  layout,
  preview,
  requestedPrUrl,
  selectStackPullRequest = CANNOT_SELECT_IN_PLACE,
}: {
  conversationId: string;
  info: ReturnType<typeof usePullRequestInfo>;
  /** The PR this view was opened for, known before its read answers. */
  requestedPrUrl?: string;
  /** A page layout's header and Overview, rendered inside these details' tabs. */
  layout?: PullRequestDetailsLayout;
  /** The shown PR as its list knows it, which a page layout lays out while the details read. */
  preview?: PullRequestPreview;
  /** Adds this view's header and diff to the panel's load timing. */
  onTimingMilestone?: PullRequestTimingMark;
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
  /** Called when the asked-for tab can't show here, such as Commits on an older host. */
  onTabUnavailable?: (shown: PullRequestSubtab) => void;
  /** A file a tab's place names in the Changes tab, revealed and its lines marked. */
  requestedFile?: PullRequestPanelView["file"];
  /** A file picked in the Changes tab, or none to show all changes; set where a tab owns the view. */
  onFileChosen?: (path: string | undefined, options: { replace: boolean }) => void;
  selectedCommit?: string;
  onSelectCommit: (sha: string | undefined) => void;
  /** Where commits and files can open in their own tab, as ⌘-click asks. */
  onOpenInNewTab?: (target: PullRequestNewTabTarget, options: { background: boolean }) => void;
  /** Show another PR of this one's stack in this view; false when it can't show there. */
  selectStackPullRequest?: (url: string) => boolean;
  /** Where another PR of the stack reads, to read it ahead there; this view's own by default. */
}) {
  const baseRef = info.data?.base_ref ?? undefined;
  const prUrl = info.data?.selected_pr_url;
  // The server's association carries the PR identity; `storedPr` is set only while offline.
  const headSha = info.data?.pr?.head_sha;
  const baseSha = info.data?.pr?.base_sha;
  const revision = pullRequestPatchRevision(info.data?.pr);
  const hasPr = !!info.data?.pr;
  const serveable = usePullRequestViewServeable(conversationId);
  const sessionFree = useSessionFreePullRequestScope(conversationId);
  const changes = usePullRequestChangedFiles(conversationId, hasPr, prUrl, revision);
  // Only the new page shows stacks; the classic panel keeps its description as it is.
  const {
    stack,
    waiting: stackWaiting,
    shown: readStack,
    loading: stackLoading,
  } = usePullRequestStackRead(layout ? (info.data?.pr ?? null) : null, selectStackPullRequest, {
    subtab: activeTab,
    // When its details were read: a comment or other change patched in since keeps this time.
    detailsReadAt: info.data?.dispatched_at ?? 0,
    listed: preview?.stack,
    conversationId,
  });
  // The header and Overview show the stack another member's read or the list gave until this
  // PR's own answers; merging, comparing and editing wait for that.
  const prDiff = usePullRequestDiff(conversationId, hasPr, prUrl, revision);
  // Pull Requests v2 shows the Changes tab on the shared review; Classic keeps the stacked view.
  const sharedReview = usePullRequestsV2();
  // The new page's Try Again on a failed Changes read asks both reads again.
  const retryChanges = useCallback(() => {
    void changes.refetch();
    void prDiff.refetch();
  }, [changes, prDiff]);
  const retryPatch = useCallback(() => void prDiff.refetch(), [prDiff]);
  // A patch read that failed with nothing read before; one behind a read patch leaves it showing.
  const patchError = prDiff.error && prDiff.data === undefined ? prDiff.error : null;
  const patchState = useMemo<PullRequestPatchState>(
    () =>
      patchError
        ? { status: "failed", message: patchError.message }
        : prDiff.isLoading
          ? { status: "loading" }
          : { status: "ready" },
    [patchError, prDiff.isLoading],
  );
  const sandboxAsleep = useWorkspaceAsleep(conversationId);
  const canEdit = useCanChangePullRequests(conversationId);
  const changeAccess = usePullRequestChangeAccess(conversationId);
  const mobile = useIsMobileViewport();
  // The diff's highlighting workers start now, so opening Changes finds them ready.
  useWarmSharedDiffWorkers();
  const shownStack = readStack;
  // On the new page, a stacked PR's Changes can show only its own changes since its parent.
  const refetchInfo = info.refetch;
  const reloadPullRequest = useCallback(async () => {
    const result = await refetchInfo();
    return result.status === "success" ? (result.data?.pr ?? null) : null;
  }, [refetchInfo]);
  // Only the shared review shows a stacked PR's own changes; Summary's check needs no renderer.
  const shownSurface =
    active && ((activeTab === "changes" && sharedReview) || activeTab === "summary")
      ? activeTab
      : null;
  const stackDiff = usePullRequestStackDiff({
    conversationId,
    pr: layout ? (info.data?.pr ?? null) : null,
    stack,
    surface: shownSurface,
    // Summary compares only to tell whether a merged parent's commits are still in this PR.
    diagnose: wantsLandingDiagnostic(stack, info.data?.pr ?? null),
    reloadPullRequest,
  });
  const changesStackDiff = sharedReview ? stackDiff : null;
  // A choice in the comparison menu can swap the review holding it; focus stays on the menu.
  useBaseMenuFocusHandoff();
  const landing = useMemo(
    () => stackLanding(stack, info.data?.pr ?? null, stackDiff),
    [stack, info.data?.pr, stackDiff],
  );
  const showChanges = useCallback(() => setActiveTab("changes"), [setActiveTab]);

  // The Commits tab reads through the desktop shell's GitHub fast track, as the
  // account the host reports; it stays hidden wherever that isn't available.
  const prLocation = githubPullRequestLocation(prUrl ?? info.data?.pr?.url);
  const fastTrackAccount = pullRequestFastTrackAccount({
    provider: info.data ? normalizePullRequestInfo(info.data).provider : null,
    prHost: prLocation?.host,
    selectedAccount: info.data?.auth?.selected_account,
  });
  // Where this shell reads neither way, the session's host lists them once it serves both reads.
  const editsServed = usePullRequestEditsServed(conversationId);
  const hostListsCommits = usePullRequestMethodServed(conversationId, "listPullRequestCommits");
  const hostDiffsCommits = usePullRequestMethodServed(conversationId, "getCommitDiff");
  const hostCommitsUrl = prUrl ?? info.data?.pr?.url;
  const hostCommits =
    hostListsCommits && hostDiffsCommits && !!prLocation && !!hostCommitsUrl && !!headSha;
  const fastTrack = useGitHubFastTrack(
    hasPr && !hostCommits ? fastTrackAccount : null,
    COMMIT_OPERATIONS,
  );
  // A registered merge readiness source adds its own status to the PRs it covers.
  const queryClient = useQueryClient();
  const livePrUrl = info.data?.pr?.url;
  // The host's own account: readiness and owned files are about who merges.
  const accountHost = fastTrackAccount?.host;
  const accountLogin = fastTrackAccount?.login;
  const mergeReadinessTargets = useMemo(
    () =>
      livePrUrl
        ? [
            {
              url: livePrUrl,
              account:
                accountHost && accountLogin ? { host: accountHost, login: accountLogin } : null,
              headSha,
            },
          ]
        : [],
    [livePrUrl, accountHost, accountLogin, headSha],
  );
  // A hidden panel reads nothing in the background; showing it again reads fresh.
  const mergeReadiness = useMergeReadiness(mergeReadinessTargets, {
    active,
    pollMs: PULL_REQUEST_LIST_POLL_MS,
  }).get(livePrUrl ?? "");
  // Where the source knows them, the changed files this reader may approve come first in Changes.
  const ownedPaths = useOwnedFiles(
    { url: livePrUrl, viewerLogin: accountLogin, headSha },
    { active, pollMs: PULL_REQUEST_LIST_POLL_MS },
  );
  const commitsSource: PullRequestCommitsSource | null =
    hostCommits && prLocation && hostCommitsUrl
      ? {
          account: { host: prLocation.host, login: "" },
          repository: prLocation.repository,
          number: prLocation.number,
          headSha,
          baseSha,
          host: { conversationId, prUrl: hostCommitsUrl },
        }
      : fastTrack.status === "ready" && prLocation
        ? {
            account: fastTrack.account,
            repository: prLocation.repository,
            number: prLocation.number,
            headSha,
            baseSha,
          }
        : null;
  // The one commit-list observer, so a hidden panel stops reading and polling.
  const commits = usePullRequestCommits(commitsSource, { enabled: active });
  const commitsTab = useCommitsTabStatus({
    path: hostCommits ? "host" : hasPr && prLocation ? "fast-track" : null,
    fastTrack,
    fastTrackKey: gitHubFastTrackStatusKey(
      hasPr && !hostCommits ? fastTrackAccount : null,
      COMMIT_OPERATIONS,
    ),
    source: commitsSource,
    commits,
    onTab: activeTab === "commits",
  });
  const showCommitsTab = commitsTab.status.kind !== "hidden";
  // A list read at another head or base re-reads the PR once, so the two agree.
  useCommitListReconciliation(commits.data, { headSha, baseSha }, () => void info.refetch());
  const shownTab = activeTab === "commits" && !showCommitsTab ? "summary" : activeTab;
  // Only once the PR and its read paths are known: while loading, offline or still
  // checking the account, Commits may yet show, so the request is kept.
  const commitsSettledHidden =
    hasPr && !info.isFetching && !showCommitsTab && fastTrack.status !== "checking";
  useEffect(() => {
    if (activeTab === "commits" && commitsSettledHidden) onTabUnavailable?.("summary");
  }, [activeTab, commitsSettledHidden, onTabUnavailable]);

  // Summary (checks + PR body), Comments, and Changes (the stacked diff).
  // Summary is the landing tab — like GitHub's PR page opening on the
  // Conversation view.
  const reviewThreadsReadable =
    !sandboxAsleep && serveable !== false && !!info.data?.capabilities?.review_threads;
  const reviewThreadsWanted = isReviewThreadsWanted({
    active,
    tab: activeTab,
    newPage: layout !== undefined,
    readable: reviewThreadsReadable,
    hasPr,
  });
  const reviews = usePullRequestReviewThreads(
    conversationId,
    prUrl,
    info.data?.auth?.selected_account ?? info.data?.selected_account,
    revision,
    reviewThreadsWanted,
  );

  const themeType = useResolvedThemeMode();
  // Diff layout is the app-global FileViewer preference (unified/split); seed
  // from the persisted value and write toggles back so the choice carries over.
  const [diffStyle, setDiffStyle] = useState<"unified" | "split">(() =>
    !mobile && readFileViewPreferences().diffLayout === "split" ? "split" : "unified",
  );
  const changeDiffStyle = useCallback((next: "unified" | "split") => {
    setDiffStyle(next);
    writeFileViewPreferences({ ...readFileViewPreferences(), diffLayout: next });
  }, []);
  const toggleDiffStyle = useCallback(() => {
    setDiffStyle((prev) => {
      const next = prev === "split" ? "unified" : "split";
      writeFileViewPreferences({ ...readFileViewPreferences(), diffLayout: next });
      return next;
    });
  }, []);

  const files = useMemo<PullRequestChangedFile[]>(() => changes.data?.data ?? [], [changes.data]);
  const ownedChangedFiles = useMemo(
    () =>
      ownedChangedPaths(
        files.map((file) => file.path),
        ownedPaths,
      ),
    [files, ownedPaths],
  );
  // The sidebar groups the flat file list into a compacted folder tree (the
  // diff scroll below stays linear, in file order).
  const fileTree = useMemo(() => buildSidebarTree(files), [files]);

  // Per-file collapse is lifted here so the toolbar's "expand/collapse all" can
  // drive every section; a path in the set is collapsed.
  const [collapsedPaths, setCollapsedPaths] = useState<ReadonlySet<string>>(() => new Set());
  const [sidebarCollapsed, setSidebarCollapsed] = useState(mobile);
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
  const { filesByPath, parseError } = useMemo(() => {
    const map = new Map<string, FileDiffMetadata>();
    const patch = prDiff.data?.patch;
    if (patch) {
      try {
        for (const file of patchFileDiffs(patch)) map.set(file.name, file);
      } catch {
        return {
          filesByPath: map,
          parseError: "Couldn’t read the PR diff. Refresh or open the PR on its provider.",
        };
      }
    }
    return { filesByPath: map, parseError: undefined };
  }, [prDiff.data]);

  // Expand-context loader: fetch a file's full old/new content on demand when
  // the reader expands unchanged regions.
  // The account's pending review, read with the threads; its drafts show in them.
  const pendingReview = usePendingReview(prUrl, reviewThreadsWanted, conversationId);
  const shownPage = useThreadsWithPendingDrafts(reviews.page, pendingReview.state);
  const shownReviews = { ...reviews, page: shownPage };
  const threads = shownPage?.data ?? NO_REVIEW_THREADS;
  const threadGroups = useReviewThreadGroups(threads, files, filesByPath);
  const notPlacedDrafts = draftsNotPlaced(reviews.page, pendingReview.state);
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
  const contextLoads = useContextLoads(loadDiffFiles);

  const diffOptions = useMemo<DiffOptions>(
    () => ({
      theme: DIFF_SYNTAX_THEME,
      themeType,
      diffStyle,
      unsafeCSS: DIFF_PALETTE_CSS + CONTEXT_LOAD_CSS,
      // The section renders its own header; the diff body has none.
      disableFileHeader: true,
      // Expand unchanged context 10 lines at a time (default is 100). The
      // unchanged lines aren't in the patch, so expansion (and the exact
      // trailing-region count) is served by loadDiffFiles on demand.
      expansionLineCount: 10,
      loadDiffFiles: contextLoads.load,
    }),
    [themeType, diffStyle, contextLoads.load],
  );

  // A commit's patch renders alone: the host can't serve its surrounding lines.
  const commitDiffOptions = useMemo<DiffOptions>(
    () => ({ ...diffOptions, loadDiffFiles: undefined, hunkSeparators: "simple" }),
    [diffOptions],
  );

  // The stacked diff scrolls as one; the sidebar highlights the file at the top
  // of the viewport and jumps to a file on click.
  const scrollRef = useRef<HTMLDivElement>(null);
  const sectionEls = useRef<Map<string, HTMLElement>>(new Map());
  const [activePath, setActivePath] = useState<string | null>(null);

  // A requested file whose section hasn't mounted yet; its section finishes the reveal.
  const pendingReveal = useRef<((path: string) => void) | null>(null);
  const registerRef = useCallback((path: string, el: HTMLElement | null) => {
    if (el) sectionEls.current.set(path, el);
    else sectionEls.current.delete(path);
    if (el) pendingReveal.current?.(path);
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

  // Jumping to a collapsed file also expands it, so the click lands on its diff.
  const jumpTo = useCallback((path: string) => {
    setActivePath(path);
    setCollapsedPaths((prev) => {
      if (!prev.has(path)) return prev;
      const next = new Set(prev);
      next.delete(path);
      return next;
    });
    sectionEls.current.get(path)?.scrollIntoView({ block: "start" });
  }, []);

  // A tab's place names a file: bring it into view once its diff is listed.
  const requestedPath = requestedFile?.path;
  const revealedRequest = useRef<string | undefined>(undefined);
  const requestedMissing =
    requestedPath !== undefined &&
    !!changes.data &&
    !files.some((file) => file.path === requestedPath);
  useEffect(() => {
    // Leaving Changes, or a place without a file, re-arms the request, so Back and
    // Forward to the file reveal it again.
    if (shownTab !== "changes" || requestedPath === undefined) {
      revealedRequest.current = undefined;
      return;
    }
    if (sharedReview) return;
    if (revealedRequest.current === requestedPath) return;
    let current = true;
    let frame: number | undefined;
    const reveal = () => {
      if (!current) return;
      pendingReveal.current = null;
      revealedRequest.current = requestedPath;
      jumpTo(requestedPath);
    };
    // Only once the file's diff section is on screen; the list can arrive before the diff.
    if (sectionEls.current.has(requestedPath)) {
      reveal();
      return;
    }
    pendingReveal.current = (path) => {
      if (path === requestedPath && frame === undefined) frame = requestAnimationFrame(reveal);
    };
    // A newer request, sub-view or list retires this one, including a reveal already queued.
    return () => {
      current = false;
      pendingReveal.current = null;
      if (frame !== undefined) cancelAnimationFrame(frame);
    };
  }, [sharedReview, requestedPath, shownTab, files, filesByPath, jumpTo]);
  const chooseSidebarFile = useCallback(
    (path: string) => {
      jumpTo(path);
      revealedRequest.current = path;
      onFileChosen?.(path, { replace: false });
    },
    [jumpTo, onFileChosen],
  );
  // The shared review scrolls to a file picked in its tree; a tab's place names it, as above.
  const chooseReviewFile = useCallback(
    (path: string) => {
      revealedRequest.current = path;
      onFileChosen?.(path, { replace: false });
    },
    [onFileChosen],
  );
  const openSidebarFile = useCallback(
    (path: string, intent: PullRequestOpenIntent) =>
      openByIntent(
        intent,
        () => chooseSidebarFile(path),
        onOpenInNewTab && ((options) => onOpenInNewTab({ path }, options)),
      ),
    [chooseSidebarFile, onOpenInNewTab],
  );

  const [focusedThread, setFocusedThread] = useState<{ id: string } | null>(null);
  useEffect(() => {
    const container = scrollRef.current;
    if (!container || !focusedThread || activeTab !== "changes") return;
    const focusThread = () => {
      const element = container.querySelector<HTMLElement>(
        `[data-review-thread="${CSS.escape(focusedThread.id)}"]`,
      );
      if (!element || !element.getClientRects().length) return;
      element.scrollIntoView({ block: "center" });
      element.focus({ preventScroll: true });
      observer.disconnect();
    };
    const observer = new MutationObserver(focusThread);
    observer.observe(container, { childList: true, subtree: true });
    focusThread();
    return () => observer.disconnect();
  }, [focusedThread, activeTab]);

  const hostReturned = useRecentHostReturn(conversationId);
  const checkAgain = () => {
    clearSessionHostTransportRecovery(conversationId);
    void invalidateSessionPullRequestReads(queryClient, conversationId, ["pr-page-github-info"]);
  };
  // Mutation responses seed the query cache raw, so normalize before reading.
  const normalized = info.data && normalizePullRequestInfo(info.data);
  const copy = gitProviderCopy(
    normalized?.provider ?? null,
    normalized?.auth.hint,
    normalized?.provider_display,
  );

  const panelState = derivePullRequestPanelState({
    isLoading: info.isLoading,
    error: info.error,
    data: info.data,
    sourceAsleep: sandboxAsleep,
    failureReason: info.failureReason,
    hostReturned,
  });
  // The panel owns the clock; this view adds its header and diff to it.
  const headerVisible = panelState.kind === "ready";
  const diffSettled = hasPr && !prDiff.isLoading && (prDiff.data !== undefined || !!prDiff.error);
  useEffect(() => {
    if (headerVisible) onTimingMilestone?.("header");
  }, [headerVisible, onTimingMilestone]);
  useEffect(() => {
    if (diffSettled) onTimingMilestone?.("diff");
  }, [diffSettled, onTimingMilestone]);
  // Where a cold diff's time goes: asking for the patch, reading it, and showing it in Changes.
  const patchRequested = hasPr && prDiff.fetchStatus === "fetching";
  useEffect(() => {
    if (patchRequested) onTimingMilestone?.("patchRequested");
  }, [patchRequested, onTimingMilestone]);
  const changesShown = active && activeTab === "changes";
  useEffect(() => {
    onTimingMilestone?.(changesShown ? "changesShown" : "changesHidden");
  }, [changesShown, onTimingMilestone]);
  useEffect(() => {
    if (changesShown && diffSettled) onTimingMilestone?.("diffShown");
  }, [changesShown, diffSettled, onTimingMilestone]);

  // The Commits tab's navigation in ⌘K and on the open commit's header, from one definition.
  const commitList = commits.data?.commits;
  const openCommitIndex =
    selectedCommit && commitList
      ? commitList.findIndex((commit) => commit.sha === selectedCommit)
      : -1;
  // A command's focus move waits for its target, such as a list still loading, and is
  // dropped once the person does anything else, leaves these details, or picks another PR.
  const detailsRef = useRef<HTMLDivElement>(null);
  const tabStripRef = useRef<HTMLDivElement>(null);
  const commandFocus = usePendingFocus<CommitFocusTarget | SectionFocusTarget>(detailsRef);
  const settleCommandFocus = commandFocus.settle;
  useEffect(() => settleCommandFocus(), [conversationId, prUrl, settleCommandFocus]);
  // What the page asks the Overview's Activity to show, for this PR, reader and pane only.
  const readingAccount = info.data?.auth?.selected_account ?? info.data?.selected_account ?? null;
  const activityReveal = useActivityRevealRequest(
    layout && prUrl ? { conversationId, prUrl, account: readingAccount } : null,
  );
  const revealActivity = activityReveal.reveal;
  // A posted comment shows in place, under whatever filter or sort, once the read brings it.
  const onCommentPosted = useCallback(
    (comment: PullRequestComment) =>
      revealActivity({ kind: "posted", key: postedCommentKey(comment) }),
    [revealActivity],
  );
  // The new page has no Comments tab: a link, saved tab or command for it opens Overview's
  // Activity with every kind, as Comments listed the conversation and its threads.
  useEffect(() => {
    if (!layout || activeTab !== "comments") return;
    // A routed page replaces the place it was sent to, so Back doesn't return to it.
    if (onTabUnavailable) onTabUnavailable("summary");
    else setActiveTab("summary");
    revealActivity({ kind: "section" });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- setActiveTab changes each render
  }, [activeTab, layout, onTabUnavailable, revealActivity]);
  const commitFocusRequest =
    commandFocus.pending && !isSectionFocusTarget(commandFocus.pending.target)
      ? (commandFocus.pending as PendingFocus<CommitFocusTarget>)
      : null;
  // A section command leaves focus on that section's tab or picker, once it's the one shown.
  useEffect(() => {
    const target = commandFocus.pending?.target;
    if (!isSectionFocusTarget(target) || target !== sectionFocusTarget(shownTab)) return;
    if (focusSectionControl(tabStripRef.current)) settleCommandFocus();
  }, [commandFocus.pending, shownTab, settleCommandFocus]);
  // Shows a section and moves focus to its control, for ⌘K and a commit's "All changes".
  const showSection = (section: SectionCommandTarget) => {
    // On the new page, comments are Overview's Activity, which takes focus itself.
    if (layout && section === "comments") {
      setActiveTab("summary");
      revealActivity({ kind: "section" });
      return;
    }
    setActiveTab(section);
    commandFocus.request(sectionFocusTarget(section));
  };
  const commitCommandsById = useCommitCommands({
    conversationId,
    active,
    facts: {
      tabShown: showCommitsTab && !storedPr && headerVisible,
      onCommitsTab: shownTab === "commits",
      open: selectedCommit
        ? {
            sha: selectedCommit,
            listed: openCommitIndex >= 0,
            previousSha: commitList?.[openCommitIndex - 1]?.sha ?? null,
            nextSha: openCommitIndex >= 0 ? (commitList?.[openCommitIndex + 1]?.sha ?? null) : null,
            // A host lists only the first commits of a long PR.
            moreAfterList: (commits.data?.total_count ?? 0) > (commitList?.length ?? 0),
          }
        : null,
      newTab: onOpenInNewTab !== undefined,
    },
    handlers: {
      showCommits: () => {
        onSelectCommit(undefined);
        setActiveTab("commits");
        commandFocus.request("list");
      },
      openCommit: (sha, direction) => {
        onSelectCommit(sha);
        commandFocus.request(direction);
      },
      showAllCommits: () => {
        onSelectCommit(undefined);
        commandFocus.request("list");
      },
      showChanges: () => showSection("changes"),
      openInNewTab: (sha) => onOpenInNewTab?.({ commit: sha }, { background: true }),
    },
  });
  useSectionCommands({
    conversationId,
    active,
    facts: {
      // The new page's sections; the classic panel names them differently and keeps its own.
      available: !!layout && headerVisible && !storedPr,
      shown: shownTab,
      commitOpen: shownTab === "commits" && !!selectedCommit,
    },
    show: showSection,
  });
  // The file on screen in Changes, in a tab of its own, from the same place ⌘-click opens.
  // The shared review tracks its own file, so only the panel's own file list names one.
  const shownFilePath =
    shownTab === "changes" && headerVisible && !sharedReview ? activePath : null;
  const fileNewTab = onOpenInNewTab !== undefined && !storedPr;
  const onOpenInNewTabRef = useRef(onOpenInNewTab);
  onOpenInNewTabRef.current = onOpenInNewTab;
  const filePaletteCommands = useMemo<PaletteCommand[]>(
    () =>
      pullRequestFileCommands({ shownPath: shownFilePath, newTab: fileNewTab }).map((command) => ({
        id: `${command.id}:${command.path}`,
        label: command.paletteLabel,
        icon: SquarePlusIcon,
        keywords: [...command.keywords, command.path],
        availability: command.availability,
        run: () => onOpenInNewTabRef.current?.({ path: command.path }, { background: true }),
      })),
    [shownFilePath, fileNewTab],
  );
  const filePaletteInstance = useId();
  usePaletteCommands(
    `pull-request-file:${conversationId}:${filePaletteInstance}`,
    "Pull request",
    active ? filePaletteCommands : NO_PALETTE_COMMANDS,
  );

  if (storedPr) {
    return (
      <StoredPullRequestDetails
        conversationId={conversationId}
        pr={storedPr}
        copy={gitProviderCopy(storedPr.provider, undefined, storedPr.provider_display)}
        sandboxAsleep={sandboxAsleep}
        showHeading={showHeading}
        refreshButton={refreshButton}
      />
    );
  }
  if (notSynced) {
    return sandboxAsleep ? (
      <div className="p-3">
        <p className="text-ui text-muted-foreground">
          This session’s PRs haven’t synced to the server yet.
        </p>
        <SandboxResumeNotice
          sessionId={conversationId}
          layout="inline"
          purpose="load them"
          className="mt-1 px-0"
        />
      </div>
    ) : (
      <p role="status" className="p-3 text-ui text-muted-foreground">
        This session’s PRs haven’t synced to the server yet. To load them, please reconnect the
        session’s host.
      </p>
    );
  }

  // A session-free view with nothing read for the PR has no host of its own to ask.
  if (sessionFree && !info.data && !info.isLoading) {
    const shownUrl = requestedPrUrl ?? prUrl;
    return (
      <SessionFreePullRequestState
        linkingSession={shownUrl ? sessionFree.linkingSessionOf?.(shownUrl) : undefined}
      />
    );
  }

  // ── Whole-panel states (before the header + stacked diff) ───────────────
  // One central switch: every non-`ready` kind returns its own whole-panel
  // state, so the diff below renders only when there's an open PR.
  switch (panelState.kind) {
    case "loading":
      // The page lays the PR out from what its list knows, so the details fill in place.
      if (layout?.renderPreview && preview) {
        return layout.renderPreview(
          preview,
          gitProviderCopy(parseCodeHostPullRequestUrl(preview.url)?.provider),
          selectStackPullRequest,
          activeTab,
        );
      }
      return (
        <PanelMessage>
          <Loader2Icon className="size-5 animate-spin" />
          Loading pull requests…
        </PanelMessage>
      );
    case "host-reconnecting":
      return (
        <PanelMessage>
          <Loader2Icon className="size-5 animate-spin" aria-hidden />
          Reconnecting to this session’s machine…
        </PanelMessage>
      );
    case "host-unreachable":
      return (
        <PullRequestEmptyState
          icon={ServerOffIcon}
          title="Can’t reach this session’s machine"
          hint={
            panelState.returned
              ? "It came back online but isn’t answering yet. Check again in a moment."
              : "It didn’t answer. Check again in a moment."
          }
        >
          <Button size="sm" variant="outline" disabled={info.isFetching} onClick={checkAgain}>
            {info.isFetching && <Loader2Icon className="size-3.5 animate-spin" aria-hidden />}
            Check again
          </Button>
          {emptyStateAction}
        </PullRequestEmptyState>
      );
    case "host-starting":
      return (
        <PullRequestEmptyState
          icon={Loader2Icon}
          title="Checking this repository"
          hint="This session’s machine just came back online. Pull requests load once it can reach GitHub."
        >
          {emptyStateAction}
        </PullRequestEmptyState>
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
          <SandboxResumeNotice
            sessionId={conversationId}
            purpose="load pull requests"
            className="flex-none p-0"
          />
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
    case "unsupported-remote": {
      const remote = unsupportedRemoteRecovery(panelState.remoteHost);
      if (remote.kind === "named-host-remote") {
        return (
          <PullRequestEmptyState
            icon={ServerOffIcon}
            title={`This repository is on ${remote.label}`}
            hint={
              <>
                Its pull requests are on <span className="font-mono">{remote.host}</span>. Omnigent
                can’t show {remote.label} pull requests here yet.
              </>
            }
          >
            {emptyStateAction}
          </PullRequestEmptyState>
        );
      }
      return (
        <PullRequestEmptyState
          icon={ServerOffIcon}
          title="No supported remote"
          hint={
            panelState.remoteHost ? (
              <>
                Omnigent couldn’t identify the git provider for{" "}
                <span className="font-mono">{panelState.remoteHost}</span>.
              </>
            ) : (
              "Omnigent couldn’t identify this workspace’s git provider."
            )
          }
          // A GitHub Enterprise host the user never signed in to lands here. Desktop
          // reads GitHub with the computer's account, so it doesn't send people to the host.
          note={
            panelState.remoteHost &&
            !isElectronShell() && (
              <>
                On the execution host, run{" "}
                <span className="font-mono">
                  glab auth login --hostname {panelState.remoteHost}
                </span>{" "}
                for GitLab or{" "}
                <span className="font-mono">gh auth login --hostname {panelState.remoteHost}</span>{" "}
                for GitHub Enterprise. For a GitHub SSH alias, run{" "}
                <span className="font-mono">git config --local omnigent.gitprovider github</span> in
                this checkout, then refresh for installation or sign-in help.
              </>
            )
          }
        >
          {emptyStateAction}
        </PullRequestEmptyState>
      );
    }
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
    case "repo-unresolved": {
      const recovery = normalized && githubRecovery(normalized);
      if (recovery) {
        const recoveryCopy = githubRecoveryCopy(recovery, recoverySurface());
        return (
          <PullRequestEmptyState
            icon={KeyRoundIcon}
            title={recoveryCopy.title}
            hint={codeSpans(recoveryCopy.hint)}
          >
            {recovery.kind === "sign-in-not-working" && (
              <ul
                aria-label="GitHub accounts on the host"
                className="text-ui text-muted-foreground"
              >
                {recovery.accounts.map((account) => (
                  <li key={`${account.host ?? ""}/${account.login}`}>
                    <span className="font-mono text-foreground">{account.login}</span>
                    {account.host && account.host !== "github.com" && (
                      <> on {account.host}</>
                    )} — {accountCheckLabel(account.check)}
                  </li>
                ))}
              </ul>
            )}
            {(recovery.kind === "repo-check-failed" || recovery.kind === "unknown") &&
              normalized && (
                <GithubAccountSelector conversationId={conversationId} info={normalized} />
              )}
            <Button size="sm" variant="outline" disabled={info.isFetching} onClick={checkAgain}>
              {info.isFetching && <Loader2Icon className="size-3.5 animate-spin" aria-hidden />}
              Check again
            </Button>
            {emptyStateAction}
          </PullRequestEmptyState>
        );
      }
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
    }
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
  const commentControls = pullRequestCommentControls(data);
  // On this Mac every edit runs as its account, so none waits on the session's machine.
  const editCapabilities = offeredEditCapabilities(data, editsServed);
  const canPostComment = !!(canEdit && commentControls.comment && data.selected_pr_url);
  // A file's link opens what the diff shows: the patch's pinned head, else the PR's, in the
  // repository its commits live in. "head_repository_url" stays absent from older hosts.
  const sourceRepository =
    "head_repository_url" in pr ? { headRepositoryUrl: pr.head_repository_url } : {};
  const fileRevision: PullRequestChangesReviewProps["fileRevision"] = {
    sha: prDiff.data?.head_sha ?? pr.head_sha,
    ...sourceRepository,
  };
  const threadContext: ReviewThreadContext = {
    conversationId,
    prUrl: data.selected_pr_url ?? pr.url,
    account: commentControls.account,
    canReply: !!(canEdit && commentControls.reply),
    canComment: canPostComment,
    canCreate: !!(
      canEdit &&
      commentControls.createThread &&
      pr.state === "OPEN" &&
      pr.head_sha &&
      pr.base_sha &&
      pr.head_sha === prDiff.data?.head_sha &&
      pr.base_sha === prDiff.data?.base_sha &&
      reviews.page
    ),
    headSha: pr.head_sha,
    baseSha: pr.base_sha,
    provider: data.provider,
    loadComments: reviews.loadComments,
    loadingComments: reviews.loadingComments,
  };
  // Every comment the PR's loaded reads list; a change to any other is kept in reach on Comments.
  const listedCommentIds = new Set([
    ...(pr.comments ?? []).flatMap((comment) => (comment.id ? [comment.id] : [])),
    ...threads.flatMap((thread) => thread.comments.data.map((comment) => comment.id)),
  ]);
  const matchingRevision =
    !!reviews.page &&
    reviews.page.head_sha === prDiff.data?.head_sha &&
    reviews.page.base_sha === prDiff.data?.base_sha;
  // An error with nothing loaded replaces the changes; one behind loaded changes leaves them showing.
  // The shared review lists the files even when the patch failed, each saying why, so a patch
  // error replaces only Monaco's view.
  const filesError = changes.error && changes.data === undefined ? changes.error : null;
  const changesError = filesError ?? (sharedReview ? null : patchError);
  const changesUpdateError = changesError
    ? null
    : (changes.error ?? (patchError ? null : prDiff.error) ?? null);
  const changesUpdateNotice = changesUpdateError && (
    <p role="alert" className="shrink-0 border-b border-border p-2 text-ui text-destructive">
      Couldn’t update the changes: {(changesUpdateError as Error).message} Showing what was loaded
      before.
    </p>
  );
  // The shared review shows the files as soon as they're listed; Monaco's view waits for the patch.
  const canShowFiles =
    !changesError &&
    !parseError &&
    !prDiff.data?.unavailable_reason &&
    files.length > 0 &&
    (sharedReview || !prDiff.isLoading);
  const { byFile: threadsByFile, elsewhere: earlierThreads } = canShowFiles
    ? threadGroups
    : { byFile: NO_THREADS_BY_FILE, elsewhere: threads };
  // The new page's Changes states, in the reference's shapes; the classic page draws its own.
  const changesPageState =
    layout === undefined || canShowFiles ? null : changesError || parseError ? (
      <PanelStateMessage
        variant="failure"
        title="Changes Unavailable"
        actions={
          <Button variant="outline" size="sm" onClick={retryChanges}>
            Try Again
          </Button>
        }
      >
        {parseError || (changesError as Error).message}
      </PanelStateMessage>
    ) : prDiff.data?.unavailable_reason ? (
      <PanelStateMessage variant="failure" title="Changes Unavailable">
        {diffUnavailableMessage(prDiff.data)}
      </PanelStateMessage>
    ) : changes.isLoading || prDiff.isLoading ? (
      <PanelStateMessage variant="loading">Loading changes…</PanelStateMessage>
    ) : changedFilesUnavailable(changes.data) ? (
      <PanelStateMessage variant="failure" title="Changes Unavailable">
        Changed files are unavailable.
      </PanelStateMessage>
    ) : (
      <PanelStateMessage variant="empty" title="No Changes">
        There are no changes in this pull request.
      </PanelStateMessage>
    );
  const unresolved = threads.filter((thread) => !thread.resolved);
  // The changed file a conversation is on, by its path or the path it had before a rename,
  // as the list says or, before the list says, its patch.
  const changedFileOf = (thread: ReviewThread) =>
    files.find(
      (entry) =>
        entry.path === thread.path ||
        entry.previous_path === thread.path ||
        filesByPath.get(entry.path)?.prevName === thread.path,
    );
  const navigateThread = (direction: number) => {
    const index = unresolved.findIndex((thread) => thread.id === focusedThread?.id);
    const start = index < 0 ? (direction > 0 ? -1 : 0) : index;
    const next = unresolved[(start + direction + unresolved.length) % unresolved.length];
    if (!next) return;
    const file = changedFileOf(next);
    if (file && !sharedReview) jumpTo(file.path);
    setFocusedThread({ id: next.id });
  };
  // Show in Changes, from Activity: the conversation at its line, or above its file where no
  // line shows it. A file the changes no longer list says so, and the page stays where it is.
  const showThreadInChanges = (thread: ReviewThread) => {
    const file = changedFileOf(thread);
    // Only a complete, readable list proves a file gone; otherwise Changes shows what it can.
    const listedWhole = changes.data !== undefined && !changedFilesUnavailable(changes.data);
    if (!file && listedWhole) {
      toast.info(`${thread.path} isn’t in this pull request’s changes anymore`, {
        description: "It may have been renamed, or its changes undone.",
      });
      return;
    }
    if (file && !sharedReview) jumpTo(file.path);
    setActiveTab("changes");
    setFocusedThread({ id: thread.id });
  };
  // A linked PR can come from another repository than the session's.
  const repository =
    data.prs?.find((entry) => entry.url === pr.url)?.repository ??
    data.repo?.name_with_owner ??
    copy.label;
  const completeFiles =
    !changes.isLoading && changes.data !== undefined && !changes.data?.has_more && files.length > 0
      ? files
      : undefined;
  // Who a reviewer ping can remind: the PR as read, by the URL it was opened with.
  const pingPullRequest: ReviewerPingPullRequest | null = pr.reviewers
    ? {
        url: data.selected_pr_url ?? pr.url,
        number: pr.number,
        state: pr.state,
        author: pr.author,
        reviewers: pr.reviewers,
      }
    : null;
  // Each Summary section, which a page layout may arrange on its own.
  const reviewersSection = pr.reviewers !== undefined && (
    <PullRequestReviewers
      conversationId={conversationId}
      prUrl={data.selected_pr_url ?? null}
      reviewers={pr.reviewers}
      author={pr.author}
      editable={canEdit && editCapabilities.reviewers}
      pingTarget={pingPullRequest}
    />
  );
  const labelsSection = pr.labels !== undefined && data.selected_pr_url && (
    <PullRequestLabels
      conversationId={conversationId}
      prUrl={data.selected_pr_url}
      labels={pr.labels}
      editable={canEdit && editCapabilities.labels}
    />
  );
  // A stacked PR's advice about landing, beside Merge and in its dialogs; it never holds a merge.
  const landingNotice = landing.notice && <PullRequestStackLandingNotice landing={landing} />;
  const mergeProps =
    mergeSectionShown(pr, {
      canEdit,
      merging: editCapabilities.merging,
    }) && data.selected_pr_url
      ? {
          conversationId,
          prUrl: data.selected_pr_url,
          pr,
          accounts: mergeAccounts(data),
          dialogNotice: landingNotice,
          // GitHub's own stack merges together, on GitHub; the bar and ⌘K hand off there.
          handoff: nativeStackMergeHandoff(stack, data.selected_pr_url),
          stackPending: stackWaiting,
        }
      : null;
  const mergeSection = (landingNotice || mergeProps) && (
    <>
      {landingNotice}
      {mergeProps && (
        // A different PR starts fresh, so an open dialog can't outlive its PR.
        <PullRequestMerge key={mergeProps.prUrl} {...mergeProps} />
      )}
    </>
  );
  const mergeReadinessSection = (
    <PullRequestMergeReadinessSection
      view={mergeReadiness}
      onRenewed={() => void refreshMergeReadiness(queryClient)}
    />
  );
  const checksProps = {
    checks,
    supported: pr.checks_supported !== false,
    unavailableHint: copy.checksUnavailableHint,
    providerLabel: copy.label,
    pullRequestUrl: pr.url,
  };
  const checksSection = (
    <>
      {mergeReadinessSection}
      <PullRequestChecksSection
        // Each PR starts with its own completed checks folded or open.
        key={pr.url}
        {...checksProps}
      />
    </>
  );
  const attachmentsAvailability = {
    available: !!data.capabilities.attachment_uploads,
    needsHost: false,
  };
  const descriptionSection = (
    <PullRequestDescription
      conversationId={conversationId}
      prUrl={data.selected_pr_url ?? null}
      body={pr.body}
      stack={shownStack}
      editable={canEdit && editCapabilities.details}
      attachments={attachmentsAvailability.available}
      attachmentsNeedHost={attachmentsAvailability.needsHost}
    />
  );
  // One comment box, shown under whichever of Comments and the Overview is open.
  // Shown to every editor: where nothing can post now, the box keeps the draft and says why.
  const commentComposer = canEdit && data.selected_pr_url && (
    <PullRequestCommentComposer
      key={`${conversationId}:${data.selected_pr_url}`}
      conversationId={conversationId}
      prUrl={data.selected_pr_url}
      account={commentControls.account}
      onPosted={onCommentPosted}
      active={active}
      prState={pr.state}
      writesBlocked={!canPostComment}
      provider={copy}
    />
  );
  // A push opens its commit on Commits, where this PR shows them.
  const openCommit = showCommitsTab
    ? (sha: string) => {
        onSelectCommit(sha);
        setActiveTab("commits");
        commandFocus.request("commit");
      }
    : undefined;
  // The new page's Overview timeline: the conversation, every review thread and the pushes.
  const activitySection = layout && (
    <>
      <PullRequestCommentRecoveries
        conversationId={conversationId}
        prUrl={threadContext.prUrl}
        listedIds={listedCommentIds}
        active={active && shownTab === "summary"}
      />
      <PullRequestActivity
        active={active && shownTab === "summary"}
        comments={comments}
        commentsPartial={!!pr.comments_partial}
        events={pr.timeline_events ?? null}
        eventsPartial={!!pr.timeline_partial}
        submittedReviews={pr.reviews ?? null}
        submittedReviewsPartial={!!pr.reviews_partial}
        reviews={shownReviews}
        // Without the PR's own link the read can't start, so it says so rather than spin.
        threadsAvailable={reviewThreadsReadable && !!prUrl && hasPr}
        context={{ ...threadContext, showInChanges: showThreadInChanges }}
        providerLabel={copy.label}
        pullRequestAuthor={pr.author}
        pullRequestUrl={pr.url}
        openCommit={openCommit}
        revealRequest={activityReveal.request}
        onRevealSettled={activityReveal.settle}
      />
    </>
  );
  // What a page layout reads to fill the header and Overview slots.
  const layoutState: PullRequestDetailsState = {
    conversationId,
    copy,
    repository,
    pr: {
      number: pr.number,
      url: pr.url,
      state: pr.state,
      isDraft: pr.is_draft === true,
      title: pr.title,
      author: pr.author,
      reviewers: pr.reviewers,
      headRef: pr.head_ref ?? data.branch,
      baseRef: pr.base_ref ?? baseRef,
    },
    hostAccount: viewerLogin(data.auth),
    selectedPrUrl: data.selected_pr_url ?? null,
    titleEditable: canEdit && editCapabilities.details,
    files: completeFiles,
    lineTotals: completeFiles && changedLineTotals(completeFiles),
    mergeStatus: pullRequestStatusDisplay(pr, mergeReadiness),
    headChecks: pr.checks,
    heading: showHeading ? <PanelTitle /> : undefined,
    refreshButton,
    // The oldest data kept on this device that the page shows, until GitHub answers again.
    freshness: pullRequestFreshness([
      {
        updatedAt: info.dataUpdatedAt,
        restored: info.data?.restored === true,
        refreshing: info.isFetching,
        failed: info.isError,
      },
      ...(reviewThreadsWanted ? [reviews.freshness] : []),
    ]),
    tabs: {
      shown: shownTab,
      select: setActiveTab,
      commits: { shown: showCommitsTab, count: commits.data?.total_count },
      changes: changedFilesTabCount(changes.data),
    },
    sections: {
      reviewers: reviewersSection,
      labels: labelsSection,
      mergeReadiness: mergeReadinessSection,
      stackLanding: landingNotice,
      description: descriptionSection,
    },
    checks: checksProps,
    merge: mergeProps,
    activity: activitySection,
    composer: commentComposer || null,
    lifecycle: {
      conversationId,
      // The URL the PR was opened and is read by, which GitHub's own may differ from in case.
      prUrl: data.selected_pr_url ?? pr.url,
      number: pr.number,
      state: pr.state,
      isDraft: pr.is_draft === true,
      headSha: pr.head_sha,
      autoMergeOn: !!pr.auto_merge,
      // Nothing arms a merge before GitHub says whether it merges the PR with a stack.
      mergeable: mergeProps !== null && !stackWaiting,
      mergeOnGitHub: mergeProps?.handoff ?? null,
      onGitHub: !!normalized && hostedOnGitHub(normalized.provider),
    },
    active,
    stack: shownStack,
    stackLoading,
  };

  // A reference to this PR in its own description or comments shows its state without a lookup.
  const knownPullRequest: PullRequestSuggestion = {
    url: pr.url,
    number: pr.number,
    title: pr.title,
    state: pr.state,
    is_draft: pr.is_draft === true,
    repository: null,
    updated_at: null,
  };

  const completionUrl = data.selected_pr_url ?? pr.url;
  const stackMembers =
    shownStack?.members.kind === "loaded" ? shownStack.members.answer.members : [];
  const completionSource: MarkdownCompletionSource = {
    conversationId,
    prUrl: completionUrl,
    participants: pullRequestParticipants(
      parseCodeHostPullRequestUrl(completionUrl)?.repository ?? null,
      pr.author,
      pr.assignees ?? [],
      pr.reviewers ?? [],
      [
        ...(pr.reviews ?? []).map((review) => review.author),
        ...(pr.comments ?? []).map((comment) => comment.author),
      ],
    ),
    viewer: readingAccount,
    // This PR and its stack's, which the view already read.
    pullRequests: [
      {
        url: pr.url,
        number: pr.number,
        title: pr.title,
        state: pr.state,
        isDraft: pr.is_draft === true,
      },
      ...stackMembers
        .filter((member) => member.url !== pr.url)
        .map((member) => ({
          url: member.url,
          number: member.number,
          title: member.title,
          state: member.state,
          isDraft: member.is_draft,
        })),
    ],
  };

  return (
    <PullRequestDetailsProviders
      changesHere={canEdit}
      attachments={attachmentsAvailability}
      completion={completionSource}
      pullRequestUrl={data.selected_pr_url ?? pr.url}
      references={{
        known: knownPullRequest,
        knownReadAt: data.dispatched_at ?? 0,
        stack: shownStack?.members.kind === "loaded" ? shownStack.members.answer : null,
        conversationId,
      }}
    >
      {/* The details' extent, for focus moves that wait on a target inside them. */}
      {/* Also the scope a deleted comment's focus moves within. */}
      <div ref={detailsRef} data-pull-request-details="" className="contents">
        <UnresolvedConversationPaletteCommands
          source={`pull-request-threads:${conversationId}:${prUrl ?? "none"}`}
          active={active}
          unresolvedCount={unresolved.length}
          openChanges={() => setActiveTab("changes")}
          navigate={navigateThread}
        />
        <PullRequestCommentPaletteCommands
          conversationId={conversationId}
          prUrl={threadContext.prUrl}
          active={active}
        />
        <ReviewerPingPaletteCommands pr={pingPullRequest} active={active} />
        <PullRequestLabelPaletteCommands
          conversationId={conversationId}
          prUrl={data.selected_pr_url ?? null}
          labels={pr.labels}
          editable={canEdit && editCapabilities.labels}
          active={active}
        />
        <StackDiffPaletteCommands
          diff={changesStackDiff}
          active={active}
          showChanges={showChanges}
        />
        <StackStatusPaletteCommands diff={stackDiff} active={active} />
        <StackLandingPaletteCommands landing={landing} active={active} />
        <Tabs
          value={shownTab}
          onValueChange={(v) =>
            setActiveTab(v === "comments" || v === "commits" || v === "changes" ? v : "summary")
          }
          componentId="github.panel.tabs"
          className="flex h-full min-h-0 flex-col gap-0"
        >
          {/* Details cached before the sandbox stopped stay readable under the notice. */}
          {changeAccess === "not_owner" && (
            <PullRequestOwnerOnlyNotice prUrl={data.selected_pr_url ?? pr.url} />
          )}
          {sandboxAsleep && (
            <SandboxResumeNotice
              sessionId={conversationId}
              layout="banner"
              purpose="load the PR details"
            />
          )}
          {/* Header: repo + PR metadata + the Summary/Comments/Changes tabs. Refreshes on
            its own — via the git-activity SSE signal and the panel's CI poll.
            Padding lives on the title block (not the outer container) so the
            tabs align to the gutter and the underline row spans full width. */}
          {/* Clipped, so focusing a tab at the strip's edge can't shift the header sideways. */}
          <PullRequestHeaderSlot
            ref={tabStripRef}
            layout={layout}
            state={layoutState}
            className="shrink-0 overflow-x-clip border-b border-border pb-0.5"
          >
            <PullRequestHeader
              copy={copy}
              repository={repository}
              number={pr.number}
              url={pr.url}
              state={pr.state}
              isDraft={pr.is_draft === true}
              title={
                <PullRequestTitle
                  // A different PR starts fresh, so an open title edit can't outlive its PR.
                  key={data.selected_pr_url ?? pr.url}
                  conversationId={conversationId}
                  prUrl={data.selected_pr_url ?? null}
                  title={pr.title}
                  editable={canEdit && editCapabilities.details}
                />
              }
              author={pr.author}
              headRef={pr.head_ref ?? data.branch}
              baseRef={pr.base_ref ?? baseRef}
              files={completeFiles}
              showHeading={showHeading}
              refreshButton={refreshButton}
              mergeReadiness={mergeReadiness}
            />
            {/* Tab bar (Summary | Comments | Changes); the diff controls live inside
            the Changes tab, and the Comments label carries the count. gap-0 +
            flex-none keep the labels content-sized (default flex-1 spreads them). */}
            {/* A narrow pane scrolls and fades the tabs rather than clipping them. The
              padding leaves room for the active underline and focus ring; the negative
              margins keep the row where it was. */}
            <TabStripScroller fadeEdges className="-mt-[3px] -mb-[5px] pt-[3px] pb-[5px]">
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
                {showCommitsTab && (
                  <TabsTrigger value="commits" className="flex-none border-0 px-3 leading-none">
                    Commits{" "}
                    {commits.data && (
                      <span className="text-muted-foreground/70 tabular-nums">
                        {commits.data.total_count}
                      </span>
                    )}
                  </TabsTrigger>
                )}
                <TabsTrigger value="changes" className="flex-none border-0 px-3 leading-none">
                  Changes
                </TabsTrigger>
              </TabsList>
            </TabStripScroller>
          </PullRequestHeaderSlot>

          {/* Summary: reviewers, CI checks, and the PR description. A size container,
            so the description editor can fit itself to the visible height (cqh). */}
          <PullRequestSummarySlot layout={layout} state={layoutState}>
            <PullRequestSummaryTab
              reviewers={reviewersSection}
              labels={labelsSection}
              merge={mergeSection}
              checks={checksSection}
              description={descriptionSection}
            />
          </PullRequestSummarySlot>

          {/* The new page's comments are Overview's Activity; only the classic page has this tab. */}
          {!layout && (
            <TabsContent value="comments" className="flex min-h-0 flex-1 flex-col overflow-hidden">
              <PullRequestCommentRecoveries
                conversationId={conversationId}
                prUrl={threadContext.prUrl}
                listedIds={listedCommentIds}
                active={active && activeTab === "comments"}
              />
              <div
                role="region"
                aria-label="PR comments"
                tabIndex={0}
                className="min-h-0 flex-1 overflow-y-auto"
              >
                <PullRequestCommentsTab
                  comments={comments}
                  commentContext={threadContext}
                  hasComposer={!!commentComposer}
                  commentsPartial={pr.comments_partial}
                  events={pr.timeline_events}
                  timelinePartial={pr.timeline_partial}
                  providerLabel={copy.label}
                  pullRequestAuthor={pr.author}
                  pullRequestUrl={pr.url}
                />
              </div>
              {commentComposer}
            </TabsContent>
          )}

          {showCommitsTab && (
            <TabsContent value="commits" className="flex min-h-0 flex-1 flex-col">
              <PullRequestCommitsTab
                status={commitsTab.status}
                onRetry={commitsTab.retry}
                revision={{ headSha, baseSha }}
                source={commitsSource}
                commits={commits}
                selectedSha={selectedCommit}
                onSelectCommit={onSelectCommit}
                onOpenCommitInNewTab={
                  onOpenInNewTab && ((sha, options) => onOpenInNewTab({ commit: sha }, options))
                }
                commands={commitCommandsById}
                focusRequest={commitFocusRequest}
                onFocusRequestSettled={settleCommandFocus}
                provider={data.provider}
                prUrl={threadContext.prUrl}
                // The new page lists commits as the reference's timeline of cards.
                timeline={layout !== undefined}
                active={active && activeTab === "commits"}
                renderFiles={(commitFiles, commitFilesByPath) =>
                  sharedReview && selectedCommit ? (
                    <PullRequestCommitReview
                      key={selectedCommit}
                      files={commitFiles}
                      filesByPath={commitFilesByPath}
                      conversationId={conversationId}
                      prUrl={threadContext.prUrl}
                      provider={data.provider}
                      sha={selectedCommit}
                      repositories={sourceRepository}
                      layout={diffStyle}
                      onLayoutChange={changeDiffStyle}
                      active={active}
                    />
                  ) : (
                    <div className="github-diff-stack min-h-0 flex-1 overflow-y-auto">
                      <CommitFileSections
                        key={selectedCommit}
                        files={commitFiles}
                        filesByPath={commitFilesByPath}
                        options={commitDiffOptions}
                        context={{ ...threadContext, canReply: false, canCreate: false }}
                      />
                    </div>
                  )
                }
              />
            </TabsContent>
          )}

          {/* Changes: a controls row, then the sidebar (jump-to-file) + one scroll
            of all files' diffs. */}
          <TabsContent value="changes" className="flex min-h-0 flex-1 flex-col">
            {pr.url && (
              <div className="shrink-0 border-b border-border px-3 py-1 text-ui empty:hidden">
                <PendingReviewNotice
                  state={pendingReview.state}
                  notPlaced={notPlacedDrafts}
                  conversationId={conversationId}
                  pullRequestUrl={pr.url}
                  onRetry={pendingReview.refetch}
                />
              </div>
            )}
            {data.capabilities.review_threads &&
              (reviews.page?.has_more ||
                reviews.historyStale ||
                reviews.page?.warning ||
                reviews.error) && (
                <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-3 py-1 text-ui">
                  {reviews.page?.has_more && (
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={reviews.loadingMore}
                      onClick={reviews.loadMore}
                    >
                      {reviews.loadingMore ? "Loading…" : "Load more conversations"}
                    </Button>
                  )}
                  {reviews.historyStale && (
                    <span className="text-muted-foreground">
                      Older pages may have changed.{" "}
                      <button type="button" className="underline" onClick={reviews.refreshHistory}>
                        Reload conversations
                      </button>
                    </span>
                  )}
                  {(reviews.page?.warning || reviews.error) && (
                    <span role="status" className="text-destructive">
                      {reviews.error ?? reviews.page?.warning}
                    </span>
                  )}
                </div>
              )}
            {(changes.data?.has_more || changes.data?.warning) && (
              <p
                role="status"
                className="shrink-0 border-b border-border p-2 text-ui text-muted-foreground"
              >
                {changes.data.warning || "The changed-file list is incomplete."}
              </p>
            )}
            {requestedMissing && (
              <div
                role="status"
                className="flex shrink-0 items-center gap-2 border-b border-border p-2 text-ui text-muted-foreground"
              >
                <span className="min-w-0 flex-1">
                  {requestedPath} isn&apos;t in this pull request&apos;s changes.
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => onFileChosen?.(undefined, { replace: true })}
                >
                  Show all changes
                </Button>
              </div>
            )}
            {changesStackDiff && changesStackDiff.effective.kind !== "all" ? (
              // Only an accepted comparison shows files; comparing or stale shows none.
              changesStackDiff.effective.kind === "this-pr" ? (
                <PullRequestStackOnlyReview
                  comparison={changesStackDiff.effective.comparison}
                  parentLabel={changesStackDiff.parent.label}
                  conversationId={conversationId}
                  prUrl={threadContext.prUrl}
                  provider={data.provider}
                  repositories={{
                    ...sourceRepository,
                    parentHeadRepository: changesStackDiff.parent.headRepository,
                  }}
                  conversationCount={threads.length}
                  onShowAllChanges={() => changesStackDiff.choose("all")}
                  revealPath={requestedMissing ? null : (requestedPath ?? null)}
                  onOpenFileInNewTab={
                    onOpenInNewTab && ((path, options) => onOpenInNewTab({ path }, options))
                  }
                  layout={diffStyle}
                  onLayoutChange={changeDiffStyle}
                  active={active}
                  base={stackDiffReviewBase(changesStackDiff)}
                />
              ) : (
                <StackDiffWaiting diff={changesStackDiff} />
              )
            ) : changesPageState ? (
              // Without a diff the new page shows one message across the pane, with no file list.
              <div className="github-diff-stack flex min-h-0 flex-1 flex-col overflow-y-auto">
                {changesStackDiff && <StackDiffBaseRow diff={changesStackDiff} />}
                {changesPageState}
                <EarlierConversations threads={earlierThreads} context={threadContext} />
              </div>
            ) : sharedReview && canShowFiles ? (
              <>
                {changesUpdateNotice}
                <PullRequestChangesReview
                  revealPath={requestedMissing ? null : (requestedPath ?? null)}
                  files={files}
                  filesByPath={filesByPath}
                  patch={patchState}
                  onRetryPatch={retryPatch}
                  threadsByFile={threadsByFile}
                  elsewhereThreads={earlierThreads}
                  context={threadContext}
                  fileRevision={fileRevision}
                  matchingRevision={matchingRevision}
                  revision={revision}
                  layout={diffStyle}
                  onLayoutChange={changeDiffStyle}
                  loadDiffFiles={loadDiffFiles}
                  focusedThread={focusedThread}
                  unresolvedCount={unresolved.length}
                  onNavigateUnresolved={navigateThread}
                  loadingThreads={reviews.isFetching}
                  active={active}
                  onOpenFileInNewTab={
                    onOpenInNewTab && ((path, options) => onOpenInNewTab({ path }, options))
                  }
                  onFileChosen={chooseReviewFile}
                  ownedPaths={ownedChangedFiles}
                  base={changesStackDiff && stackDiffReviewBase(changesStackDiff)}
                />
              </>
            ) : (
              <>
                {(files.length > 0 || unresolved.length > 0 || reviews.isFetching) && (
                  <div className="flex w-full shrink-0 items-center justify-between gap-2 border-b border-border px-2 py-1">
                    <div className="flex items-center gap-0.5">
                      {files.length > 0 && (
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
                      )}
                      {files.length > 0 && <ChangedFilesSummary files={files} />}
                      {unresolved.length > 0 && (
                        <>
                          <IconButton
                            label="Previous unresolved conversation"
                            onClick={() => navigateThread(-1)}
                            className="size-5 text-muted-foreground"
                          >
                            <ChevronUpIcon className="size-3.5" />
                          </IconButton>
                          <IconButton
                            label="Next unresolved conversation"
                            onClick={() => navigateThread(1)}
                            className="size-5 text-muted-foreground"
                          >
                            <ChevronDownIcon className="size-3.5" />
                          </IconButton>
                        </>
                      )}
                    </div>
                    <div className="flex items-center gap-0.5">
                      {reviews.isFetching && (
                        <span
                          role="status"
                          className="mr-1 flex items-center gap-1 whitespace-nowrap text-xs text-muted-foreground"
                        >
                          Loading review threads
                          <Loader2Icon className="size-3 animate-spin" aria-hidden="true" />
                        </span>
                      )}
                      {files.length > 0 && (
                        <>
                          <IconButton
                            label={
                              diffStyle === "split"
                                ? "Switch to unified view"
                                : "Switch to split view"
                            }
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
                        </>
                      )}
                    </div>
                  </div>
                )}
                <div
                  ref={bodyRef as React.RefObject<HTMLDivElement>}
                  className="flex min-h-0 flex-1"
                >
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
                      ) : changes.error && changes.data === undefined ? (
                        <p className="px-2 py-1 text-ui text-muted-foreground">
                          {changes.error instanceof RunnerOfflineError
                            ? "Runner offline."
                            : (changes.error as Error).message}
                        </p>
                      ) : files.length === 0 ? (
                        <p className="px-2 py-1 text-ui text-muted-foreground">
                          {changes.data?.available === false ||
                          changes.data?.has_more ||
                          changes.data?.warning
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
                            onSelectFile={openSidebarFile}
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
                    {changesUpdateNotice}
                    {changesError || parseError ? (
                      <PanelMessage>
                        <p role="alert">{parseError || (changesError as Error).message}</p>
                      </PanelMessage>
                    ) : prDiff.data?.unavailable_reason ? (
                      <PanelMessage>{diffUnavailableMessage(prDiff.data)}</PanelMessage>
                    ) : files.length === 0 || prDiff.isLoading ? (
                      <PanelMessage>
                        {changes.isLoading || prDiff.isLoading ? (
                          <>
                            <Loader2Icon className="size-5 animate-spin" />
                            Loading changes…
                          </>
                        ) : changedFilesUnavailable(changes.data) ? (
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
                          threads={threadsByFile.get(file.path) ?? NO_REVIEW_THREADS}
                          threadContext={threadContext}
                          matchingRevision={matchingRevision}
                          contextLoads={contextLoads}
                        />
                      ))
                    )}
                    <EarlierConversations threads={earlierThreads} context={threadContext} />
                  </div>
                </div>
              </>
            )}
          </TabsContent>
        </Tabs>
      </div>
    </PullRequestDetailsProviders>
  );
}

/** Why the PR's diff can't show, as the host or the reader explained it. */
function diffUnavailableMessage(diff: {
  message?: string | null;
  unavailable_reason?: string | null;
}) {
  return (
    diff.message ||
    (diff.unavailable_reason === "pr_outside_workspace"
      ? "Diff unavailable for a PR outside this workspace's repository"
      : "Diff unavailable. Refresh or open the request on its provider.")
  );
}

/** The changed-files read answered without the full list: unavailable, partial or warned. */
function changedFilesUnavailable(
  data: { available?: boolean; has_more?: boolean; warning?: string | null } | undefined,
): boolean {
  return data?.available === false || !!data?.has_more || !!data?.warning;
}

/** Focus moves aimed at a section's tab, or the picker standing in for it. */
type SectionFocusTarget = `${PullRequestSubtab}-tab`;

function sectionFocusTarget(section: PullRequestSubtab): SectionFocusTarget {
  return `${section}-tab`;
}

const SECTION_FOCUS_TARGETS: ReadonlySet<unknown> = new Set<SectionFocusTarget>([
  "summary-tab",
  "comments-tab",
  "commits-tab",
  "changes-tab",
]);

function isSectionFocusTarget(target: unknown): target is SectionFocusTarget {
  return SECTION_FOCUS_TARGETS.has(target);
}

/**
 * The comparison menu where Changes show no review to hold it, in the toolbar's place, with a
 * fallback's reason beside it since there are no files to hover past.
 */
function StackDiffBaseRow({ diff }: { diff: PullRequestStackDiff }) {
  const { effective } = diff;
  const reason = effective.kind === "all" && effective.fallback ? stackDiffStatusText(diff) : null;
  return (
    <div
      className="flex min-h-9 shrink-0 items-center gap-1 border-b border-border px-2 py-0.5 text-ui"
      data-testid="pull-request-stack-diff-base-row"
    >
      <DiffReviewBaseMenu base={stackDiffReviewBase(diff)} />
      {reason && <span className="min-w-0 truncate text-muted-foreground">{reason}</span>}
    </div>
  );
}

/** A stacked PR's Changes while their comparison with its parent is read again or went stale. */
function StackDiffWaiting({ diff }: { diff: PullRequestStackDiff }) {
  const reload = reloadStackStatusCommand(diff);
  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="pull-request-stack-diff-waiting">
      <StackDiffBaseRow diff={diff} />
      {diff.effective.kind === "stale" ? (
        <PanelStateMessage
          variant="empty"
          title="Stack Changed"
          actions={
            reload && (
              <Button variant="outline" size="sm" onClick={reload.run}>
                Reload
              </Button>
            )
          }
        >
          {stackDiffStatusText(diff)}
        </PanelStateMessage>
      ) : (
        <PanelStateMessage variant="loading">{stackDiffStatusText(diff)}</PanelStateMessage>
      )}
    </div>
  );
}
