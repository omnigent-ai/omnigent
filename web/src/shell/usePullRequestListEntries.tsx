// The Pull Requests list's data and row actions, shared by every list layout: each linked
// PR's live read, its stats and merge readiness, the row model, the actions its menus and
// ⌘K run, and the merge dialog those actions open.

import { isReadOnlyHostPullRequest } from "@/lib/readOnlyCodeHosts";
import { useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useCallback, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import {
  CheckCircle2Icon,
  ExternalLinkIcon,
  GitMergeIcon,
  type LucideIcon,
  MessageSquareIcon,
  Trash2Icon,
} from "lucide-react";
import type { PageGrouping, RepositoryExclusion } from "@/pullRequestList/pullRequestListPageModel";
import {
  MERGE_ON_GITHUB,
  mergeOnGitHub,
  mergeOnGitHubDescription,
  type NativeStackMergeHandoff,
} from "@/pullRequestPage/nativeStackMerge";
import {
  normalizePullRequestInfo,
  usePullRequestInfos,
  type PullRequest,
  type PullRequestAssociation,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";
import { useCanChangePullRequests } from "@/hooks/usePermissions";
import { useStableCallback } from "@/hooks/useStableCallback";
import {
  offeredEditCapabilities,
  usePullRequestEditsServed,
} from "@/newPullRequestPage/hooks/pullRequestEditOffers";
import { useMergeReadiness } from "@/hooks/useMergeReadiness";
import {
  usePullRequestListStats,
  withKnownStats,
  type PullRequestListStatsResult,
} from "@/hooks/usePullRequestListStats";
import { RunnerOfflineError } from "@/lib/workspaceReadErrors";
import { pullRequestFastTrackAccount } from "@/lib/githubFastTrack";
import { useGitHubFastTrackOpen } from "@/lib/githubFastTrackBridge";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";
import { useEnabledReviewSite } from "@/hooks/usePullRequestReviewSite";
import {
  pullRequestListStatsDisplayed,
  type PullRequestListStats,
  type PullRequestListStatsGap,
  type PullRequestListStatsTarget,
} from "@/lib/pullRequestListStats";
import { PULL_REQUEST_LIST_POLL_MS } from "@/lib/pullRequestPolling";
import { PULL_REQUEST_ROW_BACKSTOP_60_FLAG } from "@/lib/pullRequestRowSnapshots";
import { useDesktopFeatureFlag } from "@/hooks/useDesktopFeatureFlag";
import {
  REQUEST_REVIEW_ICON,
  REQUEST_REVIEW_KEYWORDS,
  REQUEST_REVIEW_LABEL,
  requestReviewOffered,
  requestReviewPaletteLabel,
} from "@/shell/pullRequestCommands/requestReview";
import { pullRequestChangeInFlight } from "@/pullRequestPage/pullRequestChangeInFlight";
import { stateChainKey } from "@/pullRequestPage/pullRequestStateChain";
import { useStateChangeHoldsVersion } from "@/pullRequestPage/usePullRequestStateAction";
import { useRowLifecycleActions } from "@/pullRequestPage/useRowLifecycleActions";
import { reviewOfferFor } from "@/pullRequestReviews/useReviewSubmit";
import { requestReviewMenu } from "@/pullRequestReviews/reviewSubmit";
import { usePullRequestActionOrigin, withRowLanding } from "@/lib/pullRequestActionOrigin";
import { requestPullRequestControl } from "@/pullRequestPage/controlRequests";
import { useRowControlPlace } from "@/pullRequestPage/pullRequestDestination";
import { gitProviderCopy, hostedOnGitHub } from "@/lib/gitProviders";
import { openLink } from "@/lib/openLinkInApp";
import {
  AVAILABLE,
  isCommandListed,
  type PaletteCommand,
  unavailableBecause,
} from "@/store/paletteCommands";
import {
  listRemovalOf,
  removalDescription,
  removalPlace,
  type RowRemoval,
} from "@/pullRequestList/subAgentRemoval";
import { PULL_REQUEST_COMMAND_ICONS } from "./pullRequestCommands/icons";
import {
  EDIT_LABELS_ACTION_ID,
  EDIT_LABELS_LABEL,
  editLabelsAvailability,
  editLabelsPaletteLabel,
  LABEL_COMMAND_ICON,
  LABEL_COMMAND_KEYWORDS,
} from "./pullRequestLabelCommands";
import { useAutomaticReadsAllowed } from "@/lib/githubAutomaticReads";
import { githubReaderCooldownKey } from "@/lib/githubCooldown";
import { useListReadTargets } from "@/pullRequestList/useListReadTargets";
import {
  pullRequestCommands,
  pullRequestPaletteName,
  removeFinishedPullRequestsCommand,
} from "./pullRequestCommands/model";
import { runPullRequestCommand } from "./pullRequestCommands/runPullRequestCommand";
import {
  mergeButtonState,
  PullRequestMergeDialog,
  useExpiringValue,
} from "../newPullRequestPage/shell/PullRequestMerge";
import type { PullRequestOpenIntent } from "./pullRequestOpenIntent";
import {
  hostListStatsTargets,
  isFinishedState,
  pullRequestListRow,
  pullRequestListState,
  viewerLogin,
  type PullRequestListRow,
  type PullRequestListState,
} from "./pullRequestListModel";
import {
  type PullRequestListEntry,
  rowActionCommand,
  type RowAction,
  rowCommandTarget,
} from "./PullRequestListRows";
import type { ListSelectionAction } from "@/pullRequestList/listSelectionActions";
import type { RepositoryTarget } from "@/pullRequestList/repositoryGroupActions";
import { withExtraRowActions } from "./rowActionOrder";
import { useSessionPullRequestLinker } from "./pullRequestLinks/useLinkToSession";
import { isNotLinkedRefusal } from "@/pullRequestList/notLinkedRow";
import { useOpenSessionPage } from "./OpenSessionPageContext";
import type { PullRequestSessionSource } from "@/pullRequestList/subAgentPullRequests";
import { useRowStateChanges } from "./useRowStateChanges";
import { approveRowOnHost, rowApproveLogin } from "./pullRequestRowApprove";
import {
  isBeingRemoved,
  removalIncludes,
  removalPendingReason,
  type RemovalProgress,
} from "@/pullRequestList/removalProgress";

/** A group whose header offers actions: how the list groups, and which group. */
export interface PageGroupTarget {
  grouping: PageGrouping;
  id: string;
  title: string;
  /** The status a Status group lists. */
  state?: PullRequestListState;
  /** A Repository group's repositories, by provider and host: one name can sit on two hosts. */
  repositories?: readonly RepositoryTarget[];
  /** Its rows as shown when its menus were built: not filtered, folded or collapsed away. */
  entries: readonly PullRequestListEntry[];
  /** Its rows as shown now, for an action run later; null once it's gone or collapsed. */
  shownEntries: () => readonly PullRequestListEntry[] | null;
}

/** An action on a group header's menus, and in ⌘K where it names itself there. */
export interface GroupAction {
  id: string;
  label: string;
  icon: LucideIcon;
  /** Lists it in ⌘K, e.g. "Mark All Ready for Review in Draft (3)". */
  paletteLabel?: string;
  keywords?: string[];
  /** Acts on the group's rows: disabled while it's collapsed, and doesn't keep a header shown. */
  onRows?: boolean;
  /** Shown under it, disabled, where it can't run now. */
  unavailableReason?: string;
  run: () => void;
}

/** What the panel hands every Pull Requests list layout. */
export interface PullRequestListProps {
  conversationId: string;
  /**
   * What owns the rows' toasts across this list's replacement, such as the page that mounts a
   * new table on each tab: their Undo and Try Again run through its list mounted now. Without
   * one they run only while this list stays mounted.
   */
  recoveryScope?: string;
  /** False while the panel is hidden; pauses polling. */
  active: boolean;
  /** The session's PRs, each carrying its provider. */
  associations: PullRequestAssociation[];
  /** Which sub-agent linked each PR of `associations`, and the others that did; by URL. */
  sessionSources?: ReadonlyMap<string, PullRequestSessionSource>;
  /** Whether the user owns the shown session tree, so removal reaches its sub-agents' links. */
  ownsSessionTree?: boolean;
  /** The list belongs to one session, so its sub-agents' PRs join it and it groups by sub-agent. */
  subAgentPullRequests?: boolean;
  hostOffline: boolean;
  sandboxAsleep: boolean;
  /** The PR the panel last showed, marked so returning to the list keeps your place. */
  selectedUrl?: string;
  /** Open a row's PR: shown here, or in the background where tabs can hold it. */
  onSelect: (url: string, intent: PullRequestOpenIntent) => void;
  /** Rows offer "Open in new tab": the tabs sidebar's overview, where each PR has its own tab. */
  opensInNewTab?: boolean;
  /**
   * Rows offer "Open in workspace sidebar", as their ⌘-click does, which reaches `onSelect`
   * as a "foreground" open: the Pull Requests page, whose sidebar holds PRs opened beside it.
   */
  opensInWorkspace?: boolean;
  /** Absent when the host can't unlink PRs. */
  onUnlink?: (urls: string[]) => void;
  /**
   * The list belongs to one session's workspace, so rows select and can be removed from it.
   * Off on the Pull Requests page, which shows PRs from many sessions and GitHub.
   */
  managesSessionLinks?: boolean;
  /**
   * What selected rows offer beyond removing them from a session, for the selection bar and
   * ⌘K; a list with these selects rows even where it doesn't manage session links.
   */
  selectionActions?: (selected: readonly PullRequestListEntry[]) => readonly ListSelectionAction[];
  /** Selection is cleared when this changes, e.g. as the page reads as another account. */
  selectionScope?: string;
  unlinkPending: boolean;
  /** The removal under way while `unlinkPending`, which says how far it got. */
  removalProgress?: RemovalProgress | null;
  /** The panel's own actions, for a layout that draws its own header; others leave them be. */
  headerActions?: ReactNode;
  /** Whether the header shows its own title; a page that names itself turns it off. */
  showHeading?: boolean;
  /** Notices about this list, between the status filters and the table. */
  notices?: ReactNode;
  /** Shown in the table's place (loading, an error), keeping the header where it is. */
  placeholder?: ReactNode;
  /**
   * Shown in the table's place when the list has no PRs at all; the filter field, Display
   * and status filters step aside, as there is nothing for them to act on.
   */
  emptyState?: ReactNode;
  /**
   * More actions for a row's menus and ⌘K: before its copy actions, or for `page` ones,
   * after them and before Unlink.
   */
  extraRowActions?: (url: string) => RowAction[];
  /**
   * Offers the Filter menu's fields (repository, author, label, branches, assignee, reviewer)
   * and their chips, as the Pull Requests page does.
   */
  filtersByRepository?: boolean;
  /** "All" lists only PRs still in progress; merged and closed ones show under their own chips. */
  allInProgress?: boolean;
  /**
   * The rows come in a search's own order, which the list keeps, ungrouped, until the user picks
   * another display; Display offers it as Search order.
   */
  searchOrdered?: boolean;
  /** Repositories the caller hides for good; the Filter menu shows and changes them. */
  repositoryExclusion?: RepositoryExclusion;
  /** Actions for a group header's menus, such as hiding a repository's group. */
  groupActions?: (group: PageGroupTarget) => GroupAction[];
  /** Diff and update times the caller already read, by PR URL; a row's own read wins. */
  knownStats?: ReadonlyMap<string, PullRequestListStats>;
  /**
   * The merge GitHub makes for a PR in its own stack, as the caller's read of it says; null
   * where the PR merges here or the read can't say. Its row's Merge opens GitHub instead.
   */
  mergeHandoffOf?: (url: string) => NativeStackMergeHandoff | null;
  /**
   * The rows as shown, top to bottom, after filter, sort and collapsed groups, on each change;
   * null when the list itself goes.
   */
  onVisibleOrderChange?: (urls: readonly string[] | null) => void;
}

export interface PullRequestListEntries {
  entries: PullRequestListEntry[];
  rows: PullRequestListRow[];
  entriesByUrl: ReadonlyMap<string, PullRequestListEntry>;
  stats: PullRequestListStatsResult;
  /** The list lays out its Diff and Updated columns: the desktop shell can read them. */
  statsColumns: boolean;
  canUnlink: boolean;
  /** What removing a row unlinks, the one rule every removal route follows. */
  /** What removing a shown row takes away; nothing for a URL the list doesn't show. */
  removalOf: (url: string) => RowRemoval | undefined;
  /** Merged and closed rows, which "Unlink all" and its command remove. */
  finished: string[];
  actionsFor: (entry: PullRequestListEntry) => RowAction[];
  /** GitHub accepted a merge of this PR; the row says so until a refetch shows it merged. */
  mergeRequestedFor: (url: string) => boolean;
  /** Unlinks every merged and closed row; absent when there are none or unlinking isn't allowed. */
  unlinkFinishedCommand: PaletteCommand | undefined;
  /** Each row's actions as ⌘K commands. */
  rowCommands: PaletteCommand[];
  /** The merge confirmation a row action opened; render it beside the list. */
  mergeDialog: ReactNode;
  /** What a state change GitHub is making to `url` now says on its row, e.g. "Closing…". */
  changeInFlightFor: (url: string) => string | null;
}

export interface PullRequestListEntriesOptions {
  /** The new page's list: rows that ask the host's account for a review offer Approve. */
  approveOnRows?: boolean;
  /** The new page's list: rows offer Ready for review and Convert to draft, made on this Mac. */
  lifecycleOnRows?: boolean;
  /**
   * A list that draws only the rows near its viewport: only the rows in view, once they've
   * stayed there, and these exceptions read GitHub. Without it every row reads.
   */
  readWindow?: { visible: readonly string[]; exceptions: readonly string[] };
  /**
   * The new page's list, whose rows say how old their reviewers are: only it reads rows hourly
   * and slows its polls while nobody uses the window. Without it, a list keeps today's cadence.
   */
  showsReadAges?: boolean;
}

const NO_URLS: readonly string[] = [];

export function usePullRequestListEntries(
  {
    conversationId,
    recoveryScope,
    active,
    associations,
    sessionSources,
    ownsSessionTree,
    hostOffline,
    onSelect: onSelectProp,
    opensInNewTab = false,
    opensInWorkspace = false,
    onUnlink,
    managesSessionLinks = true,
    unlinkPending,
    removalProgress = null,
    extraRowActions,
    knownStats,
    mergeHandoffOf,
  }: PullRequestListProps,
  options: PullRequestListEntriesOptions = {},
): PullRequestListEntries {
  // One identity, so a parent that rebuilds its handler each render leaves the rows' actions be.
  const onSelect = useStableCallback(onSelectProp);
  const openSessionPage = useOpenSessionPage();
  const canEdit = useCanChangePullRequests(conversationId);
  // Rows offer an optional edit only where the session's host serves all it needs.
  const editsServed = usePullRequestEditsServed(conversationId);
  // The session's panel that shows this list reads its stored links; rows only follow them.
  const linker = useSessionPullRequestLinker(conversationId, { readsStoredList: false });
  // A followed row reads at least hourly, at a time of its own, instead of every 10 minutes,
  // only where its Reviewers cell says how old they are.
  const showsReadAges = options.showsReadAges === true;
  const hourlyBackstop =
    useDesktopFeatureFlag(PULL_REQUEST_ROW_BACKSTOP_60_FLAG, true).value && showsReadAges;
  const queryClient = useQueryClient();
  // A sub-agent's row reads through the session that links it, as the server's tree names it:
  // the server only reads a PR for a session that links it.
  const linkingSessionOf = useCallback(
    (url: string) => sessionSources?.get(url)?.session?.id,
    [sessionSources],
  );
  // A windowed list reads only its read targets; the other rows show what's cached.
  const readWindow = options.readWindow;
  const automaticReads = useAutomaticReadsAllowed(githubReaderCooldownKey("github.com", null));
  const latestInfos = useRef<Map<string, { data?: unknown; isStale: boolean }>>(new Map());
  const readTargets = useListReadTargets({
    visible: readWindow?.visible ?? NO_URLS,
    exceptions: readWindow?.exceptions ?? NO_URLS,
    needsRead: (url) => {
      const info = latestInfos.current.get(url);
      return !info?.data || info.isStale;
    },
    allowed: automaticReads,
  });
  const readUrls = readWindow ? readTargets : undefined;
  const infos = usePullRequestInfos(
    conversationId,
    associations.map((association) => association.url),
    // A windowed list observes only its read targets: every other row shows its cached answer.
    {
      poll: active ? "list" : false,
      readUrls,
      observedUrls: readUrls,
      hourlyBackstop,
      idlePaced: showsReadAges,
      linkingSessionOf,
    },
  );
  latestInfos.current = new Map(
    associations.map((association, index) => [association.url, infos[index] ?? { isStale: true }]),
  );

  // A merge started from a row menu or ⌘K. It keeps its session and the PR it opened with,
  // so an offline refresh or a session switch can't drop the merge in flight.
  const [confirming, setConfirming] = useState<{
    conversationId: string;
    url: string;
    headSha: string;
    pr: PullRequest;
    hint: string | undefined;
  }>();
  // What GitHub merges for a PR now, read when an action runs rather than when it was offered.
  const handoffRef = useRef(mergeHandoffOf);
  handoffRef.current = mergeHandoffOf;
  const handoffNow = useCallback((url: string) => handoffRef.current?.(url) ?? null, []);
  // GitHub accepted this {@link mergeRequestKey}; the row says so until the refetch shows it merged.
  const [mergeRequested, setMergeRequested] = useExpiringValue<string>();

  // Each PR's live state as its host answers it, before the list model reads it.
  const live = useMemo(
    () =>
      associations.map((association, index) => {
        const info = infos[index];
        const data = info?.data ? normalizePullRequestInfo(info.data) : undefined;
        const offline = hostOffline || info?.error instanceof RunnerOfflineError;
        const pr = !offline && data?.pr?.url === association.url ? data.pr : undefined;
        return { association, info, data, offline, pr };
      }),
    [associations, infos, hostOffline],
  );
  // Diff size and update times come from the session's host where it serves them; elsewhere
  // the desktop shell's fast track reads them as each PR's own host account. The fast track
  // closes once the page's server moves.
  const hostReadsStats = usePullRequestMethodServed(conversationId, "getPullRequestListStats");
  const fastTrackOpen = useGitHubFastTrackOpen();
  const fastTrackTargets = useMemo(
    () =>
      live.flatMap(({ association, data, offline, pr }): PullRequestListStatsTarget[] => {
        const account =
          fastTrackOpen && !hostReadsStats && !offline && data
            ? pullRequestFastTrackAccount({
                provider: data.provider,
                prHost: association.host,
                selectedAccount: data.auth.selected_account,
              })
            : null;
        if (!account) return [];
        return [
          {
            url: association.url,
            source: { kind: "github-fast-track", account },
            repository: association.repository,
            number: association.number,
            headSha: pr?.head_sha,
            open: !isFinishedState(pullRequestListState(pr ?? association)),
          },
        ];
      }),
    [live, fastTrackOpen, hostReadsStats],
  );
  const hostTargets = useMemo(
    () =>
      hostReadsStats
        ? hostListStatsTargets(
            live.map(({ association, offline, pr }) => ({ association, offline, pr })),
            conversationId,
          )
        : [],
    [conversationId, hostReadsStats, live],
  );
  const statsTargets = useMemo(
    () => [...fastTrackTargets, ...hostTargets],
    [fastTrackTargets, hostTargets],
  );
  // Rows answer one by one, on first load and on every refresh; stats batches read after
  // the last answer, so it's one stats read per refresh.
  const fastTrackSettled = live.every(({ info }) => !info?.isFetching);
  // A windowed list batches stats only for its read targets, and shows each row's newest.
  const readStats = usePullRequestListStats(statsTargets, {
    enabled: active,
    fastTrackSettled,
    followsWindow: readUrls !== undefined,
    readUrls,
    idlePaced: showsReadAges,
  });
  const stats = useMemo(() => withKnownStats(readStats, knownStats), [readStats, knownStats]);
  // Laid out where a shell bridge reads them, or the session's host does.
  const statsColumns = pullRequestListStatsDisplayed() || hostReadsStats;
  const statsServices = useMemo(
    () => new Map(statsTargets.map((target) => [target.url, "github" as const] as const)),
    [statsTargets],
  );
  // A registered merge readiness source reads its PRs as each PR's own account.
  const readinessTargets = useMemo(
    () =>
      fastTrackTargets.map((target) => ({
        url: target.url,
        account: target.source.kind === "github-fast-track" ? target.source.account : null,
        headSha: target.headSha,
      })),
    [fastTrackTargets],
  );
  // Where this list's rows open a PR, for a row's request to open one of its controls there.
  const rowPlace = useRowControlPlace();
  const infoByUrl = useMemo(
    () => new Map(live.flatMap(({ association, data }) => (data ? [[association.url, data]] : []))),
    [live],
  );
  const mergeReadiness = useMergeReadiness(readinessTargets, {
    active,
    pollMs: PULL_REQUEST_LIST_POLL_MS,
  });

  const entries = useMemo<PullRequestListEntry[]>(() => {
    return live.map(({ association, info, data, offline, pr }) => {
      const linkState = isNotLinkedRefusal(info?.error)
        ? linker.stateOf(association.url)
        : undefined;
      const merge = pr ? mergeButtonState(pr) : undefined;
      const mergeable =
        canEdit &&
        !info?.error &&
        !!data?.capabilities.merging &&
        pr?.state === "OPEN" &&
        !pr.is_draft &&
        merge?.kind === "ready" &&
        mergeRequested !== mergeRequestKey(conversationId, association.url, merge.headSha);
      return {
        association,
        // Each PR's own account: linked PRs can sit on other hosts or accounts.
        row: {
          ...pullRequestListRow(
            association,
            pr,
            data ? viewerLogin(data.auth) : null,
            stats.byUrl.get(association.url),
            mergeReadiness.get(association.url),
            data?.dispatched_at,
          ),
          sessionSource: sessionSources?.get(association.url),
        },
        pr,
        offline,
        statsService: statsServices.get(association.url),
        statsGap: statsServices.has(association.url)
          ? stats.gaps.get(association.url)
          : stats.byUrl.has(association.url)
            ? undefined
            : untargetedStatsGap(data, offline || !!info?.error),
        // A windowed list's row that hasn't read yet, scrolled off or waiting its turn, isn't
        // failing: it shows what its list already knows.
        status: info?.error
          ? "error"
          : pr
            ? "ready"
            : info?.isLoading || info?.isEnabled === false
              ? "loading"
              : "error",
        error: info?.error,
        ...(linkState?.status === "failed" ? { linkFailure: linkState.message } : {}),
        mergeHead: mergeable && merge?.kind === "ready" ? merge.headSha : undefined,
        mergeHint: merge && "hint" in merge ? merge.hint : undefined,
        mergeUnavailable: offline || !!info?.error || !canEdit || !data?.capabilities.merging,
        editCapabilities: canEdit && data ? data.capabilities : null,
        ...(pr && info?.dataUpdatedAt ? { detailsReadAt: info.dataUpdatedAt } : {}),
      };
    });
  }, [
    live,
    stats.byUrl,
    stats.gaps,
    statsServices,
    mergeReadiness,
    canEdit,
    mergeRequested,
    conversationId,
    sessionSources,
    linker,
  ]);
  const rows = useMemo(() => entries.map((entry) => entry.row), [entries]);
  const entriesByUrl = useMemo(
    () => new Map(entries.map((entry) => [entry.association.url, entry])),
    [entries],
  );
  const canUnlink = managesSessionLinks && canEdit && !!onUnlink;
  // The one removal rule for every route: which sessions' links a row's Remove takes away.
  const removalOf = useMemo(
    () =>
      listRemovalOf({
        shown: entries.map((entry) => entry.association.url),
        sources: sessionSources,
        viewedSessionId: conversationId,
        canEditViewed: canUnlink,
        ownsTree: canUnlink ? ownsSessionTree : false,
      }),
    [canUnlink, conversationId, entries, ownsSessionTree, sessionSources],
  );
  const removeLinks = useCallback(
    (urls: readonly string[]) => {
      const removable = urls.filter((url) => (removalOf(url)?.targets.length ?? 0) > 0);
      if (removable.length > 0) onUnlink?.(removable);
    },
    [onUnlink, removalOf],
  );

  const reviewSite = useEnabledReviewSite();
  const removal = !canUnlink ? "hidden" : unlinkPending ? "pending" : "available";

  const draftOwner = useWorkspaceDraftOwner();
  // Redrawn as any state change for a row starts or settles, wherever it was asked for.
  const holdsVersion = useStateChangeHoldsVersion();
  const changeInFlightFor = useCallback(
    (url: string) => {
      void holdsVersion;
      return (
        pullRequestChangeInFlight(stateChainKey(draftOwner, url))?.label ??
        (isBeingRemoved(removalProgress, url) ? "Removing…" : null)
      );
    },
    [draftOwner, holdsVersion, removalProgress],
  );
  const mergesOnGitHub = useCallback((url: string) => !!mergeHandoffOf?.(url), [mergeHandoffOf]);
  const { actionsFor: lifecycleActionsFor, dialog: lifecycleDialog } = useRowLifecycleActions(
    conversationId,
    !!options.lifecycleOnRows,
    entriesByUrl,
    recoveryScope,
    mergesOnGitHub,
  );
  const openHere = useCallback((url: string) => onSelect(url, "here"), [onSelect]);
  // Rows offer Approve only where the session's host submits verdicts.
  const approvesOnHost =
    usePullRequestMethodServed(conversationId, "submitReviewVerdict") && !!options.approveOnRows;
  const origin = usePullRequestActionOrigin();
  const rowStateChanges = useRowStateChanges({
    conversationId,
    recoveryScope,
    entriesByUrl,
    onOpen: openHere,
  });
  // A row's Edit Labels… waits while a label change to its PR is still going from this Mac.
  const actionsFor = useCallback(
    (entry: PullRequestListEntry): RowAction[] => {
      const { association, row } = entry;
      const target = rowCommandTarget(entry);
      const handlers = {
        conversationId,
        open: () => onSelect(association.url, "here"),
        openInNewTab: () => onSelect(association.url, "background"),
        openInWorkspace: () => onSelect(association.url, "foreground"),
        remove: () => removeLinks([association.url]),
        link: () => linker.link(association.url),
      };
      const actions: RowAction[] = [];
      const source = row.sessionSource?.session ?? null;
      // A row the session doesn't link: the session's machine won't read it, or this Mac found
      // it on the branch. Only its owner may link it; hidden while ownership is unknown, and
      // after the server refused the user.
      const linkState = linker.stateOf(association.url);
      const unlinked =
        (entry.status === "error" && isNotLinkedRefusal(entry.error)) ||
        (association.relationship === "inferred" && !linker.linkedOf(association.url));
      const offersLink =
        managesSessionLinks &&
        !source &&
        ownsSessionTree === true &&
        unlinked &&
        !(linkState.status === "failed" && linkState.refused);
      const shownRemoval = removalOf(association.url);
      const rowRemovalFacts = shownRemoval ? removalDescription(shownRemoval) : null;
      for (const command of pullRequestCommands(target, {
        openable: true,
        reviewSite,
        removal,
        ...(removal === "pending"
          ? {
              removalPendingReason: removalPendingReason(
                removalProgress,
                removalIncludes(removalProgress, [association.url]),
              ),
            }
          : {}),
        newTab: opensInNewTab,
        workspaceTab: opensInWorkspace,
        ...(offersLink ? { link: linker.availabilityOf(association.url) } : {}),
      })) {
        // The sidebar opens beside the list, so focus comes back from the menu as usual.
        const besideList = command.id === "open-in-workspace";
        if (!isCommandListed(command.availability)) continue;
        // Remove names the agents it unlinks the row from, and why it can't where it can't.
        const removeFacts =
          command.id === "remove" && row.sessionSource && rowRemovalFacts
            ? {
                label: rowRemovalFacts.menuLabel,
                paletteLabel: command.paletteLabel.replace(
                  / from this session$/,
                  rowRemovalFacts.menuLabel.replace(/^Remove/, ""),
                ),
                ...(rowRemovalFacts.detail ? { description: rowRemovalFacts.detail } : {}),
                ...(rowRemovalFacts.unavailableReason && command.availability === AVAILABLE
                  ? { availability: unavailableBecause(rowRemovalFacts.unavailableReason) }
                  : {}),
              }
            : null;
        actions.push({
          id: command.id,
          section: command.section,
          label: command.menuLabel,
          nestedLabel: command.nestedLabel,
          paletteLabel: command.paletteLabel,
          // Clicking the row opens it, so its menus leave Open to ⌘K.
          ...(command.id === "open" ? { paletteOnly: true as const } : {}),
          icon: PULL_REQUEST_COMMAND_ICONS[command.id],
          keywords: command.keywords,
          href: command.href,
          availability: command.availability,
          movesFocus: command.movesFocus,
          ...(besideList
            ? { shortcut: "rows.openInNewTab" as const, afterMenuFocusReturns: true }
            : {}),
          run: () => runPullRequestCommand(command, handlers),
          ...removeFacts,
        });
      }
      if (source) {
        // Opens the sub-agent that linked the PR, in its own session.
        actions.push({
          id: `session:open:${source.id}`,
          section: "session",
          label: `Open ${source.name}`,
          paletteLabel: `Open ${source.name}`,
          description: "The sub-agent that linked this pull request",
          icon: MessageSquareIcon,
          keywords: ["session", "sub-agent", "agent"],
          movesFocus: true,
          run: () => openSessionPage(source.id),
        });
      }
      // This app can't change a read-only host's PR: it offers only what opens the PR there.
      const readOnly = isReadOnlyHostPullRequest(association.url);
      // A sub-agent's row reads through the session that links it. This list's changes go
      // through its own session, which doesn't link the PR, so that row offers none of them.
      const linkingSession = linkingSessionOf(association.url);
      const throughList = !linkingSession || linkingSession === conversationId;
      const changesHere = !readOnly && throughList;
      const rowData = infoByUrl.get(association.url);
      const approveAs =
        approvesOnHost && entry.pr && changesHere && rowData
          ? rowApproveLogin(entry.pr, viewerLogin(rowData.auth))
          : null;
      const approveHead = entry.pr?.head_sha;
      if (approveAs && approveHead) {
        const firstCopy = actions.findIndex((action) => action.section === "copy");
        const openReview = () => {
          onSelect(association.url, "here");
          requestReviewMenu(association.url, "APPROVE", rowPlace);
        };
        actions.splice(firstCopy < 0 ? actions.length : firstCopy, 0, {
          id: "approve",
          section: "review",
          label: "Approve",
          paletteLabel: `Approve ${pullRequestPaletteName(target)}`,
          icon: CheckCircle2Icon,
          keywords: ["approve", "review", "pull request", "pr"],
          // Approves at once with no comment; where it can't, the PR's Review menu says why.
          run: () => {
            const sent = approveRowOnHost({
              conversationId,
              owner: draftOwner,
              prUrl: association.url,
              headSha: approveHead,
              login: approveAs,
              origin: withRowLanding(origin, "approved"),
              queryClient,
              onRefused: (message) =>
                toast.error(`Couldn’t approve ${pullRequestPaletteName(target)}: ${message}`, {
                  action: { label: "Open Review", onClick: openReview },
                }),
            });
            if (!sent) openReview();
          },
        });
      }
      // Review…, Request Review… and Edit Labels… open the PR at that control, each where its
      // own control would offer it: the Review menu's offer, and the sections' edit rules.
      const firstCopyAt = () => {
        const at = actions.findIndex((action) => action.section === "copy");
        return at < 0 ? actions.length : at;
      };
      const name = pullRequestPaletteName(target);
      const openAt = (control: "reviewers" | "labels") => () => {
        onSelect(association.url, "here");
        requestPullRequestControl(association.url, control, rowPlace, null);
      };
      const providerLabel = gitProviderCopy(
        association.provider,
        undefined,
        association.provider_display,
      ).label;
      const review = entry.pr
        ? reviewOfferFor({ url: association.url, state: entry.pr.state }, providerLabel)
        : ({ kind: "none" } as const);
      if (review.kind === "link") {
        actions.splice(firstCopyAt(), 0, {
          id: "review-on-provider",
          section: "review",
          label: review.label,
          paletteLabel: `Review ${name} on ${providerLabel}`,
          icon: ExternalLinkIcon,
          keywords: ["review", "browser", "open", "pull request", "pr"],
          run: () => openLink(conversationId, review.url),
        });
      }
      const info = infoByUrl.get(association.url);
      const editable = canEdit && info ? offeredEditCapabilities(info, editsServed) : null;
      if (changesHere && editsServed.reviewers && requestReviewOffered(entry.editCapabilities)) {
        actions.splice(firstCopyAt(), 0, {
          id: "request-review",
          section: "review",
          label: REQUEST_REVIEW_LABEL,
          paletteLabel: requestReviewPaletteLabel(name),
          icon: REQUEST_REVIEW_ICON,
          keywords: [...REQUEST_REVIEW_KEYWORDS, "pull request", "pr"],
          movesFocus: true,
          run: openAt("reviewers"),
        });
      }
      if (editable?.labels && changesHere) {
        actions.splice(firstCopyAt(), 0, {
          id: EDIT_LABELS_ACTION_ID,
          section: "review",
          label: EDIT_LABELS_LABEL,
          paletteLabel: editLabelsPaletteLabel(name),
          icon: LABEL_COMMAND_ICON,
          keywords: [...LABEL_COMMAND_KEYWORDS],
          availability: editLabelsAvailability(false),
          movesFocus: true,
          run: openAt("labels"),
        });
      }
      const placed = withExtraRowActions(actions, extraRowActions?.(association.url) ?? []);
      const handoff = mergeHandoffOf?.(association.url) ?? null;
      // Merging here needs the host's merge read and write; GitHub's own stack merge doesn't.
      if (entry.mergeHead && entry.pr && changesHere && (handoff || editsServed.merging)) {
        const headSha = entry.mergeHead;
        const pr = entry.pr;
        const url = association.url;
        const firstCopy = placed.findIndex((action) => action.section === "copy");
        placed.splice(
          firstCopy < 0 ? placed.length : firstCopy,
          0,
          handoff
            ? {
                id: "merge",
                section: "merge",
                label: MERGE_ON_GITHUB.label,
                paletteLabel: `Merge ${pullRequestPaletteName(target)} on GitHub`,
                // The PR page's own explanation: what merging there also merges.
                description: mergeOnGitHubDescription(handoff),
                icon: ExternalLinkIcon,
                keywords: [...MERGE_ON_GITHUB.keywords],
                run: () => mergeOnGitHub(handoffNow(url) ?? handoff),
              }
            : {
                id: "merge",
                section: "merge",
                label: "Merge…",
                paletteLabel: `Merge ${pullRequestPaletteName(target)}`,
                icon: GitMergeIcon,
                run: () => {
                  // A menu or ⌘K row kept from before GitHub said it stacks the PR hands off too.
                  const now = handoffNow(url);
                  if (now) {
                    mergeOnGitHub(now);
                    return;
                  }
                  setConfirming({ conversationId, url, headSha, pr, hint: entry.mergeHint });
                },
              },
        );
      }
      // Ready for review, Convert to draft, then Close or Reopen follow merging, before the
      // PR's copies; Convert and Close ask first.
      const lifecycleActions = throughList
        ? lifecycleActionsFor({
            url: association.url,
            number: row.number,
            paletteName: pullRequestPaletteName(target),
            pr: entry.pr,
            onGitHub: hostedOnGitHub(association.provider),
          })
        : [];
      const stateChange = changesHere
        ? rowStateChanges.actionFor(entry, pullRequestPaletteName(target))
        : null;
      const copyAt = placed.findIndex((action) => action.section === "copy");
      placed.splice(
        copyAt < 0 ? placed.length : copyAt,
        0,
        ...lifecycleActions,
        ...(stateChange ? [stateChange] : []),
      );
      return placed;
    },
    [
      approvesOnHost,
      draftOwner,
      origin,
      queryClient,
      canEdit,
      removalOf,
      openSessionPage,
      infoByUrl,
      rowPlace,
      rowStateChanges,
      conversationId,
      extraRowActions,
      reviewSite,
      lifecycleActionsFor,
      onSelect,
      removeLinks,
      opensInNewTab,
      opensInWorkspace,
      removal,
      removalProgress,
      mergeHandoffOf,
      handoffNow,
      linker,
      managesSessionLinks,
      ownsSessionTree,
      linkingSessionOf,
      editsServed,
    ],
  );

  // Finished rows this session can remove: a sub-agent's PR is linked to that sub-agent.
  const finished = useMemo(
    () =>
      rows
        .filter(
          (row) => isFinishedState(row.state) && (removalOf(row.url)?.targets.length ?? 0) > 0,
        )
        .map((row) => row.url),
    [removalOf, rows],
  );
  const unlinkFinishedCommand = useMemo<PaletteCommand | undefined>(() => {
    // ⌘K lists it only with something to remove; the PR header's menu shows it disabled instead.
    if (!canUnlink || finished.length === 0) return undefined;
    const command = removeFinishedPullRequestsCommand({
      finished: finished.length,
      removal: unlinkPending ? "pending" : "available",
      removalPendingReason: removalPendingReason(
        removalProgress,
        removalIncludes(removalProgress, finished),
      ),
    });
    const place = removalPlace(finished.flatMap((url) => removalOf(url) ?? []));
    return {
      id: "unlink-finished",
      label: command.paletteLabel,
      description: place.detail ?? undefined,
      icon: Trash2Icon,
      keywords: [...command.keywords],
      availability: command.availability,
      run: () => removeLinks(finished),
    };
  }, [canUnlink, finished, removalOf, removalProgress, removeLinks, unlinkPending]);
  const rowCommands = useMemo<PaletteCommand[]>(
    () =>
      entries.flatMap((entry) =>
        actionsFor(entry).map((action) => rowActionCommand(entry, action)),
      ),
    [actionsFor, entries],
  );
  const mergeRequestedFor = useCallback(
    (url: string) => !!mergeRequested?.startsWith(mergeRequestKey(conversationId, url, "")),
    [conversationId, mergeRequested],
  );

  // Another session's rows say nothing about a merge started elsewhere, even for the same PR.
  const confirmingEntry =
    confirming?.conversationId === conversationId ? entriesByUrl.get(confirming.url) : undefined;
  // A merge GitHub makes can't be confirmed here: an unsent confirmation goes, for good.
  const confirmingHandsOff = !!confirming && !!mergeHandoffOf?.(confirming.url);
  if (confirmingHandsOff) setConfirming(undefined);
  const mergeConfirmation = confirming && !confirmingHandsOff && (
    <PullRequestMergeDialog
      conversationId={confirming.conversationId}
      prUrl={confirming.url}
      pr={confirmingEntry?.pr ?? confirming.pr}
      headSha={confirming.headSha}
      warning={confirming.hint}
      // Offline, unlinked, or in another session: it shows its outcome but can't confirm.
      unavailable={confirmingEntry?.mergeUnavailable ?? true}
      onMerged={() =>
        setMergeRequested(
          mergeRequestKey(confirming.conversationId, confirming.url, confirming.headSha),
        )
      }
      onClose={() => setConfirming(undefined)}
    />
  );

  // A row's merge and its Enable Auto-merge… each ask in their own dialog, opened from its menu.
  const mergeDialog = (
    <>
      {mergeConfirmation}
      {lifecycleDialog}
    </>
  );

  return {
    entries,
    rows,
    entriesByUrl,
    stats,
    statsColumns,
    canUnlink,
    removalOf,
    finished,
    actionsFor,
    mergeRequestedFor,
    unlinkFinishedCommand,
    rowCommands,
    mergeDialog,
    changeInFlightFor,
  };
}

/**
 * Why a row with no stats read has none, once its host has answered: a GitHub PR whose
 * host named no account, or a code host without the read. Pending or offline says nothing.
 */
function untargetedStatsGap(
  data: { provider: string | null; auth: { selected_account: string | null } } | undefined,
  unanswered: boolean,
): PullRequestListStatsGap | undefined {
  if (!data || unanswered) return undefined;
  if (data.provider === "github") {
    return data.auth.selected_account ? undefined : { kind: "no-account" };
  }
  return data.provider ? { kind: "unsupported-host" } : undefined;
}

/** A merge GitHub accepted, scoped to the session that asked for it. */
function mergeRequestKey(conversationId: string, url: string, headSha: string): string {
  return `${conversationId}|${url}@${headSha}`;
}
