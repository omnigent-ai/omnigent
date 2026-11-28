// Tests for PullRequestPanel — the stacked "Files changed" view. The GitHub data
// hooks and the heavy MonacoDiffViewer are mocked; IntersectionObserver (absent
// in jsdom) is stubbed to fire immediately so lazy sections mount.

import {
  chooseSettingValue,
  expectSetting,
  settingIn,
  settingValues,
} from "@/test/paletteCommands";
import { useState } from "react";
import type { Session } from "@/lib/types";

import { CHECKING_ACCESS_REASON, CommandContextProvider } from "../../shell/commandContext";
import { LINK_PULL_REQUEST_NEEDS_EDIT } from "../../shell/linkPullRequestAvailability";
import { MobilePanelDrawer } from "../../shell/MobilePanelDrawer";
import type * as ChangesReviewModule from "../../shell/PullRequestChangesReview";
import type * as SharedDiffWorkersModule from "@/components/diff-review/sharedDiffWorkers";
import { act, cleanup, render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  requestPullRequestControl,
  resetControlRequestsForTests,
} from "@/pullRequestPage/controlRequests";
import { toast } from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

import type * as StackReadModule from "@/pullRequestPage/usePullRequestStack";
import type * as StackDiffModule from "@/pullRequestPage/stackDiff/usePullRequestStackDiff";
import { usePullRequestStackDiff } from "@/pullRequestPage/stackDiff/usePullRequestStackDiff";
import { comparison as stackComparison } from "@/pullRequestPage/stackDiff/stackDiffFixtures";
import {
  type DiffReviewBase,
  DiffReviewBaseMenu,
} from "@/components/diff-review/DiffReviewBaseMenu";
import { TooltipProvider } from "@/components/ui/tooltip";
import { usePullRequestStackRead } from "@/pullRequestPage/usePullRequestStack";
import { resetHostTransportRecovery } from "@/lib/hostTransportRecovery";
import { observeSandboxResumes, resetHostReturns } from "@/store/hostReturns";
import type * as UsePullRequestCommitsModule from "@/hooks/usePullRequestCommits";
import type * as UsePullRequestsModule from "@/newPullRequestPage/hooks/usePullRequests";
import type * as PullRequestPageRouteModule from "@/pullRequestPage/pullRequestPageRoute";
import type * as IdentityModule from "@/lib/identity";
import type * as WorkspaceSurfacesModule from "@/lib/workspaceSurfaces";
import type * as CodeHostSignInModule from "@/lib/codeHostSignIn";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import type * as UseSessionPullRequestsModule from "@/newPullRequestPage/hooks/useSessionPullRequests";
import {
  sessionPullRequestsKey,
  startSessionUnlinkRun,
} from "@/newPullRequestPage/hooks/useSessionPullRequests";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type {
  PullRequest,
  PullRequestAccount,
  PullRequestAssociation,
  PullRequestAuth,
  PullRequestChangedFile,
  PullRequestComment,
  PullRequestDiffResponse,
  PullRequestInfo,
  PullRequestRepoLabels,
  PullRequestReviewCandidates,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { draftOwner } from "@/lib/workspaceDraftOwner";
import { resetPullRequestDescriptionDrafts } from "./PullRequestDetails";

const state = vi.hoisted(() => ({
  /** The viewer's level in each session's loaded snapshot; `null` while no snapshot has loaded. */
  access: 4 as number | null,
  info: null as {
    data?: PullRequestInfo;
    isLoading: boolean;
    error: unknown;
    isFetching: boolean;
  } | null,
  changes: null as {
    data?: {
      available: boolean;
      data: PullRequestChangedFile[];
      has_more?: boolean;
      warning?: string;
    };
    isLoading: boolean;
    error: unknown;
    isFetching: boolean;
  } | null,
  // Per-file diffs the stubbed parsePatchFiles yields (name + optional
  // rename fields), so tests can exercise renamed/pure-rename rendering.
  parsedFiles: [] as {
    name: string;
    prevName?: string;
    type?: string;
    unifiedLineCount?: number;
  }[],
  // The whole-PR diff response; the panel parses its patch into per-file diffs.
  diff: null as PullRequestDiffResponse | null,
  diffError: null as Error | null,
  diffRefetchError: null as Error | null,
  diffLoading: false,
  refetchDiff: vi.fn(),
  canEdit: true,
  /** Whether the viewer owns the session, which changing its PRs needs. */
  changeAccess: "owner" as "owner" | "not_owner" | "unknown",
  /** A stand-in route to this Mac for the PR's changes, or null for the real one. */
  updateSessionPr: vi.fn(),
  realPrHooks: false,
  commitsPriority: null as string | null,
  /** What a recent list read on this Mac saw of the shown PR's commits. */
  /** Each render's patch read: whether it's enabled, the PR it reads and its revision. */
  diffReads: [] as [boolean, string | undefined, string | undefined][],
  repoLabels: {
    data: undefined as PullRequestRepoLabels | undefined,
    isPending: true,
    isError: false,
    error: null as Error | null,
  },
  updateLabels: { mutate: vi.fn(), isError: false, error: null as Error | null },
  reviewCandidates: {
    data: undefined as PullRequestReviewCandidates | undefined,
    isPending: true,
    isError: false,
    error: null as Error | null,
  },
  updateReviewers: { mutate: vi.fn(), isError: false, error: null as Error | null },
  // The server's stored PRs per session; absent sessions keep what the panel seeded.
  stored: {} as Record<string, PullRequestAssociation[]>,
  // Sessions the server hasn't imported yet; others are synced.
  unsynced: new Set<string>(),
  // Sessions whose stored-list read fails, as a server error does.
  storedFailing: new Set<string>(),
  storedFetches: 0,
  // Set to hold the stored list back, as a slow server does.
  storedGate: null as Promise<void> | null,
  storesPrs: true,
  unlink: {
    mutate: vi.fn(),
    mutateAsync: vi.fn(async (_url: string) => ({ prs: [] as PullRequestAssociation[] })),
    isPending: false,
    isError: false,
    error: null as Error | null,
  },
  unlinkMany: vi.fn(async (_urls: readonly string[]) => [] as PullRequestAssociation[]),
  unlinkTree: vi.fn(async (_removals: readonly unknown[]) => [] as PullRequestAssociation[]),
  rootOwner: "local" as string | null,
  sandboxAsleep: false,
  // False once the session's host disconnects, which leaves cached live info without an error.
  serveable: undefined as boolean | undefined,
}));

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "alice" }));

// The commit list reads for real; it also notes where the page's secondary reads wait now.

vi.mock("@/hooks/usePullRequestCommits", async (importOriginal) => {
  const original = await importOriginal<typeof UsePullRequestCommitsModule>();
  const { useSecondaryReadPriority } = await import("@/hooks/secondaryReadPriority");
  return {
    ...original,
    usePullRequestCommits: (...args: Parameters<typeof original.usePullRequestCommits>) => {
      state.commitsPriority = useSecondaryReadPriority();
      return original.usePullRequestCommits(...args);
    },
  };
});
vi.mock("@/newPullRequestPage/hooks/usePullRequests", async (importOriginal) => {
  const original = await importOriginal<typeof UsePullRequestsModule>();
  return {
    // The panel reads every info payload through the real normalizer.
    normalizePullRequestInfo: original.normalizePullRequestInfo,
    // The reads a stack's read ahead shares with each PR's view, as they really are.
    pullRequestPatchRevision: original.pullRequestPatchRevision,
    pullRequestChangedFilesQueryOptions: original.pullRequestChangedFilesQueryOptions,
    pullRequestDiffQueryOptions: original.pullRequestDiffQueryOptions,
    // Quote Reply writes the real draft the comment box reads.
    usePullRequestCommentQuote: original.usePullRequestCommentQuote,
    // Edit and Delete run through the real routing, which keeps them off without this Mac.
    usePullRequestCommentEditWrite: original.usePullRequestCommentEditWrite,
    usePullRequestThreadResolutionWrite: original.usePullRequestThreadResolutionWrite,
    usePullRequestCommentReactionWrite: original.usePullRequestCommentReactionWrite,
    pullRequestCommentControls: original.pullRequestCommentControls,
    accountSwitchingOffered: original.accountSwitchingOffered,
    pullRequestEditCapabilities: original.pullRequestEditCapabilities,
    mergeSectionShown: original.mergeSectionShown,
    usePullRequestCommentComposer: original.usePullRequestCommentComposer,
    // The comment box's Close / Reopen runs on this Mac only; the panel's tests never send one.
    usePullRequestLifecycleChange: () => ({ change: vi.fn(), isPending: false }),
    // Draft, ready and auto-merge run on this Mac only; the panel's tests never send one.
    usePullRequestDraftChange: () => ({ change: vi.fn(), isPending: false }),
    useEnablePullRequestAutoMerge: () => ({ change: vi.fn(), isPending: false }),
    usePullRequestAutoMergeDisableChange: () => ({ change: vi.fn(), isPending: false }),
    usePullRequestReviewThreadComposer: original.usePullRequestReviewThreadComposer,
    pullRequestCommentTooLong: original.pullRequestCommentTooLong,
    MAX_PULL_REQUEST_COMMENT_CHARACTERS: original.MAX_PULL_REQUEST_COMMENT_CHARACTERS,
    applyPullRequestLabelChange: original.applyPullRequestLabelChange,
    pullRequestLabelsMutationKey: original.pullRequestLabelsMutationKey,
    // The real pending-change reader, so the controls see edits as the cache holds them.
    usePendingPullRequestChanges: original.usePendingPullRequestChanges,
    usePullRequestRepoLabels: vi.fn(() => state.repoLabels),
    usePullRequestLabelSearch: original.usePullRequestLabelSearch,
    usePullRequestLabelSearchOffered: original.usePullRequestLabelSearchOffered,
    useUpdatePullRequestLabels: () => state.updateLabels,
    usePullRequestMergeOptions: (
      ...args: Parameters<typeof original.usePullRequestMergeOptions>
    ) =>
      state.realPrHooks
        ? original.usePullRequestMergeOptions(...args)
        : { data: undefined, isPending: true, isError: false },
    useMergePullRequest: (id: string) =>
      state.realPrHooks
        ? original.useMergePullRequest(id)
        : { mutate: vi.fn(), isPending: false, isError: false },
    useDisablePullRequestAutoMerge: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
    applyPullRequestReviewerChange: original.applyPullRequestReviewerChange,
    pullRequestReviewerKey: original.pullRequestReviewerKey,
    pullRequestReviewersMutationKey: original.pullRequestReviewersMutationKey,
    usePullRequestReviewCandidates: vi.fn(() => state.reviewCandidates),
    useUpdatePullRequestReviewers: () => state.updateReviewers,
    useUpdatePullRequestDetails: () => ({
      mutate: vi.fn(),
      reset: vi.fn(),
      isPending: false,
      isError: false,
    }),
    uploadPullRequestAttachment: vi.fn(),
    usePullRequestInfo: vi.fn((...args: Parameters<typeof original.usePullRequestInfo>) =>
      state.realPrHooks ? original.usePullRequestInfo(...args) : state.info,
    ),
    // The list loads each PR through the single-PR stub, so tests read one call per row.
    usePullRequestInfos: (
      id: string | undefined,
      prUrls: string[],
      options?: { poll?: "list" | false },
    ) =>
      state.realPrHooks
        ? original.usePullRequestInfos(id, prUrls, options)
        : prUrls.map(
            (prUrl) =>
              vi.mocked(usePullRequestInfo)(id, { poll: options?.poll ?? "list", prUrl }) as never,
          ),
    usePullRequestRowReads: original.usePullRequestRowReads,
    usePullRequestChangedFiles: vi.fn(() => state.changes),
    usePullRequestDiff: (
      _conversationId: string,
      enabled: boolean,
      prUrl: string | undefined,
      revision: string | undefined,
    ) => {
      state.diffReads.push([enabled, prUrl, revision]);
      return {
        // A first read that failed has no data; `diffRefetchError` fails a read behind loaded data.
        data: state.diffLoading || state.diffError ? undefined : state.diff,
        isLoading: state.diffLoading,
        error: state.diffError ?? state.diffRefetchError,
        isFetching: false,
        fetchStatus: state.diffLoading ? "fetching" : "idle",
        refetch: state.refetchDiff,
      };
    },
    usePullRequestCommits: () => ({ data: undefined, isLoading: false, error: null }),
    usePullRequestCommitDiff: () => ({ data: undefined, isLoading: false, error: null }),
    usePullRequestCommitsUnsupported: () => false,
    fetchPullRequestFileContents: async () => ({ before: "old", after: "new" }),
    // The account selector (shown in the repo-unresolved empty state) calls this;
    // stub the mutation shape it reads.
    useUpdateSessionPr: (id: string) =>
      state.realPrHooks
        ? original.useUpdateSessionPr(id)
        : {
            mutate: state.updateSessionPr,
            // A test can hold the host's answer back by returning a promise.
            mutateAsync: async (...args: unknown[]) => {
              const answer: unknown = state.updateSessionPr(...args);
              return answer instanceof Promise ? answer : state.info!.data!;
            },
            isPending: false,
            isError: false,
          },
    useSetPullRequestPreference: (
      ...args: Parameters<typeof original.useSetPullRequestPreference>
    ) =>
      state.realPrHooks
        ? original.useSetPullRequestPreference(...args)
        : { mutate: () => {}, isPending: false, isError: false, error: null },
  };
});
vi.mock("@/newPullRequestPage/hooks/useSessionPullRequests", async (importOriginal) => {
  const original = await importOriginal<typeof UseSessionPullRequestsModule>();
  const { useQuery } = await import("@tanstack/react-query");
  return {
    sessionPullRequestsKey: original.sessionPullRequestsKey,
    // A sub-agent's stored list is read through the real reader, under the same key.
    fetchSessionPullRequests: original.fetchSessionPullRequests,
    storedListToShow: original.storedListToShow,
    // The sidebar's data, read by the ⌘K palette test.
    useLoadedSessionPullRequests: original.useLoadedSessionPullRequests,
    storesSessionPullRequests: () => state.storesPrs,
    useSessionPullRequests: (
      id: string | undefined,
      _live?: unknown,
      options: { enabled?: boolean } = {},
    ) => {
      return useQuery({
        queryKey: original.sessionPullRequestsKey(id),
        queryFn: async () => {
          state.storedFetches += 1;
          await state.storedGate;
          if (state.storedFailing.has(id ?? "")) throw new Error("500 Internal Server Error");
          return { prs: state.stored[id ?? ""] ?? [], synced: !state.unsynced.has(id ?? "") };
        },
        // Sessions without a server list here read live info only.
        enabled: (options.enabled ?? true) && !!id && id in state.stored && state.storesPrs,
      });
    },
    useUnlinkSessionPr: () => state.unlink,
    useUnlinkSessionPrs: () => state.unlinkMany,
    useUnlinkSessionTree: () => state.unlinkTree,
    useSessionUnlinkPending: () => state.unlink.isPending,
    sessionUnlinkRunning: () => state.unlink.isPending,
    // Real, so a run a test starts for real shows its progress; null with the mocked unlinks.
    useSessionRemovalProgress: original.useSessionRemovalProgress,
    startSessionUnlinkRun: original.startSessionUnlinkRun,
    followPullRequestIntents: original.followPullRequestIntents,
    // Read again through the list's own query above, as the panel observes it.
    readSessionPullRequestsAgain: async (client: QueryClient, id: string) => {
      await client.refetchQueries({ queryKey: original.sessionPullRequestsKey(id), exact: true });
    },
  };
});
vi.mock("@/hooks/usePermissions", () => ({
  useCanEdit: () => state.canEdit,
  useCanChangePullRequests: () => state.canEdit && state.changeAccess === "owner",
  usePullRequestChangeAccess: () => state.changeAccess,
  // The root's owner, compared with the viewer to decide who owns the sub-agent tree.
  useSessionOwner: () => ({ isSuccess: true, data: state.rootOwner }),
}));

// The new page's flag, settable mid-test the way a live flag answer arrives.
const pageV2 = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  let on = false;
  return {
    on: () => on,
    set(value: boolean) {
      on = value;
      for (const listener of listeners) listener();
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
});
// The pair Changes shows runs as it is, unless a test shows a previous push's, kept after a push.
// The account picker's change and the label, reviewer and merge edits are served as on the
// server's route, unless a case serves fewer; every other method keeps its real answer, which
// is none without a route.
const routeServes = vi.hoisted(() => ({ methods: new Set<string>() }));
const ROUTE_EDIT_METHODS = [
  "setPullRequestAccount",
  "listLabels",
  "updateLabels",
  "listReviewerCandidates",
  "updateReviewers",
  "getMergeOptions",
  "mergePullRequest",
];
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => {
  const original = await importOriginal<typeof PullRequestPageRouteModule>();
  return {
    ...original,
    usePullRequestMethodServed: (...args: Parameters<typeof original.usePullRequestMethodServed>) =>
      routeServes.methods.has(args[1]) || original.usePullRequestMethodServed(...args),
  };
});
// Read for real, watched for what the details hand it.
// The stack diff runs as it is, except where a test shows only a stacked PR's own changes.
vi.mock("@/pullRequestPage/stackDiff/usePullRequestStackDiff", async (importOriginal) => {
  const original = await importOriginal<typeof StackDiffModule>();
  return { ...original, usePullRequestStackDiff: vi.fn(original.usePullRequestStackDiff) };
});
const stackOnlyReview = vi.hoisted(() => ({
  props: undefined as Record<string, unknown> | undefined,
}));
vi.mock("@/pullRequestPage/stackDiff/PullRequestStackOnlyReview", () => ({
  PullRequestStackOnlyReview: (reviewProps: Record<string, unknown>) => {
    stackOnlyReview.props = reviewProps;
    return (
      <div data-testid="stack-only-review">
        <ReviewBaseMenu base={reviewProps.base as DiffReviewBase | null | undefined} />
      </div>
    );
  },
}));
vi.mock("@/pullRequestPage/usePullRequestStack", async (importOriginal) => {
  const original = await importOriginal<typeof StackReadModule>();
  return { ...original, usePullRequestStackRead: vi.fn(original.usePullRequestStackRead) };
});
// Who asks for the diff's highlighting workers to start, passing each ask to the real hook.
const warmDiffWorkers = vi.hoisted(() => vi.fn());
vi.mock("@/components/diff-review/sharedDiffWorkers", async (importOriginal) => {
  const actual = await importOriginal<typeof SharedDiffWorkersModule>();
  return {
    ...actual,
    useWarmSharedDiffWorkers: () => {
      warmDiffWorkers();
      actual.useWarmSharedDiffWorkers();
    },
  };
});
// Pull Requests v2 brings the new page and the shared review; Classic neither.
vi.mock("@/lib/workspaceSurfaces", async (importOriginal) => {
  const { useSyncExternalStore } = await import("react");
  return {
    ...(await importOriginal<typeof WorkspaceSurfacesModule>()),
    usePullRequestsV2: () => useSyncExternalStore(pageV2.subscribe, pageV2.on),
  };
});
// Example Host links kept on this Mac are scoped to the signed-in user, which tests set.
const signedInUser = vi.hoisted(() => ({ id: undefined as string | undefined }));
// Session reads a test answers itself, such as a sub-agent tree; other reads go to the real fetch.
const sessionReads = vi.hoisted(() => ({
  answer: undefined as
    undefined | ((url: string) => Promise<Response | undefined> | Response | undefined),
}));
vi.mock("@/lib/identity", async (importOriginal) => {
  const original = await importOriginal<typeof IdentityModule>();
  return {
    ...original,
    getCurrentUserId: () => signedInUser.id ?? original.getCurrentUserId(),
    authenticatedFetch: (
      input: Parameters<typeof original.authenticatedFetch>[0],
      options?: Parameters<typeof original.authenticatedFetch>[1],
    ) => {
      return Promise.resolve(sessionReads.answer?.(String(input))).then((answer) =>
        answer ? answer : original.authenticatedFetch(input, options),
      );
    },
  };
});
// Recovery opens the app's GitHub sign-in; tests answer it instead of driving the dialog.
const signInToGitHubOnThisMac = vi.hoisted(() =>
  vi.fn(async (_request: { host: string }) => ({ ok: false, code: "cancelled" }) as const),
);
vi.mock("@/lib/codeHostSignIn", async (importOriginal) => ({
  ...(await importOriginal<typeof CodeHostSignInModule>()),
  signInToGitHubOnThisMac,
}));
// An unseeded session snapshot stays loading, so its access stays unconfirmed.
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  getSessionSlim: () => new Promise(() => {}),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof UseWorkspaceChangedFilesModule>()),
  useWorkspaceAsleep: () => state.sandboxAsleep,
  useWorkspaceServeable: () => state.serveable,
}));
let sandboxResuming = false;
vi.mock("@/hooks/useSandboxResume", () => ({
  useSandboxResume: () => ({
    status: sandboxResuming ? "resuming" : state.sandboxAsleep ? "asleep" : "none",
    failure: null,
    availability: { available: true },
    resume: () => {},
  }),
}));

// The diff rendering (@pierre/diffs) is exercised by the library itself; here
// we only assert a section renders one diff per parsed file. parsePatchFiles is
// stubbed to yield the files configured on `state` (name + optional rename
// metadata), matching the whole-PR patch.
vi.mock("@pierre/diffs", () => ({
  parsePatchFiles: vi.fn(() => [{ files: state.parsedFiles }]),
  registerCustomTheme: vi.fn(),
}));
vi.mock("@pierre/diffs/react", () => ({
  FileDiff: ({ fileDiff }: { fileDiff: { name: string } }) => (
    <div data-testid="diff" data-path={fileDiff.name} />
  ),
}));
// The shared review renders on the real library in its own tests; here it only
// records what the Changes tab hands it.
const sharedReview = vi.hoisted(() => ({
  props: undefined as Record<string, unknown> | undefined,
}));
const commitReview = vi.hoisted(() => ({
  props: undefined as Record<string, unknown> | undefined,
}));
vi.mock("../../shell/PullRequestCommitReview", () => ({
  PullRequestCommitReview: (reviewProps: Record<string, unknown>) => {
    commitReview.props = reviewProps;
    return <div data-testid="commit-review" />;
  },
}));
/** The reviews' base menu as their toolbar shows it, so the stubs keep the real menu. */
function ReviewBaseMenu({ base }: { base: DiffReviewBase | null | undefined }) {
  return base ? (
    <TooltipProvider>
      <DiffReviewBaseMenu base={base} />
    </TooltipProvider>
  ) : null;
}
vi.mock("../../shell/PullRequestChangesReview", async (importOriginal) => ({
  ...(await importOriginal<typeof ChangesReviewModule>()),
  PullRequestChangesReview: (reviewProps: Record<string, unknown>) => {
    sharedReview.props = reviewProps;
    return (
      <div data-testid="shared-review">
        <ReviewBaseMenu base={reviewProps.base as DiffReviewBase | null | undefined} />
      </div>
    );
  },
}));
// The resolved theme mode drives @pierre/diffs' themeType.
vi.mock("@/components/theme/useResolvedThemeMode", () => ({
  useResolvedThemeMode: () => "light",
}));
// The Summary tab renders markdown via MessageResponse (Streamdown); stub it to
// a passthrough so tests assert the text without the real renderer.
vi.mock("@/components/ai-elements/message", () => ({
  MessageResponse: ({
    children,
    rehypePluginsAfterSanitizing,
  }: {
    children: string;
    rehypePluginsAfterSanitizing?: readonly unknown[];
  }) => (
    <div
      data-testid="markdown"
      data-github-markdown={rehypePluginsAfterSanitizing?.length ? "on" : undefined}
    >
      {children}
    </div>
  ),
}));

import { usePullRequestAttachStore } from "@/store/pullRequestAttach";
import type { ReviewThreadContext } from "./PullRequestReviewThread";
import { usePullRequestListCounts } from "@/store/pullRequestListCounts";
import { PullRequestListPage } from "@/pullRequestList/PullRequestListPage";
import { AttachPullRequestDialog } from "../../shell/AttachPullRequestDialog";
import {
  usePullRequestInfo,
  usePullRequestChangedFiles,
  usePullRequestRepoLabels,
  usePullRequestReviewCandidates,
} from "@/newPullRequestPage/hooks/usePullRequests";

import {
  PullRequestPanel,
  type PullRequestTabsMode,
  derivePullRequestPanelState,
  REFRESH_PULL_REQUESTS_LABEL,
} from "./PullRequestPanel";
import {
  NEXT_UNRESOLVED_CONVERSATION_LABEL,
  PREVIOUS_UNRESOLVED_CONVERSATION_LABEL,
  UnresolvedConversationPaletteCommands,
} from "../../shell/pullRequestCommands/UnresolvedConversationPaletteCommands";
import { RunnerOfflineError } from "@/lib/workspaceReadErrors";
import { isCommandAvailable, usePaletteCommandsStore } from "@/store/paletteCommands";
import { useChatStore } from "@/store/chatStore";
import { useApprovalPromptOnScreen, useApproveHotkey } from "@/hooks/useApproveHotkey";
import {
  type PullRequestPanelView,
  pullRequestPageViewKey,
  pullRequestPanelViewKey,
  showPullRequestInPanel,
} from "../../shell/pullRequestPanelView";
import { invalidatedKey } from "@/test/invalidatedQueries";
import type { MergeReadinessView } from "@/lib/mergeReadiness";
import {
  EXAMPLE_REVIEW_SITE,
  exampleMergeReadinessSource,
  withPullRequestIntegrations,
  EXAMPLE_DEVICE_HOST,
} from "@/test/pullRequestIntegrations";
import { commentsOrderPreference } from "@/pullRequestComments/commentsOrder";
import { resetPullRequestIntentsForTests } from "@/store/sessionPullRequestIntents";

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const FORGE_DISPLAY = {
  id: "example_forge",
  display_name: "Example Forge",
  request_name: "pull request",
  number_prefix: "!",
};

const OTHER_PR: PullRequestAssociation = {
  url: "https://github.com/acme/app/pull/6001",
  host: "github.com",
  repository: "acme/app",
  number: 6001,
  relationship: "attached",
};

function file(
  path: string,
  status: PullRequestChangedFile["status"],
  adds: number | null = 1,
  dels: number | null = 0,
): PullRequestChangedFile {
  return {
    path,
    name: path.split("/").pop() ?? path,
    status,
    bytes: null,
    modified_at: null,
    lines_added: adds,
    lines_removed: dels,
  };
}

/** The session snapshot each panel reads its access from, as a loaded `GET /v1/sessions/{id}` caches it. */
function sessionSnapshot(id: string, permissionLevel: number | null): Session {
  return {
    id,
    agentId: "agent_1",
    agentName: null,
    status: "idle",
    createdAt: 0,
    title: null,
    items: [],
    permissionLevel,
    parentSessionId: null,
  } as unknown as Session;
}

/**
 * The ⌘K group a mounted panel part registered under `source`. Each part scopes its
 * source to its own instance, so this finds the one registration beginning with it.
 */
function paletteGroup(source: string) {
  const groups = usePaletteCommandsStore.getState().groups;
  // A pane in a palette scope registers its source after the scope's namespace and `::`.
  const keys = Object.keys(groups).filter(
    (key) =>
      key === source ||
      key.startsWith(`${source}:`) ||
      key.endsWith(`::${source}`) ||
      key.includes(`::${source}:`),
  );
  return keys.length === 0 ? undefined : groups[keys[0]!];
}

/** Opens a row menu's Open › or Copy › submenu by hovering its trigger, and returns the item `item`. */
async function openRowSubmenu(
  user: ReturnType<typeof userEvent.setup>,
  trigger: "Open" | "Copy",
  item: string,
) {
  await user.hover(screen.getByRole("menuitem", { name: trigger }));
  return screen.findByRole("menuitem", { name: item });
}

/**
 * Chooses a nested item of a row menu's submenu. A pointer click would leave the trigger
 * on its way there and close the submenu, so the item is focused and activated with Enter.
 */
async function chooseRowSubmenuItem(
  user: ReturnType<typeof userEvent.setup>,
  trigger: "Open" | "Copy",
  item: string,
) {
  const element = await openRowSubmenu(user, trigger, item);
  act(() => element.focus());
  await user.keyboard("{Enter}");
}

function renderPanel(mobile = false, active?: boolean, prepare?: (client: QueryClient) => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  prepare?.(client);
  // No command context: like a grid pane, the panel reads its own session's snapshot.
  if (state.access !== null) {
    for (const id of ["conv_1", "conv_2", "conv_other"]) {
      client.setQueryData(["session", id], sessionSnapshot(id, state.access));
    }
  }
  return {
    client,
    ...render(<PullRequestPanel conversationId="conv_1" active={active} />, {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>
          {mobile ? (
            <MobilePanelDrawer open title="Pull Requests" onClose={() => {}}>
              {children}
            </MobilePanelDrawer>
          ) : (
            children
          )}
          {/* Every "Link a PR" entry point opens the one shared picker. */}
          <AttachPullRequestDialog />
        </QueryClientProvider>
      ),
    }),
  };
}

/** The shared "Link a PR" picker, once an entry point has opened it. */
function findLinkPicker() {
  return screen.findByRole("combobox", { name: "Link a pull request" });
}

function queryLinkPicker() {
  return screen.queryByRole("combobox", { name: "Link a pull request" });
}

/** Render, then switch to the Changes tab — Summary is the default, so the
 *  diff view (and its toolbar) only exists after activating Changes. Radix
 *  tabs select on pointer-down, so mouseDown (not click) flips the tab. */
function renderChanges() {
  const r = renderPanel();
  fireEvent.mouseDown(screen.getByRole("tab", { name: /^Changes/ }));
  return r;
}

/** Render, then switch to the Comments tab (see renderChanges). */
function renderComments() {
  const r = renderPanel();
  fireEvent.mouseDown(screen.getByRole("tab", { name: /^Comments/ }));
  return r;
}

/** Silences console.error for the current test and returns a reader for the React
 *  duplicate- and missing-key warnings logged so far. */
function watchKeyWarnings(): () => string[] {
  const errors = vi.spyOn(console, "error").mockImplementation(() => {});
  onTestFinished(() => errors.mockRestore());
  return () =>
    errors.mock.calls
      .map(([message]) => String(message))
      .filter((message) => /same key|unique "key"/.test(message));
}

/** The markdown body of each comment card, in rendered order. Only the active
 *  tab mounts, so on the Comments tab every markdown block is a comment. */
function commentBodies(): (string | null)[] {
  return screen.getAllByTestId("markdown").map((el) => el.textContent);
}

let scrollIntoView: ReturnType<typeof vi.fn>;

beforeEach(() => {
  routeServes.methods = new Set(ROUTE_EDIT_METHODS);
  resetPullRequestIntentsForTests();
  usePullRequestAttachStore.setState({
    openSessionId: null,
    drafts: {},
    attempts: {},
    lastAttached: null,
  });
  // The diff-layout toggle seeds from persisted prefs; start each test clean.
  window.localStorage.clear();
  usePullRequestListCounts.setState({ counts: {} });
  state.realPrHooks = false;
  state.commitsPriority = null;
  state.diffReads = [];
  state.access = 4;
  state.stored = {};
  state.unsynced = new Set();
  state.storedFailing = new Set();
  state.storedFetches = 0;
  state.storedGate = null;
  state.storesPrs = true;
  state.unlink = {
    mutate: vi.fn(),
    mutateAsync: vi.fn(async () => ({ prs: [] })),
    isPending: false,
    isError: false,
    error: null,
  };
  state.unlinkMany = vi.fn(async () => []);
  state.unlinkTree = vi.fn(async () => []);
  state.rootOwner = "local";
  state.sandboxAsleep = false;
  state.serveable = undefined;
  // Fire the observer callback immediately on observe so lazy sections mount.
  class IO {
    private cb: IntersectionObserverCallback;
    constructor(cb: IntersectionObserverCallback) {
      this.cb = cb;
    }
    observe(el: Element) {
      this.cb(
        [{ isIntersecting: true, target: el } as IntersectionObserverEntry],
        this as unknown as IntersectionObserver,
      );
    }
    unobserve() {}
    disconnect() {}
    takeRecords(): IntersectionObserverEntry[] {
      return [];
    }
  }
  vi.stubGlobal("IntersectionObserver", IO);
  scrollIntoView = vi.fn();
  Element.prototype.scrollIntoView = scrollIntoView as unknown as Element["scrollIntoView"];

  state.info = {
    data: {
      object: "session.github.info",
      available: true,
      gh_available: true,
      authenticated: true,
      branch: "test/pr-view",
      base_ref: "main",
      repo: { name_with_owner: "acme/app" },
      pr: {
        number: 6000,
        title: "chore: dummy PR",
        state: "OPEN",
        url: "https://example.com/pr/6000",
        is_draft: false,
        author: "dev",
        base_ref: "main",
        head_ref: "test/pr-view",
        checks: {
          passing: 66,
          failing: 2,
          pending: 0,
          total: 68,
          runs: [
            { name: "unit tests", bucket: "passing", url: null },
            { name: "e2e", bucket: "failing", url: null },
          ],
        },
      },
    },
    isLoading: false,
    error: null,
    isFetching: false,
  };
  state.changes = {
    data: {
      available: true,
      data: [file("hello.py", "created"), file("src/app.ts", "modified", 3, 1)],
    },
    isLoading: false,
    error: null,
    isFetching: false,
  };
  // Default parsed diffs mirror the two changed files above.
  state.parsedFiles = [{ name: "hello.py" }, { name: "src/app.ts" }];
  state.diff = { object: "session.github.pr_diff", patch: "PATCH" };
  state.diffError = null;
  state.diffRefetchError = null;
  state.diffLoading = false;
  state.refetchDiff = vi.fn();
  state.canEdit = true;
  state.changeAccess = "owner";
  state.repoLabels = { data: undefined, isPending: true, isError: false, error: null };
  state.updateLabels = { mutate: vi.fn(), isError: false, error: null };
});

afterEach(() => {
  pageV2.set(false);
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  // Title and description drafts outlive the panel, as they do in the app.
  resetPullRequestDescriptionDrafts();
});

/** The panel with its view owned outside, as the tabs sidebar owns it, recording each report. */
/**
 * A prompt pending in the real chat store, with the chat's approval chord listening:
 * ⌘Enter (Ctrl+Enter off macOS) accepts it unless the focused element owns the chord.
 */
function pendingApproval(mac: boolean) {
  const platform = vi
    .spyOn(navigator, "platform", "get")
    .mockReturnValue(mac ? "MacIntel" : "Linux x86_64");
  const submitApproval = vi.fn();
  const before = useChatStore.getState();
  useChatStore.setState({
    blocks: [{ type: "elicitation", elicitationId: "review-e1", status: "pending" }] as never,
    submitApproval: submitApproval as never,
  });
  function Approvals() {
    useApproveHotkey(mac);
    return null;
  }
  const hotkey = render(<Approvals />);
  const pressCommandEnter = (target: Element) =>
    fireEvent.keyDown(target, { key: "Enter", metaKey: mac, ctrlKey: !mac });
  const restore = () => {
    hotkey.unmount();
    useChatStore.setState({ blocks: before.blocks, submitApproval: before.submitApproval });
    platform.mockRestore();
  };
  return { submitApproval, pressCommandEnter, restore };
}

function renderControlled(initial: PullRequestPanelView, tabsMode?: PullRequestTabsMode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (state.access !== null) {
    client.setQueryData(["session", "conv_1"], sessionSnapshot("conv_1", state.access));
  }
  const reports: { view: PullRequestPanelView; replace: boolean }[] = [];
  let setFromOutside: (view: PullRequestPanelView) => void = () => {};
  function Harness() {
    const [view, setView] = useState(initial);
    setFromOutside = setView;
    const onViewChange = (next: PullRequestPanelView, { replace }: { replace: boolean }) => {
      reports.push({ view: next, replace });
      setView(next);
    };
    return (
      <PullRequestPanel
        conversationId="conv_1"
        view={view}
        tabsMode={tabsMode}
        onViewChange={onViewChange}
      />
    );
  }
  render(
    <QueryClientProvider client={client}>
      <Harness />
    </QueryClientProvider>,
  );
  return {
    client,
    reports,
    setView: (view: PullRequestPanelView) => act(() => setFromOutside(view)),
  };
}

describe("PullRequestPanel", () => {
  it.each([
    { mobile: false, multiple: false },
    { mobile: true, multiple: false },
    { mobile: false, multiple: true },
    { mobile: true, multiple: true },
  ])(
    "restores cached PR details after session switches and remounts ($mobile, $multiple)",
    async ({ mobile, multiple }) => {
      state.realPrHooks = true;
      const url = state.info!.data!.pr!.url;
      const payload = {
        ...state.info!.data!,
        tracking_available: true,
        selected_pr_url: url,
        prs: [
          {
            url,
            host: "github.com",
            repository: "acme/app",
            number: 6000,
            relationship: "attached",
          },
        ],
      };
      if (multiple) {
        payload.prs.push({ ...payload.prs[0]!, url: `${url}1`, number: 60001 });
      }
      let refreshing = false;
      const fetchMock = vi.fn(() =>
        refreshing
          ? new Promise<Response>(() => {})
          : Promise.resolve(new Response(JSON.stringify(payload))),
      );
      vi.stubGlobal("fetch", fetchMock);
      const user = userEvent.setup();
      const { client, rerender } = renderPanel(mobile);
      if (multiple) {
        await user.click(
          await screen.findByRole("button", { name: "acme/app #6000 — chore: dummy PR" }),
        );
      }
      await waitFor(() => {
        expect(client.getQueryData(["pr-page-github-info", "conv_1", url])).toBeDefined();
      });
      await user.click(await screen.findByRole("tab", { name: "Comments" }));
      expect(screen.getByRole("tab", { name: "Comments" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      // A subsequent poll replaces the original response in the shared cache.
      payload.pr = { ...payload.pr!, title: "Updated by polling" };
      await act(() => client.refetchQueries({ queryKey: ["pr-page-github-info", "conv_1"] }));
      expect(await screen.findByText("Updated by polling")).toBeVisible();

      expect(client.getQueryData(pullRequestPageViewKey("conv_1"))).toMatchObject({
        subtab: "comments",
      });
      rerender(<PullRequestPanel conversationId="conv_other" />);
      if (multiple) {
        expect(await screen.findByRole("list", { name: "Pull requests" })).toBeVisible();
      } else {
        expect(await screen.findByRole("tab", { name: "Summary" })).toHaveAttribute(
          "aria-selected",
          "true",
        );
      }
      fetchMock.mockClear();
      rerender(<PullRequestPanel conversationId="conv_1" />);
      expect(screen.getByText("Updated by polling")).toBeVisible();
      expect(screen.getByRole("tab", { name: "Comments" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      expect(fetchMock).not.toHaveBeenCalled();

      // Even a stale response remains visible while the return refresh is pending.
      rerender(<div />);
      await act(() =>
        client.invalidateQueries({
          queryKey: ["pr-page-github-info", "conv_1"],
          refetchType: "none",
        }),
      );
      refreshing = true;
      rerender(<PullRequestPanel conversationId="conv_1" />);
      expect(screen.getByText("Updated by polling")).toBeVisible();
      expect(screen.getByRole("tab", { name: "Comments" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      expect(screen.queryByText("Loading pull requests…")).toBeNull();
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    },
  );

  it.each(["UNKNOWN", "BLOCKED", "error"])(
    "keeps a row menu's merge confirmation and pending refusal through a polling transition: %s",
    async (transition) => {
      state.realPrHooks = true;
      const url = "https://github.com/acme/app/pull/6000";
      const head = "a".repeat(40);
      const payload = {
        ...state.info!.data!,
        tracking_available: true,
        selected_pr_url: url,
        prs: [
          OTHER_PR,
          {
            url,
            host: "github.com",
            repository: "acme/app",
            number: 6000,
            relationship: "attached",
          },
        ],
        capabilities: { merging: true },
        pr: { ...state.info!.data!.pr!, url, head_sha: head, merge_state_status: "CLEAN" },
      };
      let failed = false;
      let respond: (response: Response) => void = () => {};
      const requests: unknown[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const path = new URL(String(input), "http://localhost").pathname;
          if (path.endsWith("/merge-options"))
            return new Response(JSON.stringify({ methods: ["squash"], default: "squash" }));
          if (path.endsWith("/merge")) {
            requests.push(JSON.parse(String(init?.body)));
            return new Promise<Response>((resolve) => {
              respond = resolve;
            });
          }
          return new Response(
            JSON.stringify(failed ? { error: { message: "Temporary failure" } } : payload),
            { status: failed ? 500 : 200 },
          );
        }),
      );
      const user = userEvent.setup();
      const { client } = renderPanel();
      // The row offers Merge once its live PR has loaded.
      expect(await screen.findByText(state.info!.data!.pr!.title)).toBeVisible();
      await user.click(screen.getByRole("button", { name: "Actions for acme/app #6000" }));
      await user.click(await screen.findByRole("menuitem", { name: "Merge…" }));
      const confirm = await screen.findByRole("button", { name: "Squash and merge" });
      const dialog = screen.getByRole("dialog");
      payload.pr.merge_state_status = transition === "error" ? "CLEAN" : transition;
      payload.pr.head_sha = "b".repeat(40);
      failed = transition === "error";
      await act(() => client.refetchQueries({ queryKey: ["pr-page-github-info", "conv_1"] }));
      expect(screen.getByRole("dialog")).toBe(dialog);
      if (failed) {
        await waitFor(() => expect(confirm).toBeDisabled());
        failed = false;
        await act(() => client.refetchQueries({ queryKey: ["pr-page-github-info", "conv_1"] }));
      }
      await user.click(confirm);
      expect(requests).toEqual([{ pr_url: url, merge_method: "squash", expected_head: head }]);
      payload.pr.merge_state_status = "BLOCKED";
      await act(() => client.refetchQueries({ queryKey: ["pr-page-github-info", "conv_1"] }));
      expect(within(dialog).getByRole("button", { name: "Merging…" })).toBeDisabled();
      await act(async () =>
        respond(
          new Response(JSON.stringify({ error: { message: "Head changed; refresh and retry" } }), {
            status: 409,
          }),
        ),
      );
      expect(await within(dialog).findByRole("alert")).toHaveTextContent("Head changed");
      expect(screen.getByRole("dialog")).toBe(dialog);
    },
  );

  it.each(["success", "failure"])(
    "keeps a row menu's merge dialog and its %s through an offline refresh",
    async (outcome) => {
      state.realPrHooks = true;
      const url = "https://github.com/acme/app/pull/6000";
      const payload = {
        ...state.info!.data!,
        tracking_available: true,
        selected_pr_url: url,
        prs: [
          OTHER_PR,
          {
            url,
            host: "github.com",
            repository: "acme/app",
            number: 6000,
            relationship: "attached",
          },
        ],
        capabilities: { merging: true },
        pr: {
          ...state.info!.data!.pr!,
          url,
          head_sha: "a".repeat(40),
          merge_state_status: "CLEAN",
        },
      };
      let respond: (response: Response) => void = () => {};
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL) => {
          const path = new URL(String(input), "http://localhost").pathname;
          if (path.endsWith("/merge-options"))
            return new Response(JSON.stringify({ methods: ["squash"], default: "squash" }));
          if (path.endsWith("/merge")) {
            return new Promise<Response>((resolve) => {
              respond = resolve;
            });
          }
          return new Response(JSON.stringify(payload));
        }),
      );
      const user = userEvent.setup();
      const { rerender } = renderPanel();
      expect(await screen.findByText(state.info!.data!.pr!.title)).toBeVisible();
      await user.click(screen.getByRole("button", { name: "Actions for acme/app #6000" }));
      await user.click(await screen.findByRole("menuitem", { name: "Merge…" }));
      await user.click(await screen.findByRole("button", { name: "Squash and merge" }));
      const dialog = screen.getByRole("dialog");
      expect(within(dialog).getByRole("button", { name: "Merging…" })).toBeDisabled();

      // The host drops while GitHub is still answering; the dialog and its request stay.
      state.serveable = false;
      rerender(<PullRequestPanel conversationId="conv_1" />);
      expect(await screen.findAllByText("Host offline")).not.toHaveLength(0);
      expect(screen.getByRole("dialog")).toBe(dialog);
      expect(within(dialog).getByRole("button", { name: "Merging…" })).toBeDisabled();

      await act(async () =>
        respond(
          outcome === "success"
            ? new Response(JSON.stringify({ object: "session.github.pr_merge", pr_url: url }))
            : new Response(
                JSON.stringify({ error: { message: "Head changed; refresh and retry" } }),
                {
                  status: 409,
                },
              ),
        ),
      );
      if (outcome === "success") {
        await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      } else {
        expect(await within(dialog).findByRole("alert")).toHaveTextContent("Head changed");
        // Still offline, so it can't confirm again.
        expect(within(dialog).getByRole("button", { name: "Squash and merge" })).toBeDisabled();
      }

      state.serveable = undefined;
      rerender(<PullRequestPanel conversationId="conv_1" />);
      expect(await screen.findByText(state.info!.data!.pr!.title)).toBeVisible();
      if (outcome === "success") {
        // Reconnecting doesn't reopen a fresh confirmation.
        expect(screen.queryByRole("dialog")).toBeNull();
      } else {
        expect(screen.getByRole("dialog")).toBe(dialog);
        await waitFor(() =>
          expect(within(dialog).getByRole("button", { name: "Squash and merge" })).toBeEnabled(),
        );
      }
    },
  );

  it.each(["success", "failure"] as const)(
    "keeps a row menu's merge %s with its session after switching sessions and back",
    async (outcome) => {
      state.realPrHooks = true;
      const url = "https://github.com/acme/app/pull/6000";
      const payload = {
        ...state.info!.data!,
        tracking_available: true,
        selected_pr_url: url,
        prs: [
          OTHER_PR,
          {
            url,
            host: "github.com",
            repository: "acme/app",
            number: 6000,
            relationship: "attached",
          },
        ],
        capabilities: { merging: true },
        pr: {
          ...state.info!.data!.pr!,
          url,
          head_sha: "a".repeat(40),
          merge_state_status: "CLEAN",
        },
      };
      const mergePaths: string[] = [];
      let respond: (response: Response) => void = () => {};
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL) => {
          const path = new URL(String(input), "http://localhost").pathname;
          if (path.endsWith("/merge-options"))
            return new Response(JSON.stringify({ methods: ["squash"], default: "squash" }));
          if (path.endsWith("/merge")) {
            mergePaths.push(path);
            return new Promise<Response>((resolve) => {
              respond = resolve;
            });
          }
          return new Response(JSON.stringify(payload));
        }),
      );
      const user = userEvent.setup();
      const { rerender } = renderPanel();
      expect(await screen.findByText(state.info!.data!.pr!.title)).toBeVisible();
      // Both sessions have been visited, so switching keeps the list on screen.
      rerender(<PullRequestPanel conversationId="conv_2" />);
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Actions for acme/app #6000" })).toBeVisible(),
      );
      rerender(<PullRequestPanel conversationId="conv_1" />);
      await user.click(await screen.findByRole("button", { name: "Actions for acme/app #6000" }));
      await user.click(await screen.findByRole("menuitem", { name: "Merge…" }));
      await user.click(await screen.findByRole("button", { name: "Squash and merge" }));
      const dialog = screen.getByRole("dialog");
      expect(within(dialog).getByRole("button", { name: "Merging…" })).toBeDisabled();

      // Away to a session that links the same PR, then back, while GitHub is still answering.
      rerender(<PullRequestPanel conversationId="conv_2" />);
      expect(screen.getByRole("dialog")).toBe(dialog);
      expect(within(dialog).getByRole("button", { name: "Merging…" })).toBeDisabled();
      rerender(<PullRequestPanel conversationId="conv_1" />);
      expect(screen.getByRole("dialog")).toBe(dialog);
      expect(within(dialog).getByRole("button", { name: "Merging…" })).toBeDisabled();

      await act(async () =>
        respond(
          outcome === "success"
            ? new Response(JSON.stringify({ object: "session.github.pr_merge", pr_url: url }))
            : new Response(
                JSON.stringify({ error: { message: "Head changed; refresh and retry" } }),
                { status: 409 },
              ),
        ),
      );
      expect(mergePaths).toHaveLength(1);
      expect(mergePaths[0]).toContain("conv_1");
      if (outcome === "success") {
        await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
        expect(await screen.findByText("Merge requested")).toBeVisible();
      } else {
        expect(await within(dialog).findByRole("alert")).toHaveTextContent("Head changed");
        await waitFor(() =>
          expect(within(dialog).getByRole("button", { name: "Squash and merge" })).toBeEnabled(),
        );
      }
    },
  );

  it("keeps a linked PR selected in details while an older stored-list read finishes", async () => {
    state.realPrHooks = true;
    const one = "https://github.com/acme/app/pull/1";
    const two = "https://github.com/acme/app/pull/2";
    const association = (url: string, number: number): PullRequestAssociation => ({
      url,
      number,
      host: "github.com",
      repository: "acme/app",
      relationship: "attached",
      title: `PR ${number}`,
    });
    const first = association(one, 1);
    const second = association(two, 2);
    state.stored.conv_1 = [first];
    let attached = false;
    const payload = (url: string) => ({
      ...state.info!.data!,
      tracking_available: true,
      selected_pr_url: url,
      prs: attached ? [first, second] : [first],
      pr: {
        ...state.info!.data!.pr!,
        url,
        number: url === two ? 2 : 1,
        title: url === two ? "PR 2" : "PR 1",
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = new URL(String(input), "http://localhost");
        // The server stores the link, so a read of its list afterwards has it.
        if (init?.method === "POST") {
          attached = true;
          state.stored.conv_1 = [first, second];
        }
        return new Response(
          JSON.stringify(
            payload(init?.method === "POST" ? two : (request.searchParams.get("pr_url") ?? one)),
          ),
        );
      }),
    );
    const user = userEvent.setup();
    const { client } = renderPanel();
    await screen.findByRole("heading", { name: "PR 1" });
    let resolveStored: (value: {
      prs: PullRequestAssociation[];
      synced: boolean;
    }) => void = () => {};
    const oldRead = client
      .fetchQuery({
        queryKey: ["session-pull-requests", "conv_1"],
        queryFn: () =>
          new Promise<{ prs: PullRequestAssociation[]; synced: boolean }>((resolve) => {
            resolveStored = resolve;
          }),
      })
      .catch(() => {});
    await user.click(screen.getByRole("button", { name: "Link a PR" }));
    await user.type(await findLinkPicker(), two);
    await user.keyboard("{Enter}");
    await screen.findByRole("heading", { name: "PR 2" });
    expect(client.getQueryData(["session-pull-requests", "conv_1"])).toMatchObject({
      prs: [first, second],
      synced: true,
    });
    await act(async () => {
      resolveStored({ prs: [first], synced: true });
      await oldRead;
    });
    expect(screen.getByRole("heading", { name: "PR 2" })).toBeVisible();
    await act(async () => {
      client.setQueryData(["session-pull-requests", "conv_1"], {
        prs: [first, second],
        synced: true,
      });
    });
    expect(screen.getByRole("heading", { name: "PR 2" })).toBeVisible();
  });

  it.each([1, 2])(
    "uses one mobile heading with working header actions for %i PRs",
    async (count) => {
      const user = userEvent.setup();
      state.storesPrs = false;
      const url = state.info!.data!.pr!.url;
      Object.assign(state.info!.data!, {
        tracking_available: true,
        selected_pr_url: url,
        prs: [
          {
            url,
            host: "github.com",
            repository: "acme/app",
            number: 6000,
            relationship: "attached",
          },
          ...(count > 1 ? [OTHER_PR] : []),
        ],
      });
      const { client } = renderPanel(true);
      const invalidate = vi.spyOn(client, "invalidateQueries");
      const heading = screen.getByRole("heading", { name: "Pull Requests" });
      const header = heading.closest("header")!;
      expect(screen.getAllByRole("heading", { name: "Pull Requests" })).toHaveLength(1);
      await user.click(within(header).getByRole("button", { name: "Refresh" }));
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["session-pull-requests", "conv_1"] });
      await user.click(within(header).getByRole("button", { name: "Link a PR" }));
      expect(await findLinkPicker()).toBeVisible();
      await user.keyboard("{Escape}");
      await waitFor(() => expect(queryLinkPicker()).toBeNull());
      if (count > 1) {
        await user.click(screen.getByRole("button", { name: "acme/app #6000 — chore: dummy PR" }));
      }
      expect(
        within(header).getByRole("button", { name: "Remove from this session" }),
      ).toBeEnabled();
      expect(screen.getByRole("tab", { name: "Summary" })).toBeVisible();
      if (count > 1) {
        await user.click(within(header).getByRole("button", { name: "All pull requests (2)" }));
        expect(
          within(header).queryByRole("button", { name: "Remove from this session" }),
        ).toBeNull();
        expect(screen.getByRole("list", { name: "Pull requests" })).toBeVisible();
      } else {
        expect(screen.queryByRole("button", { name: /All pull requests/ })).toBeNull();
        expect(screen.queryByRole("list", { name: "Pull requests" })).toBeNull();
      }
    },
  );

  it("shows the PR title in the header and CI check pills on the Summary tab", async () => {
    renderPanel();
    // The header names the panel, never the provider.
    const heading = screen.getByRole("heading", { name: "Pull Requests" });
    expect(heading).not.toHaveAttribute("title");
    // The shared header (every tab) carries the title and the repository link with the number.
    expect(await screen.findByRole("heading", { name: "chore: dummy PR" })).toBeInTheDocument();
    const repositoryLink = screen.getByRole("link", { name: /^Open .*#6000 on GitHub$/ });
    // The provider belongs to the PR, so its glyph rides in that row.
    const providerName = screen.getByText("GitHub");
    expect(providerName).toHaveClass("sr-only");
    expect(providerName.parentElement).toHaveAttribute("title", "GitHub");
    expect(heading).not.toContainElement(providerName);
    expect(providerName.parentElement!.nextElementSibling).toBe(repositoryLink);
    // CI checks on the Summary tab (the default): the result first, then the jobs to look at.
    expect(screen.getByRole("heading", { name: "Checks" })).toBeInTheDocument();
    expect(screen.getByText("2 failing checks")).toBeInTheDocument();
    expect(screen.getByText("2 failing, 66 successful")).toBeInTheDocument();
    expect(screen.queryByText(/\d running/)).toBeNull();
    // The fixture lists two of its 68 jobs, so the rest are pointed at on the provider.
    expect(screen.getByRole("note")).toHaveTextContent(
      "1 failing or running check isn’t listed here.",
    );
  });

  it("polls only while active and refreshes stale info on reactivation", async () => {
    const user = userEvent.setup();
    state.info!.data!.tracking_available = true;
    const url = state.info!.data!.pr!.url;
    state.stored.conv_1 = [
      { url, host: "github.com", repository: "acme/app", number: 6000, relationship: "attached" },
      {
        url: "https://github.com/acme/app/pull/6001",
        host: "github.com",
        repository: "acme/app",
        number: 6001,
        relationship: "attached",
      },
    ];
    const refetchQueries = vi.spyOn(QueryClient.prototype, "refetchQueries");
    onTestFinished(() => refetchQueries.mockRestore());
    // Only this panel's reads count, not calls an earlier test's render left behind.
    vi.mocked(usePullRequestInfo).mockClear();
    const { rerender } = renderPanel(false, false);

    // A collapsed rail keeps the panel mounted but must not poll.
    expect(await screen.findByText("chore: dummy PR")).toBeInTheDocument();
    expect(await screen.findByRole("list", { name: "Pull requests" })).toBeVisible();
    expect(
      vi.mocked(usePullRequestInfo).mock.calls.every(([, options]) => options?.poll === false),
    ).toBe(true);
    for (const pr of state.stored.conv_1) {
      expect(usePullRequestInfo).toHaveBeenCalledWith("conv_1", { poll: false, prUrl: pr.url });
    }

    expect(refetchQueries).not.toHaveBeenCalled();

    vi.mocked(usePullRequestInfo).mockClear();
    rerender(<PullRequestPanel conversationId="conv_1" active />);
    for (const pr of state.stored.conv_1) {
      expect(usePullRequestInfo).toHaveBeenCalledWith("conv_1", { poll: "list", prUrl: pr.url });
    }
    // Turning the poll back on doesn't fetch by itself.
    expect(refetchQueries).toHaveBeenCalledWith(expect.objectContaining({ stale: true }));
    expect(
      invalidatedKey(refetchQueries, [
        "pr-page-github-info",
        "conv_1",
        "https://github.com/acme/app/pull/6000",
      ]),
    ).toBe(true);
    await user.click(screen.getByRole("button", { name: "acme/app #6000 — chore: dummy PR" }));
    expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", {
      poll: true,
      prUrl: url,
      findBranch: true,
    });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Changes" }));
    expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", {
      poll: true,
      prUrl: url,
      findBranch: true,
    });
    rerender(<PullRequestPanel conversationId="conv_1" active={false} />);
    expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", {
      poll: false,
      prUrl: url,
      findBranch: true,
    });
    rerender(<PullRequestPanel conversationId="conv_1" active />);
    expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", {
      poll: true,
      prUrl: url,
      findBranch: true,
    });
    await user.click(screen.getByRole("button", { name: "All pull requests (2)" }));
    expect(screen.getByRole("list", { name: "Pull requests" })).toBeVisible();
    expect(usePullRequestInfo).toHaveBeenCalledWith("conv_1", {
      poll: "list",
      prUrl: url,
    });
    // Chosen again from the list, it opens on the section it was left on.
    await user.click(screen.getByRole("button", { name: "acme/app #6000 — chore: dummy PR" }));
    expect(screen.getByRole("tab", { name: "Changes" })).toHaveAttribute("aria-selected", "true");
    expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", {
      poll: true,
      prUrl: url,
      findBranch: true,
    });
  });

  it("shows cancelled and skipped checks as separate grey pills listing their jobs", async () => {
    const user = userEvent.setup();
    state.info!.data!.pr!.checks = {
      passing: 0,
      failing: 0,
      pending: 0,
      cancelled: 1,
      skipped: 2,
      total: 3,
      runs: [
        { name: "cancelled job", bucket: "cancelled", url: null },
        { name: "skipped job", bucket: "skipped", url: null },
        { name: "another skipped job", bucket: "skipped", url: null },
      ],
    };
    renderPanel();

    // Nothing failed or ran, so nothing needs attention: a short list starts unfolded.
    expect(screen.getByText("Checks were cancelled")).toBeInTheDocument();
    expect(screen.getByText("1 cancelled, 2 skipped")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Checks needing attention" })).toBeNull();
    const completed = screen.getByRole("list", { name: "Completed checks" });
    expect(
      within(completed)
        .getAllByRole("listitem")
        .map((row) => row.textContent),
    ).toEqual(["cancelled jobcancelled", "another skipped jobskipped", "skipped jobskipped"]);
    await user.click(screen.getByRole("button", { name: "3 completed checks" }));
    expect(screen.queryByRole("list", { name: "Completed checks" })).toBeNull();
  });

  it("shows each assigned reviewer with its current state and team kind", () => {
    state.info!.data!.pr!.reviewers = [
      { login: "alice", kind: "user", state: "PENDING" },
      { login: "bob", kind: "user", state: "APPROVED" },
      { login: "carol", kind: "user", state: "COMMENTED" },
      { login: "platform-team", kind: "team", state: "CHANGES_REQUESTED" },
      { login: "former-reviewer", kind: "user", state: "DISMISSED" },
    ];
    renderPanel();

    expect(screen.getByRole("heading", { name: "Reviewers" })).toBeInTheDocument();
    for (const [subject, status] of [
      ["alice", "Awaiting review"],
      ["bob", "Approved"],
      ["carol", "Commented"],
      ["Team platform-team", "Changes requested"],
      ["former-reviewer", "Commented"],
    ]) {
      const row = screen.getByRole("listitem", { name: `${subject}: ${status}` });
      // The state is a glyph at the row's end, named on hover.
      expect(row.lastElementChild).toHaveAttribute("title", status);
    }

    const reviewersSection = screen.getByRole("heading", { name: "Reviewers" }).closest("section");
    const checksSection = screen.getByRole("heading", { name: "Checks" }).closest("section");
    expect(reviewersSection).not.toBeNull();
    expect(checksSection).not.toBeNull();
    expect(
      reviewersSection!.compareDocumentPosition(checksSection!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("shows an empty reviewer section when the host reports no reviewers", () => {
    state.info!.data!.pr!.reviewers = [];
    renderPanel();

    expect(screen.getByRole("heading", { name: "Reviewers" })).toBeInTheDocument();
    expect(screen.getByText("No reviewers")).toBeInTheDocument();
  });

  it("updates reviewers when the active PR changes", () => {
    state.info!.data!.pr!.reviewers = [{ login: "alice", kind: "user", state: "PENDING" }];
    const { rerender } = renderPanel();
    expect(screen.getByRole("listitem", { name: "alice: Awaiting review" })).toBeInTheDocument();

    state.info!.data!.pr = {
      ...state.info!.data!.pr!,
      number: 6001,
      title: "chore: another PR",
      reviewers: [{ login: "platform-team", kind: "team", state: "APPROVED" }],
    };
    rerender(<PullRequestPanel conversationId="conv_1" />);

    expect(screen.queryByRole("listitem", { name: "alice: Awaiting review" })).toBeNull();
    expect(
      screen.getByRole("listitem", { name: "Team platform-team: Approved" }),
    ).toBeInTheDocument();
  });

  describe("when the tabs sidebar owns the view", () => {
    it("reports a sub-view click as a step, naming the pull request on screen", async () => {
      const { reports } = renderControlled({ showingDetails: false, subtab: "summary" });
      fireEvent.mouseDown(await screen.findByRole("tab", { name: "Changes" }));
      expect(reports.filter((report) => !report.replace)).toEqual([
        {
          view: expect.objectContaining({
            subtab: "changes",
            showingDetails: true,
            selected: "https://example.com/pr/6000",
          }),
          replace: false,
        },
      ]);
      expect(screen.getByRole("tab", { name: "Changes" })).toHaveAttribute("aria-selected", "true");
    });

    it("keeps a requested pull request that isn't linked and offers Link it, instead of switching to the linked one", async () => {
      const linkedUrl = "https://github.com/acme/app/pull/6000";
      const requested = "https://github.com/acme/app/pull/7000";
      state.info!.data!.tracking_available = true;
      state.stored.conv_1 = [
        {
          url: linkedUrl,
          host: "github.com",
          repository: "acme/app",
          number: 6000,
          relationship: "attached",
        },
      ];
      const { reports } = renderControlled({
        selected: requested,
        showingDetails: true,
        subtab: "summary",
      });
      expect(
        await screen.findByText("This pull request isn't linked to this session"),
      ).toBeVisible();
      expect(reports.some((report) => report.view.selected === linkedUrl)).toBe(false);
      fireEvent.click(screen.getByRole("button", { name: "Link it" }));
      // It links at once, with no picker in between.
      await waitFor(() =>
        expect(state.updateSessionPr).toHaveBeenCalledWith({ url: requested, action: "attach" }),
      );
      expect(usePullRequestAttachStore.getState().openSessionId).toBeNull();
    });

    it("keeps a requested pull request while the session's list is still unknown", async () => {
      const requested = "https://github.com/acme/app/pull/7000";
      const { reports } = renderControlled({
        selected: requested,
        showingDetails: true,
        subtab: "summary",
      });
      await screen.findByRole("tab", { name: "Summary" });
      expect(reports.every((report) => report.view.selected === requested)).toBe(true);
    });

    it("jumps to the file a place names and records a file picked in the list as a step", async () => {
      const url = "https://example.com/pr/6000";
      const { reports } = renderControlled({
        selected: url,
        showingDetails: true,
        subtab: "changes",
        file: { path: "src/app.ts" },
      });
      const picked = (await screen.findAllByRole("button", { name: /hello\.py/ })).find(
        (button) => button.getAttribute("title") === "hello.py",
      )!;
      fireEvent.click(picked);
      expect(reports.at(-1)).toEqual({
        view: expect.objectContaining({ subtab: "changes", file: { path: "hello.py" } }),
        replace: false,
      });
    });

    describe("in a pull request's own tab", () => {
      const CHANGES: PullRequestPanelView = {
        selected: "https://example.com/pr/6000",
        showingDetails: true,
        subtab: "changes",
      };
      const pullMode = () => ({
        kind: "pull" as const,
        onShowAllPullRequests: vi.fn(),
        onOpenInNewTab: vi.fn(),
      });
      const fileRow = async () =>
        (await screen.findAllByRole("button", { name: /hello\.py/ })).find(
          (button) => button.getAttribute("title") === "hello.py",
        )!;

      it("opens a file behind the tab on ⌘-click, a middle click or ⌘Enter, and in front on ⇧⌘-click", async () => {
        const platform = vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
        const mode = pullMode();
        const { reports } = renderControlled(CHANGES, mode);
        const row = await fileRow();
        const before = reports.length;
        fireEvent.click(row, { metaKey: true });
        expect(mode.onOpenInNewTab).toHaveBeenLastCalledWith(
          { path: "hello.py" },
          { background: true },
        );
        fireEvent.click(row, { metaKey: true, shiftKey: true });
        expect(mode.onOpenInNewTab).toHaveBeenLastCalledWith(
          { path: "hello.py" },
          { background: false },
        );
        fireEvent(row, new MouseEvent("auxclick", { bubbles: true, button: 1 }));
        expect(mode.onOpenInNewTab).toHaveBeenLastCalledWith(
          { path: "hello.py" },
          { background: true },
        );
        fireEvent.keyDown(row, { key: "Enter", metaKey: true });
        expect(mode.onOpenInNewTab).toHaveBeenCalledTimes(4);
        // None of them moved this tab; a plain click still steps to the file here.
        expect(reports).toHaveLength(before);
        fireEvent.click(row);
        expect(mode.onOpenInNewTab).toHaveBeenCalledTimes(4);
        expect(reports.at(-1)).toEqual({
          view: expect.objectContaining({ subtab: "changes", file: { path: "hello.py" } }),
          replace: false,
        });
        platform.mockRestore();
      });

      it("offers the file on screen in ⌘K as Open in a new tab, behind this one", async () => {
        const mode = pullMode();
        renderControlled({ ...CHANGES, file: { path: "hello.py" } }, mode);
        await fileRow();
        const row = await waitFor(() => {
          const found = paletteGroup("pull-request-file:conv_1")?.commands.find(
            (command) => command.label === "Open hello.py in a new tab",
          );
          expect(found).toBeDefined();
          return found!;
        });
        act(() => row.run());
        expect(mode.onOpenInNewTab).toHaveBeenCalledWith(
          { path: "hello.py" },
          { background: true },
        );
      });

      it("offers no new tab for a file outside the tabs sidebar, where a ⌘-click shows it here", async () => {
        const { reports } = renderControlled({ ...CHANGES, file: { path: "src/app.ts" } });
        const row = await fileRow();
        expect(paletteGroup("pull-request-file:conv_1")?.commands ?? []).toEqual([]);
        fireEvent.click(row, { metaKey: true, ctrlKey: true });
        expect(reports.at(-1)?.view).toEqual(
          expect.objectContaining({ file: { path: "hello.py" } }),
        );
      });

      it("offers no file row in ⌘K on the shared review, which tracks its own file", async () => {
        pageV2.set(true);
        renderControlled({ ...CHANGES, file: { path: "hello.py" } }, pullMode());
        await waitFor(() => expect(sharedReview.props).toBeTruthy());
        expect(paletteGroup("pull-request-file:conv_1")?.commands ?? []).toEqual([]);
      });

      it.each([true, false])(
        "⌘Enter on a focused file opens it behind the tab, never accepting a pending prompt (macOS: %s)",
        async (mac) => {
          const approval = pendingApproval(mac);
          const mode = pullMode();
          renderControlled(CHANGES, mode);
          const row = await fileRow();
          row.focus();
          approval.pressCommandEnter(row);
          expect(mode.onOpenInNewTab).toHaveBeenCalledWith(
            { path: "hello.py" },
            { background: true },
          );
          expect(approval.submitApproval).not.toHaveBeenCalled();
          approval.pressCommandEnter(document.body);
          expect(approval.submitApproval).toHaveBeenCalledWith("review-e1", "accept");
          approval.restore();
        },
      );
    });

    it("says when a place's file isn't in the pull request's changes and can show them all", async () => {
      const url = "https://example.com/pr/6000";
      const { reports } = renderControlled({
        selected: url,
        showingDetails: true,
        subtab: "changes",
        file: { path: "missing.ts" },
      });
      expect(
        await screen.findByText("missing.ts isn't in this pull request's changes."),
      ).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "Show all changes" }));
      expect(reports.at(-1)).toEqual({
        view: expect.objectContaining({ subtab: "changes", file: undefined }),
        replace: true,
      });
    });

    it("hands the file to the shared review, and records a file picked in its tree as a step", async () => {
      pageV2.set(true);
      const { reports } = renderControlled({
        selected: "https://example.com/pr/6000",
        showingDetails: true,
        subtab: "changes",
        file: { path: "src/app.ts" },
      });
      await waitFor(() => expect(sharedReview.props).toBeTruthy());
      expect(sharedReview.props).toMatchObject({ revealPath: "src/app.ts" });
      expect(sharedReview.props).not.toHaveProperty("markedLines");

      const onFileChosen = sharedReview.props!.onFileChosen as (path: string) => void;
      act(() => onFileChosen("hello.py"));
      expect(reports.at(-1)).toEqual({
        view: expect.objectContaining({ subtab: "changes", file: { path: "hello.py" } }),
        replace: false,
      });
    });

    it("opens files at the patch's pinned head in the PR's source repository, apart from comments", async () => {
      pageV2.set(true);
      const overviewHead = "a".repeat(40);
      const pinnedHead = "b".repeat(40);
      // The overview moved on after the patch on screen was read.
      state.info!.data!.pr!.head_sha = overviewHead;
      state.info!.data!.pr!.head_repository_url = "https://example.com/fork/app";
      state.diff = { ...state.diff!, head_sha: pinnedHead };
      renderControlled({
        selected: "https://example.com/pr/6000",
        showingDetails: true,
        subtab: "changes",
      });
      await waitFor(() => expect(sharedReview.props).toBeTruthy());
      expect(sharedReview.props!.fileRevision).toEqual({
        sha: pinnedHead,
        headRepositoryUrl: "https://example.com/fork/app",
      });
      // Comments keep the overview's revision.
      expect((sharedReview.props!.context as { headSha?: string }).headSha).toBe(overviewHead);
    });

    it("keeps no previous pair on the classic Changes, which shows its loading state", async () => {
      state.diffLoading = true;
      renderControlled({
        selected: "https://example.com/pr/6000",
        showingDetails: true,
        subtab: "changes",
      });
      await screen.findByText("Loading changes…");
    });

    it("falls back to the overview's head, and leaves an older host's source repository unsaid", async () => {
      pageV2.set(true);
      const overviewHead = "a".repeat(40);
      state.info!.data!.pr!.head_sha = overviewHead;
      delete state.info!.data!.pr!.head_repository_url;
      state.diff = { object: "session.github.pr_diff", patch: "PATCH" };
      renderControlled({
        selected: "https://example.com/pr/6000",
        showingDetails: true,
        subtab: "changes",
      });
      await waitFor(() => expect(sharedReview.props).toBeTruthy());
      expect(sharedReview.props!.fileRevision).toEqual({ sha: overviewHead });
      expect(sharedReview.props!.fileRevision).not.toHaveProperty("headRepositoryUrl");
      // A host that says it doesn't know is passed on as such, not dropped.
      cleanup();
      state.info!.data!.pr!.head_repository_url = null;
      renderControlled({
        selected: "https://example.com/pr/6000",
        showingDetails: true,
        subtab: "changes",
      });
      await waitFor(() =>
        expect(sharedReview.props!.fileRevision).toEqual({
          sha: overviewHead,
          headRepositoryUrl: null,
        }),
      );
    });

    it("reveals the requested file once its diff arrives after the file list", async () => {
      state.diffLoading = true;
      const initial: PullRequestPanelView = {
        selected: "https://example.com/pr/6000",
        showingDetails: true,
        subtab: "changes",
        file: { path: "src/app.ts" },
      };
      const { setView } = renderControlled(initial);
      await screen.findByText("Loading changes…");
      expect(scrollIntoView).not.toHaveBeenCalled();
      state.diffLoading = false;
      setView({ ...initial });
      await screen.findAllByTestId("diff");
      await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    });

    it("reveals the same file again after leaving Changes and coming back, as Back does", async () => {
      const initial: PullRequestPanelView = {
        selected: "https://example.com/pr/6000",
        showingDetails: true,
        subtab: "changes",
        file: { path: "src/app.ts" },
      };
      const { setView } = renderControlled(initial);
      await waitFor(() => expect(scrollIntoView, "first visit").toHaveBeenCalled());
      scrollIntoView.mockClear();
      setView({ selected: initial.selected, showingDetails: true, subtab: "summary" });
      setView({ ...initial });
      await screen.findAllByTestId("diff");
      await waitFor(() => expect(scrollIntoView, "return").toHaveBeenCalled());
    });

    it("reveals the file again after Changes without a file, as Back then Forward do", async () => {
      const initial: PullRequestPanelView = {
        selected: "https://example.com/pr/6000",
        showingDetails: true,
        subtab: "changes",
        file: { path: "src/app.ts" },
      };
      const { setView } = renderControlled(initial);
      await waitFor(() => expect(scrollIntoView, "first visit").toHaveBeenCalled());
      scrollIntoView.mockClear();
      setView({ selected: initial.selected, showingDetails: true, subtab: "changes" });
      setView({ ...initial });
      await waitFor(() => expect(scrollIntoView, "return").toHaveBeenCalled());
    });

    it("drops a reveal queued for a file once a newer file is requested", async () => {
      const frames = new Map<number, FrameRequestCallback>();
      let lastFrame = 0;
      vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
        frames.set(++lastFrame, callback);
        return lastFrame;
      });
      vi.stubGlobal("cancelAnimationFrame", (frame: number) => frames.delete(frame));
      state.diffError = new Error("temporary diff failure");
      const initial: PullRequestPanelView = {
        selected: "https://example.com/pr/6000",
        showingDetails: true,
        subtab: "changes",
        file: { path: "src/app.ts" },
      };
      const { setView } = renderControlled(initial);
      await screen.findByText("temporary diff failure");
      // The diff recovers, so the file's section mounts and queues its reveal.
      state.diffError = null;
      setView({ ...initial });
      await screen.findAllByTestId("diff");
      const revealed = () =>
        scrollIntoView.mock.contexts.map((element) =>
          (element as HTMLElement).getAttribute("data-github-file"),
        );
      // What was revealed before the newer request stays; nothing queued for it lands after.
      const before = revealed().length;
      setView({ ...initial, file: { path: "hello.py" } });
      expect(revealed().at(-1)).toBe("hello.py");
      const queued = [...frames.values()];
      frames.clear();
      act(() => queued.forEach((callback) => callback(0)));
      expect(revealed().slice(before)).not.toContain("src/app.ts");
    });

    it("shows the sub-view the owner sets, without writing the shared panel cache", async () => {
      const { client, reports, setView } = renderControlled({
        showingDetails: false,
        subtab: "summary",
      });
      fireEvent.mouseDown(await screen.findByRole("tab", { name: /^Comments/ }));
      const selected = reports.at(-1)!.view.selected!;
      setView({ selected, showingDetails: true, subtab: "summary" });
      expect(await screen.findByRole("tab", { name: "Summary" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      setView({ selected, showingDetails: true, subtab: "changes" });
      expect(await screen.findByRole("tab", { name: "Changes" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      // The panel's own remembered view stays as it was, and Classic's is never written.
      expect(client.getQueryData(pullRequestPageViewKey("conv_1"))).toEqual({
        showingDetails: false,
        subtab: "summary",
      });
      expect(client.getQueryData(pullRequestPanelViewKey("conv_1"))).toBeUndefined();
    });
  });

  describe("title and description editing", () => {
    const prUrl = "https://github.com/acme/app/pull/6000";

    beforeEach(() => {
      state.info!.data!.selected_pr_url = prUrl;
      state.info!.data!.capabilities = {
        account_switching: true,
        base_remote_selection: true,
        line_counts: true,
        linked_pr_diff: true,
        details_editing: true,
        attachment_uploads: true,
      };
    });

    it.each([
      [
        "a view-only collaborator",
        () => {
          state.canEdit = false;
        },
      ],
      [
        "a host without details editing",
        () => {
          delete state.info!.data!.capabilities;
        },
      ],
      [
        "a host that names no selected PR",
        () => {
          delete state.info!.data!.selected_pr_url;
        },
      ],
    ])("offers no title or description editing to %s", (_caller, setup) => {
      setup();
      renderPanel();

      expect(screen.queryByRole("button", { name: "Edit title" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Edit description" })).not.toBeInTheDocument();
    });
  });

  describe("reviewer editing", () => {
    const prUrl = "https://github.com/acme/app/pull/6000";

    beforeEach(() => {
      state.info!.data!.selected_pr_url = prUrl;
      state.info!.data!.pr!.reviewers = [
        { login: "alice", kind: "user", state: "PENDING" },
        { login: "bob", kind: "user", state: "APPROVED" },
      ];
      state.info!.data!.capabilities = {
        account_switching: true,
        base_remote_selection: true,
        line_counts: true,
        linked_pr_diff: true,
        reviewer_editing: true,
      };
    });

    it("wires the rows and the picker to the selected PR", () => {
      renderPanel();

      fireEvent.click(screen.getByRole("button", { name: "Withdraw review request from alice" }));
      expect(state.updateReviewers.mutate).toHaveBeenLastCalledWith(
        { prUrl, add: [], remove: [{ login: "alice", kind: "user" }] },
        expect.any(Object),
      );
      fireEvent.click(screen.getByRole("button", { name: "Re-request review from bob" }));
      expect(state.updateReviewers.mutate).toHaveBeenLastCalledWith(
        { prUrl, add: [{ login: "bob", kind: "user" }], remove: [] },
        expect.any(Object),
      );
      expect(usePullRequestReviewCandidates).toHaveBeenLastCalledWith("conv_1", prUrl, false);
      fireEvent.click(screen.getByRole("button", { name: "Request reviewers" }));
      expect(usePullRequestReviewCandidates).toHaveBeenLastCalledWith("conv_1", prUrl, true);
    });

    it.each([
      [
        "a view-only collaborator",
        () => {
          state.canEdit = false;
        },
      ],
      [
        "a host without reviewer editing",
        () => {
          delete state.info!.data!.capabilities;
        },
      ],
      [
        "a host that names no selected PR",
        () => {
          delete state.info!.data!.selected_pr_url;
        },
      ],
      [
        "a host that doesn't list reviewer candidates",
        () => {
          routeServes.methods.delete("listReviewerCandidates");
        },
      ],
      [
        "a host that doesn't change reviewers",
        () => {
          routeServes.methods.delete("updateReviewers");
        },
      ],
    ])("shows reviewers read-only for %s", (_caller, setup) => {
      setup();
      renderPanel();

      expect(screen.getByRole("listitem", { name: "alice: Awaiting review" })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Request reviewers" })).toBeNull();
      expect(screen.queryByRole("button", { name: /review request from|review from/ })).toBeNull();
    });
  });

  describe("labels", () => {
    const prUrl = "https://github.com/acme/app/pull/6000";
    const bug = { name: "bug", color: "d73a4a", description: "Something isn't working" };
    const docs = { name: "Docs", color: "0075ca", description: null };

    beforeEach(() => {
      state.info!.data!.selected_pr_url = prUrl;
      state.info!.data!.pr!.labels = [docs, bug];
      state.info!.data!.capabilities = {
        account_switching: true,
        base_remote_selection: true,
        line_counts: true,
        linked_pr_diff: true,
        label_editing: true,
      };
    });

    it("lists the PR's labels by name beside reviewers, above checks", () => {
      state.info!.data!.pr!.reviewers = [];
      renderPanel();

      const list = screen.getByRole("list", { name: "Labels" });
      expect(
        within(list)
          .getAllByRole("listitem")
          .map((item) => item.textContent),
      ).toEqual(["bug", "Docs"]);
      expect(within(list).getAllByRole("listitem")[0]).toHaveAttribute(
        "title",
        "Something isn't working",
      );
      const labels = list.closest("section")!;
      const reviewers = screen.getByRole("heading", { name: "Reviewers" }).closest("section")!;
      const checks = screen.getByText("Checks").closest("section")!;
      expect(
        labels.compareDocumentPosition(reviewers) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
      expect(
        labels.compareDocumentPosition(checks) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
      // Reviewers and labels share one row, the reviewers column bordering the labels, above a
      // divider. Narrow, the columns stack and the border turns horizontal.
      const row = reviewers.parentElement!.parentElement!;
      expect(row).toContainElement(labels);
      expect(reviewers.parentElement).toHaveClass("border-l");
      expect(reviewers.parentElement).toHaveClass("@max-[480px]:border-t");
      expect(row).toHaveClass("@max-[480px]:flex-col");
      expect(row.nextElementSibling).toHaveAttribute("data-slot", "separator");
    });

    it("wires editing to the selected PR: chips remove, and the picker loads on open", () => {
      renderPanel();

      fireEvent.click(screen.getByRole("button", { name: "Remove label bug" }));
      expect(state.updateLabels.mutate).toHaveBeenLastCalledWith(
        { prUrl, add: [], remove: [bug], madeBy: { owner: expect.any(String) } },
        expect.any(Object),
      );
      expect(usePullRequestRepoLabels).toHaveBeenLastCalledWith("conv_1", prUrl, false);
      fireEvent.click(screen.getByRole("button", { name: "Edit labels" }));
      expect(usePullRequestRepoLabels).toHaveBeenLastCalledWith("conv_1", prUrl, true);
      expect(screen.getByRole("combobox", { name: "Filter labels" })).toBeInTheDocument();
    });

    it.each(["listLabels", "updateLabels"])(
      "shows labels read-only where the host doesn't serve %s",
      (method) => {
        routeServes.methods.delete(method);
        renderPanel();

        expect(screen.getByRole("list", { name: "Labels" })).toBeInTheDocument();
        expect(screen.queryByRole("button", { name: "Edit labels" })).toBeNull();
        expect(screen.queryByRole("button", { name: "Remove label bug" })).toBeNull();
        expect(usePullRequestRepoLabels).not.toHaveBeenCalledWith("conv_1", prUrl, true);
      },
    );
  });

  describe("merge button", () => {
    beforeEach(() => {
      state.info!.data!.selected_pr_url = "https://github.com/acme/app/pull/6000";
      state.info!.data!.pr!.head_sha = "a".repeat(40);
      state.info!.data!.pr!.merge_state_status = "CLEAN";
      state.info!.data!.pr!.auto_merge = null;
      state.info!.data!.capabilities = {
        account_switching: true,
        base_remote_selection: true,
        line_counts: true,
        linked_pr_diff: true,
        merging: true,
      };
    });

    it("offers the Merge as picker only when the host merges as the account it sends", () => {
      const accounts = [
        { login: "octo", active: true },
        { login: "other", active: false },
      ];
      state.info!.data!.auth = {
        authenticated: true,
        hint: null,
        cli: { name: "gh", available: true },
        accounts: accounts.map((account) => ({ ...account, state: "success", host: "github.com" })),
        selected_account: "octo",
      } as never;
      state.info!.data!.capabilities = {
        account_switching: true,
        base_remote_selection: true,
        line_counts: true,
        linked_pr_diff: true,
        merging: true,
        merge_as_account: true,
      };
      renderPanel();

      const merge = screen.getByRole("region", { name: "Merge" });
      fireEvent.click(within(merge).getByRole("button", { name: "Merge pull request" }));
      expect(screen.getByRole("combobox", { name: "Merge as" })).toHaveTextContent("octo");
    });

    it.each(["getMergeOptions", "mergePullRequest"])(
      "offers no merge where the host doesn't serve %s",
      (method) => {
        renderPanel();
        expect(screen.getByRole("region", { name: "Merge" })).toBeInTheDocument();
        cleanup();
        routeServes.methods.delete(method);
        renderPanel();
        expect(screen.queryByRole("region", { name: "Merge" })).toBeNull();
      },
    );
  });

  it.each([
    ["OPEN", "Open", "text-status-green"],
    ["CLOSED", "Closed", "text-status-red"],
    ["MERGED", "Merged", "text-status-purple"],
  ])("shows a %s status pill beside the PR title", (stateName, label, tone) => {
    state.info!.data!.pr!.state = stateName;
    renderPanel();

    const pill = screen.getByLabelText(`Pull request status: ${label}`);
    expect(pill).toHaveTextContent(label);
    expect(pill).toHaveClass(tone, "h-5", "rounded-full", "border", "text-xs");
    // The pill sits beside the repository link that opens the PR; the title wraps below.
    expect(pill.previousElementSibling).toHaveAccessibleName(/^Open .* on GitHub$/);
    expect(screen.getByRole("heading", { name: /chore: dummy PR/ })).toHaveClass("break-words");
  });

  it("renders GitHub emoji shortcodes in the description and comments, as GitHub does", async () => {
    state.info!.data!.pr!.body = "Ready :green_book:";
    state.info!.data!.pr!.comments = [
      {
        author: "octocat",
        body: "Looks good :+1: but `:nope:` stays",
        created_at: "2026-09-05T07:32:02Z",
        url: "https://example.com/pr/6000#c1",
      },
    ];
    renderPanel();
    // The renderer itself is stubbed here; message.test.tsx renders the emoji for real.
    const description = await screen.findByText("Ready :green_book:");
    expect(description).toHaveAttribute("data-github-markdown", "on");
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Comments 1" }));
    expect(await screen.findByText(/Looks good :\+1:/)).toHaveAttribute(
      "data-github-markdown",
      "on",
    );
  });

  it("shows empty states when the PR has no body or comments", () => {
    // The default fixture carries neither a body nor comments.
    renderPanel();
    expect(screen.getByText("No description provided.")).toBeInTheDocument();
    expect(screen.queryByTestId("diff")).toBeNull();

    const commentsTab = screen.getByRole("tab", { name: "Comments" });
    expect(commentsTab).toHaveTextContent(/^Comments\s*$/);
    fireEvent.mouseDown(commentsTab);
    expect(screen.getByText("No comments yet")).toBeInTheDocument();
  });

  it("marks the PR author's comments, ignoring case", () => {
    state.info!.data!.pr!.comments = [
      { author: "DEV", body: "Addressed the review", created_at: null, url: null },
      { author: "reviewer", body: "Thanks!", created_at: null, url: null },
    ];
    renderComments();
    const [byAuthor, byReviewer] = screen.getAllByRole("listitem");
    expect(within(byAuthor).getByText("Author")).toHaveAttribute(
      "title",
      "Opened this pull request",
    );
    expect(within(byReviewer).queryByText("Author")).toBeNull();
  });

  it("marks incomplete checks and comments without claiming they are empty", () => {
    state.info!.data!.pr!.checks = {
      passing: 0,
      failing: 0,
      pending: 0,
      total: 0,
      runs: [],
      partial: true,
    };
    state.info!.data!.pr!.comments_partial = true;
    renderPanel();

    expect(screen.getByRole("heading", { name: "Checks" })).toBeInTheDocument();
    expect(screen.getByText("Checks couldn’t be loaded")).toBeInTheDocument();
    expect(screen.queryByText("No checks reported")).toBeNull();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Comments" }));
    expect(screen.getByText(/more comments than Omnigent shows/)).toBeInTheDocument();
    expect(screen.queryByText("No comments yet")).not.toBeInTheDocument();
  });

  it("shows who opened the PR, its branches, and the size of the change in the header", () => {
    const { rerender } = renderPanel();
    expect(screen.getByText("Opened by dev")).toBeInTheDocument();
    const branches = screen.getByText("test/pr-view").closest("p")!;
    expect(branches).toHaveTextContent("test/pr-view→main2 files+4−1");

    // A changed-file list that's still loading or incomplete doesn't claim a size.
    state.changes!.data!.has_more = true;
    rerender(<PullRequestPanel conversationId="conv_1" />);
    expect(screen.getByText("test/pr-view").closest("p")).toHaveTextContent("test/pr-view→main");
    expect(screen.queryByText("2 files")).toBeNull();

    // A file without line counts keeps the file count but drops the totals.
    state.changes!.data = {
      available: true,
      data: [file("hello.py", "created"), file("image.png", "created", null, null)],
    };
    rerender(<PullRequestPanel conversationId="conv_1" />);
    expect(screen.getByText("test/pr-view").closest("p")).toHaveTextContent(
      "test/pr-view→main2 files",
    );
    expect(screen.queryByText(/^\+\d/)).toBeNull();
  });

  it("names a linked PR's own repository in the header, not the session's", () => {
    const pr = state.info!.data!.pr!;
    state.info!.data!.prs = [
      {
        url: pr.url,
        host: "example.com",
        repository: "acme/other",
        number: pr.number,
        relationship: "attached",
      },
    ];
    renderPanel();
    expect(screen.getByRole("link", { name: "Open acme/other #6000 on GitHub" })).toHaveAttribute(
      "href",
      pr.url,
    );
  });

  it("totals the changed files above the diffs on the Changes tab", () => {
    renderChanges();
    const toolbar = screen.getByRole("button", { name: "Hide file list" }).parentElement!;
    expect(toolbar).toHaveTextContent("2 files+4−1");
  });

  it("lists failing and running jobs with links to their runs, and folds the rest", async () => {
    const user = userEvent.setup();
    state.info!.data!.pr!.checks = {
      passing: 6,
      failing: 1,
      pending: 1,
      total: 8,
      runs: [
        ...Array.from({ length: 6 }, (_, index) => ({
          name: `unit ${index + 1}`,
          bucket: "passing" as const,
          url: null,
        })),
        { name: "deploy", bucket: "pending", url: null },
        { name: "e2e", bucket: "failing", url: "https://ci.example.test/runs/7" },
      ],
    };
    renderPanel();

    expect(screen.getByText("1 failing check")).toBeInTheDocument();
    expect(screen.getByText("1 failing, 1 running, 6 successful")).toBeInTheDocument();
    const attention = screen.getByRole("list", { name: "Checks needing attention" });
    expect(within(attention).getByRole("link", { name: "e2e: failing" })).toHaveAttribute(
      "href",
      "https://ci.example.test/runs/7",
    );
    expect(within(attention).getByLabelText("deploy: running")).toBeInTheDocument();
    // Six passing jobs fold away while something needs attention.
    expect(screen.queryByRole("list", { name: "Completed checks" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "6 completed checks" }));
    expect(
      within(screen.getByRole("list", { name: "Completed checks" })).getAllByRole("listitem"),
    ).toHaveLength(6);
  });

  it("explains a failing check beyond the listed jobs and links to all of them", () => {
    state.info!.data!.pr!.checks = {
      passing: 300,
      failing: 1,
      pending: 0,
      total: 301,
      runs: Array.from({ length: 300 }, (_, index) => ({
        name: `job ${index}`,
        bucket: "passing" as const,
        url: null,
      })),
    };
    state.info!.data!.pr!.url = "https://github.com/acme/app/pull/6000";
    renderPanel();

    expect(screen.getByText("1 failing check")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Checks needing attention" })).toBeNull();
    const note = screen.getByRole("note");
    expect(note).toHaveTextContent("1 failing or running check isn’t listed here.");
    expect(within(note).getByRole("link", { name: "See all checks on GitHub" })).toHaveAttribute(
      "href",
      "https://github.com/acme/app/pull/6000",
    );
    expect(screen.getByRole("button", { name: "300 completed checks" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("folds completed checks that arrive on a later poll, and keeps the user's choice", async () => {
    const user = userEvent.setup();
    state.info!.data!.pr!.checks = { passing: 0, failing: 0, pending: 0, total: 0, runs: [] };
    const { rerender } = renderPanel();
    expect(screen.getByText("No checks reported")).toBeInTheDocument();

    const polled = (failing: number) => ({
      passing: 100,
      failing,
      pending: 0,
      total: 100 + failing,
      runs: [
        ...Array.from({ length: 100 }, (_, index) => ({
          name: `job ${index}`,
          bucket: "passing" as const,
          url: null,
        })),
        ...(failing ? [{ name: "e2e", bucket: "failing" as const, url: null }] : []),
      ],
    });
    state.info!.data!.pr!.checks = polled(1);
    rerender(<PullRequestPanel conversationId="conv_1" />);
    const fold = screen.getByRole("button", { name: "100 completed checks" });
    expect(fold).toHaveAttribute("aria-expanded", "false");

    // Opened by hand, the list stays open as later polls change the results.
    await user.click(fold);
    state.info!.data!.pr!.checks = polled(0);
    rerender(<PullRequestPanel conversationId="conv_1" />);
    expect(screen.getByRole("button", { name: "100 completed checks" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("explains unreadable checks instead of showing empty counts", () => {
    // The token read the PR but GitHub refused its check runs.
    state.info!.data!.pr!.checks = { passing: 0, failing: 0, pending: 0, total: 0, runs: [] };
    state.info!.data!.pr!.checks_supported = false;
    renderPanel();

    expect(screen.getByText("Checks")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Checks unavailable");
    expect(screen.getByRole("status")).toHaveTextContent(
      "Checks can’t be read with this GitHub token. Fine-grained personal access tokens can’t read check runs; use a classic token or a GitHub App.",
    );
    expect(screen.queryByText(/Some checks are unavailable/)).toBeNull();
    expect(
      screen.queryByRole("button", { name: /passed|failed|pending|cancelled|skipped/ }),
    ).toBeNull();
    expect(screen.getByText("No description provided.")).toBeInTheDocument();
  });

  it("retains loaded comments while indicating that more could not be fetched", () => {
    state.info!.data!.pr!.comments_partial = true;
    state.info!.data!.pr!.comments = [
      { author: "reviewer", body: "Loaded comment", created_at: null, url: null },
    ];
    renderComments();

    expect(screen.getByRole("tab", { name: "Comments 1+" })).toBeInTheDocument();
    expect(screen.getByText("Loaded comment")).toBeInTheDocument();
    expect(screen.getByText(/more comments than Omnigent shows/)).toBeInTheDocument();
  });

  it("shows the Changes tab on the shared review under the Tabs design, and stacked under Classic", async () => {
    pageV2.set(true);
    const view = renderChanges();
    expect(await screen.findByTestId("shared-review")).toBeInTheDocument();
    expect(screen.queryAllByTestId("diff")).toHaveLength(0);
    const props = sharedReview.props!;
    expect((props.files as { path: string }[]).map((entry) => entry.path)).toEqual([
      "hello.py",
      "src/app.ts",
    ]);
    expect(props.loadDiffFiles).toEqual(expect.any(Function));
    view.unmount();

    pageV2.set(false);
    renderChanges();
    expect(await screen.findAllByTestId("diff")).toHaveLength(2);
    expect(screen.queryByTestId("shared-review")).not.toBeInTheDocument();
  });

  it("withdraws the shared review's commands while the rail hides the panel", async () => {
    pageV2.set(true);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const panel = (active: boolean) => (
      <QueryClientProvider client={client}>
        <PullRequestPanel conversationId="conv_1" active={active} />
      </QueryClientProvider>
    );
    const view = render(panel(true));
    fireEvent.mouseDown(screen.getByRole("tab", { name: /^Changes/ }));
    await screen.findByTestId("shared-review");
    expect(sharedReview.props!.active).toBe(true);
    view.rerender(panel(false));
    expect(sharedReview.props!.active).toBe(false);
    view.rerender(panel(true));
    expect(sharedReview.props!.active).toBe(true);
  });

  it.each([1, 2])("offers the stored PR in the empty state with %i GitHub accounts", (count) => {
    const url = "https://github.com/acme/app/pull/6000";
    const info = state.info!.data!;
    info.pr = null;
    info.repo = null;
    info.tracking_available = true;
    info.selected_pr_url = url;
    info.prs = [
      { url, host: "github.com", repository: "acme/app", number: 6000, relationship: "created" },
    ];
    info.accounts = ["personal", "work"].slice(0, count).map((login) => ({
      login,
      active: login === "personal",
      state: "success",
      host: "github.com",
    }));
    info.selected_account = "personal";
    renderPanel();
    const emptyState = screen.getByText("Couldn’t open this repository").parentElement;
    const link = screen.getByRole("link", { name: "Open the PR on GitHub" });
    expect(emptyState).toContainElement(link);
    expect(emptyState).toContainElement(screen.getByText("or", { exact: true }));
    expect(link).toHaveAttribute("href", url);
    expect(link).toHaveAttribute("target", "_blank");
    const account = screen.queryByRole("combobox", { name: "GitHub account" });
    if (count > 1) {
      expect(emptyState).toContainElement(account);
      expect(
        account!.compareDocumentPosition(link) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    } else {
      expect(account).toBeNull();
    }
  });

  it("hides the account selector when the provider can't switch accounts", () => {
    const info = state.info!.data!;
    info.repo = null;
    info.accounts = ["personal", "work"].map((login) => ({
      login,
      active: login === "personal",
      state: "success",
      host: "github.com",
    }));
    info.selected_account = "personal";
    info.capabilities = {
      account_switching: false,
      base_remote_selection: true,
      line_counts: true,
      linked_pr_diff: true,
    };
    renderPanel();
    expect(screen.getByText("Couldn’t open this repository")).toBeInTheDocument();
    expect(screen.queryByText(/pick another account/i)).toBeNull();
    expect(screen.queryByRole("combobox", { name: "GitHub account" })).toBeNull();
  });

  describe("when the host's GitHub CLI can't reach the repo", () => {
    function signInState(accounts: PullRequestAccount[], authenticated = false) {
      const info = state.info!.data!;
      info.repo = null;
      info.pr = null;
      info.auth = {
        authenticated,
        hint: null,
        cli: { name: "gh", available: true },
        accounts,
        selected_account: null,
      };
    }

    it("doesn't claim signed out when gh lists no accounts, and checks again on click", async () => {
      signInState([]);
      const invalidate = vi.spyOn(QueryClient.prototype, "invalidateQueries").mockResolvedValue();
      onTestFinished(() => invalidate.mockRestore());
      renderPanel();
      expect(screen.getByText("Sign in to GitHub on the host")).toBeInTheDocument();
      expect(screen.getByText(/or Omnigent couldn’t check it/)).toBeInTheDocument();
      expect(screen.getByText("gh auth login")).toHaveClass("font-mono");
      await userEvent.click(screen.getByRole("button", { name: "Check again" }));
      expect(invalidatedKey(invalidate, ["pr-page-github-info", "conv_1"])).toBe(true);
    });

    it("on desktop, says what's wrong without sending people to the host's GitHub CLI", () => {
      (window as unknown as Record<string, unknown>).omnigentDesktop = { kind: "electron" };
      onTestFinished(() => {
        delete (window as unknown as Record<string, unknown>).omnigentDesktop;
      });
      signInState([]);
      renderPanel();
      expect(screen.getByText("Can’t load this session’s pull requests")).toBeInTheDocument();
      expect(screen.getByText(/GitHub account yet\./)).toBeInTheDocument();
      expect(screen.queryByText(/gh auth/)).toBeNull();
      expect(screen.getByRole("button", { name: "Check again" })).toBeInTheDocument();
    });

    it("on desktop, gives a host too old to list accounts truthful copy and Check again", () => {
      (window as unknown as Record<string, unknown>).omnigentDesktop = { kind: "electron" };
      onTestFinished(() => {
        delete (window as unknown as Record<string, unknown>).omnigentDesktop;
      });
      // The legacy payload: signed in by default, no account list, no repo.
      state.info!.data!.repo = null;
      state.info!.data!.pr = null;
      renderPanel();
      expect(screen.getByText("Can’t reach this repository")).toBeInTheDocument();
      expect(
        screen.getByText(/The session’s host couldn’t open this repository\./),
      ).toBeInTheDocument();
      expect(screen.queryByText(/gh auth/)).toBeNull();
      expect(screen.getByRole("button", { name: "Check again" })).toBeInTheDocument();
    });

    it("lists each account with gh's own check result", () => {
      signInState([
        { login: "personal", active: true, state: "error", host: "github.com" },
        { login: "work_data", active: false, state: "timeout", host: "ghe.example.com" },
      ]);
      renderPanel();
      expect(screen.getByText("GitHub sign-in on the host isn’t working")).toBeInTheDocument();
      const list = screen.getByRole("list", { name: "GitHub accounts on the host" });
      expect(list).toHaveTextContent("personal — failed gh’s sign-in check");
      expect(list).toHaveTextContent("work_data on ghe.example.com — sign-in check timed out");
      expect(screen.queryByText(/expired/i)).toBeNull();
    });
  });

  it("says a Example Host remote's pull requests aren't shown yet, without the Enterprise sign-in line", () => {
    state.info = {
      data: {
        object: "session.github.info",
        available: false,
        reason: "unsupported_remote",
        remote_host: "3.host.example.com",
        provider: null,
      },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    renderPanel();
    expect(screen.getByText("This repository is on Example Host")).toBeInTheDocument();
    expect(screen.getByText("3.host.example.com")).toHaveClass("font-mono");
    expect(screen.queryByText(/For a GitHub Enterprise host/)).toBeNull();
  });

  it("leaves out the host sign-in line on desktop", () => {
    (window as unknown as Record<string, unknown>).omnigentDesktop = { kind: "electron" };
    onTestFinished(() => {
      delete (window as unknown as Record<string, unknown>).omnigentDesktop;
    });
    state.info = {
      data: {
        object: "session.github.info",
        available: false,
        reason: "unsupported_remote",
        remote_host: "git.example.com",
        provider: null,
      },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    renderPanel();
    expect(screen.getByText("No supported remote")).toBeInTheDocument();
    expect(screen.queryByText(/gh auth login/)).toBeNull();
  });

  describe("with Example Forge", () => {
    const prUrl = "https://forge.example.test/contoso/web/_git/app/pullrequest/7";
    const auth = (over: Partial<PullRequestAuth> = {}): PullRequestAuth => ({
      authenticated: true,
      hint: "Run `forge login` on the host, or set `FORGE_TOKEN`.",
      cli: { name: "forge", available: true },
      accounts: null,
      selected_account: null,
      ...over,
    });
    const forge = (over: Partial<PullRequestInfo> = {}): PullRequestInfo => ({
      object: "session.github.info",
      available: true,
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
      auth: auth(),
      capabilities: {
        account_switching: false,
        base_remote_selection: false,
        line_counts: false,
        linked_pr_diff: false,
      },
      branch: "feat/widget",
      base_ref: "main",
      repo: { name_with_owner: "contoso/web/app" },
      pr: {
        number: 7,
        title: "Add the widget",
        state: "OPEN",
        url: prUrl,
        is_draft: false,
        author: "dev",
        base_ref: "main",
        head_ref: "feat/widget",
        checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
      },
      ...over,
    });
    const association = (over: Partial<PullRequestAssociation> = {}): PullRequestAssociation => ({
      url: prUrl,
      host: "forge.example.test",
      repository: "contoso/web/app",
      number: 7,
      relationship: "created",
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
      ...over,
    });

    it("shows the provider, the repo, and a !-prefixed PR with no account selector", () => {
      state.info!.data = forge();
      renderPanel();
      const provider = screen.getByTitle("Example Forge");
      expect(provider.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
      expect(screen.getByText(/contoso\/web\/app/)).toBeInTheDocument();
      expect(
        screen.getByRole("link", { name: "Open contoso/web/app !7 on Example Forge" }),
      ).toHaveAttribute("href", prUrl);
      expect(screen.getByRole("heading", { name: "Add the widget" })).toBeInTheDocument();
      expect(screen.queryByRole("combobox", { name: "GitHub account" })).toBeNull();
      expect(screen.queryByText(/GitHub/)).toBeNull();
    });

    it("labels linked PRs with a !, leaving out forge.example.test but not other hosts", async () => {
      const other = "https://other.example.test/fabrikam/api/_git/svc/pullrequest/12";
      state.info!.data = forge({
        tracking_available: true,
        selected_pr_url: prUrl,
        prs: [
          association({ title: "Add the widget" }),
          association({
            url: other,
            host: "other.example.test",
            repository: "fabrikam/api/svc",
            number: 12,
            relationship: "inferred",
          }),
        ],
      });
      renderPanel();
      const list = within(screen.getByRole("list", { name: "Pull requests" }));
      expect(
        list.getByRole("button", {
          name: "forge.example.test/contoso/web/app !7 — Add the widget",
        }),
      ).toBeVisible();
      // Rows show the forge's number prefix, and an untitled PR names its repository.
      expect(list.getByText("!7")).toBeVisible();
      expect(list.getByText("fabrikam/api/svc !12")).toBeVisible();
      expect(
        screen.getByRole("button", {
          name: "other.example.test/fabrikam/api/svc !12 (from branch)",
        }),
      ).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "Link a PR" }));
      expect(await findLinkPicker()).toHaveAttribute(
        "placeholder",
        "Paste a pull request link or type !123",
      );
    });

    describe("comment and check lists", () => {
      // Every comment in one discussion thread carries the thread's URL.
      const threadUrl = `${prUrl}?discussionId=3`;
      const comment = (over: Partial<PullRequestComment>): PullRequestComment => ({
        author: "Ada Lovelace",
        author_id: "u-ada",
        body: "",
        created_at: "2026-09-05T07:32:02.100Z",
        url: threadUrl,
        ...over,
      });
      const withPr = (over: Partial<PullRequest>): PullRequestInfo =>
        forge({ pr: { ...forge().pr!, ...over } });

      it("renders every comment of a thread whose replies share one URL", () => {
        const keyWarnings = watchKeyWarnings();
        state.info!.data = withPr({
          comments: [
            comment({ body: "Why not reuse the helper?" }),
            comment({
              author: "Grace Hopper",
              author_id: "u-grace",
              body: "It predates the helper.",
              created_at: "2026-09-05T08:01:40.250Z",
            }),
            comment({ body: "Fair, resolving this.", created_at: "2026-09-05T08:15:09.000Z" }),
          ],
        });
        renderComments();

        expect(screen.getByRole("tab", { name: "Comments 3" })).toBeInTheDocument();
        expect(commentBodies()).toEqual([
          "Why not reuse the helper?",
          "It predates the helper.",
          "Fair, resolving this.",
        ]);
        // Each keeps its own actions, though all three link to the one thread.
        expect(screen.getAllByRole("button", { name: "Comment actions" })).toHaveLength(3);
        expect(keyWarnings()).toEqual([]);
      });

      it("lists every check and links only http(s) run URLs, even when checks share a URL or a name", () => {
        const keyWarnings = watchKeyWarnings();
        const buildUrl = "https://forge.example.test/contoso/web/_build/results?buildId=41";
        state.info!.data = withPr({
          checks: {
            passing: 5,
            failing: 0,
            pending: 0,
            total: 5,
            runs: [
              { name: "Build", bucket: "passing", url: buildUrl },
              { name: "Lint", bucket: "passing", url: buildUrl },
              { name: "Deploy", bucket: "passing", url: null },
              { name: "Deploy", bucket: "passing", url: null },
              // A status poster controls its target URL, so a script URL must not render.
              { name: "Status", bucket: "passing", url: "javascript:alert(1)" },
            ],
          },
        });
        renderPanel();

        expect(screen.getByText("All checks have passed")).toBeInTheDocument();
        // Five quiet checks start unfolded, by name.
        const rows = within(screen.getByRole("list", { name: "Completed checks" })).getAllByRole(
          "listitem",
        );
        expect(rows.map((row) => row.textContent)).toEqual([
          "Buildsuccessful",
          "Deploysuccessful",
          "Deploysuccessful",
          "Lintsuccessful",
          "Statussuccessful",
        ]);
        expect(
          rows.map((row) => within(row).queryByRole("link")?.getAttribute("href") ?? null),
        ).toEqual([buildUrl, null, null, buildUrl, null]);
        expect(within(rows[0]).getByRole("link")).toHaveAttribute("target", "_blank");
        expect(keyWarnings()).toEqual([]);
      });
    });
  });

  it("refreshes info, files, diff and the stored PRs even from an error state", async () => {
    state.info!.error = new Error("Request timed out.");
    const invalidate = vi.spyOn(QueryClient.prototype, "invalidateQueries").mockResolvedValue();
    onTestFinished(() => invalidate.mockRestore());
    renderPanel();
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      // The session's PR reads, its own and the shared records of its PRs, by one predicate.
      undefined,
      ["session-pull-requests", "conv_1"],
      // The desktop shell's account checks and commit lists; commit diffs never change.
      ["github-fast-track", "account_status"],
      ["github-fast-track", "pull_request_commits"],
      ["github-fast-track", "pull_request_list_stats"],
      // Every stack's members, and GitHub's own stacks, which aren't the session's own queries.
      ["pull-request-stack-members"],
      ["pull-request-native-stack"],
    ]);
    for (const family of [
      "pr-page-github-info",
      "pr-page-github-review-threads",
      "pr-page-github-changed-files",
      "pr-page-github-pr-diff",
      "pr-page-github-pr-commits",
      "pr-page-github-pr-commit-diff",
    ]) {
      expect(
        invalidatedKey(invalidate, [family, "conv_1", "https://github.com/acme/app/pull/6000"]),
        family,
      ).toBe(true);
    }
  });

  it("refreshes from ⌘K as the header's Refresh button does, while the panel is active", async () => {
    const invalidate = vi.spyOn(QueryClient.prototype, "invalidateQueries").mockResolvedValue();
    onTestFinished(() => invalidate.mockRestore());
    renderPanel();
    const refresh = () =>
      paletteGroup("pull-request-panel:conv_1")?.commands.find(
        (command) => command.label === REFRESH_PULL_REQUESTS_LABEL,
      );
    expect(refresh()!.availability).toEqual({ available: true });
    await act(async () => refresh()!.run());
    expect(invalidatedKey(invalidate, ["pr-page-github-info", "conv_1"])).toBe(true);
  });

  it("offers linking beneath the empty-state description when tracking is available", async () => {
    const user = userEvent.setup();
    state.info!.data!.pr = null;
    state.info!.data!.prs = [];
    state.info!.data!.tracking_available = true;
    renderPanel();
    const description = screen.getByText(/Pull requests created in this session appear here/);
    const link = screen.getByRole("button", { name: "Link a PR" });
    expect(description.parentElement).toContainElement(link);
    expect(screen.queryByRole("combobox", { name: "Session pull request" })).toBeNull();
    await user.click(link);
    const input = await findLinkPicker();
    await waitFor(() => expect(input).toHaveFocus());
    await user.type(input, "#7");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(queryLinkPicker()).toBeNull());
    // Closing without linking keeps the draft for next time.
    await user.click(screen.getByRole("button", { name: "Link a PR" }));
    expect(await findLinkPicker()).toHaveValue("#7");
  });

  describe("Link a PR follows the same access rule as ⌘K", () => {
    const linkedPr = "https://github.com/example/one/pull/42";

    function withLinkedPr() {
      state.info!.data!.tracking_available = true;
      state.info!.data!.selected_pr_url = linkedPr;
      state.info!.data!.prs = [
        {
          url: linkedPr,
          host: "github.com",
          repository: "example/one",
          number: 42,
          title: "First repository",
          relationship: "created",
        },
      ];
    }

    function withoutPrs() {
      state.info!.data!.pr = null;
      state.info!.data!.prs = [];
      state.info!.data!.tracking_available = true;
    }

    it.each([
      ["read-only access", 1, LINK_PULL_REQUEST_NEEDS_EDIT],
      ["unconfirmed access", null, CHECKING_ACCESS_REASON],
    ])("disables the header button with %s", async (_scenario, access, reason) => {
      const user = userEvent.setup();
      state.access = access;
      withLinkedPr();
      renderPanel();
      const button = screen.getByRole("button", { name: `Link a PR: ${reason}` });
      expect(button).toBeDisabled();
      await user.click(button);
      expect(queryLinkPicker()).toBeNull();
    });

    it.each([
      ["read-only access", 1, LINK_PULL_REQUEST_NEEDS_EDIT],
      ["unconfirmed access", null, CHECKING_ACCESS_REASON],
    ])(
      "explains %s in the empty state instead of offering the button",
      (_scenario, access, reason) => {
        state.access = access;
        withoutPrs();
        renderPanel();
        expect(screen.getByRole("status")).toHaveTextContent(reason);
        expect(screen.queryByRole("button", { name: /^Link a PR/ })).toBeNull();
      },
    );

    it("reads the panel's own session even when the routed session differs", async () => {
      const user = userEvent.setup();
      state.access = 2;
      withLinkedPr();
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      client.setQueryData(["session", "conv_1"], sessionSnapshot("conv_1", 2));
      // A grid route: the shell's context is open on another session the viewer can only read.
      render(
        <QueryClientProvider client={client}>
          <CommandContextProvider
            session={{
              id: "conv_routed",
              permissionLevel: 1,
              permissionResolved: true,
              topLevel: true,
              archived: false,
            }}
            view="session"
            workspaceTab="github"
            filePath={null}
          >
            <PullRequestPanel conversationId="conv_1" />
            <AttachPullRequestDialog />
          </CommandContextProvider>
        </QueryClientProvider>,
      );
      await user.click(screen.getByRole("button", { name: "Link a PR" }));
      expect(await findLinkPicker()).toBeVisible();
    });

    it.each([
      ["header", withLinkedPr],
      ["empty state", withoutPrs],
    ])("opens the picker from the %s with edit access", async (_entry, setup) => {
      const user = userEvent.setup();
      state.access = 2;
      setup();
      renderPanel();
      await user.click(screen.getByRole("button", { name: "Link a PR" }));
      expect(await findLinkPicker()).toBeVisible();
    });
  });
});

describe("on the new pull request page", () => {
  beforeEach(() => pageV2.set(true));

  it("keeps loaded details when a read behind them fails, and says so above them", async () => {
    const read = state.info!;
    state.info = { ...read, error: new Error("GitHub didn't answer in time.") } as never;
    renderPanel();
    expect(await screen.findByRole("tab", { name: "Overview" })).toBeVisible();
    expect(
      screen.getByText(/^Couldn’t update this pull request: GitHub didn't answer in time\./),
    ).toBeVisible();
    // With nothing loaded, the failure is the whole state, as before.
    cleanup();
    state.info = {
      ...read,
      data: undefined,
      error: new Error("GitHub didn't answer in time."),
    } as never;
    renderPanel();
    expect(screen.queryByRole("tab", { name: "Overview" })).toBeNull();
  });

  it("dates the PR's stack by when its details were read, not by a change patched in since", () => {
    // Read at 1 s; a comment patched in at six minutes moved the cache's update time only.
    const read = state.info!;
    state.info = {
      ...read,
      data: { ...read.data, dispatched_at: 1_000 },
      dataUpdatedAt: 361_000,
    } as never;
    renderPanel();
    const options = vi.mocked(usePullRequestStackRead).mock.lastCall?.[2];
    expect(options?.detailsReadAt).toBe(1_000);
  });

  describe("Changes states", () => {
    // The new page's Changes tab carries a count, so it's opened by its name's start.
    const openChanges = () => {
      const view = renderPanel();
      fireEvent.mouseDown(screen.getByRole("tab", { name: /^Changes/ }));
      return view;
    };

    /** The new page's states fill the pane alone: no file list beside them. */
    const expectNoFileList = () => {
      expect(screen.queryByLabelText("Resize file list")).toBeNull();
      expect(screen.queryByRole("button", { name: /file list$/ })).toBeNull();
    };

    it("titles a failed file list, says why, and retries both reads", async () => {
      const refetchFiles = vi.fn();
      state.changes = {
        ...state.changes!,
        data: undefined,
        error: new Error("Could not load changed files."),
        refetch: refetchFiles,
      } as never;
      openChanges();
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("Changes Unavailable");
      expect(alert).toHaveTextContent("Could not load changed files.");
      // The reason is said once, in the message, with no file list column repeating it.
      expect(screen.getAllByText("Could not load changed files.")).toHaveLength(1);
      expectNoFileList();
      expect(screen.getByRole("tabpanel", { name: /^Changes/ })).toContainElement(alert);
      await userEvent.click(screen.getByRole("button", { name: "Try Again" }));
      expect(refetchFiles).toHaveBeenCalled();
      expect(state.refetchDiff).toHaveBeenCalled();
    });

    it("keeps loaded changes when a read behind them fails, and says so", async () => {
      state.diffRefetchError = new Error("GitHub didn't answer in time.");
      openChanges();
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(
        "Couldn’t update the changes: GitHub didn't answer in time. Showing what was loaded before.",
      );
      expect(await screen.findByTestId("shared-review")).toBeInTheDocument();
      expect((sharedReview.props!.files as { path: string }[])[0].path).toBe("hello.py");
      expect(screen.queryByText("Changes Unavailable")).toBeNull();
    });

    it("keeps a loaded file list when its own read behind it fails", async () => {
      state.changes = { ...state.changes!, error: new Error("Could not load changed files.") };
      openChanges();
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Couldn’t update the changes: Could not load changed files.",
      );
      expect(await screen.findByTestId("shared-review")).toBeInTheDocument();
      expect((sharedReview.props!.files as { path: string }[])[0].path).toBe("hello.py");
    });

    it("says a pull request with no changed files has no changes", async () => {
      state.changes!.data = { available: true, data: [] } as never;
      state.parsedFiles = [];
      openChanges();
      expect(await screen.findByText("No Changes")).toBeVisible();
      expect(screen.getByText("There are no changes in this pull request.")).toBeVisible();
      expectNoFileList();
    });

    it("says changed files are unavailable when the list came back partial", async () => {
      state.changes!.data = { available: true, data: [], has_more: true } as never;
      state.parsedFiles = [];
      openChanges();
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("Changes Unavailable");
      expect(alert).toHaveTextContent("Changed files are unavailable.");
    });

    describe("on the shared review, before the patch arrives", () => {
      it("lists the changed files with their diffs loading, then hands over the patch", async () => {
        state.diffLoading = true;
        const view = openChanges();
        expect(await screen.findByTestId("shared-review")).toBeInTheDocument();
        expect(screen.queryByText("Loading changes…")).toBeNull();
        expect(sharedReview.props).toMatchObject({ patch: { status: "loading" } });
        expect(
          (sharedReview.props!.files as { path: string }[]).map((listed) => listed.path),
        ).toEqual(["hello.py", "src/app.ts"]);
        state.diffLoading = false;
        view.rerender(<PullRequestPanel conversationId="conv_1" />);
        await waitFor(() =>
          expect(sharedReview.props).toMatchObject({ patch: { status: "ready" } }),
        );
        expect((sharedReview.props!.filesByPath as Map<string, unknown>).size).toBe(2);
      });

      it("keeps the files when the patch fails, and retries only the patch", async () => {
        const refetchFiles = vi.fn();
        state.changes = { ...state.changes!, refetch: refetchFiles } as never;
        state.diffError = new Error("GitHub didn't answer in time.");
        openChanges();
        expect(await screen.findByTestId("shared-review")).toBeInTheDocument();
        expect(screen.queryByRole("alert")).toBeNull();
        expect(sharedReview.props).toMatchObject({
          patch: { status: "failed", message: "GitHub didn't answer in time." },
        });
        act(() => (sharedReview.props!.onRetryPatch as () => void)());
        expect(state.refetchDiff).toHaveBeenCalledTimes(1);
        expect(refetchFiles).not.toHaveBeenCalled();
      });

      it("still says the changes are unavailable when the patch can't be read as a diff", async () => {
        const parse = vi.mocked((await import("@pierre/diffs")).parsePatchFiles);
        parse.mockImplementationOnce(() => {
          throw new Error("bad patch");
        });
        openChanges();
        expect(await screen.findByRole("alert")).toHaveTextContent("Changes Unavailable");
        expect(screen.queryByTestId("shared-review")).toBeNull();
      });
    });

    it("switches between the classic and new states as the design changes, while one is showing", async () => {
      state.changes!.data = undefined;
      state.changes!.error = new Error("Could not load changed files.");
      openChanges();
      expect(await screen.findByRole("alert")).toHaveTextContent("Changes Unavailable");
      act(() => pageV2.set(false));
      expect(screen.getByRole("alert")).toHaveTextContent("Could not load changed files.");
      expect(screen.getByRole("alert")).not.toHaveTextContent("Changes Unavailable");
      // The classic page keeps its file list beside the message.
      expect(screen.getByLabelText("Resize file list")).toBeInTheDocument();
      act(() => pageV2.set(true));
      expect(screen.getByRole("alert")).toHaveTextContent("Changes Unavailable");
      expectNoFileList();
    });
  });

  /** Answer the session's review-thread read with one resolved, outdated thread, on `path`. */
  function serveReviewThreads(path = "devtools/server/src/edge.rs") {
    state.info!.data!.capabilities = {
      ...state.info!.data!.capabilities,
      review_threads: true,
    } as never;
    state.info!.data!.selected_pr_url = state.info!.data!.pr!.url;
    const page = {
      data: [
        {
          id: "thread-1",
          path,
          side: "RIGHT",
          line: null,
          start_line: null,
          original_line: 273,
          original_start_line: null,
          subject_type: "LINE",
          resolved: true,
          outdated: true,
          can_reply: true,
          diff_hunk: "@@ -1,1 +1,2 @@\n line\n+let _ = connections.send((tls, addr)).await;",
          comments: {
            data: [
              {
                id: "c-1",
                author: "reviewer",
                body: "Keep it off the loop",
                created_at: null,
                url: null,
              },
            ],
            has_more: false,
            next_cursor: null,
          },
        },
      ],
      has_more: false,
      next_cursor: null,
      head_sha: state.info!.data!.pr?.head_sha ?? "",
      base_sha: state.info!.data!.pr?.base_sha ?? "",
    };
    const reads: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/resources/github/review-threads")) {
          reads.push(url);
          return new Response(JSON.stringify(page));
        }
        return new Response("{}", { status: 404 });
      }),
    );
    return reads;
  }

  it("lists review threads in the Overview's Activity, resolved ones included", async () => {
    const reads = serveReviewThreads();
    renderPanel();
    const activity = screen.getByRole("region", { name: "Activity" });
    const fold = await within(activity).findByRole("button", { name: /Resolved conversation/ });
    expect(fold).toHaveAttribute("aria-expanded", "false");
    expect(
      within(activity).getByRole("button", { name: /^Threads/, pressed: false }),
    ).toHaveTextContent("Threads1");
    expect(within(activity).getByText("Line 273")).toBeVisible();
    expect(reads.length).toBeGreaterThan(0);
  });

  it("starts a conversation on the latest push's lines", async () => {
    pageV2.set(true);
    serveReviewThreads("src/app.ts");
    const pr = state.info!.data!.pr!;
    Object.assign(pr, { head_sha: "h".repeat(40), base_sha: "b".repeat(40) });
    Object.assign(state.info!.data!.capabilities!, { review_thread_creation: true });
    state.diff = { ...state.diff!, head_sha: pr.head_sha, base_sha: pr.base_sha };
    const openChanges = async () => {
      renderPanel();
      fireEvent.mouseDown(await screen.findByRole("tab", { name: /^Changes/ }));
      await screen.findByTestId("shared-review");
    };
    await openChanges();
    await waitFor(() =>
      expect((sharedReview.props!.context as ReviewThreadContext).canCreate).toBe(true),
    );
    expect(sharedReview.props!.outdated).toBeUndefined();
  });

  it("asks the shared review to bring a conversation shown from Activity into view", async () => {
    pageV2.set(true);
    serveReviewThreads("src/app.ts");
    renderPanel();
    const activity = screen.getByRole("region", { name: "Activity" });
    fireEvent.click(
      await within(activity).findByRole("button", {
        name: "Show src/app.ts, Line 273, in Changes",
      }),
    );
    expect(await screen.findByTestId("shared-review")).toBeInTheDocument();
    expect(sharedReview.props?.focusedThread).toEqual({ id: "thread-1" });
  });

  it("says a conversation's file is no longer changed, and stays on Overview", async () => {
    const info = vi.spyOn(toast, "info");
    serveReviewThreads("devtools/server/src/edge.rs");
    renderPanel();
    const activity = screen.getByRole("region", { name: "Activity" });
    fireEvent.click(
      await within(activity).findByRole("button", {
        name: /^Show devtools\/server\/src\/edge\.rs/,
      }),
    );
    expect(info).toHaveBeenCalledWith(
      "devtools/server/src/edge.rs isn’t in this pull request’s changes anymore",
      { description: "It may have been renamed, or its changes undone." },
    );
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    info.mockRestore();
  });

  it("follows a rename the changed files list before the patch arrives", async () => {
    const info = vi.spyOn(toast, "info");
    onTestFinished(() => info.mockRestore());
    state.changes!.data = {
      available: true,
      data: [{ ...file("src/app.ts", "renamed"), previous_path: "src/old.ts" }],
    };
    state.diffLoading = true;
    serveReviewThreads("src/old.ts");
    renderPanel();
    const activity = screen.getByRole("region", { name: "Activity" });
    fireEvent.click(await within(activity).findByRole("button", { name: /^Show src\/old\.ts/ }));
    expect(info).not.toHaveBeenCalled();
    expect(screen.getByRole("tab", { name: /^Changes/ })).toHaveAttribute("aria-selected", "true");
  });

  it("opens Changes rather than calling a file gone while the changed files can't be read", async () => {
    const info = vi.spyOn(toast, "info");
    onTestFinished(() => info.mockRestore());
    const showInChanges = async () =>
      fireEvent.click(
        await within(screen.getByRole("region", { name: "Activity" })).findByRole("button", {
          name: /^Show src\/app\.ts/,
        }),
      );
    state.changes!.data = { available: false, data: [] };
    serveReviewThreads("src/app.ts");
    const { unmount } = renderPanel();
    await showInChanges();
    expect(info).not.toHaveBeenCalled();
    expect(screen.getByRole("tab", { name: /^Changes/ })).toHaveAttribute("aria-selected", "true");
    unmount();

    // A list that was read and is empty does prove the file gone.
    state.changes!.data = { available: true, data: [] };
    renderPanel();
    await showInChanges();
    expect(info).toHaveBeenCalledWith(
      "src/app.ts isn’t in this pull request’s changes anymore",
      expect.anything(),
    );
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  });

  it("has no Comments tab: the classic page's Comments switches to Overview's Activity as the flag turns on", async () => {
    pageV2.set(false);
    const reads = serveReviewThreads();
    renderComments();
    const settle = () =>
      act(
        () =>
          new Promise<void>((resolve) => {
            setTimeout(resolve, 50);
          }),
      );
    await settle();
    expect(reads).toEqual([]);

    act(() => pageV2.set(true));
    expect(await screen.findByRole("button", { name: /Resolved conversation/ })).toBeVisible();
    expect(screen.queryByRole("tab", { name: /^Comments/ })).toBeNull();
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(screen.getByRole("region", { name: "Activity" })).toHaveFocus());
    expect(reads.length).toBeGreaterThan(0);
    const readsWhileOn = reads.length;

    // Back on the classic page, its Summary reads no threads.
    act(() => pageV2.set(false));
    expect(screen.queryByRole("region", { name: "Activity" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Summary" })).toHaveAttribute("aria-selected", "true");
    await settle();
    expect(reads).toHaveLength(readsWhileOn);
  });

  it("reads review threads on the new page's Overview, for its activity, and not the classic one's", async () => {
    const settle = () =>
      act(
        () =>
          new Promise<void>((resolve) => {
            setTimeout(resolve, 50);
          }),
      );
    pageV2.set(false);
    const classic = serveReviewThreads();
    renderPanel();
    await settle();
    expect(classic).toEqual([]);
    cleanup();

    pageV2.set(true);
    const reads = serveReviewThreads();
    renderPanel();
    expect(await screen.findByRole("tab", { name: "Overview" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await waitFor(() => expect(reads.length).toBeGreaterThan(0));
  });

  it("reads no review threads on the classic page's Comments", async () => {
    pageV2.set(false);
    const reads = serveReviewThreads();
    renderComments();
    await act(
      () =>
        new Promise<void>((resolve) => {
          setTimeout(resolve, 50);
        }),
    );
    expect(reads).toEqual([]);
    expect(screen.queryByRole("navigation", { name: "Views" })).toBeNull();
  });

  it("leads with the repository, state and section tabs, and an Overview led by the title", () => {
    renderPanel();

    expect(screen.getByRole("link", { name: /^Open acme\/app #6000 on / })).toBeVisible();
    expect(screen.getByLabelText("Pull request status: Open")).toBeVisible();
    const overview = screen.getByRole("tab", { name: "Overview" });
    expect(overview).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("tab", { name: "Summary" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Changes 2" })).toBeVisible();
    expect(screen.getByRole("heading", { level: 1, name: "chore: dummy PR" })).toBeVisible();
    expect(screen.getByText("Opened by dev")).toBeVisible();
    expect(screen.getByText("2 files")).toBeVisible();
    expect(screen.getByText("+4")).toBeVisible();
    expect(screen.getByText("−1")).toBeVisible();
    expect(screen.getByText("e2e")).toBeInTheDocument();
  });

  it("arranges the Overview's sections in the reference's order, with labelled edits", () => {
    state.info!.data!.selected_pr_url = "https://github.com/acme/app/pull/6000";
    state.info!.data!.capabilities = {
      account_switching: true,
      base_remote_selection: true,
      line_counts: true,
      linked_pr_diff: true,
      details_editing: true,
    };
    state.info!.data!.pr = { ...state.info!.data!.pr!, labels: [], reviewers: [] };
    renderPanel();

    const panel = screen.getByRole("tabpanel");
    const headings = within(panel)
      .getAllByRole("heading")
      .map((heading) => heading.textContent);
    expect(headings).toEqual([
      "chore: dummy PR",
      "Labels",
      "Reviewers",
      "Description",
      "Checks",
      "Activity",
    ]);
    expect(within(panel).getByRole("button", { name: "Edit description" })).toHaveTextContent(
      "Edit",
    );
    expect(within(panel).getByRole("button", { name: "Edit title" })).toHaveTextContent("Edit");
  });

  describe("activity", () => {
    const prUrl = "https://github.com/acme/app/pull/6000";
    /** The comment box's Markdown textarea. */
    async function commentBox(
      container: HTMLElement = document.body,
    ): Promise<HTMLTextAreaElement> {
      // The box starts folded to one line; open it the way a person would.
      const folded = within(container).queryByRole("button", { name: "Add a comment…" });
      if (folded) fireEvent.click(folded);
      return (await within(container).findByRole("textbox", {
        name: "New PR comment",
      })) as HTMLTextAreaElement;
    }

    function typeAtEnd(textarea: HTMLTextAreaElement, text: string): void {
      act(() => textarea.focus());
      fireEvent.change(textarea, { target: { value: textarea.value + text } });
    }
    const posted: PullRequestComment = {
      author: "alice",
      body: "Thanks, **merging** after CI.",
      created_at: "2026-11-01T12:00:00Z",
      url: `${prUrl}#issuecomment-3`,
    };
    beforeEach(() => {
      state.info!.data!.selected_pr_url = prUrl;
      state.info!.data!.capabilities = {
        account_switching: true,
        base_remote_selection: true,
        line_counts: true,
        linked_pr_diff: true,
        comment_posting: true,
      };
      state.info!.data!.pr = {
        ...state.info!.data!.pr!,
        url: prUrl,
        comments: [
          {
            author: "ada",
            body: "**Why** not reuse the helper?\nIt reads the same file.",
            created_at: "2026-11-01T09:00:00Z",
            url: `${prUrl}#issuecomment-1`,
          },
          {
            author: "grace",
            body: "It predates the helper.",
            created_at: "2026-11-01T11:00:00Z",
            url: `${prUrl}#issuecomment-2`,
          },
        ],
        timeline_events: [
          {
            id: "commit-1",
            kind: "commit",
            author: "dev",
            created_at: "2026-11-01T10:00:00Z",
            url: null,
            sha: "abcdef1234567",
            message: "Reuse the helper\n\nWith a longer body",
          },
        ],
      };
    });
    const activity = () => screen.getByRole("region", { name: "Activity" });
    const activityRows = () =>
      within(screen.getByRole("list", { name: "Activity" }))
        .getAllByRole("listitem")
        .map((row) => row.firstElementChild!.getAttribute("aria-label"));

    it("lists the comments and pushes already read, newest first", () => {
      renderPanel();

      expect(activityRows()).toEqual([
        "Comment by grace",
        expect.stringMatching(/^dev pushed abcdef1 /),
        "Comment by ada",
      ]);
      expect(
        within(activity()).getByRole("button", { name: /^All/, pressed: true }),
      ).toHaveTextContent("All3+");
    });

    it("lists them oldest first once Oldest First is chosen for the conversation", () => {
      commentsOrderPreference.write("oldest");
      renderPanel();

      expect(activityRows()).toEqual([
        "Comment by ada",
        expect.stringMatching(/^dev pushed /),
        "Comment by grace",
      ]);
    });

    it("shows a push it can't open as a plain row, where there's no Commits tab", () => {
      renderPanel();

      expect(screen.queryByRole("tab", { name: /^Commits/ })).toBeNull();
      expect(within(activity()).queryByRole("button", { name: /^dev pushed/ })).toBeNull();
      expect(within(activity()).getByRole("group", { name: /^dev pushed abcdef1/ })).toBeVisible();
    });

    it.each([
      ["every comment and push was read", {}, "No activity yet"],
      // A read with no push history, such as this Mac's, can't say there are no commits.
      ["no push history was read", { timeline_events: undefined }, "No comments yet"],
      ["some push history is missing", { timeline_partial: true }, "No comments yet"],
      ["the PR has comments that weren't read", { comments_partial: true }, "No activity loaded"],
    ] as const)(
      "says only what it knows when it's empty and %s",
      (_, over: Partial<PullRequest>, text) => {
        state.info!.data!.pr = {
          ...state.info!.data!.pr!,
          comments: [],
          timeline_events: [],
          ...over,
        };
        renderPanel();

        expect(within(activity()).getByText(text)).toBeVisible();
      },
    );

    it("pins the comment box under the Overview, outside the contents that scroll", async () => {
      renderPanel();

      const overview = screen.getByRole("tabpanel", { name: "Overview" });
      const box = await commentBox(overview);
      const contents = overview.firstElementChild!;
      expect(contents).toHaveClass("overflow-y-auto");
      expect(contents.contains(box)).toBe(false);
      expect(contents).toContainElement(activity());
    });

    it("leaves the comment box out where the PR can't be commented on", () => {
      state.canEdit = false;
      renderPanel();

      expect(screen.queryByRole("textbox", { name: "New PR comment" })).toBeNull();
      expect(activity()).toBeVisible();
    });

    it("shows a comment posted from the Overview in place, focused and highlighted, whatever was filtered", async () => {
      const respond: ((response: Response) => void)[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn((input: RequestInfo | URL) =>
          new URL(String(input), "http://localhost").pathname.endsWith("/github/comments")
            ? new Promise<Response>((resolve) => {
                respond.push(resolve);
              })
            : Promise.resolve(new Response("{}", { status: 404 })),
        ),
      );
      renderPanel();
      fireEvent.click(within(activity()).getByRole("button", { name: /^Pushes/ }));
      expect(activityRows()).toEqual([expect.stringMatching(/^dev pushed /)]);
      typeAtEnd(await commentBox(), "Thanks, **merging** after CI.");
      const post = screen.getByRole("button", { name: "Comment" });
      await waitFor(() => expect(post).toBeEnabled());
      fireEvent.click(post);
      await waitFor(() => expect(respond).toHaveLength(1));
      // The PR's next read includes it, as the posted comment's cache update does.
      state.info!.data!.pr!.comments = [...state.info!.data!.pr!.comments!, posted];
      await act(async () => respond[0]!(new Response(JSON.stringify({ comment: posted }))));

      const comment = await within(activity()).findByRole("article", { name: "Comment by alice" });
      const row = comment.closest("li")!;
      await waitFor(() => expect(row).toHaveFocus());
      expect(row).toHaveClass("animate-message-highlight");
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "center" });
      expect(within(activity()).getByRole("button", { name: /^All/ })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      await waitFor(() => expect(row).not.toHaveClass("animate-message-highlight"), {
        timeout: 3000,
      });
      // The posted comment's composer starts the next one empty.
      await waitFor(async () => expect((await commentBox()).value).toBe(""));
    });
  });

  describe("merge controls", () => {
    beforeEach(() => {
      state.info!.data!.selected_pr_url = "https://github.com/acme/app/pull/6000";
      state.info!.data!.pr!.head_sha = "a".repeat(40);
      state.info!.data!.pr!.merge_state_status = "CLEAN";
      state.info!.data!.pr!.auto_merge = null;
      state.info!.data!.capabilities = {
        account_switching: true,
        base_remote_selection: true,
        line_counts: true,
        linked_pr_diff: true,
        merging: true,
      };
    });

    it("close the checks card, say it's ready, and still confirm before merging", () => {
      renderPanel();

      const checks = screen.getByRole("region", { name: "Checks" });
      const merge = within(checks).getByRole("region", { name: "Merge" });
      expect(within(checks).getByText("Ready to merge")).toBeVisible();
      expect(within(checks).getByText("Merges into main")).toBeVisible();
      fireEvent.click(within(merge).getByRole("button", { name: "Merge pull request" }));
      expect(screen.getByText("Merge pull request #6000?")).toBeInTheDocument();
    });

    it("leave out the ready caption while the PR can't merge, and keep the reason", () => {
      state.info!.data!.pr!.merge_state_status = "BLOCKED";
      renderPanel();

      const checks = screen.getByRole("region", { name: "Checks" });
      expect(within(checks).queryByText("Ready to merge")).toBeNull();
      expect(within(checks).getByRole("button", { name: "Merge pull request" })).toBeDisabled();
    });

    it("stay out of the card where the PR can't be merged from here", () => {
      state.canEdit = false;
      renderPanel();

      expect(screen.queryByRole("region", { name: "Merge" })).toBeNull();
      expect(screen.queryByText("Ready to merge")).toBeNull();
    });
  });

  it.each([
    ["a link to Comments", undefined],
    [
      "a workspace tab saved on Comments",
      { kind: "pull" as const, onShowAllPullRequests: vi.fn(), onOpenInNewTab: vi.fn() },
    ],
  ])("opens Overview's Activity for %s, in that place's history entry", async (_name, tabsMode) => {
    state.info!.data!.selected_pr_url = "https://github.com/acme/app/pull/6000";
    const { reports } = renderControlled(
      {
        selected: "https://github.com/acme/app/pull/6000",
        showingDetails: true,
        subtab: "comments",
      },
      tabsMode,
    );
    const activity = await screen.findByRole("region", { name: "Activity" });
    expect(screen.queryByRole("tab", { name: /^Comments/ })).toBeNull();
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    expect(reports).toContainEqual({
      view: expect.objectContaining({ subtab: "summary" }),
      replace: true,
    });
    await waitFor(() => expect(activity).toHaveFocus());
    expect(within(activity).getByRole("button", { name: /^All/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("pairs each tab with the details' own panel", () => {
    renderPanel();
    for (const name of ["Overview", /^Changes/]) {
      const tab = screen.getByRole("tab", { name });
      fireEvent.mouseDown(tab);
      const panel = document.getElementById(tab.getAttribute("aria-controls")!);
      expect(panel).toHaveAttribute("role", "tabpanel");
      expect(panel).toHaveAttribute("aria-labelledby", tab.id);
    }
    // One tab list: the layout's triggers are the details' only ones.
    expect(screen.getAllByRole("tablist")).toHaveLength(1);
  });

  it("shows nothing to choose about where it's read where only one source exists", () => {
    renderPanel();
    expect(screen.getByRole("heading", { level: 1 })).toBeVisible();
    expect(screen.queryByRole("button", { name: /^Read from / })).toBeNull();
    expect(paletteGroup("pull-request-read-source")?.commands ?? []).toEqual([]);
  });

  it("shows the new page in the phone drawer too, under the drawer's one heading", () => {
    renderPanel(true);
    expect(screen.getByRole("tab", { name: "Overview" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Summary" })).toBeNull();
    // The drawer's header names the panel; the page adds no second "Pull Requests" heading.
    expect(screen.getAllByRole("heading", { name: "Pull Requests" })).toHaveLength(1);
  });

  it("returns focus to the opened pull request's row when the phone drawer goes back", async () => {
    const user = userEvent.setup();
    state.storesPrs = false;
    const url = state.info!.data!.pr!.url;
    Object.assign(state.info!.data!, {
      tracking_available: true,
      selected_pr_url: url,
      prs: [
        { url, host: "github.com", repository: "acme/app", number: 6000, relationship: "attached" },
        OTHER_PR,
      ],
    });
    renderPanel(true);
    // A click makes the row active; Enter opens it, as from the keyboard.
    await user.click(await screen.findByRole("row", { name: /^acme\/app #6001/ }));
    await user.keyboard("{Enter}");
    const back = await screen.findByRole("button", { name: "All pull requests (2)" });
    await user.click(back);
    const grid = await screen.findByRole("grid");
    await waitFor(() => expect(grid).toHaveFocus());
    const active = document.getElementById(grid.getAttribute("aria-activedescendant") ?? "");
    expect(active?.textContent).toContain("#6001");
  });

  describe("a chat link's pull request that isn't linked to the session", () => {
    const linkedUrl = "https://github.com/acme/app/pull/6000";
    const requested = "https://github.com/acme/app/pull/7000";
    const linkedPr = (number: number): PullRequestAssociation => ({
      url: `https://github.com/acme/app/pull/${number}`,
      host: "github.com",
      repository: "acme/app",
      number,
      relationship: "attached",
    });
    function openRequestedInDrawer({ linked = [6000, 6001] }: { linked?: number[] } = {}) {
      state.info!.data!.tracking_available = true;
      state.stored.conv_1 = linked.map(linkedPr);
      let release = () => {};
      state.storedGate = new Promise((resolve) => {
        release = resolve;
      });
      const { client } = renderPanel(true, true, (queryClient) =>
        showPullRequestInPanel(queryClient, "conv_1", requested),
      );
      const shownView = () =>
        client.getQueryData<PullRequestPanelView>(pullRequestPageViewKey("conv_1"));
      return { release, shownView };
    }

    it("stays in the phone drawer under Tabs once the session's list loads, offering Link it", async () => {
      const { release, shownView } = openRequestedInDrawer();
      expect(shownView()).toMatchObject({ selected: requested, showingDetails: true });
      await act(async () => release());
      expect(
        await screen.findByText("This pull request isn't linked to this session"),
      ).toBeVisible();
      expect(shownView()).toMatchObject({ selected: requested, showingDetails: true });
      fireEvent.click(screen.getByRole("button", { name: "Link it" }));
      await waitFor(() =>
        expect(state.updateSessionPr).toHaveBeenCalledWith({ url: requested, action: "attach" }),
      );
      expect(usePullRequestAttachStore.getState().openSessionId).toBeNull();

      // Back shows the session's own list, with focus on it, and the notice goes.
      fireEvent.click(screen.getByRole("button", { name: "All pull requests (2)" }));
      const grid = await screen.findByRole("grid");
      expect(screen.getByRole("row", { name: /^acme\/app #6001/ })).toBeInTheDocument();
      expect(screen.queryByText("This pull request isn't linked to this session")).toBeNull();
      await waitFor(() => expect(grid).toHaveFocus());
    });

    it("goes back to the session's one pull request from an unlinked one in the phone drawer", async () => {
      const { release, shownView } = openRequestedInDrawer({ linked: [6000] });
      await act(async () => release());
      await screen.findByText("This pull request isn't linked to this session");
      fireEvent.click(screen.getByRole("button", { name: "All pull requests (1)" }));
      await waitFor(() =>
        expect(shownView()).toMatchObject({ selected: linkedUrl, showingDetails: true }),
      );
      expect(screen.queryByText("This pull request isn't linked to this session")).toBeNull();
    });

    it("offers Link it in the phone drawer under Tabs when the session links none", async () => {
      const { release, shownView } = openRequestedInDrawer({ linked: [] });
      await act(async () => release());
      expect(
        await screen.findByText("This pull request isn't linked to this session"),
      ).toBeVisible();
      expect(screen.getByRole("button", { name: "Link it" })).toBeVisible();
      expect(shownView()).toMatchObject({ selected: requested, showingDetails: true });
      expect(usePullRequestAttachStore.getState().openSessionId).toBeNull();
    });

    it("gives way to the session's own pull requests under Classic, as before", async () => {
      pageV2.set(false);
      const { release, shownView } = openRequestedInDrawer();
      await act(async () => release());
      await waitFor(() => expect(shownView()?.selected).not.toBe(requested));
      expect(screen.queryByText("This pull request isn't linked to this session")).toBeNull();
    });
  });

  it("keeps the classic layout in the phone drawer under Classic", () => {
    pageV2.set(false);
    renderPanel(true);
    expect(screen.getByRole("tab", { name: "Summary" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Overview" })).toBeNull();
  });

  it("switches when the design changes after the details have rendered, keeping the tab", async () => {
    pageV2.set(false);
    renderPanel();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Changes" }));

    act(() => pageV2.set(true));
    expect(screen.getByRole("tab", { name: /^Changes/ })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByTestId("shared-review")).toBeInTheDocument();

    act(() => pageV2.set(false));
    expect(screen.getByRole("tab", { name: "Changes" })).toHaveAttribute("aria-selected", "true");
  });

  it("keeps an open title edit when the page switches either way", () => {
    state.info!.data!.selected_pr_url = "https://github.com/acme/app/pull/6000";
    state.info!.data!.capabilities = {
      account_switching: true,
      base_remote_selection: true,
      line_counts: true,
      linked_pr_diff: true,
      details_editing: true,
    };
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: "Edit title" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Pull request title" }), {
      target: { value: "fix: a clearer title" },
    });
    act(() => pageV2.set(false));
    expect(screen.getByRole("textbox", { name: "Pull request title" })).toHaveValue(
      "fix: a clearer title",
    );
    act(() => pageV2.set(true));
    expect(screen.getByRole("textbox", { name: "Pull request title" })).toHaveValue(
      "fix: a clearer title",
    );
  });

  describe("on a session shared with the viewer", () => {
    const PR = "https://github.com/acme/app/pull/6000";

    function showShared() {
      state.info!.data!.selected_pr_url = PR;
      // The server turns off every change capability for anyone but the session's owner.
      state.info!.data!.capabilities = {
        account_switching: false,
        base_remote_selection: false,
        line_counts: true,
        linked_pr_diff: true,
        comment_posting: false,
      };
      renderPanel();
    }

    it("offers an editor no comment box", async () => {
      state.changeAccess = "not_owner";
      showShared();

      const notice = await screen.findByText(
        "Only the session’s owner can change this pull request here.",
      );
      expect(notice.closest("[role=status]")).toBeVisible();
      expect(screen.getByRole("link", { name: "Open on GitHub" })).toHaveAttribute("href", PR);
      expect(screen.queryByRole("button", { name: "Add a comment…" })).toBeNull();
    });
  });

  it("keeps a half-written comment when the page switches", async () => {
    state.info!.data!.selected_pr_url = "https://github.com/acme/app/pull/6000";
    state.info!.data!.capabilities = {
      account_switching: true,
      base_remote_selection: true,
      line_counts: true,
      linked_pr_diff: true,
      comment_posting: true,
    };
    renderPanel();
    // The new page writes comments in the Markdown composer, under its Overview.
    fireEvent.click(await screen.findByRole("button", { name: "Add a comment…" }));
    const box = await screen.findByRole("textbox", { name: "New PR comment" });
    fireEvent.change(box, { target: { value: "Looks good once CI passes" } });

    act(() => pageV2.set(false));
    fireEvent.mouseDown(screen.getByRole("tab", { name: /^Comments/ }));
    expect(screen.getByPlaceholderText("Leave a comment...")).toHaveValue(
      "Looks good once CI passes",
    );
  });
});

describe("a PR's patch read", () => {
  it("waits for the overview", () => {
    const url = "https://github.com/acme/app/pull/6000";
    state.info = { data: undefined, isLoading: true, error: null, isFetching: true };
    renderControlled({ selected: url, showingDetails: true, subtab: "summary" });
    expect(state.diffReads.at(-1)?.[0]).toBe(false);
  });
});

describe("the page's secondary reads", () => {
  it("wait behind the patch while Changes shows, and are asked for elsewhere", () => {
    renderPanel(false, true);
    expect(state.commitsPriority).toBe("interactive");
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Changes" }));
    expect(state.commitsPriority).toBe("prefetch");
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Summary" }));
    expect(state.commitsPriority).toBe("interactive");
  });
});

describe("Pull Requests load timing", () => {
  it("times a cold open through automatic selection and the diff as one interval", () => {
    const send = vi.fn();
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      reportPullRequestTiming: send,
    };
    onTestFinished(() => {
      delete (window as unknown as Record<string, unknown>).omnigentDesktop;
    });
    let now = 1000;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    const url = "https://github.com/acme/app/pull/6000";
    const ready = state.info!.data!;
    Object.assign(ready, {
      tracking_available: true,
      selected_pr_url: url,
      prs: [
        { url, host: "github.com", repository: "acme/app", number: 6000, relationship: "inferred" },
      ],
    });
    ready.pr!.url = url;
    const diff = state.diff;
    state.info = { data: undefined, isLoading: true, error: null, isFetching: true };
    // The real query has no data while the diff loads.
    state.diff = undefined as never;

    const { rerender } = renderPanel();
    // Discovery answers and the panel selects the branch PR, remounting the PR view.
    now = 1500;
    state.info = { data: ready, isLoading: false, error: null, isFetching: false };
    rerender(<PullRequestPanel conversationId="conv_1" active />);
    expect(screen.getByRole("heading", { name: "chore: dummy PR" })).toBeInTheDocument();
    expect(send).not.toHaveBeenCalled();
    now = 2200;
    state.diff = diff;
    rerender(<PullRequestPanel conversationId="conv_1" active />);

    expect(send).toHaveBeenCalledExactlyOnceWith({
      sessionId: "conv_1",
      path: "host",
      outcome: "ready",
      cached: false,
      stateMs: 500,
      headerMs: 500,
      diffMs: 1200,
    });
  });
  function installTimingShell() {
    const send = vi.fn();
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      reportPullRequestTiming: send,
    };
    onTestFinished(() => {
      delete (window as unknown as Record<string, unknown>).omnigentDesktop;
    });
    let now = 1000;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    return { send, at: (time: number) => (now = time) };
  }

  it("splits a cold diff's time into asking for the patch, reading it, and showing it in Changes", () => {
    const { send, at } = installTimingShell();
    state.diffLoading = true;
    const { rerender } = renderPanel(false, true);
    // The user moves to Changes while the patch is still being read.
    at(1300);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Changes" }));
    expect(send).not.toHaveBeenCalled();
    at(2600);
    state.diffLoading = false;
    rerender(<PullRequestPanel conversationId="conv_1" active />);

    expect(send).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({
        outcome: "ready",
        patchRequestedMs: 0,
        changesShownMs: 300,
        diffMs: 1600,
        diffShownMs: 1600,
      }),
    );
  });

  it("reports at the patch's read when the user went back to Overview while it loaded", () => {
    const { send, at } = installTimingShell();
    state.diffLoading = true;
    const { rerender, unmount } = renderPanel(false, true);
    at(1300);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Changes" }));
    at(1500);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Summary" }));
    at(2600);
    state.diffLoading = false;
    rerender(<PullRequestPanel conversationId="conv_1" active />);

    expect(send).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ changesShownMs: 300, diffMs: 1600 }),
    );
    expect(send.mock.calls[0][0]).not.toHaveProperty("diffShownMs");
    unmount();
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("times a reopening of a hidden panel from what it already shows", () => {
    const { send, at } = installTimingShell();
    const { rerender } = renderPanel(false, true);
    expect(send).toHaveBeenCalledTimes(1);
    at(5000);
    rerender(<PullRequestPanel conversationId="conv_1" active={false} />);
    at(9000);
    rerender(<PullRequestPanel conversationId="conv_1" active />);
    expect(send).toHaveBeenCalledTimes(2);
    expect(send).toHaveBeenLastCalledWith({
      sessionId: "conv_1",
      path: "host",
      outcome: "ready",
      cached: true,
      stateMs: 0,
      headerMs: 0,
      diffMs: 0,
    });
  });

  it("times a reopening after the info and diff settled while the panel was hidden", () => {
    const { send, at } = installTimingShell();
    const ready = state.info!.data!;
    const diff = state.diff;
    state.info = { data: undefined, isLoading: true, error: null, isFetching: true };
    // The real query has no data while the diff loads.
    state.diff = undefined as never;
    const { rerender } = renderPanel(false, true);
    rerender(<PullRequestPanel conversationId="conv_1" active={false} />);
    // Nothing settled before the panel hid, so that opening reports nothing.
    expect(send).not.toHaveBeenCalled();
    state.info = { data: ready, isLoading: false, error: null, isFetching: false };
    state.diff = diff;
    rerender(<PullRequestPanel conversationId="conv_1" active={false} />);
    at(4000);
    rerender(<PullRequestPanel conversationId="conv_1" active />);
    expect(send).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({
        outcome: "ready",
        cached: true,
        stateMs: 0,
        headerMs: 0,
        diffMs: 0,
      }),
    );
  });
});

describe("derivePullRequestPanelState", () => {
  const ready: PullRequestInfo = {
    object: "session.github.info",
    available: true,
    gh_available: true,
    authenticated: true,
    branch: "feat/x",
    base_ref: "main",
    repo: { name_with_owner: "acme/app" },
    pr: {
      number: 1,
      title: "t",
      state: "OPEN",
      url: "u",
      is_draft: false,
      author: "a",
      base_ref: "main",
      head_ref: "feat/x",
      checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
    },
  };
  const q = (over: Partial<{ isLoading: boolean; error: unknown; data: PullRequestInfo }>) => ({
    isLoading: false,
    error: null as unknown,
    data: undefined as PullRequestInfo | undefined,
    ...over,
  });

  it("orders transient states ahead of data", () => {
    expect(derivePullRequestPanelState(q({ isLoading: true })).kind).toBe("loading");
    expect(derivePullRequestPanelState(q({ error: new RunnerOfflineError() })).kind).toBe(
      "runner-offline",
    );
    expect(derivePullRequestPanelState(q({ error: new Error("boom") })).kind).toBe("error");
  });

  it("keeps a host that doesn't answer apart from every answer it gives", () => {
    const gatewayError = Object.assign(new Error("502 Bad Gateway"), { status: 502 });
    expect(
      derivePullRequestPanelState({ ...q({ isLoading: true }), failureReason: gatewayError }).kind,
    ).toBe("host-reconnecting");
    expect(derivePullRequestPanelState(q({ error: gatewayError }))).toEqual({
      kind: "host-unreachable",
      returned: false,
    });
    expect(
      derivePullRequestPanelState({ ...q({ error: gatewayError }), hostReturned: true }),
    ).toEqual({ kind: "host-unreachable", returned: true });
    // Info loaded before the host stopped answering keeps showing.
    expect(derivePullRequestPanelState(q({ error: gatewayError, data: ready })).kind).toBe("ready");
    for (const status of [401, 403, 500]) {
      const answered = Object.assign(new Error(`${status}`), { status });
      expect(derivePullRequestPanelState(q({ error: answered })).kind, `${status}`).toBe("error");
    }
  });

  it("reads an unresolved repo right after the host returns as starting, but not a sign-in failure", () => {
    const signedInNoRepo: PullRequestInfo = {
      ...ready,
      provider: "github",
      repo: null,
      pr: null,
      auth: {
        authenticated: true,
        hint: null,
        cli: { name: "gh", available: true },
        accounts: [{ login: "o", host: "github.com", state: "success", active: true }],
        selected_account: "o",
      },
    };
    const timedOut: PullRequestInfo = {
      ...signedInNoRepo,
      auth: {
        ...signedInNoRepo.auth!,
        authenticated: false,
        accounts: [{ login: "o", host: "github.com", state: "timeout", active: true }],
      },
    };
    const signInFailed: PullRequestInfo = {
      ...signedInNoRepo,
      auth: {
        ...signedInNoRepo.auth!,
        authenticated: false,
        accounts: [{ login: "o", host: "github.com", state: "error", active: true }],
      },
    };
    const returned = (data: PullRequestInfo) =>
      derivePullRequestPanelState({ ...q({ data }), hostReturned: true }).kind;
    expect(returned(signedInNoRepo)).toBe("host-starting");
    expect(returned(timedOut)).toBe("host-starting");
    expect(returned(signInFailed)).toBe("repo-unresolved");
    expect(derivePullRequestPanelState(q({ data: signedInNoRepo })).kind).toBe("repo-unresolved");
  });

  it("reads an offline sandbox as sandbox-offline unless info was cached before it stopped", () => {
    const asleep = (over: Parameters<typeof q>[0]) => ({ ...q(over), sourceAsleep: true });
    expect(derivePullRequestPanelState(asleep({ isLoading: true })).kind).toBe("sandbox-offline");
    expect(derivePullRequestPanelState(asleep({})).kind).toBe("sandbox-offline");
    expect(derivePullRequestPanelState(asleep({ data: ready })).kind).toBe("ready");
    // The refetch an asleep sandbox can't answer keeps the cached info, not a "send a message" state.
    for (const error of [new RunnerOfflineError(), new Error("503")]) {
      expect(derivePullRequestPanelState(asleep({ data: ready, error })).kind).toBe("ready");
    }
  });

  it("keeps a PR this Mac reads itself on its own errors and loading while the sandbox sleeps", () => {
    // The caller passes sourceAsleep false for a Mac-read PR, so Resume never explains it.
    const local = (over: Parameters<typeof q>[0]) => ({ ...q(over), sourceAsleep: false });
    const certificate = new Error("certificate has expired");
    // Details loaded before keep showing, and say the update failed.
    expect(derivePullRequestPanelState(local({ data: ready, error: certificate }))).toEqual({
      kind: "ready",
      updateFailed: "certificate has expired",
    });
    expect(derivePullRequestPanelState(local({ error: certificate })).kind).toBe("error");
    expect(derivePullRequestPanelState(local({ isLoading: true })).kind).toBe("loading");
  });

  it("maps each unavailable reason to its own state", () => {
    expect(derivePullRequestPanelState(q({ data: undefined })).kind).toBe("unavailable");
    expect(
      derivePullRequestPanelState(
        q({ data: { object: "session.github.info", available: false, reason: "no_os_env" } }),
      ).kind,
    ).toBe("unavailable");
    expect(
      derivePullRequestPanelState(
        q({ data: { object: "session.github.info", available: false, reason: "not_a_git_repo" } }),
      ).kind,
    ).toBe("not-a-git-repo");
    expect(
      derivePullRequestPanelState(
        q({ data: { object: "session.github.info", available: false, reason: "host_outdated" } }),
      ).kind,
    ).toBe("host-outdated");
  });

  it("walks the gh layer: cli → auth → repo → pr → ready", () => {
    expect(derivePullRequestPanelState(q({ data: { ...ready, gh_available: false } }))).toEqual({
      kind: "no-cli",
      cli: "gh",
    });
    expect(derivePullRequestPanelState(q({ data: { ...ready, authenticated: false } })).kind).toBe(
      "repo-unresolved",
    );
    expect(derivePullRequestPanelState(q({ data: { ...ready, repo: null } })).kind).toBe(
      "repo-unresolved",
    );
    const noPr = derivePullRequestPanelState(q({ data: { ...ready, pr: null } }));
    expect(noPr).toEqual({ kind: "no-pr", branch: "feat/x" });
    expect(derivePullRequestPanelState(q({ data: ready }))).toEqual({ kind: "ready" });
  });

  it("reads the remote host and the provider's auth from the payload", () => {
    expect(
      derivePullRequestPanelState(
        q({
          data: {
            object: "session.github.info",
            available: false,
            reason: "unsupported_remote",
            remote_host: "git.example.com",
          },
        }),
      ),
    ).toEqual({ kind: "unsupported-remote", remoteHost: "git.example.com" });
    // `auth` wins over the legacy fields, which still say gh is ready.
    const auth: PullRequestAuth = {
      authenticated: true,
      hint: null,
      cli: { name: "forge", available: true },
      accounts: null,
      selected_account: null,
    };
    const az = (over: Partial<PullRequestAuth>) => ({ ...ready, auth: { ...auth, ...over } });
    expect(derivePullRequestPanelState(q({ data: az({ authenticated: false }) })).kind).toBe(
      "repo-unresolved",
    );
    expect(derivePullRequestPanelState(q({ data: az({}) }))).toEqual({ kind: "ready" });
  });

  it("stops at a missing CLI only while signed out", () => {
    const azNoCli = (authenticated: boolean, over: Partial<PullRequestInfo> = {}) => ({
      ...ready,
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
      auth: {
        authenticated,
        hint: null,
        cli: { name: "forge", available: false },
        accounts: null,
        selected_account: null,
      },
      ...over,
    });
    expect(derivePullRequestPanelState(q({ data: azNoCli(false) }))).toEqual({
      kind: "no-cli",
      cli: "forge",
    });
    // A token signs in without the CLI, so the signed-in flow goes on as usual.
    expect(derivePullRequestPanelState(q({ data: azNoCli(true) }))).toEqual({ kind: "ready" });
    expect(derivePullRequestPanelState(q({ data: azNoCli(true, { pr: null }) }))).toEqual({
      kind: "no-pr",
      branch: "feat/x",
    });
    expect(derivePullRequestPanelState(q({ data: azNoCli(true, { repo: null }) })).kind).toBe(
      "repo-unresolved",
    );
  });
});

describe("session PR selection", () => {
  it.each([false, true])(
    "selects a previewed PR in a mounted panel (active: %s)",
    async (active) => {
      Object.assign(state.info!.data!, {
        tracking_available: true,
        prs: [
          {
            ...OTHER_PR,
            url: state.info!.data!.pr!.url,
            number: 6000,
          },
          OTHER_PR,
        ],
      });
      const { client, rerender } = renderPanel(false, active);
      expect(await screen.findByRole("list", { name: "Pull requests" })).toBeVisible();

      act(() => showPullRequestInPanel(client, "conv_1", OTHER_PR.url));

      expect(await screen.findByRole("tab", { name: "Summary" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      expect(screen.queryByRole("list", { name: "Pull requests" })).toBeNull();
      expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", {
        poll: active,
        prUrl: OTHER_PR.url,
        findBranch: true,
      });

      rerender(<div />);
      showPullRequestInPanel(client, "conv_1", state.info!.data!.pr!.url);
      rerender(<PullRequestPanel conversationId="conv_1" active={active} />);
      expect(screen.getByRole("tab", { name: "Summary" })).toHaveAttribute("aria-selected", "true");
      expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", {
        poll: active,
        prUrl: state.info!.data!.pr!.url,
        findBranch: true,
      });
    },
  );

  it.each([false, true])("opens a single PR directly (stored list: %s)", async (stored) => {
    const url = state.info!.data!.pr!.url;
    const association: PullRequestAssociation = {
      url,
      host: "github.com",
      repository: "acme/app",
      number: 6000,
      relationship: "attached",
    };
    state.storesPrs = stored;
    Object.assign(state.info!.data!, {
      tracking_available: true,
      selected_pr_url: url,
      prs: [association],
    });
    if (stored) state.stored.conv_1 = [association];
    const { client, rerender } = renderPanel();

    expect(await screen.findByRole("tab", { name: "Summary" })).toBeVisible();
    expect(screen.queryByRole("list", { name: "Pull requests" })).toBeNull();
    expect(screen.queryByRole("button", { name: /All pull requests/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Remove from this session" })).toBeEnabled();
    expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", {
      poll: true,
      prUrl: url,
      findBranch: true,
    });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Changes" }));
    rerender(<PullRequestPanel conversationId="conv_1" />);
    expect(screen.getByRole("tab", { name: "Changes" })).toHaveAttribute("aria-selected", "true");

    Object.assign(state.info!.data!, { prs: [association, OTHER_PR] });
    if (stored) state.stored.conv_2 = [association, OTHER_PR];
    rerender(<PullRequestPanel conversationId="conv_2" />);
    expect(await screen.findByRole("list", { name: "Pull requests" })).toBeVisible();
    expect(screen.queryByRole("tab", { name: "Summary" })).toBeNull();

    state.info!.data = { ...state.info!.data!, prs: [association] };
    if (stored) {
      act(() => {
        client.setQueryData(["session-pull-requests", "conv_2"], {
          prs: [association],
          synced: true,
        });
      });
    }
    rerender(<PullRequestPanel conversationId="conv_2" />);
    expect(await screen.findByRole("tab", { name: "Summary" })).toBeVisible();
    expect(screen.queryByRole("list", { name: "Pull requests" })).toBeNull();
    expect(screen.queryByRole("button", { name: /All pull requests/ })).toBeNull();
    expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_2", {
      poll: true,
      prUrl: url,
      findBranch: true,
    });
  });

  it.each([
    ["ready", {}, true],
    ["draft", { is_draft: true }, false],
    ["blocked", { merge_state_status: "BLOCKED" }, false],
    ["conflicts", { merge_state_status: "DIRTY" }, false],
    ["behind", { merge_state_status: "BEHIND" }, false],
    ["hooks", { merge_state_status: "HAS_HOOKS" }, true],
    ["missing head", { head_sha: undefined }, false],
    ["unknown", { merge_state_status: "UNKNOWN" }, false],
    ["unsupported checks", { checks_supported: false }, true],
    ["merged", { state: "MERGED" }, false],
    [
      "auto merge",
      { auto_merge: { enabled_at: "today", enabled_by: "ada", merge_method: "SQUASH" } },
      false,
    ],
  ] as const)(
    "offers Merge in the row menu only when eligible: %s",
    async (_name, overrides, expected) => {
      const pr = {
        ...state.info!.data!.pr!,
        checks: { total: 1, passing: 1, failing: 0, pending: 0, runs: [] },
        merge_state_status: "CLEAN",
        head_sha: "head",
        ...overrides,
      };
      Object.assign(state.info!.data!, {
        pr,
        capabilities: { merging: true },
        tracking_available: true,
        prs: [
          OTHER_PR,
          {
            url: pr.url,
            repository: "example/repo",
            host: "github.com",
            number: pr.number,
            relationship: "created",
          },
        ],
      });
      renderPanel();
      const user = userEvent.setup();
      // Merge lives with the row's other actions rather than as a permanent button.
      expect(screen.queryByRole("button", { name: "Merge" })).toBeNull();
      await user.click(screen.getByRole("button", { name: "Actions for example/repo #6000" }));
      expect(!!screen.queryByRole("menuitem", { name: "Merge…" })).toBe(expected);
      expect(await openRowSubmenu(user, "Copy", "GitHub link")).toBeVisible();
      expect(screen.queryByRole("tab", { name: "Summary" })).toBeNull();
    },
  );

  it.each(["failing", "pending", "cancelled", "skipped", "partial", "read-only"])(
    "keeps check details and uses mergeability for %s",
    async (reason) => {
      const pr = {
        ...state.info!.data!.pr!,
        merge_state_status: "UNSTABLE",
        head_sha: "head",
        // One passing job plus, per case, one job in the bucket under test.
        checks: {
          total: reason === "partial" || reason === "read-only" ? 1 : 2,
          passing: 1,
          failing: 0,
          pending: 0,
          runs: [{ name: "Build", bucket: "passing" as const, url: null }],
          ...(reason === "partial"
            ? { partial: true }
            : reason !== "read-only"
              ? { [reason]: 1 }
              : {}),
        },
      };
      state.canEdit = reason !== "read-only";
      Object.assign(state.info!.data!, {
        pr,
        capabilities: { merging: true },
        tracking_available: true,
        prs: [
          OTHER_PR,
          {
            url: pr.url,
            repository: "example/repo",
            host: "github.com",
            number: pr.number,
            relationship: "created",
          },
        ],
      });
      renderPanel();
      const user = userEvent.setup();
      await user.click(screen.getByRole("button", { name: "Actions for example/repo #6000" }));
      expect(!!screen.queryByRole("menuitem", { name: "Remove from this session" })).toBe(
        reason !== "read-only",
      );
      expect(!!screen.queryByRole("menuitem", { name: "Merge…" })).toBe(reason !== "read-only");
      expect(await openRowSubmenu(user, "Copy", "GitHub link")).toBeEnabled();
      await user.keyboard("{Escape}");

      // One compact CI result per row: failures and running checks show their count.
      const list = within(screen.getByRole("list", { name: "Pull requests" }));
      const result = list.getByRole("button", {
        name: {
          failing: "1 failing check · 1 of 2 passed",
          pending: "1 running · 1 of 2 passed",
          cancelled: "1 cancelled check · 1 of 2 passed",
          skipped: "All checks passed · 1 passed, 1 skipped",
          partial: "Some checks unavailable · 1 of 1 passed",
          "read-only": "All checks passed · 1 of 1 passed",
        }[reason],
      });
      expect(result).toHaveTextContent(
        ["failing", "pending", "cancelled"].includes(reason) ? /^1$/ : /^$/,
      );
      // Clicking drills down to the jobs, and Escape puts them away.
      await user.click(result);
      const jobs = await screen.findByRole("list", { name: "Checks" });
      expect(within(jobs).getByText("Build")).toBeVisible();
      await user.keyboard("{Escape}");
      expect(screen.queryByRole("list", { name: "Checks" })).toBeNull();
    },
  );

  it("opens rows and returns to the list while details load or fail", async () => {
    const user = userEvent.setup();
    const one = "https://github.com/example/one/pull/42";
    const two = "https://github.com/example/two/pull/42";
    state.info = {
      isLoading: false,
      error: null,
      isFetching: false,
      data: {
        ...state.info!.data!,
        object: "session.github.info",
        available: true,
        gh_available: true,
        authenticated: true,
        tracking_available: true,
        selected_pr_url: one,
        pr: { ...state.info!.data!.pr!, url: one, number: 42, title: "First repository" },
        prs: [
          {
            url: one,
            host: "github.com",
            repository: "example/one",
            number: 42,
            title: "First repository",
            relationship: "created",
          },
          {
            url: two,
            host: "github.com",
            repository: "example/two",
            number: 42,
            title: "Second repository",
            relationship: "created",
          },
        ],
      },
    };
    const { rerender } = renderPanel();
    await user.click(screen.getByRole("button", { name: "Actions for example/two #42" }));
    expect(screen.getByRole("menuitem", { name: "Open on GitHub" })).toHaveAttribute("href", two);
    await chooseRowSubmenuItem(user, "Copy", "GitHub link");
    expect(await navigator.clipboard.readText()).toBe(two);
    await user.click(screen.getByRole("button", { name: "Actions for example/two #42" }));
    await user.click(screen.getByRole("menuitem", { name: "Remove from this session" }));
    expect(state.unlink.mutate).toHaveBeenCalledWith(two, expect.any(Object));

    expect(screen.queryByRole("tab", { name: "Summary" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "example/two #42 — Second repository" }));
    expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", {
      poll: true,
      prUrl: two,
      findBranch: true,
    });
    expect(screen.queryByRole("list", { name: "Pull requests" })).toBeNull();
    expect(screen.getByRole("button", { name: "All pull requests (2)" })).toBeVisible();

    state.info = { isLoading: true, error: null, isFetching: true };
    rerender(<PullRequestPanel conversationId="conv_1" />);
    expect(screen.getByText("Loading pull requests…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Link a PR" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Remove from this session" })).toBeEnabled();

    state.info = { isLoading: false, error: new Error("Metadata unavailable"), isFetching: false };
    rerender(<PullRequestPanel conversationId="conv_1" />);
    expect(screen.getByRole("link", { name: "Open the PR on GitHub" })).toHaveAttribute(
      "href",
      two,
    );
    await user.click(screen.getByRole("button", { name: "All pull requests (2)" }));
    expect(screen.getByRole("list", { name: "Pull requests" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "example/one #42 — First repository" }));
    expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", {
      poll: true,
      prUrl: one,
      findBranch: true,
    });

    state.info = { isLoading: true, error: null, isFetching: true };
    rerender(<PullRequestPanel conversationId="conv_other" />);
    expect(screen.queryByRole("button", { name: /All pull requests/ })).toBeNull();
  });

  it("shows the server's stored PRs and lets them be unlinked while the host is offline", async () => {
    const user = userEvent.setup();
    const one = "https://github.com/example/one/pull/42";
    const two = "https://github.com/example/two/pull/7";
    const checkedAt = Date.UTC(2026, 9, 22, 17, 30) / 1000;
    state.info = { isLoading: false, error: new RunnerOfflineError(), isFetching: false };
    state.stored.conv_1 = [
      {
        url: one,
        host: "github.com",
        repository: "example/one",
        number: 42,
        title: "Ship it",
        relationship: "created",
        state: "MERGED",
        is_draft: false,
        state_checked_at: checkedAt,
      },
      {
        url: two,
        host: "github.com",
        repository: "example/two",
        number: 7,
        title: null,
        relationship: "inferred",
      },
    ];
    const { client } = renderPanel();

    const list = await screen.findByRole("list", { name: "Pull requests" });
    // Stored state still groups and labels rows while the host is away.
    expect(within(list).getByRole("img", { name: "Merged" })).toBeVisible();
    expect(within(list).getByRole("list", { name: "Merged" })).toBeVisible();
    expect(within(list).getAllByText("Host offline")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Merge" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Actions for example/one #42" }));
    expect(screen.getByRole("menuitem", { name: "Open on GitHub" })).toHaveAttribute("href", one);
    await chooseRowSubmenuItem(user, "Copy", "GitHub link");
    expect(await navigator.clipboard.readText()).toBe(one);
    await user.click(screen.getByRole("button", { name: "example/one #42 — Ship it" }));
    const stored = screen.getByTestId("github-stored-pr");
    expect(within(stored).getByRole("heading", { name: "Ship it" })).toBeInTheDocument();
    expect(within(stored).getByRole("link", { name: /^Open example\/one #42 on/ })).toHaveAttribute(
      "href",
      one,
    );
    expect(within(stored).getByLabelText("Pull request status: Merged")).toBeInTheDocument();
    expect(within(stored).getByRole("status")).toHaveTextContent(
      `Last sync time: ${new Date(checkedAt * 1000).toLocaleString()}. To load the PR details, please reconnect the session’s host.`,
    );
    // Linking verifies the PR with the host's account; unlinking is the server's.
    expect(
      screen.getByRole("button", { name: "Link a PR (needs the host online)" }),
    ).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Remove from this session" }));
    expect(state.unlink.mutate).toHaveBeenCalledWith(one, expect.anything());
    const [, { onSuccess }] = state.unlink.mutate.mock.calls[0];
    act(() => {
      const remaining = { prs: state.stored.conv_1.slice(1), synced: true };
      client.setQueryData(["session-pull-requests", "conv_1"], remaining);
      onSuccess(remaining);
    });
    // A PR the host never fetched has no sync time to show.
    expect(within(screen.getByTestId("github-stored-pr")).getByRole("status")).toHaveTextContent(
      /^To load the PR details, please reconnect the session’s host\.$/,
    );
    expect(screen.queryByRole("button", { name: /All pull requests/ })).toBeNull();
    expect(screen.queryByRole("list", { name: "Pull requests" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Remove from this session" }));
    expect(state.unlink.mutate).toHaveBeenLastCalledWith(two, expect.any(Object));
    const [, callbacks] = state.unlink.mutate.mock.calls[1];
    act(() => {
      client.setQueryData(["session-pull-requests", "conv_1"], { prs: [], synced: true });
      callbacks.onSuccess({ prs: [] });
    });
    expect(screen.queryByRole("list", { name: "Pull requests" })).toBeNull();
    expect(screen.queryByTestId("github-stored-pr")).toBeNull();
  });

  it.each([
    { reason: "error", prCount: 2 },
    { reason: "health", prCount: 2 },
    { reason: "sandbox", prCount: 2 },
    { reason: "health", prCount: 1 },
  ])(
    "replaces cached checks/merge with stored metadata and recovers: $reason ($prCount PRs)",
    async ({ reason, prCount }) => {
      const user = userEvent.setup();
      const url = "https://github.com/acme/app/pull/6000";
      const attached: PullRequestAssociation = {
        url,
        host: "github.com",
        repository: "acme/app",
        number: 6000,
        title: "Attached while online",
        relationship: "attached",
        state: "OPEN",
        is_draft: false,
        state_checked_at: Date.now() / 1000 - 60,
      };
      const prs = prCount === 1 ? [attached] : [attached, OTHER_PR];
      Object.assign(state.info!.data!, {
        tracking_available: true,
        selected_pr_url: url,
        prs,
        capabilities: { merging: true },
        pr: {
          ...state.info!.data!.pr!,
          url,
          title: "Live title",
          merge_state_status: "UNSTABLE",
          head_sha: "head",
        },
      });
      state.stored.conv_1 = prs;
      const { rerender } = renderPanel();
      if (prCount === 1) {
        await screen.findByRole("tab", { name: "Summary" });
        expect(screen.queryByRole("list", { name: "Pull requests" })).toBeNull();
        expect(screen.getByRole("region", { name: "Merge" })).toBeVisible();
        expect(screen.getByText("2 failing, 66 successful")).toBeVisible();
      } else {
        await screen.findByRole("list", { name: "Pull requests" });
        await user.click(screen.getByRole("button", { name: "Actions for acme/app #6000" }));
        expect(screen.getByRole("menuitem", { name: "Merge…" })).toBeVisible();
        await user.keyboard("{Escape}");
        expect(
          screen.getByRole("button", { name: "2 failing checks · 66 of 68 passed" }),
        ).toBeVisible();
      }
      const online = state.info!;
      expect(screen.queryByTestId("github-stored-pr")).toBeNull();

      // React Query keeps the last payload when the refetch fails.
      state.info = { ...state.info!, error: reason === "error" ? new RunnerOfflineError() : null };
      state.serveable = reason === "health" ? false : undefined;
      state.sandboxAsleep = reason === "sandbox";
      rerender(<PullRequestPanel conversationId="conv_1" />);

      expect(screen.queryByRole("region", { name: "Merge" })).toBeNull();
      expect(screen.queryByText("2 failing, 66 successful")).toBeNull();
      expect(screen.queryByRole("button", { name: /failing checks/ })).toBeNull();
      if (prCount > 1) {
        const status = screen.getAllByText(
          reason === "sandbox" ? "Sandbox asleep" : "Host offline",
        )[0];
        await user.hover(status);
        expect(await screen.findByRole("tooltip")).toHaveTextContent("Last sync time:");
        await user.unhover(status);
        await user.click(
          screen.getByRole("button", { name: "acme/app #6000 — Attached while online" }),
        );
      } else {
        expect(screen.getByRole("heading", { name: "Pull Requests" })).toBeVisible();
        expect(screen.queryByRole("button", { name: /All pull requests/ })).toBeNull();
      }
      const stored = screen.getByTestId("github-stored-pr");
      expect(stored).toHaveTextContent("Last sync time:");
      // A sleeping sandbox offers to resume it right here; a host needs reconnecting.
      if (reason === "sandbox") {
        expect(within(stored).getByRole("button", { name: "Resume sandbox" })).toBeVisible();
      }
      expect(within(stored).getByRole("heading", { name: "Attached while online" })).toBeVisible();
      expect(within(stored).getByRole("link", { name: /^Open .* on GitHub$/ })).toHaveAttribute(
        "href",
        url,
      );
      expect(within(stored).getByLabelText("Pull request status: Open")).toBeInTheDocument();
      expect(screen.queryByText(/The agent is asleep/)).toBeNull();
      expect(screen.queryByRole("tab", { name: "Summary" })).toBeNull();
      state.info = online;
      state.serveable = true;
      state.sandboxAsleep = false;
      rerender(<PullRequestPanel conversationId="conv_1" />);
      expect(screen.getByRole("tab", { name: "Summary" })).toBeVisible();
      if (prCount > 1) {
        await user.click(screen.getByRole("button", { name: "All pull requests (2)" }));
        expect(
          screen.getByRole("button", { name: "2 failing checks · 66 of 68 passed" }),
        ).toBeVisible();
      } else {
        expect(screen.queryByRole("list", { name: "Pull requests" })).toBeNull();
        expect(screen.queryByRole("button", { name: /All pull requests/ })).toBeNull();
        expect(screen.getByRole("region", { name: "Merge" })).toBeVisible();
        expect(screen.getByText("2 failing, 66 successful")).toBeVisible();
      }
      expect(screen.queryByTestId("github-stored-pr")).toBeNull();
      expect(screen.queryByText("Host offline")).toBeNull();
    },
  );

  it.each([true, false])(
    "treats an empty server list as authoritative (offline: %s)",
    async (offline) => {
      const url = "https://github.com/acme/app/pull/6000";
      const cached: PullRequestAssociation = {
        url,
        host: "github.com",
        repository: "acme/app",
        number: 6000,
        title: "Unlinked in another tab",
        relationship: "attached",
      };
      Object.assign(state.info!.data!, {
        tracking_available: true,
        selected_pr_url: url,
        prs: [cached],
      });
      state.info = { ...state.info!, error: offline ? new RunnerOfflineError() : null };
      state.stored.conv_1 = [cached];
      const { client } = renderPanel();
      await screen.findByRole("button", { name: "Remove from this session" });
      expect(screen.queryByRole("button", { name: /All pull requests/ })).toBeNull();
      act(() => {
        client.setQueryData(["session-pull-requests", "conv_1"], { prs: [], synced: true });
      });

      await waitFor(() => expect(screen.queryByRole("list", { name: "Pull requests" })).toBeNull());
      expect(screen.queryByTestId("github-stored-pr")).toBeNull();
      expect(screen.queryByRole("tab", { name: "Summary" })).toBeNull();
    },
  );

  it("explains in an open link picker that the host disconnected", async () => {
    const user = userEvent.setup();
    Object.assign(state.info!.data!, {
      tracking_available: true,
      prs: [
        {
          url: "https://github.com/acme/app/pull/6000",
          host: "github.com",
          repository: "acme/app",
          number: 6000,
          relationship: "attached",
        },
      ],
    });
    const { rerender } = renderPanel();
    await user.click(await screen.findByRole("button", { name: "Link a PR" }));
    const picker = await findLinkPicker();
    await user.type(picker, "https://github.com/acme/app/pull/7");
    state.serveable = false;
    rerender(<PullRequestPanel conversationId="conv_1" />);
    expect(picker).toHaveAccessibleDescription(
      "To link a PR, please reconnect the session’s host.",
    );
    expect(screen.queryByRole("option")).toBeNull();
    state.serveable = true;
    rerender(<PullRequestPanel conversationId="conv_1" />);
    expect(picker).toHaveValue("https://github.com/acme/app/pull/7");
    expect(screen.getByRole("option", { name: /Link acme\/app#7/ })).toBeVisible();
  });

  it.each([
    {
      offline: "the host is disconnected",
      serveable: false,
      sandboxAsleep: false,
      message: "To link a PR, please reconnect the session’s host.",
    },
    {
      offline: "the sandbox is asleep",
      serveable: undefined,
      sandboxAsleep: true,
      message: "To link a PR, resume the session’s sandbox.",
    },
  ])(
    "asks for the host instead of offering to link a PR while $offline",
    async ({ serveable, sandboxAsleep, message }) => {
      state.serveable = serveable;
      state.sandboxAsleep = sandboxAsleep;
      // Cached from before the host went away, as after unlinking the last PR offline.
      Object.assign(state.info!.data!, {
        tracking_available: true,
        prs: [],
        pr: null,
        selected_pr_url: undefined,
      });
      state.stored.conv_1 = [];
      renderPanel();

      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(screen.getByText(/No open PR for/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Link a PR/ })).toBeNull();
    },
  );

  it("still offers to link a PR on an older server while the host is disconnected", () => {
    state.storesPrs = false;
    state.serveable = false;
    Object.assign(state.info!.data!, {
      tracking_available: true,
      prs: [],
      pr: null,
      selected_pr_url: undefined,
    });
    renderPanel();

    expect(screen.getByRole("button", { name: "Link a PR" })).toBeEnabled();
  });

  it("says when the server hasn't imported the session's PRs yet", async () => {
    state.info = { isLoading: false, error: new RunnerOfflineError(), isFetching: false };
    state.stored.conv_1 = [];
    state.unsynced.add("conv_1");
    renderPanel();

    expect(
      await screen.findByText(
        "This session’s PRs haven’t synced to the server yet. To load them, please reconnect the session’s host.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps an older server's offline panel as it was", () => {
    state.storesPrs = false;
    Object.assign(state.info!.data!, {
      tracking_available: true,
      selected_pr_url: "https://github.com/acme/app/pull/6000",
      prs: [
        {
          url: "https://github.com/acme/app/pull/6000",
          host: "github.com",
          repository: "acme/app",
          number: 6000,
          title: "Live only",
          relationship: "attached",
        },
      ],
    });
    state.info = { ...state.info!, error: new RunnerOfflineError() };
    renderPanel();

    expect(screen.queryByTestId("github-stored-pr")).toBeNull();
    expect(screen.getByRole("button", { name: "Link a PR" })).toBeEnabled();
  });

  it.each(["javascript:alert(1)", "http://github.com/acme/app/pull/1"])(
    "links a stored PR only over https and marks a draft: %s",
    async (url) => {
      state.info = { isLoading: false, error: new RunnerOfflineError(), isFetching: false };
      state.stored.conv_1 = [
        {
          url,
          host: "github.com",
          repository: "acme/app",
          number: 1,
          title: "Not a link",
          relationship: "created",
          state: "OPEN",
          is_draft: true,
        },
      ];
      renderPanel();

      // The details remount once the selection settles on the stored PR.
      await waitFor(() =>
        expect(
          within(screen.getByTestId("github-stored-pr")).getByLabelText(
            "Pull request status: Draft",
          ),
        ).toBeInTheDocument(),
      );
      const stored = screen.getByTestId("github-stored-pr");
      expect(within(stored).queryByRole("link")).toBeNull();
      expect(within(stored).getByText("Not a link")).toBeInTheDocument();
    },
  );

  it("offers to resume a sleeping sandbox rather than reconnect a host", async () => {
    const url = "https://github.com/acme/app/pull/6000";
    state.sandboxAsleep = true;
    state.info = { isLoading: false, error: null, isFetching: false };
    state.stored.conv_1 = [
      {
        url,
        host: "github.com",
        repository: "acme/app",
        number: 6000,
        title: "Sandbox PR",
        relationship: "created",
        state: "OPEN",
        is_draft: false,
        state_checked_at: null,
      },
    ];
    renderPanel();

    await waitFor(() =>
      expect(within(screen.getByTestId("github-stored-pr")).getByRole("status")).toHaveTextContent(
        /^Sandbox is asleepResume it to load the PR details\.Resume sandbox$/,
      ),
    );
  });

  it("says the sandbox is resuming while it wakes", async () => {
    const url = "https://github.com/acme/app/pull/6001";
    state.sandboxAsleep = true;
    sandboxResuming = true;
    try {
      state.info = { isLoading: false, error: null, isFetching: false };
      state.stored.conv_1 = [
        {
          url,
          host: "github.com",
          repository: "acme/app",
          number: 6001,
          title: "Waking PR",
          relationship: "created",
          state: "OPEN",
          is_draft: false,
          state_checked_at: null,
        },
      ];
      renderPanel();
      await waitFor(() =>
        expect(
          within(screen.getByTestId("github-stored-pr")).getByRole("status"),
        ).toHaveTextContent("Resuming sandbox…"),
      );
    } finally {
      sandboxResuming = false;
    }
  });

  it.each([undefined, null, "", "   "])(
    "falls back to the PR identity when its title is %j",
    (title) => {
      const url = "https://github.com/example/one/pull/42";
      Object.assign(state.info!.data!, {
        tracking_available: true,
        selected_pr_url: url,
        prs: [
          OTHER_PR,
          {
            url,
            host: "github.com",
            repository: "example/one",
            number: 42,
            title,
            relationship: "created",
          },
        ],
      });
      renderPanel();
      const list = within(screen.getByRole("list", { name: "Pull requests" }));
      // An untitled row names its repository and number instead.
      expect(list.getByText("example/one #42")).toBeVisible();
      expect(list.getByRole("button", { name: "example/one #42" })).toBeVisible();
    },
  );

  it("keeps the host and inferred marker alongside a trimmed PR title", () => {
    const url = "https://github.example.com/example/one/pull/42";
    Object.assign(state.info!.data!, {
      tracking_available: true,
      selected_pr_url: url,
      prs: [
        OTHER_PR,
        {
          url,
          host: "github.example.com",
          repository: "example/one",
          number: 42,
          title: "  Fix session selection  ",
          relationship: "inferred",
        },
      ],
    });
    renderPanel();
    const list = within(screen.getByRole("list", { name: "Pull requests" }));
    const label = "github.example.com/example/one #42 (from branch) — Fix session selection";
    expect(list.getByRole("button", { name: label })).toBeVisible();
    expect(list.getByText("Fix session selection")).toBeVisible();
    expect(list.getByText("From branch")).toBeVisible();
  });

  it("passes the selected PR and revisions into file queries", () => {
    const url = "https://github.com/example/two/pull/42";
    state.info = {
      isLoading: false,
      error: null,
      isFetching: false,
      data: {
        object: "session.github.info",
        available: true,
        gh_available: true,
        authenticated: true,
        selected_pr_url: url,
        pr: {
          number: 42,
          title: "Second repo",
          url,
          state: "OPEN",
          is_draft: false,
          author: "user",
          head_ref: "topic",
          base_ref: "main",
          head_sha: "head",
          base_sha: "base",
          checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
        },
      },
    };
    renderPanel();
    expect(usePullRequestChangedFiles).toHaveBeenLastCalledWith("conv_1", true, url, "base:head");
  });
});

it.each([
  [true, true, "posts"],
  [false, true, "hidden"],
  [true, false, "blocked"],
  [true, undefined, "blocked"],
] as const)(
  "gates comment posting on edit access %s and host capability %s",
  async (canEdit, capability, expected) => {
    state.canEdit = canEdit;
    state.info!.data!.selected_pr_url = state.info!.data!.pr!.url;
    state.info!.data!.capabilities = {
      account_switching: false,
      base_remote_selection: false,
      line_counts: false,
      linked_pr_diff: false,
      comment_posting: capability,
    };
    renderComments();
    const box = screen.queryByRole("textbox", { name: "New PR comment" });
    if (expected === "hidden") {
      expect(box).toBeNull();
      return;
    }
    // An editor always keeps the box and its draft; where nothing can post, it says why.
    expect(box).toBeEnabled();
    fireEvent.change(box!, { target: { value: "Kept while blocked" } });
    const postButton = screen.getByRole("button", { name: "Post comment" });
    await waitFor(() => expect(box).toHaveValue("Kept while blocked"));
    if (expected === "posts") {
      await waitFor(() => expect(postButton).toBeEnabled());
      expect(screen.queryByTestId("pull-request-comment-writes-notice")).toBeNull();
    } else {
      expect(postButton).toBeDisabled();
      expect(screen.getByTestId("pull-request-comment-writes-notice")).toHaveTextContent(
        "The session’s machine can’t post comments on this pull request.",
      );
      expect(screen.getByRole("link", { name: "Open on GitHub" })).toHaveAttribute(
        "href",
        state.info!.data!.pr!.url,
      );
    }
  },
);

it("keeps the box, blocked and explained, when the host sends no capabilities", () => {
  state.info!.data!.selected_pr_url = state.info!.data!.pr!.url;
  delete state.info!.data!.capabilities;
  renderComments();
  expect(screen.getByRole("textbox", { name: "New PR comment" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "Post comment" })).toBeDisabled();
  expect(screen.getByTestId("pull-request-comment-writes-notice")).toBeVisible();
});

it("names a GitLab merge request's own provider when its host can't post comments", () => {
  state.info!.data!.selected_pr_url = state.info!.data!.pr!.url;
  state.info!.data!.provider = "gitlab";
  state.info!.data!.provider_display = {
    id: "gitlab",
    display_name: "GitLab",
    request_name: "merge request",
    number_prefix: "!",
  };
  state.info!.data!.capabilities = {
    account_switching: false,
    base_remote_selection: false,
    line_counts: false,
    linked_pr_diff: false,
  };
  renderComments();
  expect(screen.getByRole("button", { name: "Post comment" })).toBeDisabled();
  expect(screen.getByTestId("pull-request-comment-writes-notice")).toHaveTextContent(
    "The session’s machine can’t post comments on this merge request.",
  );
  expect(screen.getByRole("link", { name: "Open on GitLab" })).toBeVisible();
  expect(screen.queryByRole("link", { name: "Open on GitHub" })).toBeNull();
  expect(screen.queryByRole("button", { name: /^Sign in to/ })).toBeNull();
});

it("hides comment posting without a selected pull request", () => {
  state.info!.data!.capabilities = {
    account_switching: false,
    base_remote_selection: false,
    line_counts: false,
    linked_pr_diff: false,
    comment_posting: true,
  };
  delete state.info!.data!.selected_pr_url;
  renderComments();
  expect(screen.queryByRole("textbox", { name: "New PR comment" })).toBeNull();
  // Without a composer there's nowhere below to start, so the hint doesn't point there.
  expect(screen.getByText("Comments on this pull request appear here.")).toBeVisible();
  expect(screen.getByRole("status").parentElement).toHaveClass("pb-16");
});

describe("pull request list", () => {
  const OPEN = "https://github.com/acme/app/pull/1";
  const MERGED = "https://github.com/acme/app/pull/2";
  const DRAFT = "https://github.com/acme/web/pull/3";
  const CLOSED = "https://github.com/acme/web/pull/4";

  it("opens a PR linked through a sub-agent in the parent's workspace without linking it to the parent", async () => {
    pageV2.set(true);
    const { prs } = linkThree();
    state.stored.conv_1 = prs.filter((pr) => pr.url !== OPEN);
    const { client } = renderControlled(
      { selected: OPEN, showingDetails: true, subtab: "summary" },
      { kind: "pull", onShowAllPullRequests: vi.fn(), onOpenInNewTab: vi.fn() },
    );
    act(() => {
      client.setQueryData(sessionPullRequestsKey("child-review"), { prs: [prs[0]], synced: true });
      client.setQueryData(["conversation", "conv_1", "descendants"], {
        rows: [{ info: { id: "child-review", title: "Child" }, parentId: "conv_1", depth: 1 }],
        truncated: false,
      });
    });
    await waitFor(() =>
      expect(client.getQueryData(sessionPullRequestsKey("conv_1"))).toBeDefined(),
    );
    expect(screen.queryByText("This pull request isn't linked to this session")).toBeNull();
  });

  it("lists a sub-agent's PR in a parent with no PRs of its own, rather than offering Link it", async () => {
    pageV2.set(true);
    const { prs } = linkThree();
    state.stored.conv_1 = [];
    const { client } = renderControlled(
      { selected: undefined, showingDetails: false, subtab: "summary" },
      { kind: "overview", onOpenPullRequest: vi.fn() },
    );
    act(() => {
      client.setQueryData(["conversation", "conv_1", "descendants"], {
        rows: [{ info: { id: "child-review", title: "Child" }, parentId: "conv_1", depth: 1 }],
        truncated: false,
      });
      client.setQueryData(sessionPullRequestsKey("child-review"), { prs: [prs[0]], synced: true });
    });
    expect(await screen.findByRole("grid", { name: "Linked pull requests" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Link it" })).toBeNull();
  });

  it("reads no sub-agent tree while one of the session's own PRs opens in its own place", async () => {
    pageV2.set(true);
    const { prs, livePrFor } = linkThree();
    state.stored.conv_1 = prs;
    const { client } = renderControlled(
      { selected: OPEN, showingDetails: true, subtab: "summary" },
      { kind: "pull", onShowAllPullRequests: vi.fn(), onOpenInNewTab: vi.fn() },
    );
    // The details are up and the session's own list has loaded, so the panel has settled on it.
    expect((await screen.findAllByText(String(livePrFor(OPEN)?.title))).length).toBeGreaterThan(0);
    await waitFor(() =>
      expect(client.getQueryData(sessionPullRequestsKey("conv_1"))).toBeDefined(),
    );
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
    });
    expect(
      client.getQueryCache().find({ queryKey: ["conversation", "conv_1", "descendants"] }),
    ).toBeUndefined();
  });

  it("opens a sub-agent's PR in a parent with no PRs of its own, with its details", async () => {
    pageV2.set(true);
    const { prs, livePrFor } = linkThree();
    state.stored.conv_1 = [];
    const { client } = renderControlled(
      { selected: OPEN, showingDetails: true, subtab: "summary" },
      { kind: "pull", onShowAllPullRequests: vi.fn(), onOpenInNewTab: vi.fn() },
    );
    act(() => {
      client.setQueryData(sessionPullRequestsKey("child-review"), {
        prs: prs.filter((pr) => pr.url === OPEN),
        synced: true,
      });
      client.setQueryData(["conversation", "conv_1", "descendants"], {
        rows: [{ info: { id: "child-review", title: "Child" }, parentId: "conv_1", depth: 1 }],
        truncated: false,
      });
    });
    // The new page's details, with their sections, rather than the not-linked or empty state.
    expect(await screen.findByRole("tab", { name: "Overview" })).toBeVisible();
    expect(screen.getAllByText(String(livePrFor(OPEN)?.title)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/isn.t linked to this session/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Link it" })).toBeNull();
  });

  it("lists a sub-agent's known PR when the session's own reads fail, and says so", async () => {
    pageV2.set(true);
    const { prs } = linkThree();
    state.stored.conv_1 = [];
    state.storedFailing.add("conv_1");
    // The session's own info read fails too; each PR's own read still answers.
    const perPullRequest = vi.mocked(usePullRequestInfo).getMockImplementation()!;
    vi.mocked(usePullRequestInfo).mockImplementation((id, options) =>
      options?.prUrl
        ? perPullRequest(id, options)
        : ({
            data: undefined,
            isLoading: false,
            error: new Error("500 Internal Server Error"),
            isFetching: false,
          } as never),
    );
    const onOpenPullRequest = vi.fn();
    const { client } = renderControlled(
      { selected: undefined, showingDetails: false, subtab: "summary" },
      { kind: "overview", onOpenPullRequest },
    );
    act(() => {
      client.setQueryData(sessionPullRequestsKey("child-review"), {
        prs: prs.filter((pr) => pr.url === OPEN),
        synced: true,
      });
      client.setQueryData(["conversation", "conv_1", "descendants"], {
        rows: [{ info: { id: "child-review", title: "Child" }, parentId: "conv_1", depth: 1 }],
        truncated: false,
      });
    });
    const grid = await screen.findByRole("grid", { name: "Linked pull requests" });
    const row = within(grid).getByRole("row", { name: /Retry uploads/ });
    expect(
      await screen.findByText(
        /Couldn’t load this session’s own pull requests: 500 Internal Server Error/,
      ),
    ).toBeVisible();
    // The sub-agent's PR has no own link to remove, and it still opens.
    expect(within(row).queryByRole("button", { name: /^Remove / })).toBeNull();
    fireEvent.click(within(row).getByText("Retry uploads"));
    expect(onOpenPullRequest).toHaveBeenCalledWith(OPEN, expect.anything());
  });

  it("removes only the session's own finished PRs from ⌘K, leaving a sub-agent's closed one", async () => {
    pageV2.set(true);
    const { prs } = linkThree();
    state.stored.conv_1 = prs;
    const closedBySubAgent = {
      url: CLOSED,
      host: "github.com",
      repository: "acme/web",
      number: 4,
      relationship: "created",
      state: "CLOSED",
    };
    const { client } = renderControlled(
      { selected: undefined, showingDetails: false, subtab: "summary" },
      { kind: "overview", onOpenPullRequest: vi.fn() },
    );
    act(() => {
      client.setQueryData(sessionPullRequestsKey("child-review"), {
        prs: [closedBySubAgent],
        synced: true,
      });
      client.setQueryData(["conversation", "conv_1", "descendants"], {
        rows: [{ info: { id: "child-review", title: "Child" }, parentId: "conv_1", depth: 1 }],
        truncated: false,
      });
    });
    const grid = await screen.findByRole("grid", { name: "Linked pull requests" });
    // The sub-agent's closed PR is listed, tagged with the sub-agent.
    expect((await within(grid).findAllByText("Child")).length).toBeGreaterThan(0);
    const finished = paletteGroup("pull-requests:conv_1")!.commands.find(
      (command) => command.id === "unlink-finished",
    )!;
    // ⌘K counts only what it removes: the session's merged PR, not the sub-agent's closed one.
    expect(finished.label).toBe("Remove merged and closed pull requests (1)");
    act(() => finished.run());
    expect(state.unlink.mutate).toHaveBeenCalledExactlyOnceWith(MERGED, expect.any(Object));
    expect(state.unlinkMany).not.toHaveBeenCalled();
  });

  describe("a sub-agent's PR opened cold, with no tree cached", () => {
    afterEach(() => {
      sessionReads.answer = undefined;
    });

    const CHILD_TREE = {
      object: "list",
      data: [
        {
          id: "child-review",
          title: "researcher:auth",
          parent_session_id: "conv_1",
          depth: 1,
          tool: "researcher",
          session_name: "auth",
          current_task_status: null,
          busy: false,
        },
      ],
      has_more: false,
      last_id: null,
    };

    // The sub-agent reads wait until `release`, so the loading state can be seen first.
    function answerTree(childPrs: unknown[] | "fail") {
      let release!: () => void;
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      sessionReads.answer = async (url) => {
        const json = (body: unknown, status = 200) =>
          new Response(JSON.stringify(body), {
            status,
            headers: { "Content-Type": "application/json" },
          });
        if (url.includes("/child_sessions")) {
          await gate;
          return childPrs === "fail" ? json({ error: { message: "nope" } }, 500) : json(CHILD_TREE);
        }
        if (url.includes("/v1/sessions/child-review/pull-requests") && childPrs !== "fail") {
          await gate;
          return json({ object: "list", data: childPrs, synced: true, has_more: false });
        }
        return undefined;
      };
      return release;
    }

    it("shows the loading state, then the details, when a sub-agent links it", async () => {
      pageV2.set(true);
      const { prs, livePrFor } = linkThree();
      state.stored.conv_1 = prs.filter((pr) => pr.url !== OPEN);
      const release = answerTree([prs[0]]);
      renderControlled(
        { selected: OPEN, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests: vi.fn(), onOpenInNewTab: vi.fn() },
      );
      await screen.findByText("Loading pull requests…");
      expect(screen.queryByText(/isn.t linked to this session/)).toBeNull();
      expect(screen.queryByRole("button", { name: "Link it" })).toBeNull();
      release();
      await waitFor(() =>
        expect(screen.queryByText("Loading pull requests…")).not.toBeInTheDocument(),
      );
      expect(screen.queryByText(/isn.t linked to this session/)).toBeNull();
      expect(screen.getAllByText(String(livePrFor(OPEN)?.title)).length).toBeGreaterThan(0);
    });

    it("shows loading, then not linked, once the tree is known and no sub-agent links it", async () => {
      pageV2.set(true);
      const { prs } = linkThree();
      state.stored.conv_1 = prs.filter((pr) => pr.url !== OPEN);
      const release = answerTree([]);
      renderControlled(
        { selected: OPEN, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests: vi.fn(), onOpenInNewTab: vi.fn() },
      );
      await screen.findByText("Loading pull requests…");
      expect(screen.queryByText(/isn.t linked to this session/)).toBeNull();
      release();
      expect(
        await screen.findByText(/isn.t linked to this session/, undefined, { timeout: 5000 }),
      ).toBeInTheDocument();
      expect(screen.queryByText("Loading pull requests…")).toBeNull();
    });

    it("records the tree read's failure, then shows not linked", async () => {
      pageV2.set(true);
      const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
      const { prs } = linkThree();
      state.stored.conv_1 = prs.filter((pr) => pr.url !== OPEN);
      const release = answerTree("fail");
      renderControlled(
        { selected: OPEN, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests: vi.fn(), onOpenInNewTab: vi.fn() },
      );
      await screen.findByText("Loading pull requests…");
      release();
      expect(
        await screen.findByText(/isn.t linked to this session/, undefined, { timeout: 5000 }),
      ).toBeInTheDocument();
      expect(warn).toHaveBeenCalledWith(
        "[omnigent] sub-agent pull requests read failed stage=descendants trigger=open reason=http parent=conv_1 status=500",
      );
      warn.mockRestore();
    });
  });

  /** Three linked PRs, each answering with its own live PR; the viewer is `viewer`. */
  function linkThree(capabilities: Record<string, boolean> = { merging: true }) {
    const base = state.info!.data!;
    const livePr = (url: string, over: Partial<PullRequest>): PullRequest => ({
      ...base.pr!,
      url,
      checks: { passing: 1, failing: 0, pending: 0, total: 1, runs: [] },
      merge_state_status: "CLEAN",
      head_sha: "head",
      ...over,
    });
    const live: Record<string, PullRequest> = {
      [OPEN]: livePr(OPEN, {
        number: 1,
        title: "Retry uploads",
        author: "ada",
        head_ref: "ada/retry",
        reviewers: [{ login: "viewer", kind: "user", state: "PENDING" }],
      }),
      [MERGED]: livePr(MERGED, { number: 2, title: "Ship docs", state: "MERGED" }),
      [DRAFT]: livePr(DRAFT, { number: 3, title: "Try a new layout", is_draft: true }),
    };
    const prs: PullRequestAssociation[] = [OPEN, MERGED, DRAFT].map((url) => ({
      url,
      host: "github.com",
      repository: url.split("/").slice(3, 5).join("/"),
      number: live[url].number,
      relationship: "created",
    }));
    const livePrFor = (url: string | undefined) => live[url ?? OPEN];
    respondWith(prs, livePrFor, capabilities);
    return { prs, livePrFor };
  }

  /** Answer each PR URL with one stable info object, as React Query's cache would. */
  function respondWith(
    prs: PullRequestAssociation[],
    pr: (url: string | undefined) => PullRequest | undefined,
    capabilities: Record<string, boolean> = { merging: true },
  ) {
    const base = state.info!.data!;
    const infos = new Map<string, unknown>();
    vi.mocked(usePullRequestInfo).mockImplementation((_id, options) => {
      const key = options?.prUrl ?? "";
      if (!infos.has(key)) {
        infos.set(key, {
          isLoading: false,
          error: null,
          isFetching: false,
          data: {
            ...base,
            tracking_available: true,
            selected_pr_url: OPEN,
            prs,
            capabilities,
            auth: {
              authenticated: true,
              hint: null,
              cli: null,
              accounts: null,
              selected_account: "viewer",
            },
            pr: pr(options?.prUrl) ?? null,
          },
        });
      }
      return infos.get(key) as never;
    });
    return prs;
  }

  afterEach(() => {
    localStorage.clear();
    pageV2.set(false);
    usePaletteCommandsStore.setState({ groups: {} });
  });

  /**
   * The overview in the tabs sidebar beside the chat's approval chord, with a prompt
   * pending in the real chat store: ⌘Enter on a focused row is the row's, not the prompt's.
   */
  function renderOverviewBesideApproval(mac: boolean) {
    const approval = pendingApproval(mac);
    const onOpenPullRequest = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(
      <QueryClientProvider client={client}>
        <PullRequestPanel
          conversationId="conv_1"
          view={{ showingDetails: false, subtab: "summary" }}
          tabsMode={{ kind: "overview", onOpenPullRequest }}
          onViewChange={() => {}}
        />
      </QueryClientProvider>,
    );
    const restore = () => {
      view.unmount();
      approval.restore();
    };
    return { ...approval, onOpenPullRequest, restore };
  }

  describe("in a tab of the tabs sidebar", () => {
    /** The panel as the tabs sidebar mounts it: its view owned outside, in one of its tab modes. */
    function renderInTab(initial: PullRequestPanelView, tabsMode: PullRequestTabsMode) {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const reports: PullRequestPanelView[] = [];
      function Harness() {
        const [view, setView] = useState(initial);
        return (
          <PullRequestPanel
            conversationId="conv_1"
            view={view}
            tabsMode={tabsMode}
            onViewChange={(next) => {
              reports.push(next);
              setView(next);
            }}
          />
        );
      }
      const result = render(
        <QueryClientProvider client={client}>
          <Harness />
        </QueryClientProvider>,
      );
      return {
        reports,
        client,
        unmount: result.unmount,
        rerender: () =>
          result.rerender(
            <QueryClientProvider client={client}>
              <Harness />
            </QueryClientProvider>,
          ),
      };
    }
    const overview = () => ({ kind: "overview" as const, onOpenPullRequest: vi.fn() });
    const LIST: PullRequestPanelView = { showingDetails: false, subtab: "summary" };

    it("the overview lists the pull requests, and choosing one opens its tab instead of switching", () => {
      linkThree();
      const mode = overview();
      const { reports } = renderInTab(LIST, mode);
      expect(screen.getByRole("list", { name: "Open" })).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: /^acme\/app #1/ }));
      expect(mode.onOpenPullRequest).toHaveBeenCalledWith(OPEN, {
        background: false,
        linkedCount: 3,
      });
      expect(reports.some((view) => view.showingDetails)).toBe(false);
      expect(screen.getByRole("list", { name: "Open" })).toBeVisible();
      expect(screen.queryByRole("tab", { name: "Summary" })).toBeNull();
    });

    it("counts the linked PRs the list shows, live ones included where the server stores none", () => {
      const { prs, livePrFor } = linkThree();
      // An older server keeps no list: the session's machine names its one PR.
      state.storesPrs = false;
      respondWith(prs.slice(0, 1), livePrFor);
      const mode = overview();
      renderInTab(LIST, mode);
      fireEvent.click(screen.getByRole("button", { name: /^acme\/app #1/ }));
      expect(mode.onOpenPullRequest).toHaveBeenCalledWith(OPEN, {
        background: false,
        linkedCount: 1,
      });
    });

    it("publishes how many pull requests the overview lists for its tab's pill, as links change", async () => {
      const { prs, livePrFor } = linkThree();
      const listCount = () => usePullRequestListCounts.getState().counts.conv_1;
      const { client, unmount } = renderInTab(LIST, overview());
      await waitFor(() => expect(listCount()).toBe(3));

      // Unlinked elsewhere: the list and its count both drop the PR.
      const remaining = prs.filter((pr) => pr.url !== MERGED);
      respondWith(remaining, livePrFor);
      act(() => {
        client.setQueryData(sessionPullRequestsKey("conv_1"), { prs: remaining, synced: true });
      });
      await waitFor(() => expect(listCount()).toBe(2));
      expect(screen.queryByRole("button", { name: /^acme\/app #2/ })).toBeNull();

      unmount();
      expect(listCount()).toBeUndefined();
    });

    it("publishes no count from a tab that shows one pull request", async () => {
      const { livePrFor } = linkThree();
      renderInTab(
        { selected: OPEN, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests: vi.fn(), onOpenInNewTab: vi.fn() },
      );
      expect((await screen.findAllByText(String(livePrFor(OPEN)?.title))).length).toBeGreaterThan(
        0,
      );
      expect(usePullRequestListCounts.getState().counts).toEqual({});
    });

    it("the overview opens a row behind it on ⌘-click, a middle click or ⌘Enter, and switches on ⇧⌘-click", () => {
      const platform = vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
      linkThree();
      const mode = overview();
      renderInTab(LIST, mode);
      const row = () => screen.getByRole("button", { name: /^acme\/app #1/ });
      fireEvent.click(row(), { metaKey: true });
      expect(mode.onOpenPullRequest).toHaveBeenLastCalledWith(OPEN, {
        background: true,
        linkedCount: 3,
      });
      fireEvent.click(row(), { metaKey: true, shiftKey: true });
      expect(mode.onOpenPullRequest).toHaveBeenLastCalledWith(OPEN, {
        background: false,
        foreground: true,
        linkedCount: 3,
      });
      fireEvent(row(), new MouseEvent("auxclick", { bubbles: true, button: 1 }));
      expect(mode.onOpenPullRequest).toHaveBeenLastCalledWith(OPEN, {
        background: true,
        linkedCount: 3,
      });
      mode.onOpenPullRequest.mockClear();
      fireEvent.keyDown(row(), { key: "Enter", metaKey: true });
      expect(mode.onOpenPullRequest).toHaveBeenCalledTimes(1);
      expect(mode.onOpenPullRequest).toHaveBeenLastCalledWith(OPEN, {
        background: true,
        linkedCount: 3,
      });
      // The overview itself stays the list throughout.
      expect(screen.getByRole("list", { name: "Open" })).toBeVisible();
      platform.mockRestore();
    });

    it.each([true, false])(
      "⌘Enter on a focused row opens it behind the overview, never accepting a pending prompt (macOS: %s)",
      (mac) => {
        linkThree();
        const page = renderOverviewBesideApproval(mac);
        const row = screen.getByRole("button", { name: /^acme\/app #1/ });
        row.focus();
        page.pressCommandEnter(row);
        expect(page.onOpenPullRequest).toHaveBeenCalledWith(OPEN, {
          background: true,
          linkedCount: 3,
        });
        expect(page.submitApproval).not.toHaveBeenCalled();
        // Anywhere else, the chord still answers the prompt.
        page.pressCommandEnter(document.body);
        expect(page.submitApproval).toHaveBeenCalledWith("review-e1", "accept");
        page.restore();
      },
    );

    it.each([true, false])(
      "⌘Enter on the new list opens its active row behind, never accepting a pending prompt (macOS: %s)",
      async (mac) => {
        pageV2.set(true);
        linkThree();
        const page = renderOverviewBesideApproval(mac);
        const grid = screen.getByRole("grid", { name: "Linked pull requests" });
        // Focusing the list makes its first row, the open one that needs you, active.
        act(() => grid.focus());
        page.pressCommandEnter(grid);
        expect(page.onOpenPullRequest).toHaveBeenLastCalledWith(OPEN, {
          background: true,
          linkedCount: 3,
        });
        expect(page.submitApproval).not.toHaveBeenCalled();

        // The filter's one match opens the same way.
        const filter = screen.getByRole("searchbox", { name: "Filter pull requests" });
        fireEvent.change(filter, { target: { value: "Try a new" } });
        page.pressCommandEnter(filter);
        expect(page.onOpenPullRequest).toHaveBeenLastCalledWith(DRAFT, {
          background: true,
          linkedCount: 3,
        });
        expect(page.submitApproval).not.toHaveBeenCalled();

        // Anywhere else, the chord still answers the prompt.
        page.pressCommandEnter(document.body);
        expect(page.submitApproval).toHaveBeenCalledWith("review-e1", "accept");
        page.restore();
      },
    );

    it.each([true, false])(
      "⇧⌘-click or ⇧⌘Enter on the new list opens or selects the row's tab in front (macOS: %s)",
      (mac) => {
        pageV2.set(true);
        linkThree();
        const page = renderOverviewBesideApproval(mac);
        const grid = screen.getByRole("grid", { name: "Linked pull requests" });
        const title = grid.querySelector<HTMLElement>(`a.pr-list-link[href="${OPEN}"]`)!;
        fireEvent.click(
          title,
          mac ? { metaKey: true, shiftKey: true } : { ctrlKey: true, shiftKey: true },
        );
        expect(page.onOpenPullRequest).toHaveBeenLastCalledWith(OPEN, {
          background: false,
          foreground: true,
          linkedCount: 3,
        });
        page.onOpenPullRequest.mockClear();
        act(() => grid.focus());
        fireEvent.keyDown(
          grid,
          mac
            ? { key: "Enter", metaKey: true, shiftKey: true }
            : { key: "Enter", ctrlKey: true, shiftKey: true },
        );
        expect(page.onOpenPullRequest).toHaveBeenCalledTimes(1);
        expect(page.onOpenPullRequest).toHaveBeenLastCalledWith(OPEN, {
          background: false,
          foreground: true,
          linkedCount: 3,
        });
        page.restore();
      },
    );

    it.each([true, false])(
      "Escape on the new list clears its own selection and chip before a visible prompt hears it (macOS: %s)",
      (mac) => {
        pageV2.set(true);
        linkThree();
        const page = renderOverviewBesideApproval(mac);
        function VisiblePrompt() {
          useApprovalPromptOnScreen("review-e1", true);
          return null;
        }
        const prompt = render(<VisiblePrompt />);
        const grid = screen.getByRole("grid", { name: "Linked pull requests" });
        const list = grid.closest("main")!;
        expect(list).not.toHaveAttribute("data-escape-owner");

        const firstRow = within(grid).getAllByRole("row", { name: /Retry uploads/ })[0]!;
        fireEvent.click(within(firstRow).getByRole("checkbox"));
        expect(list).toHaveAttribute("data-escape-owner");
        fireEvent.keyDown(grid, { key: "Escape" });
        expect(screen.queryByRole("toolbar", { name: "Selected pull requests" })).toBeNull();

        fireEvent.click(screen.getByRole("button", { name: "Open (1)" }));
        expect(list).toHaveAttribute("data-escape-owner");
        fireEvent.keyDown(grid, { key: "Escape" });
        expect(screen.getByRole("button", { name: "All (3)" })).toHaveAttribute(
          "aria-pressed",
          "true",
        );
        expect(page.submitApproval).not.toHaveBeenCalled();

        // With nothing of its own to clear, Escape declines the prompt as before.
        expect(list).not.toHaveAttribute("data-escape-owner");
        fireEvent.keyDown(grid, { key: "Escape" });
        expect(page.submitApproval).toHaveBeenCalledWith("review-e1", "decline");
        prompt.unmount();
        page.restore();
      },
    );

    it("the overview's row menu and ⌘K offer Open in new tab, which opens behind it", async () => {
      const user = userEvent.setup();
      linkThree();
      const mode = overview();
      renderInTab(LIST, mode);
      await user.click(screen.getByRole("button", { name: "Actions for acme/app #1" }));
      await user.click(await screen.findByRole("menuitem", { name: "Open in new tab" }));
      expect(mode.onOpenPullRequest).toHaveBeenLastCalledWith(OPEN, {
        background: true,
        linkedCount: 3,
      });
      const row = paletteGroup("pull-requests:conv_1")!.commands.find((command) =>
        command.label.endsWith("in a new tab"),
      );
      expect(row).toBeDefined();
    });

    it("the overview lists even one pull request, and stays a list when the count drops to one", () => {
      const { prs, livePrFor } = linkThree();
      respondWith([prs[0]!], livePrFor);
      const { reports } = renderInTab(LIST, overview());
      expect(screen.getByRole("button", { name: /^acme\/app #1/ })).toBeVisible();
      expect(screen.queryByRole("tab", { name: "Summary" })).toBeNull();
      expect(reports.some((view) => view.showingDetails || view.selected)).toBe(false);
    });

    it("the overview stays a list when the session goes from two pull requests to one", () => {
      const { prs, livePrFor } = linkThree();
      respondWith(prs.slice(0, 2), livePrFor);
      const { reports, rerender } = renderInTab(LIST, overview());
      expect(screen.getByText("Ship docs")).toBeVisible();
      respondWith([prs[0]!], livePrFor);
      rerender();
      expect(screen.getByRole("button", { name: /^acme\/app #1/ })).toBeVisible();
      expect(screen.queryByText("Ship docs")).toBeNull();
      expect(screen.queryByRole("tab", { name: "Summary" })).toBeNull();
      expect(reports.some((view) => view.showingDetails)).toBe(false);
    });

    it("on a host that reports only its branch's pull request, the overview lists that one", () => {
      state.storesPrs = false;
      vi.mocked(usePullRequestInfo).mockImplementation(() => state.info as never);
      const mode = overview();
      renderInTab(LIST, mode);
      expect(screen.getByText("chore: dummy PR")).toBeVisible();
      expect(screen.queryByRole("tab", { name: "Summary" })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: /^(?!Actions for).*#6000/ }));
      // The branch's PR is the only one the list shows, so opening it folds the list too.
      expect(mode.onOpenPullRequest).toHaveBeenCalledWith("https://example.com/pr/6000", {
        background: false,
        linkedCount: 1,
      });
    });

    describe("an empty list is the truth, whatever pull request is still cached", () => {
      const STALE = "chore: dummy PR";
      const tracking = (prs: PullRequestAssociation[]) => {
        const info = {
          ...state.info!,
          data: { ...state.info!.data!, tracking_available: true, prs },
        };
        vi.mocked(usePullRequestInfo).mockImplementation(() => info as never);
      };
      const expectEmptyOverview = () => {
        expect(screen.queryByText(STALE)).toBeNull();
        expect(screen.queryByRole("tab", { name: "Summary" })).toBeNull();
      };

      it("from a tracking host, before any stored list has loaded", () => {
        state.storesPrs = false;
        tracking([]);
        renderInTab(LIST, overview());
        expectEmptyOverview();
      });

      it("once the server's synced, empty stored list arrives", async () => {
        state.stored.conv_1 = [];
        tracking([]);
        renderInTab(LIST, overview());
        await waitFor(() => expect(state.storedFetches).toBeGreaterThan(0));
        expectEmptyOverview();
      });

      it("after the last pull request is unlinked", () => {
        state.storesPrs = false;
        tracking([
          {
            url: "https://example.com/pr/6000",
            host: "example.com",
            repository: "acme/app",
            number: 6000,
            title: STALE,
            relationship: "created",
          },
        ]);
        const { rerender } = renderInTab(LIST, overview());
        expect(screen.getByText(STALE)).toBeVisible();
        tracking([]);
        rerender();
        expectEmptyOverview();
      });
    });

    it("with no pull requests, the overview shows its empty state, never details", () => {
      state.storesPrs = false;
      const info = { ...state.info!, data: { ...state.info!.data!, pr: null } };
      vi.mocked(usePullRequestInfo).mockImplementation(() => info as never);
      renderInTab(LIST, overview());
      expect(screen.queryByRole("tab", { name: "Summary" })).toBeNull();
      expect(screen.queryByRole("list", { name: "Open" })).toBeNull();
    });

    it("the overview never moves to a pull request the link picker just attached", () => {
      linkThree();
      const { reports } = renderInTab(LIST, overview());
      act(() =>
        usePullRequestAttachStore.setState({ lastAttached: { sessionId: "conv_1", url: MERGED } }),
      );
      expect(reports).toEqual([]);
      expect(screen.getByRole("list", { name: "Open" })).toBeVisible();
    });

    it("a pull request's tab shows only it, with Pull requests back to all of them", () => {
      linkThree();
      const onShowAllPullRequests = vi.fn();
      const { reports } = renderInTab(
        { selected: MERGED, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests, onOpenInNewTab: vi.fn() },
      );
      expect(screen.getByRole("tab", { name: "Summary" })).toBeVisible();
      expect(screen.queryByRole("button", { name: /All pull requests/ })).toBeNull();
      act(() =>
        usePullRequestAttachStore.setState({ lastAttached: { sessionId: "conv_1", url: OPEN } }),
      );
      fireEvent.click(screen.getByRole("button", { name: "Pull requests" }));
      expect(onShowAllPullRequests).toHaveBeenCalledTimes(1);
      expect(reports.every((view) => view.selected === MERGED)).toBe(true);
    });

    it("on the new page, lays out a pull request still reading from what its list knows", () => {
      pageV2.set(true);
      vi.mocked(usePullRequestInfo).mockReturnValue({
        isLoading: true,
        error: null,
        isFetching: true,
        data: undefined,
      } as never);
      const onShowAllPullRequests = vi.fn();
      const preview = {
        url: MERGED,
        repository: "acme/app",
        number: 2,
        title: "Ship docs",
        state: "MERGED",
        isDraft: false,
        author: "ada",
      };
      renderInTab(
        { selected: MERGED, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests, preview },
      );
      // The header and title the details will show, where they'll show them.
      expect(screen.getByLabelText("acme/app #2")).toBeVisible();
      expect(screen.getByRole("heading", { level: 1, name: "Ship docs" })).toBeVisible();
      expect(screen.getByText("Opened by ada")).toBeVisible();
      // The sections wait for the read, which is announced instead of a spinner.
      expect(screen.getByRole("tab", { name: "Overview" })).toBeDisabled();
      expect(screen.getByRole("status")).toHaveTextContent("Loading pull request…");
      expect(screen.queryByText("Loading pull requests…")).toBeNull();
      // One way back, in the header it stands in for.
      const back = screen.getAllByRole("button", { name: "Pull requests" });
      expect(back).toHaveLength(1);
      fireEvent.click(back[0]!);
      expect(onShowAllPullRequests).toHaveBeenCalledTimes(1);
    });

    it("on the new page, shows no other pull request's preview while one reads", () => {
      pageV2.set(true);
      vi.mocked(usePullRequestInfo).mockReturnValue({
        isLoading: true,
        error: null,
        isFetching: true,
        data: undefined,
      } as never);
      const preview = {
        url: OPEN,
        repository: "acme/app",
        number: 1,
        title: "Retry uploads",
        state: "OPEN",
        isDraft: false,
        author: "ada",
      };
      renderInTab(
        { selected: MERGED, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests: vi.fn(), preview },
      );
      expect(screen.getByText("Loading pull requests…")).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "Retry uploads" })).toBeNull();
    });

    it("on the new page, leads its header with the way back instead of a row of its own", () => {
      pageV2.set(true);
      linkThree();
      const onShowAllPullRequests = vi.fn();
      renderInTab(
        { selected: MERGED, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests, onOpenInNewTab: vi.fn() },
      );
      expect(screen.getByRole("tab", { name: "Overview" })).toBeVisible();
      // One way back, in the page's header beside the repository link.
      const back = screen.getAllByRole("button", { name: "Pull requests" });
      expect(back).toHaveLength(1);
      expect(back[0]).toHaveAttribute("title", "Show all pull requests");
      // The way back leads the same group as the repository link.
      const identity = back[0]!.closest(".pr-page-header-identity")!;
      expect(within(identity as HTMLElement).getByRole("link", { name: /^Open / })).toBeVisible();
      fireEvent.click(back[0]!);
      expect(onShowAllPullRequests).toHaveBeenCalledTimes(1);
      // Linking another pull request belongs to the list and the overview, not this one's header,
      // whose menu refreshes this pull request alone.
      expect(screen.getByRole("button", { name: "Pull Request Actions" })).toBeEnabled();
      expect(screen.queryByRole("button", { name: "Refresh Pull Request" })).toBeNull();
      expect(screen.queryByRole("button", { name: /^Link a PR/ })).toBeNull();
    });

    it("on the new page, offers no Refresh of its own where its host's Refresh covers it", () => {
      pageV2.set(true);
      linkThree();
      const refreshRow = () =>
        Object.values(usePaletteCommandsStore.getState().groups)
          .flatMap((group) => group.commands)
          .find((command) => command.id === "refresh-pull-requests");
      renderInTab(
        { selected: MERGED, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests: vi.fn(), refreshedByHost: true },
      );
      expect(screen.getByRole("tab", { name: "Overview" })).toBeVisible();
      expect(screen.queryByRole("button", { name: "Refresh" })).toBeNull();
      expect(refreshRow()).toBeUndefined();
    });

    it("on the new page, offers removing the merged and closed PRs in ⌘K from a PR's own tab", () => {
      pageV2.set(true);
      const { prs, livePrFor } = linkThree();
      // The server stores each link's last known state.
      respondWith(
        prs.map((pr) => (pr.url === MERGED ? { ...pr, state: "MERGED" } : pr)),
        livePrFor,
      );
      renderInTab(
        { selected: OPEN, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests: vi.fn(), onOpenInNewTab: vi.fn() },
      );
      const rows = Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .filter((command) => command.label.startsWith("Remove merged and closed pull requests"));
      expect(rows.map((command) => command.label)).toEqual([
        "Remove merged and closed pull requests (1)",
      ]);
      expect(isCommandAvailable(rows[0]!.availability)).toBe(true);
    });

    it("removes PRs as they are now: a reopened one stays, one merged since goes", () => {
      pageV2.set(true);
      const { prs, livePrFor } = linkThree();
      // Stored as closed, but the shown PR's own read says it's open again.
      respondWith(
        prs.map((pr) => (pr.url === OPEN ? { ...pr, state: "CLOSED" } : pr)),
        livePrFor,
      );
      const finishedRow = () =>
        Object.values(usePaletteCommandsStore.getState().groups)
          .flatMap((group) => group.commands)
          .find((command) => command.label.startsWith("Remove merged and closed pull requests"));
      renderInTab(
        { selected: OPEN, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests: vi.fn(), onOpenInNewTab: vi.fn() },
      );
      expect(finishedRow()).toBeUndefined();
      cleanup();

      // Stored as open, but the shown PR's own read says it merged.
      respondWith(
        prs.map((pr) => (pr.url === MERGED ? { ...pr, state: "OPEN" } : pr)),
        livePrFor,
      );
      renderInTab(
        { selected: MERGED, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests: vi.fn(), onOpenInNewTab: vi.fn() },
      );
      expect(finishedRow()?.label).toBe("Remove merged and closed pull requests (1)");
      act(() => finishedRow()!.run());
      expect(state.unlink.mutate).toHaveBeenLastCalledWith(MERGED, expect.anything());
    });

    it("on the new page, keeps the header's actions in step with ⌘K through a stable tab mode", () => {
      pageV2.set(true);
      linkThree();
      const tabsMode: PullRequestTabsMode = {
        kind: "pull",
        onShowAllPullRequests: vi.fn(),
        onOpenInNewTab: vi.fn(),
      };
      const { rerender } = renderInTab(
        { selected: OPEN, showingDetails: true, subtab: "summary" },
        tabsMode,
      );
      const removeRow = () =>
        Object.values(usePaletteCommandsStore.getState().groups)
          .flatMap((group) => group.commands)
          .find((command) => /^Remove .* from this session$/.test(command.label));
      // The header keeps Remove in its Pull Request Actions menu, with no standalone button.
      expect(screen.queryByRole("button", { name: "Remove from this session" })).toBeNull();
      const removeItem = () => {
        const actions = screen.queryByRole("button", { name: "Pull Request Actions" });
        if (!actions || actions.hasAttribute("disabled")) return null;
        fireEvent.keyDown(actions, { key: "Enter" });
        const item = screen.queryByRole("menuitem", { name: "Remove from this session" });
        const enabled = !!item && item.getAttribute("aria-disabled") !== "true";
        fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
        return item ? { enabled } : null;
      };
      expect(removeItem()).toEqual({ enabled: true });
      expect(isCommandAvailable(removeRow()!.availability)).toBe(true);

      // An unlink in flight: the menu's item and the ⌘K row both wait.
      state.unlink = { ...state.unlink, isPending: true };
      rerender();
      expect(removeItem()?.enabled ?? false).toBe(false);
      expect(isCommandAvailable(removeRow()!.availability)).toBe(false);

      // Losing edit access takes the item and the row away together.
      state.unlink = { ...state.unlink, isPending: false };
      state.canEdit = false;
      rerender();
      const row = removeRow();
      expect(removeItem()?.enabled ?? false).toBe(!!row && isCommandAvailable(row.availability));
      expect(screen.getAllByRole("button", { name: "Pull requests" })).toHaveLength(1);
    });

    describe("on the Pull Requests page, which doesn't manage the reading session's links", () => {
      const pageMode = (): PullRequestTabsMode => ({
        kind: "pull",
        onShowAllPullRequests: vi.fn(),
        refreshedByHost: true,
        managesSessionLinks: false,
      });
      const removeRows = () =>
        Object.values(usePaletteCommandsStore.getState().groups)
          .flatMap((group) => group.commands)
          .filter((command) => /from this session$/.test(command.label));
      const expectNoSessionLinkControls = () => {
        expect(screen.queryByRole("button", { name: "Remove from this session" })).toBeNull();
        expect(removeRows()).toEqual([]);
        expect(screen.queryByText("This pull request isn't linked to this session")).toBeNull();
        expect(screen.queryByRole("button", { name: "Link it" })).toBeNull();
        expect(screen.queryByRole("button", { name: /^Link a PR/ })).toBeNull();
      };

      it("offers no Remove from this session for a PR the session links", async () => {
        pageV2.set(true);
        const { prs } = linkThree();
        state.stored = { conv_1: prs };
        renderInTab({ selected: OPEN, showingDetails: true, subtab: "summary" }, pageMode());
        expect(await screen.findByRole("tab", { name: "Overview" })).toBeVisible();
        expectNoSessionLinkControls();
      });

      it.each([
        ["names other pull requests", "mismatching"],
        ["is empty", "empty"],
      ])("shows its PR, not Link it, when the session's list %s", async (_case, list) => {
        pageV2.set(true);
        const { prs, livePrFor } = linkThree();
        const stored = list === "empty" ? [] : prs;
        state.stored = { conv_1: stored };
        respondWith(stored, (url) => ({
          ...livePrFor(OPEN),
          url: url ?? CLOSED,
          number: 9,
          title: "Shown anyway",
        }));
        const { client } = renderInTab(
          { selected: CLOSED, showingDetails: true, subtab: "summary" },
          pageMode(),
        );
        // Once the server's list has loaded, it would decide what a session's tab shows.
        await waitFor(() =>
          expect(client.getQueryData(sessionPullRequestsKey("conv_1"))).toBeDefined(),
        );
        expect(await screen.findByRole("tab", { name: "Overview" })).toBeVisible();
        expect(screen.getAllByText("Shown anyway").length).toBeGreaterThan(0);
        expectNoSessionLinkControls();
      });
    });

    it("on the new page, keeps one working way back for a PR that isn't linked, with its info cached", async () => {
      pageV2.set(true);
      const { prs, livePrFor } = linkThree();
      // The server's list names the session's three PRs; this tab's PR isn't one of them.
      state.stored = { conv_1: prs };
      respondWith(prs, (url) => ({ ...livePrFor(OPEN), url: url ?? OPEN, title: "Cached" }));
      const onShowAllPullRequests = vi.fn();
      renderInTab(
        { selected: CLOSED, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests, onOpenInNewTab: vi.fn() },
      );
      expect(
        await screen.findByText("This pull request isn't linked to this session"),
      ).toBeVisible();
      const back = screen.getAllByRole("button", { name: "Pull requests" });
      expect(back).toHaveLength(1);
      fireEvent.click(back[0]!);
      expect(onShowAllPullRequests).toHaveBeenCalledTimes(1);
    });

    it("on the new page, brings the panel's way back when its PR is unlinked while shown", async () => {
      pageV2.set(true);
      const { prs, livePrFor } = linkThree();
      state.stored = { conv_1: prs };
      const onShowAllPullRequests = vi.fn();
      const { client } = renderInTab(
        { selected: MERGED, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests, onOpenInNewTab: vi.fn() },
      );
      expect(await screen.findByRole("tab", { name: "Overview" })).toBeVisible();
      expect(screen.getAllByRole("button", { name: "Pull requests" })).toHaveLength(1);

      // Unlinked elsewhere: the server's list no longer names it.
      const unlinked = prs.filter((pr) => pr.url !== MERGED);
      respondWith(unlinked, livePrFor);
      act(() => {
        client.setQueryData(sessionPullRequestsKey("conv_1"), { prs: unlinked, synced: true });
      });
      expect(
        await screen.findByText("This pull request isn't linked to this session"),
      ).toBeVisible();
      const back = screen.getAllByRole("button", { name: "Pull requests" });
      expect(back).toHaveLength(1);
      fireEvent.click(back[0]!);
      expect(onShowAllPullRequests).toHaveBeenCalledTimes(1);
    });

    it("on the new page, keeps the way back while its pull request is still loading", () => {
      pageV2.set(true);
      linkThree();
      state.info = { data: undefined, isLoading: true, error: null, isFetching: true };
      const onShowAllPullRequests = vi.fn();
      renderInTab(
        { selected: MERGED, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests, onOpenInNewTab: vi.fn() },
      );
      fireEvent.click(screen.getByRole("button", { name: "Pull requests" }));
      expect(onShowAllPullRequests).toHaveBeenCalledTimes(1);
    });

    it("a pull request's tab keeps its way back when its pull request isn't linked", () => {
      linkThree();
      const onShowAllPullRequests = vi.fn();
      renderInTab(
        { selected: CLOSED, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests, onOpenInNewTab: vi.fn() },
      );
      fireEvent.click(screen.getByRole("button", { name: "Pull requests" }));
      expect(onShowAllPullRequests).toHaveBeenCalledTimes(1);
    });

    it("keeps the shown tab's Refresh row while a hidden tab of the same session rerenders and unmounts", () => {
      linkThree();
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const refreshRows = () =>
        Object.values(usePaletteCommandsStore.getState().groups)
          .flatMap((group) => group.commands)
          .filter((command) => command.label === REFRESH_PULL_REQUESTS_LABEL);
      function Tabs({
        shown,
        overviewMounted = true,
      }: {
        shown: "overview" | "pull";
        overviewMounted?: boolean;
      }) {
        return (
          <QueryClientProvider client={client}>
            {overviewMounted && (
              <PullRequestPanel
                conversationId="conv_1"
                active={shown === "overview"}
                view={LIST}
                tabsMode={{ kind: "overview", onOpenPullRequest: () => {} }}
                onViewChange={() => {}}
              />
            )}
            <PullRequestPanel
              conversationId="conv_1"
              active={shown === "pull"}
              view={{ selected: OPEN, showingDetails: true, subtab: "summary" }}
              tabsMode={{ kind: "pull", onShowAllPullRequests: () => {}, onOpenInNewTab: () => {} }}
              onViewChange={() => {}}
            />
          </QueryClientProvider>
        );
      }
      const view = render(<Tabs shown="pull" />);
      expect(refreshRows()).toHaveLength(1);
      view.rerender(<Tabs shown="pull" />);
      expect(refreshRows()).toHaveLength(1);
      view.rerender(<Tabs shown="overview" />);
      expect(refreshRows()).toHaveLength(1);
      view.rerender(<Tabs shown="pull" />);
      expect(refreshRows()).toHaveLength(1);
      // The hidden overview closes: the shown tab's row stays.
      view.rerender(<Tabs shown="pull" overviewMounted={false} />);
      expect(refreshRows()).toHaveLength(1);
      view.unmount();
      expect(refreshRows()).toHaveLength(0);
    });

    it("a pull request's tab keeps its way back while the session's pull requests load", () => {
      vi.mocked(usePullRequestInfo).mockReturnValue({
        isLoading: true,
        error: null,
        isFetching: true,
        data: undefined,
      } as never);
      const onShowAllPullRequests = vi.fn();
      renderInTab(
        { selected: OPEN, showingDetails: true, subtab: "summary" },
        { kind: "pull", onShowAllPullRequests, onOpenInNewTab: vi.fn() },
      );
      fireEvent.click(screen.getByRole("button", { name: "Pull requests" }));
      expect(onShowAllPullRequests).toHaveBeenCalledTimes(1);
    });
  });

  it("shows two-line rows with status, review, and CI at a glance", () => {
    linkThree();
    renderPanel();
    expect(screen.getByRole("heading", { name: "Pull Requests" })).toHaveTextContent(
      "Pull Requests3",
    );
    const open = within(screen.getByRole("list", { name: "Open" }));
    expect(open.getByText("Retry uploads")).toBeVisible();
    // Several repositories, so each row names its own.
    expect(open.getByText("app")).toBeVisible();
    expect(open.getByText("#1")).toBeVisible();
    expect(open.getByText("Needs you")).toBeVisible();
    expect(open.getByText("Review requested")).toBeVisible();
    expect(open.getByRole("button", { name: "All checks passed · 1 of 1 passed" })).toBeVisible();
    const merged = within(screen.getByRole("list", { name: "Merged" }));
    // Finished rows drop their CI result.
    expect(merged.queryByRole("button", { name: /checks/ })).toBeNull();
    expect(screen.getByRole("list", { name: "Draft" })).toHaveTextContent("Draft");
    expect(screen.queryByRole("button", { name: "Merge" })).toBeNull();
  });

  it("filters by text and status chips, and clears from the empty state", async () => {
    const user = userEvent.setup();
    linkThree();
    renderPanel();
    const chips = within(screen.getByRole("group", { name: "Status" }));
    expect(chips.getAllByRole("button").map((chip) => chip.textContent)).toEqual([
      "All3",
      "Needs You1",
      "Open1",
      "Draft1",
      "Merged1",
    ]);
    expect(chips.getByRole("button", { name: "All (3)" })).toHaveAttribute("aria-pressed", "true");

    await user.click(chips.getByRole("button", { name: "Needs You (1)" }));
    expect(chips.getByRole("button", { name: "Needs You (1)" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByText("Retry uploads")).toBeVisible();
    expect(screen.queryByText("Ship docs")).toBeNull();

    await user.click(chips.getByRole("button", { name: "All (3)" }));
    const filter = screen.getByRole("searchbox", { name: "Filter pull requests" });
    await user.type(filter, "layout");
    expect(screen.getByText("Try a new layout")).toBeVisible();
    expect(screen.queryByText("Retry uploads")).toBeNull();

    await user.clear(filter);
    await user.type(filter, "nothing-matches");
    expect(screen.getByText("No matching pull requests")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(filter).toHaveValue("");
    expect(screen.getByText("Ship docs")).toBeVisible();
  });

  it.each([
    ["a session's list offers", true],
    ["the Pull Requests page's list offers no", false],
  ])("%s removal from the row, its menu, its group and ⌘K", async (_case, managesSessionLinks) => {
    const { prs } = linkThree();
    state.stored = { conv_1: prs };
    const onUnlink = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    if (state.access !== null) {
      client.setQueryData(["session", "conv_1"], sessionSnapshot("conv_1", state.access));
    }
    render(
      <QueryClientProvider client={client}>
        <PullRequestListPage
          conversationId="conv_1"
          active
          associations={prs}
          hostOffline={false}
          sandboxAsleep={false}
          onSelect={vi.fn()}
          onUnlink={onUnlink}
          unlinkPending={false}
          managesSessionLinks={managesSessionLinks}
        />
      </QueryClientProvider>,
    );
    const grid = await screen.findByRole("grid", {
      name: managesSessionLinks ? "Linked pull requests" : "Pull requests",
    });
    await waitFor(() => expect(within(grid).getByText("Retry uploads")).toBeVisible());
    const row = within(grid).getByRole("row", { name: /Retry uploads/ });
    expect(!!within(row).queryByRole("button", { name: /from this session$/ })).toBe(
      managesSessionLinks,
    );
    fireEvent.contextMenu(row);
    const menu = await screen.findByRole("menu");
    expect(!!within(menu).queryByRole("menuitem", { name: "Remove from this session" })).toBe(
      managesSessionLinks,
    );
    fireEvent.pointerMove(within(menu).getByRole("menuitem", { name: "Copy" }), {
      pointerType: "mouse",
    });
    expect(await screen.findByRole("menuitem", { name: /^GitHub link/ })).toBeVisible();
    fireEvent.keyDown(menu, { key: "Escape" });
    expect(!!screen.queryByRole("button", { name: "Remove All" })).toBe(managesSessionLinks);
    const removals = Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .filter((command) => /from this session|^Unlink /.test(command.label));
    expect(removals.length > 0).toBe(managesSessionLinks);
  });

  it("copies the new list's selection as the Pull Requests page does", async () => {
    const user = userEvent.setup();
    pageV2.set(true);
    linkThree();
    renderPanel();
    const grid = screen.getByRole("grid", { name: "Linked pull requests" });
    for (const title of ["Retry uploads", "Ship docs"]) {
      fireEvent.click(
        within(within(grid).getByRole("row", { name: new RegExp(title) })).getByRole("checkbox"),
      );
    }
    const bar = screen.getByRole("toolbar", { name: "Selected pull requests" });
    expect(within(bar).getByRole("button", { name: /^Remove/ })).toBeVisible();
    await user.click(within(bar).getByRole("button", { name: "More Actions" }));
    expect(await screen.findByRole("menuitem", { name: "Copy Links" })).toBeVisible();
    expect(screen.getByRole("menuitem", { name: "Copy Branch Names" })).toBeVisible();
    expect(
      Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .map((command) => command.label),
    ).toEqual(expect.arrayContaining(["Copy Links to 2 Pull Requests"]));
    await user.click(screen.getByRole("menuitem", { name: "Copy Links" }));
    expect(await navigator.clipboard.readText()).toBe(`${OPEN}\n${MERGED}`);
  });

  it("shows the new list while the page flag is on, and switches live either way", () => {
    pageV2.set(true);
    linkThree();
    renderPanel();
    const grid = screen.getByRole("grid", { name: "Linked pull requests" });
    expect(screen.getByRole("heading", { level: 1, name: "Pull Requests" })).toBeVisible();
    // One header line: the list's own, holding the panel's actions.
    expect(within(grid.closest("main")!).getByRole("button", { name: "Refresh" })).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Refresh" })).toHaveLength(1);
    expect(within(grid).getByText("Retry uploads")).toBeVisible();

    act(() => pageV2.set(false));
    expect(screen.queryByRole("grid", { name: "Linked pull requests" })).toBeNull();
    expect(screen.getByRole("button", { name: "All (3)" })).toBeVisible();
    expect(screen.getByText("Retry uploads")).toBeVisible();

    act(() => pageV2.set(true));
    expect(screen.getByRole("grid", { name: "Linked pull requests" })).toBeVisible();
  });

  it("starts the diff's highlighting workers from either list, before any PR opens", () => {
    linkThree();
    warmDiffWorkers.mockClear();
    renderPanel();
    expect(screen.getByRole("button", { name: "All (3)" })).toBeVisible();
    expect(screen.queryByRole("tablist", { name: "Pull request" })).toBeNull();
    expect(warmDiffWorkers).toHaveBeenCalled();

    warmDiffWorkers.mockClear();
    act(() => pageV2.set(true));
    expect(screen.getByRole("grid", { name: "Linked pull requests" })).toBeVisible();
    expect(screen.queryByRole("tablist", { name: "Pull request" })).toBeNull();
    expect(warmDiffWorkers).toHaveBeenCalled();
  });

  it("offers to resume a sleeping sandbox above the new list, and drops it once awake", () => {
    pageV2.set(true);
    linkThree();
    state.sandboxAsleep = true;
    const { rerender } = renderPanel();
    const list = screen.getByRole("grid", { name: "Linked pull requests" }).closest("main")!;
    expect(within(list).getByRole("button", { name: "Resume sandbox" })).toBeVisible();

    state.sandboxAsleep = false;
    rerender(<PullRequestPanel conversationId="conv_1" />);
    expect(within(list).queryByRole("button", { name: "Resume sandbox" })).toBeNull();
  });

  it("focuses the filter with / and leaves it with Escape", async () => {
    const user = userEvent.setup();
    linkThree();
    renderPanel();
    const filter = screen.getByRole("searchbox", { name: "Filter pull requests" });
    screen.getByRole("button", { name: /^acme\/app #1/ }).focus();
    await user.keyboard("/");
    expect(filter).toHaveFocus();
    expect(filter).toHaveValue("");
    await user.keyboard("ship");
    await user.keyboard("{Escape}");
    expect(filter).toHaveValue("");
    expect(filter).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(filter).not.toHaveFocus();
  });

  it("collapses groups and unlinks a finished group together", async () => {
    const user = userEvent.setup();
    linkThree();
    renderPanel();
    await user.click(screen.getByRole("button", { name: "Open group (1)" }));
    expect(screen.queryByText("Retry uploads")).toBeNull();
    expect(screen.getByRole("button", { name: "Open group (1)" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    await user.click(screen.getByRole("button", { name: "Open group (1)" }));
    expect(screen.getByText("Retry uploads")).toBeVisible();

    // Only finished groups offer Unlink all.
    expect(screen.getAllByRole("button", { name: "Unlink all" })).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Unlink all" }));
    expect(state.unlink.mutate).toHaveBeenCalledExactlyOnceWith(MERGED, expect.any(Object));
  });

  it("unlinks every finished PR in one batch", async () => {
    const user = userEvent.setup();
    const { prs, livePrFor } = linkThree();
    respondWith(
      [
        ...prs,
        {
          url: CLOSED,
          host: "github.com",
          repository: "acme/web",
          number: 4,
          relationship: "created",
          state: "CLOSED",
        },
      ],
      livePrFor,
    );
    renderPanel();
    await user.click(screen.getByRole("button", { name: "Actions for acme/app #1" }));
    expect(screen.queryByRole("menuitem", { name: /Unlink merged/ })).toBeNull();
    await user.keyboard("{Escape}");
    const commands = paletteGroup("pull-requests:conv_1")!.commands;
    act(() => commands.find((c) => c.id === "unlink-finished")!.run());
    // One session-bound run unlinks the batch, outside the panel.
    expect(state.unlinkMany).toHaveBeenCalledExactlyOnceWith([MERGED, CLOSED]);
    expect(state.unlink.mutate).not.toHaveBeenCalled();
  });

  describe("saying how a removal ended, once the list it started from is gone", () => {
    const FINISHED = [MERGED, CLOSED];
    /** The finished PRs beside `open`, with GitHub holding each unlink until it's answered. */
    function finishedBeside(open: string[]) {
      const prs: PullRequestAssociation[] = [...open, ...FINISHED].map((url, index) => ({
        url,
        host: "github.com",
        repository: url.split("/").slice(3, 5).join("/"),
        number: index + 1,
        relationship: "created",
        state: url === MERGED ? "MERGED" : url === CLOSED ? "CLOSED" : "OPEN",
      }));
      const base = state.info!.data!;
      const live = (url: string | undefined) =>
        url ? { ...base.pr!, url, state: prs.find((pr) => pr.url === url)?.state } : undefined;
      respondWith(prs, live as never);
      const answers: (() => void)[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
          init?.method === "DELETE"
            ? new Promise<Response>((resolve) => {
                answers.push(() =>
                  resolve(
                    new Response(
                      JSON.stringify({ object: "list", data: [], last_id: null, has_more: false }),
                      { status: 200 },
                    ),
                  ),
                );
              })
            : // The run's read of the list once its unlinks answered: only the open PRs are left.
              Promise.resolve(
                new Response(
                  JSON.stringify({
                    object: "list",
                    data: prs.filter((pr) => pr.state === "OPEN"),
                    last_id: null,
                    has_more: false,
                    synced: true,
                  }),
                  { status: 200 },
                ),
              ),
        ),
      );
      // The panel's batch is the real, session-bound run, in the client it renders with.
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      state.unlinkMany = vi.fn((urls: readonly string[]) =>
        startSessionUnlinkRun(
          client,
          { ...FALLBACK_SERVER_INFO, session_pull_requests: true },
          "conv_1",
          urls,
        ),
      );
      return { prs, live, answers, client };
    }
    /** Run the panel's ⌘K removal of every finished PR. */
    function removeFinished() {
      const command = paletteGroup("pull-requests:conv_1")!.commands.find(
        (each) => each.id === "unlink-finished",
      )!;
      act(() => command.run());
    }
    const announcer = () => document.querySelector('[data-testid="change-announcer"]');

    afterEach(() => {
      vi.unstubAllGlobals();
    });

    it("in the overview, whose list leaves when its last rows do", async () => {
      const { answers, live, client } = finishedBeside([]);
      const onOpenPullRequest = vi.fn();
      const tree = () => (
        <QueryClientProvider client={client}>
          <PullRequestPanel
            conversationId="conv_1"
            view={{ showingDetails: false, subtab: "summary" }}
            tabsMode={{ kind: "overview", onOpenPullRequest }}
            onViewChange={() => {}}
          />
        </QueryClientProvider>
      );
      const view = render(tree());
      expect(screen.getByRole("list", { name: "Merged" })).toBeVisible();
      removeFinished();
      await waitFor(() => expect(answers).toHaveLength(2));
      await waitFor(() => expect(announcer()).toHaveTextContent("Removing 2 pull requests…"));

      // The list goes as the rows leave, before the run has ended.
      respondWith([], live as never);
      view.rerender(tree());
      expect(screen.queryByRole("list", { name: "Merged" })).toBeNull();
      await act(async () => {
        for (const answer of answers) answer();
        await state.unlinkMany.mock.results[0]!.value;
      });
      await waitFor(() => expect(announcer()).toHaveTextContent("Removed 2 pull requests."));
      expect(announcer()!.isConnected).toBe(true);
      // An ordinary render afterwards says nothing more.
      view.rerender(tree());
      await act(async () => {
        await Promise.resolve();
      });
      expect(document.querySelectorAll('[data-testid="change-announcer"]')).toHaveLength(1);
      expect(announcer()).toHaveTextContent("Removed 2 pull requests.");
    });

    it("in Classic, whose list gives way to the one PR left", async () => {
      const { answers, prs, live, client } = finishedBeside([OPEN]);
      const { rerender } = render(<PullRequestPanel conversationId="conv_1" />, {
        wrapper: ({ children }) => (
          <QueryClientProvider client={client}>{children}</QueryClientProvider>
        ),
      });
      expect(screen.getByRole("list", { name: "Merged" })).toBeVisible();
      removeFinished();
      await waitFor(() => expect(answers).toHaveLength(2));

      respondWith(
        prs.filter((pr) => pr.url === OPEN),
        live as never,
      );
      rerender(<PullRequestPanel conversationId="conv_1" />);
      expect(screen.queryByRole("list", { name: "Merged" })).toBeNull();
      await act(async () => {
        answers[0]!();
        answers[1]!();
        // The run's remaining list, and the session's list as read, keep the open PR.
        await expect(state.unlinkMany.mock.results[0]!.value).resolves.toEqual([
          expect.objectContaining({ url: OPEN }),
        ]);
      });
      await waitFor(() => expect(announcer()).toHaveTextContent("Removed 2 pull requests."));
      expect(
        client
          .getQueryData<{ prs: PullRequestAssociation[] }>(sessionPullRequestsKey("conv_1"))
          ?.prs.map((pr) => pr.url),
      ).toEqual([OPEN]);
    });
  });

  it("regroups from the Display menu and remembers the choice", async () => {
    const user = userEvent.setup();
    linkThree();
    const { unmount } = renderPanel();
    await user.click(screen.getByRole("button", { name: "Display options" }));
    await user.click(
      within(screen.getByRole("group", { name: "Group by" })).getByRole("menuitemradio", {
        name: "Repository",
      }),
    );
    expect(screen.getByRole("list", { name: "acme/app" })).toHaveTextContent("Retry uploads");
    expect(screen.getByRole("list", { name: "acme/web" })).toHaveTextContent("Try a new layout");
    unmount();

    renderPanel();
    expect(screen.getByRole("list", { name: "acme/app" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Display options" }));
    await user.click(screen.getByRole("menuitemradio", { name: "None" }));
    expect(screen.queryByRole("button", { name: /group/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Display options" }));
    await user.click(screen.getByRole("menuitem", { name: "Reset to default" }));
    expect(screen.getByRole("list", { name: "Open" })).toBeVisible();
  });

  it("offers each row's actions in ⌘K and merges from there", async () => {
    linkThree();
    renderPanel();
    const group = paletteGroup("pull-requests:conv_1")!;
    expect(group.heading).toBe("Pull requests");
    expect(group.commands.map((command) => command.label)).toEqual([
      "Filter pull requests",
      "Pull request filter",
      "Group pull requests by",
      "Order pull requests by",
      "Remove merged and closed pull requests (1)",
      "Open #1 Retry uploads",
      "Open #1 Retry uploads on GitHub",
      "Review #1 Retry uploads on GitHub",
      "Merge #1 Retry uploads",
      "Copy GitHub link to #1 Retry uploads",
      "Copy pull request number of #1 Retry uploads",
      "Copy title of #1 Retry uploads",
      "Copy branch name of #1 Retry uploads",
      "Remove #1 Retry uploads from this session",
      "Open #2 Ship docs",
      "Open #2 Ship docs on GitHub",
      "Copy GitHub link to #2 Ship docs",
      "Copy pull request number of #2 Ship docs",
      "Copy title of #2 Ship docs",
      "Copy branch name of #2 Ship docs",
      "Remove #2 Ship docs from this session",
      "Open #3 Try a new layout",
      "Open #3 Try a new layout on GitHub",
      "Review #3 Try a new layout on GitHub",
      "Copy GitHub link to #3 Try a new layout",
      "Copy pull request number of #3 Try a new layout",
      "Copy title of #3 Try a new layout",
      "Copy branch name of #3 Try a new layout",
      "Remove #3 Try a new layout from this session",
    ]);
    act(() => group.commands.find((command) => command.label.startsWith("Merge"))!.run());
    expect(await screen.findByText("Merge pull request #1?")).toBeVisible();
  });

  it("filters and regroups from ⌘K's settings as the pills and Display menu do", () => {
    linkThree();
    renderPanel();
    const commands = () => paletteGroup("pull-requests:conv_1")!.commands;
    const choose = (setting: string, value: string) =>
      act(() => void chooseSettingValue(settingIn(commands(), setting), value));
    expectSetting(commands(), "Pull request filter", { current: "All" });
    expectSetting(commands(), "Group pull requests by", {
      current: "Status",
      choices: ["Status", "Repository", "None"],
    });
    expectSetting(commands(), "Order pull requests by", { current: "Status" });
    choose("Group pull requests by", "Repository");
    expect(screen.getByRole("list", { name: "acme/web" })).toHaveTextContent("Try a new layout");
    choose("Pull request filter", "Merged");
    expect(screen.queryByRole("list", { name: "acme/web" })).toBeNull();
    expect(settingValues(commands())["Pull request filter"]).toBe("Merged");
    act(() =>
      commands()
        .find((command) => command.label === "Reset the pull request display")!
        .run(),
    );
    expect(screen.queryByRole("list", { name: "acme/app" })).toBeNull();
  });

  it("withdraws its commands when the list closes", async () => {
    const user = userEvent.setup();
    linkThree();
    const { unmount } = renderPanel();
    await user.click(screen.getByRole("button", { name: /^acme\/app #1/ }));
    // The details' own commands take over from the list's; the header's refresh stays.
    expect(Object.keys(usePaletteCommandsStore.getState().groups).sort()).toEqual([
      expect.stringMatching(/^pull-request-panel:conv_1:/),
      expect.stringMatching(/^pull-request:conv_1:/),
    ]);
    unmount();
  });

  it("moves between unresolved conversations from ⌘K, opening the Changes tab first", () => {
    const calls: string[] = [];
    const props = {
      source: "pull-request-threads:conv_1:pr",
      unresolvedCount: 2,
      openChanges: () => calls.push("changes"),
      navigate: (direction: number) => calls.push(`navigate ${direction}`),
    };
    const view = render(<UnresolvedConversationPaletteCommands {...props} active />);
    const commands = () => paletteGroup("pull-request-threads:conv_1:pr")?.commands ?? [];
    expect(commands().map((command) => command.label)).toEqual([
      PREVIOUS_UNRESOLVED_CONVERSATION_LABEL,
      NEXT_UNRESOLVED_CONVERSATION_LABEL,
    ]);
    act(() => commands()[1]!.run());
    act(() => commands()[0]!.run());
    expect(calls).toEqual(["changes", "navigate 1", "changes", "navigate -1"]);

    view.rerender(<UnresolvedConversationPaletteCommands {...props} active unresolvedCount={0} />);
    expect(commands()).toEqual([]);
    view.rerender(<UnresolvedConversationPaletteCommands {...props} active={false} />);
    expect(commands()).toEqual([]);
  });

  it("offers no commands while the rail hides the list", () => {
    linkThree();
    const { rerender } = renderPanel(false, false);
    expect(screen.getByRole("list", { name: "Pull requests" })).toBeInTheDocument();
    expect(usePaletteCommandsStore.getState().groups).toEqual({});
    rerender(<PullRequestPanel conversationId="conv_1" active />);
    expect(paletteGroup("pull-requests:conv_1")).toBeDefined();
  });

  it("decides Needs You with each PR's own account, never the selected PR's", () => {
    const { prs, livePrFor } = linkThree();
    // The selected PR's account is "viewer". The layout PR, now ready for review, asks
    // "viewer" too, but its own provider reports no account, so it can't claim you.
    const layout = {
      ...livePrFor(DRAFT),
      is_draft: false,
      reviewers: [{ login: "viewer", kind: "user" as const, state: "PENDING" as const }],
    };
    respondWith(prs, (url) => (url === DRAFT ? layout : livePrFor(url)));
    const base = vi.mocked(usePullRequestInfo).getMockImplementation()!;
    const infos = new Map<string, unknown>();
    vi.mocked(usePullRequestInfo).mockImplementation((id, options) => {
      const key = options?.prUrl ?? "";
      if (!infos.has(key)) {
        const info = base(id, options) as { data: PullRequestInfo };
        const account = key === DRAFT ? null : "viewer";
        infos.set(key, {
          ...info,
          data: { ...info.data, auth: { ...info.data.auth!, selected_account: account } },
        });
      }
      return infos.get(key) as never;
    });
    renderPanel();
    expect(
      within(screen.getByRole("list", { name: "Open" })).getAllByText("Needs you"),
    ).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Needs You (1)" })).toBeVisible();
    const needsYou = screen.getByText("Needs you").closest("li")!;
    expect(within(needsYou).getByText("Retry uploads")).toBeVisible();
  });

  it("disables unlink actions everywhere while an unlink is in flight", async () => {
    const user = userEvent.setup();
    state.unlink.isPending = true;
    linkThree();
    renderPanel();
    expect(screen.getByRole("button", { name: "Unlink all" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Actions for acme/app #1" }));
    expect(screen.getByRole("menuitem", { name: "Remove from this session" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    await user.keyboard("{Escape}");
    const commands = paletteGroup("pull-requests:conv_1")!.commands;
    const removals = commands.filter(
      (command) =>
        command.label.startsWith("Unlink") ||
        command.label.startsWith("Remove merged") ||
        command.label.endsWith("this session"),
    );
    expect(removals).toHaveLength(4);
    expect(removals.every((command) => command.availability?.available === false)).toBe(true);
    expect(removals[0].availability).toEqual({
      available: false,
      reason: "Wait for the pull request being removed to finish.",
    });
    expect(commands.find((command) => command.id === "filter")!.availability).toBeUndefined();
  });

  it("lists the same actions in a row's menu and its context menu", async () => {
    const user = userEvent.setup();
    linkThree();
    renderPanel();
    const menuLabels = () =>
      screen.getAllByRole("menuitem").map((item) => item.textContent?.trim());
    await user.click(screen.getByRole("button", { name: "Actions for acme/app #1" }));
    const dropdown = menuLabels();
    expect(dropdown).toEqual([
      "Open on GitHub",
      "Copy",
      "Review on GitHub",
      "Merge…",
      "Remove from this session",
    ]);
    // Open with the copies under it, then review, merge and the session's own, each a group.
    expect(screen.getAllByRole("separator")).toHaveLength(3);
    await user.keyboard("{Escape}");

    fireEvent.contextMenu(screen.getByRole("button", { name: /^acme\/app #1/ }));
    expect(menuLabels()).toEqual(dropdown);
    await chooseRowSubmenuItem(user, "Copy", "Pull request number");
    expect(await navigator.clipboard.readText()).toBe("1");
  });

  it("moves focus into the details opened from a row", async () => {
    const user = userEvent.setup();
    linkThree();
    renderPanel();
    screen.getByRole("button", { name: /^acme\/app #1/ }).focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.getByRole("tab", { name: "Summary" })).toHaveFocus());
  });

  it("opens a row's details from the row", async () => {
    const user = userEvent.setup();
    linkThree();
    renderPanel();
    await user.click(screen.getByRole("button", { name: /^acme\/app #1/ }));
    expect(screen.getByRole("tab", { name: "Summary" })).toBeVisible();
    expect(screen.queryByRole("list", { name: "Pull requests" })).toBeNull();
  });

  it("says when a copy fails", async () => {
    const user = userEvent.setup();
    linkThree();
    renderPanel();
    const failed = vi.spyOn(toast, "error");
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
    await user.click(screen.getByRole("button", { name: "Actions for acme/app #1" }));
    await chooseRowSubmenuItem(user, "Copy", "GitHub link");
    await waitFor(() => expect(failed).toHaveBeenCalledWith("Couldn’t copy the GitHub link"));
  });

  it("offers no title to copy while a row only knows its number", async () => {
    const user = userEvent.setup();
    const prs = linkThree().prs;
    respondWith(prs, () => undefined);
    renderPanel();
    await user.click(screen.getByRole("button", { name: "Actions for acme/app #1" }));
    expect(await openRowSubmenu(user, "Copy", "Pull request number")).toBeVisible();
    expect(screen.queryByRole("menuitem", { name: "Title" })).toBeNull();
  });

  describe("with a review site", () => {
    withPullRequestIntegrations({ reviewSite: EXAMPLE_REVIEW_SITE });

    it("offers the review site for the PRs it has a page for", async () => {
      const user = userEvent.setup();
      linkThree();
      renderPanel();
      await user.click(screen.getByRole("button", { name: "Actions for acme/app #1" }));
      expect(await openRowSubmenu(user, "Open", "In Example Review")).toHaveAttribute(
        "href",
        "https://review.example.com/acme/app/pull/1",
      );
      await chooseRowSubmenuItem(user, "Copy", "Example Review link");
      expect(await navigator.clipboard.readText()).toBe(
        "https://review.example.com/acme/app/pull/1",
      );
      const labels = paletteGroup("pull-requests:conv_1")!.commands.map((command) => command.label);
      expect(labels).toContain("Copy Example Review link to #1 Retry uploads");
    });
  });

  it("offers the details' PR in ⌘K, and removes the PR it was made for", async () => {
    const user = userEvent.setup();
    linkThree();
    renderPanel();
    await user.click(screen.getByRole("button", { name: /^acme\/app #1/ }));
    // The details' own PR commands, beside the section commands under the same heading.
    const group = () =>
      Object.entries(usePaletteCommandsStore.getState().groups).find(([key]) =>
        key.startsWith("pull-request:conv_1:"),
      )![1];
    const remove = group().commands.find(
      (command) => command.label === "Remove #1 Retry uploads from this session",
    )!;
    expect(group().commands.map((command) => command.label)).toEqual([
      "Refresh #1 Retry uploads",
      "Open #1 Retry uploads on GitHub",
      "Copy GitHub link to #1 Retry uploads",
      "Copy pull request number of #1 Retry uploads",
      "Copy title of #1 Retry uploads",
      "Copy branch name of #1 Retry uploads",
      "Remove #1 Retry uploads from this session",
    ]);
    // A palette row made before the user moved on still acts on its own PR.
    await user.click(screen.getByRole("button", { name: "All pull requests (3)" }));
    act(() => remove.run());
    expect(state.unlink.mutate).toHaveBeenCalledWith(OPEN, expect.any(Object));
  });

  it("keeps read-only viewers to non-editing actions", async () => {
    const user = userEvent.setup();
    state.canEdit = false;
    linkThree();
    renderPanel();
    expect(screen.queryByRole("button", { name: "Unlink all" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Actions for acme/app #1" }));
    expect(screen.queryByRole("menuitem", { name: "Merge…" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Remove from this session" })).toBeNull();
    expect(await openRowSubmenu(user, "Copy", "GitHub link")).toBeVisible();
    const labels = paletteGroup("pull-requests:conv_1")!.commands.map((command) => command.label);
    expect(labels.some((label) => /^(Merge|Unlink)/.test(label))).toBe(false);
  });

  describe("as a table", () => {
    const HOUR_MS = 60 * 60 * 1000;
    const STATS_OPERATIONS = ["account_status", "pull_request_list_stats"];
    let listWidth = 900;
    let githubQuery: ReturnType<typeof vi.fn<(...args: unknown[]) => Promise<unknown>>>;

    function installShell(operations: string[] | null) {
      (window as unknown as Record<string, unknown>).omnigentDesktop = {
        kind: "electron",
        getDesktopFeatures: async () =>
          operations ? { githubFastTrack: true, githubFastTrackOperations: operations } : {},
        githubQuery,
      };
    }

    const statsFor = (number: number, hoursAgo: number, additions: number, deletions: number) => ({
      repository: number === 3 ? "acme/web" : "acme/app",
      number,
      additions,
      deletions,
      changed_files: 3,
      updated_at: new Date(Date.now() - hoursAgo * HOUR_MS).toISOString(),
      head_sha: "head",
    });
    const STATS: Record<number, ReturnType<typeof statsFor>> = {
      // The merged PR changed most recently, the draft longest ago.
      1: statsFor(1, 5, 2200, 14),
      2: statsFor(2, 2, 3, 1),
      3: statsFor(3, 48, 40, 400),
    };

    beforeEach(() => {
      listWidth = 900;
      vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => listWidth);
      githubQuery = vi.fn(async (operation: unknown, _account: unknown, params: unknown) => {
        if (operation !== "pull_request_list_stats") {
          return { ok: false, error: { code: "invalid_request", message: "no" } };
        }
        const { pull_requests } = params as { pull_requests: { number: number }[] };
        return { ok: true, data: { pull_requests: pull_requests.map((pr) => STATS[pr.number]) } };
      });
    });

    afterEach(() => {
      vi.restoreAllMocks();
      delete (window as unknown as Record<string, unknown>).omnigentDesktop;
    });

    const statsCalls = () =>
      githubQuery.mock.calls.filter(([operation]) => operation === "pull_request_list_stats");
    const headers = () =>
      within(screen.getByRole("table", { name: "Pull requests" }))
        .getAllByRole("columnheader")
        .map((header) => header.textContent);
    const rowTitles = () =>
      within(screen.getByRole("table", { name: "Pull requests" }))
        .getAllByRole("row")
        .filter((row) => row.hasAttribute("data-pull-request-row"))
        .map((row) => within(row).getByRole("button", { name: /^acme/ }).textContent);

    it.each([true, false])(
      "⌘Enter on a focused table row opens it behind the overview, never accepting a pending prompt (macOS: %s)",
      async (mac) => {
        installShell(STATS_OPERATIONS);
        linkThree();
        const page = renderOverviewBesideApproval(mac);
        await screen.findByRole("table", { name: "Pull requests" });
        const row = screen.getAllByRole("button", { name: /^acme\/app #1/ })[0]!;
        expect(row).toHaveAttribute("data-row-open");
        row.focus();
        page.pressCommandEnter(row);
        expect(page.onOpenPullRequest).toHaveBeenCalledWith(OPEN, {
          background: true,
          linkedCount: 3,
        });
        expect(page.submitApproval).not.toHaveBeenCalled();
        page.pressCommandEnter(document.body);
        expect(page.submitApproval).toHaveBeenCalledWith("review-e1", "accept");
        page.restore();
      },
    );

    it("shows every column on a wide list, most recently updated first, in one read", async () => {
      installShell(STATS_OPERATIONS);
      localStorage.setItem(
        "omnigent:pull-request-list-display",
        JSON.stringify({ grouping: "none" }),
      );
      linkThree();
      renderPanel();
      expect(await screen.findByText("+2.2k")).toBeVisible();
      expect(headers()).toEqual([
        "#",
        "Title",
        "Status",
        "CI",
        "Diff",
        "Reviewers",
        "Updated",
        "Actions",
      ]);
      expect(
        screen.getByRole("button", { name: "Sort by Updated" }).closest("[role=columnheader]"),
      ).toHaveAttribute("aria-sort", "descending");
      expect(rowTitles()).toEqual(["Ship docs", "Retry uploads", "Try a new layout"]);

      const open = screen
        .getAllByRole("row")
        .find((row) => row.getAttribute("data-pull-request-row") === OPEN)!;
      const cells = within(open);
      expect(cells.getByText("#1")).toBeVisible();
      expect(cells.getByText("Open")).toBeVisible();
      expect(cells.getByRole("img", { name: "Needs you" })).toBeInTheDocument();
      expect(
        cells.getByRole("button", { name: "All checks passed · 1 of 1 passed" }),
      ).toBeVisible();
      expect(
        cells.getByRole("img", { name: "2,200 lines added, 14 removed in 3 files" }),
      ).toHaveTextContent("+2.2k−14");
      expect(cells.getByRole("img", { name: "Reviewers: viewer: Awaiting review" })).toBeVisible();
      expect(cells.getByText("5h")).toBeVisible();

      // One request for the whole list, as each PR's own host account.
      expect(statsCalls()).toHaveLength(1);
      expect(statsCalls()[0][1]).toEqual({ host: "github.com", login: "viewer" });
      expect(statsCalls()[0][2]).toEqual({
        pull_requests: [
          { repository: "acme/app", number: 1 },
          { repository: "acme/app", number: 2 },
          { repository: "acme/web", number: 3 },
        ],
      });
    });

    it("sorts from the column headers and flips on a second click", async () => {
      const user = userEvent.setup();
      installShell(STATS_OPERATIONS);
      localStorage.setItem(
        "omnigent:pull-request-list-display",
        JSON.stringify({ grouping: "none" }),
      );
      linkThree();
      renderPanel();
      await screen.findByText("+2.2k");
      await user.click(screen.getByRole("button", { name: "Sort by Updated" }));
      expect(rowTitles()).toEqual(["Try a new layout", "Retry uploads", "Ship docs"]);
      expect(
        screen.getByRole("button", { name: "Sort by Updated" }).closest("[role=columnheader]"),
      ).toHaveAttribute("aria-sort", "ascending");
      await user.click(screen.getByRole("button", { name: "Sort by Diff" }));
      expect(rowTitles()).toEqual(["Retry uploads", "Try a new layout", "Ship docs"]);
      await user.click(screen.getByRole("button", { name: "Sort by Title" }));
      expect(rowTitles()).toEqual(["Retry uploads", "Ship docs", "Try a new layout"]);
      expect(JSON.parse(localStorage.getItem("omnigent:pull-request-list-display")!)).toEqual({
        grouping: "none",
        order: "title",
        direction: "ascending",
      });
    });

    it("keeps Diff and Updated in a shell without the stats read, saying to update it", async () => {
      installShell(["account_status", "pull_request_commits"]);
      localStorage.setItem(
        "omnigent:pull-request-list-display",
        JSON.stringify({ grouping: "none" }),
      );
      linkThree();
      renderPanel();
      await waitFor(() =>
        expect(
          screen.getAllByRole("img", {
            name: "Diff size unavailable. Update the app to see it here.",
          }),
        ).toHaveLength(3),
      );
      expect(headers()).toEqual([
        "#",
        "Title",
        "Status",
        "CI",
        "Diff",
        "Reviewers",
        "Updated",
        "Actions",
      ]);
      // Without values, the list keeps status order, and never calls the read it lacks.
      expect(rowTitles()).toEqual(["Retry uploads", "Try a new layout", "Ship docs"]);
      expect(githubQuery).not.toHaveBeenCalled();
    });

    it("flips the fallback Status order on the first header click without stats", async () => {
      const user = userEvent.setup();
      localStorage.setItem(
        "omnigent:pull-request-list-display",
        JSON.stringify({ grouping: "none" }),
      );
      linkThree();
      renderPanel();
      const status = await screen.findByRole("button", { name: "Sort by Status" });
      expect(status.closest("[role=columnheader]")).toHaveAttribute("aria-sort", "ascending");
      expect(rowTitles()).toEqual(["Retry uploads", "Try a new layout", "Ship docs"]);
      await user.click(status);
      expect(status.closest("[role=columnheader]")).toHaveAttribute("aria-sort", "descending");
      expect(rowTitles()).toEqual(["Ship docs", "Try a new layout", "Retry uploads"]);
    });

    it("opens a PR from a click on its metadata, but not from its CI or menu", async () => {
      const user = userEvent.setup();
      installShell(STATS_OPERATIONS);
      linkThree();
      renderPanel();
      await user.click(await screen.findByText("+2.2k"));
      expect(await screen.findByRole("button", { name: /All pull requests/ })).toBeVisible();
    });

    it("hides Diff and Updated outside the desktop shell", async () => {
      linkThree();
      renderPanel();
      await act(async () => {});
      expect(headers()).toEqual(["#", "Title", "Status", "CI", "Reviewers", "Actions"]);
    });

    it("keeps the stats columns with the shell's reason when the read fails", async () => {
      installShell(STATS_OPERATIONS);
      githubQuery.mockImplementation(async () => ({
        ok: false,
        error: { code: "query_failed", message: "GitHub couldn't answer." },
      }));
      linkThree();
      renderPanel();
      await waitFor(() => expect(statsCalls()).toHaveLength(1));
      await waitFor(() =>
        expect(
          screen.getAllByRole("img", { name: "Update time unavailable. GitHub couldn't answer." }),
        ).toHaveLength(3),
      );
      expect(headers()).toContain("Diff");
      expect(headers()).toContain("Updated");
      expect(screen.getByText("Retry uploads")).toBeVisible();
    });

    it("reads stats once for the whole list when its rows load at different times", async () => {
      installShell(STATS_OPERATIONS);
      linkThree();
      const loaded = vi.mocked(usePullRequestInfo).getMockImplementation()!;
      let draftLoaded = false;
      // The draft's host answers last; until then its row is still loading.
      vi.mocked(usePullRequestInfo).mockImplementation((id, options) =>
        options?.prUrl === DRAFT && !draftLoaded
          ? ({ isLoading: true, error: null, isFetching: true, data: undefined } as never)
          : loaded(id, options),
      );
      const { rerender } = renderPanel();
      // Long enough for the shell to answer which reads it supports.
      await act(
        () =>
          new Promise<void>((resolve) => {
            setTimeout(resolve, 100);
          }),
      );
      expect(statsCalls()).toHaveLength(0);
      draftLoaded = true;
      rerender(<PullRequestPanel conversationId="conv_1" />);
      await screen.findByText("+2.2k");
      expect(statsCalls()).toHaveLength(1);
      expect(
        (statsCalls()[0][2] as { pull_requests: { number: number }[] }).pull_requests.map(
          (pr) => pr.number,
        ),
      ).toEqual([1, 2, 3]);
    });

    it("keeps the chosen order when Diff or Updated can't sort yet, and sorts once stats arrive", async () => {
      const user = userEvent.setup();
      installShell(STATS_OPERATIONS);
      const answer = githubQuery.getMockImplementation()!;
      githubQuery.mockImplementation(async () => ({
        ok: false,
        error: { code: "query_failed", message: "GitHub couldn't answer." },
      }));
      localStorage.setItem(
        "omnigent:pull-request-list-display",
        JSON.stringify({ grouping: "none" }),
      );
      linkThree();
      renderPanel();
      await waitFor(() => expect(statsCalls()).toHaveLength(1));
      const sortedBy = () =>
        screen.getAllByRole("columnheader").find((header) => header.getAttribute("aria-sort"))
          ?.textContent;
      await user.click(screen.getByRole("button", { name: "Sort by Title" }));
      expect(sortedBy()).toBe("Title");
      const order = rowTitles();
      const tryToSort = async (name: string) => {
        const header = screen.getByRole("button", { name });
        expect(header).toHaveAttribute("aria-disabled", "true");
        expect(header).toHaveAccessibleDescription(
          "Sorting needs diff sizes and update times, which aren't available yet.",
        );
        await user.click(header);
        expect(sortedBy()).toBe("Title");
        expect(rowTitles()).toEqual(order);
      };
      await tryToSort("Sort by Updated");
      await tryToSort("Sort by Diff");
      // Once a read answers, the same header sorts.
      githubQuery.mockImplementation(answer);
      await user.click(screen.getByRole("button", { name: "Refresh" }));
      await screen.findByText("+2.2k");
      const updated = screen.getByRole("button", { name: "Sort by Updated" });
      expect(updated).not.toHaveAttribute("aria-disabled");
      await user.click(updated);
      expect(sortedBy()).toBe("Updated");
    });

    it("replaces the numbers with the reason when a refresh after a good read fails", async () => {
      const user = userEvent.setup();
      installShell(STATS_OPERATIONS);
      linkThree();
      renderPanel();
      expect(await screen.findByText("+2.2k")).toBeVisible();
      githubQuery.mockImplementation(async () => ({
        ok: false,
        error: { code: "query_failed", message: "GitHub couldn't answer." },
      }));
      await user.click(screen.getByRole("button", { name: "Refresh" }));
      await waitFor(() => expect(statsCalls()).toHaveLength(2));
      // Stale numbers would mislead, so the old answer gives way to the reason.
      await waitFor(() => expect(screen.queryByText("+2.2k")).toBeNull());
      expect(
        screen.getAllByRole("img", { name: "Diff size unavailable. GitHub couldn't answer." }),
      ).toHaveLength(3);
      expect(screen.getByText("Retry uploads")).toBeVisible();
    });

    it("re-reads a list of only finished PRs on Refresh, though it doesn't poll them", async () => {
      const user = userEvent.setup();
      installShell(STATS_OPERATIONS);
      const { prs, livePrFor } = linkThree();
      respondWith(prs, (url) => ({ ...livePrFor(url), state: "MERGED" }));
      renderPanel();
      await screen.findByText("+2.2k");
      expect(statsCalls()).toHaveLength(1);
      await user.click(screen.getByRole("button", { name: "Refresh" }));
      await waitFor(() => expect(statsCalls()).toHaveLength(2));
    });

    it("re-reads stats once after a refresh that moves two heads at different times", async () => {
      installShell(STATS_OPERATIONS);
      linkThree();
      const loaded = vi.mocked(usePullRequestInfo).getMockImplementation()!;
      const heads: Record<string, string> = { [OPEN]: "head", [MERGED]: "head", [DRAFT]: "head" };
      const fetching = new Set<string>();
      // One stable object per answer, as React Query's cache would hand out.
      const answers = new Map<string, unknown>();
      vi.mocked(usePullRequestInfo).mockImplementation((id, options) => {
        const url = options?.prUrl ?? OPEN;
        const key = `${url}@${heads[url]}:${fetching.has(url)}`;
        if (!answers.has(key)) {
          const info = loaded(id, options) as unknown as { data: { pr: PullRequest | null } };
          answers.set(key, {
            ...info,
            isFetching: fetching.has(url),
            data: {
              ...info.data,
              pr: info.data.pr && { ...info.data.pr, head_sha: heads[url] },
            },
          });
        }
        return answers.get(key) as never;
      });
      const pause = () =>
        act(
          () =>
            new Promise<void>((resolve) => {
              setTimeout(resolve, 100);
            }),
        );
      const { rerender } = renderPanel();
      await screen.findByText("+2.2k");
      expect(statsCalls()).toHaveLength(1);

      // A host refresh: the open PR's new head lands while the merged PR is still answering.
      fetching.add(MERGED);
      heads[OPEN] = "pushed";
      rerender(<PullRequestPanel conversationId="conv_1" />);
      await pause();
      expect(statsCalls()).toHaveLength(1);
      heads[MERGED] = "pushed";
      fetching.clear();
      rerender(<PullRequestPanel conversationId="conv_1" />);
      await waitFor(() => expect(statsCalls()).toHaveLength(2));
      // GitHub still answers the old heads; the list doesn't ask again for the same heads.
      rerender(<PullRequestPanel conversationId="conv_1" />);
      await pause();
      expect(statsCalls()).toHaveLength(2);
    });

    it("reads once when GitHub already answers a newer head than the host reports", async () => {
      installShell(STATS_OPERATIONS);
      linkThree();
      githubQuery.mockImplementation(async (operation, _account, params) => {
        if (operation !== "pull_request_list_stats") {
          return { ok: false, error: { code: "invalid_request", message: "no" } };
        }
        const { pull_requests } = params as { pull_requests: { number: number }[] };
        return {
          ok: true,
          data: {
            pull_requests: pull_requests.map((pr) => ({ ...STATS[pr.number], head_sha: "newer" })),
          },
        };
      });
      const { rerender } = renderPanel();
      await screen.findByText("+2.2k");
      // Rerenders hand the list fresh arrays, as live host answers do; no host head moved.
      const rerenderAndWait = async () => {
        rerender(<PullRequestPanel conversationId="conv_1" />);
        await act(
          () =>
            new Promise<void>((resolve) => {
              setTimeout(resolve, 100);
            }),
        );
      };
      await rerenderAndWait();
      await rerenderAndWait();
      await rerenderAndWait();
      expect(statsCalls()).toHaveLength(1);
    });

    it("joins the read a stale cache starts after a refresh instead of restarting it", async () => {
      installShell(STATS_OPERATIONS);
      linkThree();
      // GitHub answers the latest heads, a little later, so a read is in flight for a while.
      let githubHead = "head";
      githubQuery.mockImplementation(async (operation, _account, params) => {
        if (operation !== "pull_request_list_stats") {
          return { ok: false, error: { code: "invalid_request", message: "no" } };
        }
        await new Promise<void>((resolve) => {
          setTimeout(resolve, 50);
        });
        const { pull_requests } = params as { pull_requests: { number: number }[] };
        return {
          ok: true,
          data: {
            pull_requests: pull_requests.map((pr) => ({
              ...STATS[pr.number],
              head_sha: githubHead,
            })),
          },
        };
      });
      const loaded = vi.mocked(usePullRequestInfo).getMockImplementation()!;
      let hostHead = "head";
      let fetching = false;
      const answers = new Map<string, unknown>();
      vi.mocked(usePullRequestInfo).mockImplementation((id, options) => {
        const key = `${options?.prUrl ?? OPEN}@${hostHead}:${fetching}`;
        if (!answers.has(key)) {
          const info = loaded(id, options) as unknown as { data: { pr: PullRequest | null } };
          answers.set(key, {
            ...info,
            isFetching: fetching,
            data: { ...info.data, pr: info.data.pr && { ...info.data.pr, head_sha: hostHead } },
          });
        }
        return answers.get(key) as never;
      });
      const pause = () =>
        act(
          () =>
            new Promise<void>((resolve) => {
              setTimeout(resolve, 200);
            }),
        );
      const { client, rerender } = renderPanel();
      await screen.findByText("+2.2k");
      expect(statsCalls()).toHaveLength(1);

      // The cached stats age past the list's 30 seconds, as between polls or on resume.
      for (const query of client
        .getQueryCache()
        .findAll({ queryKey: ["github-fast-track", "pull_request_list_stats"] })) {
        query.setState({ dataUpdatedAt: Date.now() - 31_000 });
      }
      fetching = true;
      hostHead = "pushed";
      githubHead = "pushed";
      rerender(<PullRequestPanel conversationId="conv_1" />);
      await pause();
      expect(statsCalls()).toHaveLength(1);
      fetching = false;
      rerender(<PullRequestPanel conversationId="conv_1" />);
      await pause();
      // The read re-enabling starts already covers the new heads: one read, not two.
      expect(statsCalls()).toHaveLength(2);
    });

    it("doesn't read stats while the panel is hidden", async () => {
      installShell(STATS_OPERATIONS);
      linkThree();
      renderPanel(false, false);
      await act(async () => {});
      expect(statsCalls()).toHaveLength(0);
    });

    it("drops Reviewers, then Diff, then Status text as it narrows, then shows two-line rows", async () => {
      installShell(STATS_OPERATIONS);
      listWidth = 520;
      linkThree();
      const { unmount } = renderPanel();
      await screen.findByText("+2.2k");
      expect(headers()).toEqual(["#", "Title", "Status", "CI", "Diff", "Updated", "Actions"]);
      unmount();

      listWidth = 380;
      const narrow = renderPanel();
      await screen.findByText("5h");
      // The status header narrows to its sort button, which keeps its name.
      expect(screen.getByRole("button", { name: "Sort by Status" })).toBeInTheDocument();
      expect(headers()).toEqual(["#", "Title", "", "CI", "Updated", "Actions"]);
      // The dot keeps its state name for screen readers.
      const open = screen
        .getAllByRole("row")
        .find((row) => row.getAttribute("data-pull-request-row") === OPEN)!;
      expect(within(open).getByText("Open")).toHaveClass("sr-only");
      narrow.unmount();

      listWidth = 300;
      renderPanel();
      await act(async () => {});
      expect(screen.queryByRole("table")).toBeNull();
      expect(
        within(screen.getByRole("list", { name: "Open" })).getByText("Retry uploads"),
      ).toBeVisible();
      // Two-line rows keep the update time, as the reference's compact rows do, but not Diff.
      expect(await screen.findByText("5h")).toBeVisible();
      expect(screen.queryByText("+2.2k")).toBeNull();
    });

    it("says a host named no GitHub account only once that host has answered", async () => {
      installShell(STATS_OPERATIONS);
      linkThree();
      const loaded = vi.mocked(usePullRequestInfo).getMockImplementation()!;
      vi.mocked(usePullRequestInfo).mockImplementation((id, options) => {
        // Still answering: nothing is known about its account yet.
        if (options?.prUrl === DRAFT) {
          return { isLoading: true, error: null, isFetching: true, data: undefined } as never;
        }
        const info = loaded(id, options) as unknown as { data: { auth: object } };
        if (options?.prUrl !== MERGED) return info as never;
        return {
          ...info,
          data: { ...info.data, auth: { ...info.data.auth, selected_account: null } },
        } as never;
      });
      renderPanel();
      const row = (url: string) =>
        screen
          .getAllByRole("row")
          .find((element) => element.getAttribute("data-pull-request-row") === url)!;
      await waitFor(() =>
        expect(
          within(row(MERGED)).getByRole("img", {
            name: "Diff size unavailable. The session's host didn't report a GitHub account.",
          }),
        ).toBeInTheDocument(),
      );
      expect(within(row(DRAFT)).queryByRole("img", { name: /unavailable/ })).toBeNull();
    });

    it("explains a missing update time on two-line rows", async () => {
      installShell(STATS_OPERATIONS);
      githubQuery.mockImplementation(async () => ({
        ok: false,
        error: { code: "query_failed", message: "GitHub couldn't answer." },
      }));
      listWidth = 300;
      linkThree();
      renderPanel();
      await waitFor(() =>
        expect(
          screen.getAllByRole("img", { name: "Update time unavailable. GitHub couldn't answer." }),
        ).toHaveLength(3),
      );
      expect(screen.queryByRole("img", { name: /^Diff size unavailable/ })).toBeNull();
    });

    it("marks the PR the panel last showed when returning to the list", async () => {
      const user = userEvent.setup();
      linkThree();
      renderPanel();
      await user.click(screen.getByRole("button", { name: /^acme\/web #3/ }));
      await user.click(await screen.findByRole("button", { name: /All pull requests/ }));
      expect(screen.getByRole("button", { name: /^acme\/web #3/ })).toHaveAttribute(
        "aria-current",
        "true",
      );
      expect(screen.getByRole("button", { name: /^acme\/app #1/ })).not.toHaveAttribute(
        "aria-current",
      );
    });
  });

  describe("a row's control requests", () => {
    const EDITABLE = { merging: true, label_editing: true, reviewer_editing: true };
    afterEach(() => resetControlRequestsForTests());

    /** The workspace sidebar's list beside its PR: two panels, rows aimed at the PR's. */
    function renderBeside() {
      pageV2.set(true);
      // Each PR's Labels section shows where its PR lists labels, none here.
      state.info!.data!.pr!.labels = [];
      linkThree(EDITABLE);
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      client.setQueryData(["session", "conv_1"], sessionSnapshot("conv_1", 4));
      function Beside() {
        const [selected, setSelected] = useState(OPEN);
        return (
          <>
            <PullRequestPanel
              conversationId="conv_1"
              active
              rowDestination="sidebar-detail"
              view={{ selected, showingDetails: false, subtab: "summary" }}
              onViewChange={() => {}}
              tabsMode={{ kind: "overview", onOpenPullRequest: (url) => setSelected(url) }}
            />
            <PullRequestPanel
              conversationId="conv_1"
              active
              destination="sidebar-detail"
              view={{ selected, showingDetails: true, subtab: "summary" }}
              onViewChange={() => {}}
              tabsMode={{
                kind: "pull",
                onShowAllPullRequests: () => {},
                onOpenInNewTab: () => {},
              }}
            />
          </>
        );
      }
      render(
        <QueryClientProvider client={client}>
          <Beside />
        </QueryClientProvider>,
      );
    }

    it.each([
      ["Edit Labels…", "Filter labels"],
      ["Request Review…", "Filter reviewers"],
    ])("%s on the list's row opens its picker in the PR beside it", async (label, picker) => {
      renderBeside();
      fireEvent.contextMenu(await screen.findByRole("row", { name: /Retry uploads/ }));
      await userEvent.click(screen.getByRole("menuitem", { name: label }));
      expect(await screen.findByRole("combobox", { name: picker })).toBeVisible();
    });

    it("Edit Labels on ⌘K opens the picker in the PR beside the list", async () => {
      renderBeside();
      await screen.findByRole("row", { name: /Retry uploads/ });
      const command = Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .find((candidate) => candidate.label === "Edit Labels on #1 Retry uploads…");
      act(() => command!.run());
      expect(await screen.findByRole("combobox", { name: "Filter labels" })).toBeVisible();
    });

    it("reopens nothing once the whole PR view leaves and comes back", async () => {
      pageV2.set(true);
      // Each PR's Labels section shows where its PR lists labels, none here.
      state.info!.data!.pr!.labels = [];
      linkThree(EDITABLE);
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      client.setQueryData(["session", "conv_1"], sessionSnapshot("conv_1", 4));
      const tree = (mounted: boolean) => (
        <QueryClientProvider client={client}>
          {mounted && (
            <PullRequestPanel
              conversationId="conv_1"
              active
              destination="page-detail"
              view={{ selected: OPEN, showingDetails: true, subtab: "summary" }}
              onViewChange={() => {}}
              tabsMode={{
                kind: "pull",
                onShowAllPullRequests: () => {},
                onOpenInNewTab: () => {},
              }}
            />
          )}
        </QueryClientProvider>
      );
      const view = render(tree(true));
      act(() => requestPullRequestControl(OPEN, "labels", { destination: "page-detail" }, null));
      expect(await screen.findByRole("combobox", { name: "Filter labels" })).toBeVisible();
      view.rerender(tree(false));
      view.rerender(tree(true));
      expect(screen.queryByRole("combobox", { name: "Filter labels" })).toBeNull();
    });
  });
});

describe("Commits tab", () => {
  const PR_URL = "https://github.com/acme/app/pull/6000";
  const COMMITS = [
    {
      sha: "1".repeat(40),
      subject: "Add the parser",
      author_login: "dev",
      author_name: "Dev One",
      authored_at: "2026-11-03T09:00:00Z",
      committed_at: "2026-11-03T09:00:00Z",
      url: "https://github.com/acme/app/commit/111",
      ci_state: "SUCCESS",
    },
    {
      sha: "2".repeat(40),
      subject: "Fix the parser",
      author_login: null,
      author_name: "Pat Example",
      authored_at: "2026-11-03T10:00:00Z",
      committed_at: "2026-11-03T10:00:00Z",
      url: "https://github.com/acme/app/commit/222",
      ci_state: "FAILURE",
    },
  ];

  type Answer =
    { ok: true; data: unknown } | { ok: false; error: { code: string; message: string } };
  let githubQuery: ReturnType<typeof vi.fn<(...args: unknown[]) => Promise<Answer>>>;

  function installShell(features: Record<string, unknown> | null, withQuery = true) {
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      getDesktopFeatures: async () => features,
      ...(withQuery ? { githubQuery } : {}),
    };
  }

  function withAccount(selectedAccount: string | null) {
    const data = state.info!.data!;
    state.info = {
      ...state.info!,
      data: {
        ...data,
        selected_pr_url: PR_URL,
        pr: { ...data.pr!, url: PR_URL, head_sha: "f".repeat(40), base_sha: "e".repeat(40) },
        auth: {
          authenticated: true,
          hint: null,
          cli: { name: "gh", available: true },
          accounts: [{ login: "dev", active: true, state: "success", host: "github.com" }],
          selected_account: selectedAccount,
        },
      },
    };
  }

  beforeEach(() => {
    githubQuery = vi.fn(async (operation: unknown) => {
      if (operation === "account_status") return { ok: true, data: { login: "dev" } };
      if (operation === "pull_request_commits") {
        return { ok: true, data: { total_count: 2, commits: COMMITS } };
      }
      if (operation === "commit_diff") return { ok: true, data: { sha: "x", patch: "PATCH" } };
      return { ok: false, error: { code: "invalid_request", message: "no" } };
    });
    state.parsedFiles = [
      { name: "src/parser.ts", type: "change", hunks: [] } as (typeof state.parsedFiles)[number],
    ];
    withAccount("dev");
    // Earlier suites swap in their own info implementation; read this suite's state.
    vi.mocked(usePullRequestInfo).mockImplementation(() => state.info as never);
  });

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  });

  const CAPABLE = {
    githubFastTrack: true,
    githubFastTrackOperations: ["account_status", "pull_request_commits", "commit_diff"],
  };

  async function openCommits() {
    fireEvent.mouseDown(await screen.findByRole("tab", { name: /^Commits/ }));
    return screen.findByRole("list", { name: "Commits" });
  }

  it("stays hidden outside the desktop shell", async () => {
    renderPanel();
    await screen.findByRole("tab", { name: "Changes" });
    expect(screen.queryByRole("tab", { name: /^Commits/ })).toBeNull();
  });

  it("stays hidden in an older shell without the feature or the bridge method", async () => {
    installShell({});
    const { unmount } = renderPanel();
    await screen.findByRole("tab", { name: "Changes" });
    await act(async () => {});
    expect(screen.queryByRole("tab", { name: /^Commits/ })).toBeNull();
    unmount();
    installShell({ githubFastTrack: true }, false);
    renderPanel();
    await act(async () => {});
    expect(screen.queryByRole("tab", { name: /^Commits/ })).toBeNull();
    expect(githubQuery).not.toHaveBeenCalled();
  });

  it("stays hidden when the host doesn't report its account", async () => {
    installShell(CAPABLE);
    withAccount(null);
    renderPanel();
    await act(async () => {});
    expect(screen.queryByRole("tab", { name: /^Commits/ })).toBeNull();
    expect(githubQuery).not.toHaveBeenCalled();
  });

  it("lists the PR's commits as the host's account, with author, SHA, and CI state", async () => {
    installShell(CAPABLE);
    renderPanel();
    expect(await screen.findByRole("tab", { name: "Commits 2" })).toBeInTheDocument();
    const list = await openCommits();
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Add the parser");
    expect(rows[0]).toHaveTextContent("dev");
    expect(rows[0]).toHaveTextContent("1111111");
    expect(within(rows[0]).getByRole("img", { name: "Checks passed" })).toBeInTheDocument();
    expect(rows[1]).toHaveTextContent("Pat Example");
    expect(within(rows[1]).getByRole("img", { name: "Checks failed" })).toBeInTheDocument();
    expect(githubQuery).toHaveBeenCalledWith(
      "pull_request_commits",
      { host: "github.com", login: "dev" },
      { repository: "acme/app", number: 6000 },
    );
  });

  it("lists commits as the new page's timeline while its flag is on, switching live", async () => {
    installShell(CAPABLE);
    pageV2.set(true);
    renderPanel();
    const list = await openCommits();
    expect(screen.getByRole("heading", { level: 2, name: "2 commits" })).toBeVisible();
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent(/dev committed/);

    act(() => pageV2.set(false));
    expect(screen.queryByRole("heading", { level: 2, name: "2 commits" })).toBeNull();
    expect(
      within(screen.getByRole("list", { name: "Commits" })).getAllByRole("listitem"),
    ).toHaveLength(2);

    act(() => pageV2.set(true));
    expect(screen.getByRole("heading", { level: 2, name: "2 commits" })).toBeVisible();
  });

  it("leaves Classic's view untouched, so turning the flag off never opens Classic on Commits", async () => {
    installShell(CAPABLE);
    pageV2.set(true);
    const { client } = renderPanel();
    await openCommits();
    expect(client.getQueryData(pullRequestPageViewKey("conv_1"))).toMatchObject({
      subtab: "commits",
    });
    act(() => pageV2.set(false));
    // Classic reads this key, and has no Commits tab to show.
    expect(client.getQueryData(pullRequestPanelViewKey("conv_1"))).toBeUndefined();
  });

  describe("a pull request chosen from the session's list, on the section the reader was on", () => {
    const NEXT = "https://github.com/acme/app/pull/6002";
    // Not a GitHub pull request, so it has no Commits to show.
    const ELSEWHERE = "https://forge.example.com/acme/app/-/merge_requests/7";
    const listed = (url: string, number: number, title: string): PullRequestAssociation => ({
      url,
      host: new URL(url).host,
      repository: "acme/app",
      number,
      title,
      relationship: "attached",
    });
    // Rows are titled from each PR's own read.
    const row = (number: number) =>
      screen.getByRole("button", {
        name: `${number === 7 ? "forge.example.com/" : ""}acme/app #${number} — chore: dummy PR`,
      });

    beforeEach(() => {
      const base = state.info!;
      const data = base.data!;
      Object.assign(data, {
        tracking_available: true,
        selected_pr_url: undefined,
        prs: [
          listed(PR_URL, 6000, "First"),
          listed(ELSEWHERE, 7, "Elsewhere"),
          listed(NEXT, 6002, "Next"),
        ],
      });
      // Each PR reads as itself, one answer per PR as a query keeps it.
      answers.clear();
      vi.mocked(usePullRequestInfo).mockImplementation(((
        _id: string,
        options?: { prUrl?: string },
      ) => {
        const url = options?.prUrl ?? PR_URL;
        if (!answers.has(url)) {
          const number = Number(url.split("/").pop());
          const pr = { ...data.pr!, url, number };
          answers.set(url, { ...base, data: { ...data, selected_pr_url: url, pr } });
        }
        return answers.get(url);
      }) as never);
    });

    const answers = new Map<string, unknown>();
    const back = () =>
      userEvent.click(screen.getByRole("button", { name: "All pull requests (3)" }));

    it("keeps Commits asked for while the next PR is still being read", async () => {
      installShell(CAPABLE);
      const { rerender } = renderPanel();
      await userEvent.click(
        await screen.findByRole("button", { name: "acme/app #6000 — chore: dummy PR" }),
      );
      await openCommits();
      await back();
      // The next PR is still being read when it's chosen.
      const answered = answers.get(NEXT);
      answers.set(NEXT, { isLoading: true, isFetching: true, error: null });
      await userEvent.click(row(6002));
      await act(
        () =>
          new Promise<void>((resolve) => {
            setTimeout(resolve, 300);
          }),
      );
      if (answered) answers.set(NEXT, answered);
      else answers.delete(NEXT);
      rerender(<PullRequestPanel conversationId="conv_1" />);
      expect(await screen.findByRole("tab", { name: /^Commits/ })).toHaveAttribute(
        "aria-selected",
        "true",
      );
    });

    it("keeps Changes through Back to the list, for the next one chosen there", async () => {
      installShell(CAPABLE);
      renderPanel();
      await userEvent.click(
        await screen.findByRole("button", { name: "acme/app #6000 — chore: dummy PR" }),
      );
      fireEvent.mouseDown(await screen.findByRole("tab", { name: "Changes" }));
      await back();
      await userEvent.click(row(6002));
      expect(await screen.findByRole("tab", { name: "Changes" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
    });

    it("carries Commits, not the commit open in it, and keeps it while Commits may yet show", async () => {
      installShell(CAPABLE);
      renderPanel();
      await userEvent.click(
        await screen.findByRole("button", { name: "acme/app #6000 — chore: dummy PR" }),
      );
      const list = await openCommits();
      await userEvent.click(within(list).getAllByRole("button", { name: /Add the parser/ })[0]);
      await back();
      await userEvent.click(row(6002));
      expect(await screen.findByRole("tab", { name: /^Commits/ })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      // The next PR's list of commits, not the one the reader had open in the last.
      expect(await screen.findByRole("list", { name: "Commits" })).toBeVisible();
    });

    it("carries the section it fell back to, where the PR it showed couldn't show Commits", async () => {
      installShell(CAPABLE);
      renderPanel();
      await userEvent.click(
        await screen.findByRole("button", { name: "acme/app #6000 — chore: dummy PR" }),
      );
      await openCommits();
      await back();
      await userEvent.click(row(7));
      expect(await screen.findByRole("tab", { name: "Summary" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      // Once it's settled that this PR can't show Commits, not while that's still being read.
      await act(
        () =>
          new Promise<void>((resolve) => {
            setTimeout(resolve, 300);
          }),
      );
      await back();
      await userEvent.click(row(6002));
      expect(await screen.findByRole("tab", { name: "Summary" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
    });
  });

  describe("opening a commit from the new page's Activity", () => {
    beforeEach(() => {
      pageV2.set(true);
      state.info!.data!.pr = {
        ...state.info!.data!.pr!,
        comments: [],
        timeline_events: [
          {
            id: "commit-2",
            kind: "commit",
            author: "dev",
            created_at: "2026-11-04T09:00:00Z",
            url: null,
            sha: COMMITS[1]!.sha,
            message: "Fix the parser",
          },
        ],
      };
    });
    /** Answers the commit list only once `release` runs. */
    function holdCommits() {
      let release = () => {};
      const answered = new Promise<void>((resolve) => {
        release = resolve;
      });
      const answer = githubQuery.getMockImplementation()!;
      githubQuery.mockImplementation(async (operation, ...rest) => {
        if (operation === "pull_request_commits") await answered;
        return answer(operation, ...rest);
      });
      return () => act(async () => release());
    }
    const activityRow = () => screen.getByRole("button", { name: /^dev pushed 2222222/ });

    it("opens it in Commits with focus on its title", async () => {
      installShell(CAPABLE);
      renderPanel();
      await screen.findByRole("tab", { name: "Commits 2" });
      activityRow().focus();
      fireEvent.click(activityRow());

      expect(screen.getByRole("tab", { name: /^Commits/ })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      const title = await screen.findByRole("heading", { name: "Fix the parser" });
      await waitFor(() => expect(title).toHaveFocus());
    });

    it("moves focus once a list still loading arrives", async () => {
      const release = holdCommits();
      installShell(CAPABLE);
      renderPanel();
      // Commits shows while its list is still loading.
      const row = await screen.findByRole("button", { name: /^dev pushed 2222222/ });
      row.focus();
      fireEvent.click(row);
      await release();

      const title = await screen.findByRole("heading", { name: "Fix the parser" });
      await waitFor(() => expect(title).toHaveFocus());
    });

    it("leaves focus where the person went while the list loaded", async () => {
      const release = holdCommits();
      installShell(CAPABLE);
      renderPanel();
      fireEvent.click(await screen.findByRole("button", { name: /^dev pushed 2222222/ }));
      // The person clicks into the tab strip before the list arrives.
      const tab = screen.getByRole("tab", { name: /^Commits/ });
      fireEvent.pointerDown(tab);
      tab.focus();
      await release();

      const title = await screen.findByRole("heading", { name: "Fix the parser" });
      expect(title).not.toHaveFocus();
      expect(tab).toHaveFocus();
    });
  });

  it("opens one commit's diff, steps between commits, and returns to the list", async () => {
    installShell(CAPABLE);
    renderPanel();
    const list = await openCommits();
    await userEvent.click(within(list).getByRole("button", { name: /Add the parser/ }));
    expect(await screen.findByRole("heading", { name: "Add the parser" })).toBeInTheDocument();
    await waitFor(() =>
      expect(document.querySelector('[data-github-file="src/parser.ts"]')).not.toBeNull(),
    );
    expect(githubQuery).toHaveBeenCalledWith(
      "commit_diff",
      { host: "github.com", login: "dev" },
      { repository: "acme/app", sha: COMMITS[0].sha },
    );
    expect(
      screen.getByRole("button", { name: "Previous commit: This is the first commit." }),
    ).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Next commit" }));
    expect(await screen.findByRole("heading", { name: "Fix the parser" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Next commit: This is the last commit." }),
    ).toBeDisabled();
    expect(screen.getByRole("link", { name: "Open commit on GitHub" })).toHaveAttribute(
      "href",
      COMMITS[1].url,
    );
    await userEvent.click(screen.getByRole("button", { name: "All commits" }));
    expect(await screen.findByRole("list", { name: "Commits" })).toBeInTheDocument();
  });

  it.each([false, true])(
    "opens Changes from an open commit with All changes, and focuses its tab (new page: %s)",
    async (newPage) => {
      pageV2.set(newPage);
      installShell(CAPABLE);
      renderPanel();
      const list = await openCommits();
      await userEvent.click(within(list).getByRole("button", { name: /Add the parser/ }));
      await userEvent.click(await screen.findByRole("button", { name: "All changes" }));
      const changes = screen.getByRole("tab", { name: /^Changes/ });
      expect(changes).toHaveAttribute("aria-selected", "true");
      expect(changes).toHaveFocus();
    },
  );

  describe("in a pull request's own tab", () => {
    const COMMITS_VIEW: PullRequestPanelView = {
      selected: PR_URL,
      showingDetails: true,
      subtab: "commits",
    };
    const pullMode = () => ({
      kind: "pull" as const,
      onShowAllPullRequests: vi.fn(),
      onOpenInNewTab: vi.fn(),
    });
    const firstCommit = async () =>
      within(await screen.findByRole("list", { name: "Commits" })).getByRole("button", {
        name: /Add the parser/,
      });

    it("opens a commit behind the tab on ⌘-click, a middle click or ⌘Enter, and in front on ⇧⌘-click", async () => {
      const platform = vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
      installShell(CAPABLE);
      const mode = pullMode();
      const { reports } = renderControlled(COMMITS_VIEW, mode);
      const row = await firstCommit();
      const commit = { commit: COMMITS[0].sha };
      fireEvent.click(row, { metaKey: true });
      expect(mode.onOpenInNewTab).toHaveBeenLastCalledWith(commit, { background: true });
      fireEvent.click(row, { metaKey: true, shiftKey: true });
      expect(mode.onOpenInNewTab).toHaveBeenLastCalledWith(commit, { background: false });
      fireEvent(row, new MouseEvent("auxclick", { bubbles: true, button: 1 }));
      expect(mode.onOpenInNewTab).toHaveBeenLastCalledWith(commit, { background: true });
      fireEvent.keyDown(row, { key: "Enter", metaKey: true });
      expect(mode.onOpenInNewTab).toHaveBeenCalledTimes(4);
      expect(reports.some((report) => report.view.commit !== undefined)).toBe(false);
      // The arrow keys still move along the list.
      fireEvent.keyDown(row, { key: "ArrowDown" });
      expect(
        within(screen.getByRole("list", { name: "Commits" })).getByRole("button", {
          name: /Fix the parser/,
        }),
      ).toHaveFocus();
      // A plain click opens the commit here, as a step.
      fireEvent.click(row);
      expect(mode.onOpenInNewTab).toHaveBeenCalledTimes(4);
      expect(reports.at(-1)).toEqual({
        view: expect.objectContaining({ subtab: "commits", commit: COMMITS[0].sha }),
        replace: false,
      });
      platform.mockRestore();
    });

    it("offers the open commit in ⌘K as Open commit in a new tab, behind this one", async () => {
      installShell(CAPABLE);
      const mode = pullMode();
      renderControlled({ ...COMMITS_VIEW, commit: COMMITS[0].sha }, mode);
      await screen.findByRole("heading", { name: "Add the parser" });
      const row = paletteGroup("pull-request-commits:conv_1")?.commands.find(
        (command) => command.label === "Open commit 1111111 in a new tab",
      );
      expect(row).toMatchObject({ movesFocus: false });
      act(() => row!.run());
      expect(mode.onOpenInNewTab).toHaveBeenCalledWith(
        { commit: COMMITS[0].sha },
        { background: true },
      );
    });

    it.each([true, false])(
      "⌘Enter on a focused commit opens it behind the tab, never accepting a pending prompt (macOS: %s)",
      async (mac) => {
        installShell(CAPABLE);
        const approval = pendingApproval(mac);
        const mode = pullMode();
        renderControlled(COMMITS_VIEW, mode);
        const row = await firstCommit();
        row.focus();
        approval.pressCommandEnter(row);
        expect(mode.onOpenInNewTab).toHaveBeenCalledWith(
          { commit: COMMITS[0].sha },
          { background: true },
        );
        expect(approval.submitApproval).not.toHaveBeenCalled();
        approval.pressCommandEnter(document.body);
        expect(approval.submitApproval).toHaveBeenCalledWith("review-e1", "accept");
        approval.restore();
      },
    );
  });

  describe("in ⌘K", () => {
    const commitRows = () => paletteGroup("pull-request-commits:conv_1")?.commands ?? [];
    const commitRow = (label: string) => {
      const row = commitRows().find((command) => command.label === label);
      if (!row) throw new Error(`No "${label}" among: ${commitRows().map((entry) => entry.label)}`);
      return row;
    };

    it("opens the Commits tab from another tab, with focus on the list", async () => {
      installShell(CAPABLE);
      renderPanel();
      await screen.findByRole("tab", { name: "Commits 2" });
      expect(commitRows().map((command) => command.label)).toEqual(["Show commits"]);
      expect(commitRow("Show commits").movesFocus).toBe(true);
      act(() => commitRow("Show commits").run());
      const list = await screen.findByRole("list", { name: "Commits" });
      await waitFor(() =>
        expect(within(list).getByRole("button", { name: /Add the parser/ })).toHaveFocus(),
      );
      // The list's own rows take the arrow keys, so ⌘K adds nothing there.
      expect(commitRows()).toEqual([]);
    });

    it("steps through commits, greyed out at the ends, and leaves for the list or Changes", async () => {
      installShell(CAPABLE);
      renderPanel();
      const list = await openCommits();
      await userEvent.click(within(list).getByRole("button", { name: /Add the parser/ }));
      await screen.findByRole("heading", { name: "Add the parser" });
      expect(commitRows().map((command) => command.label)).toEqual([
        "Go to the previous commit",
        "Go to the next commit",
        "Show all commits",
        "Show all changes",
      ]);
      expect(commitRow("Go to the previous commit").availability).toEqual({
        available: false,
        reason: "This is the first commit.",
      });
      act(() => commitRow("Go to the next commit").run());
      expect(await screen.findByRole("heading", { name: "Fix the parser" })).toBeInTheDocument();
      expect(commitRow("Go to the next commit").availability).toMatchObject({ available: false });

      act(() => commitRow("Show all commits").run());
      const back = await screen.findByRole("list", { name: "Commits" });
      // Back on the list, the commit just left has focus.
      await waitFor(() =>
        expect(within(back).getByRole("button", { name: /Fix the parser/ })).toHaveFocus(),
      );

      await userEvent.click(within(back).getByRole("button", { name: /Fix the parser/ }));
      await screen.findByRole("heading", { name: "Fix the parser" });
      act(() => commitRow("Show all changes").run());
      await waitFor(() =>
        expect(screen.getByRole("tab", { name: "Changes" })).toHaveAttribute(
          "aria-selected",
          "true",
        ),
      );
      expect(screen.getByRole("tab", { name: "Changes" })).toHaveFocus();
      expect(commitRows().map((command) => command.label)).toEqual(["Show commits"]);
    });

    it("shows a section from ⌘K with focus on its tab, leaving Commits to its own command", async () => {
      pageV2.set(true);
      installShell(CAPABLE);
      renderPanel();
      await screen.findByRole("tab", { name: "Commits 2" });
      const sectionRows = () => paletteGroup("pull-request-sections:conv_1")?.commands ?? [];
      expect(sectionRows().map((command) => command.label)).toEqual([
        "Show the pull request’s comments",
        "Show the pull request’s changes",
      ]);
      const changes = sectionRows().find((command) => command.label.includes("changes"))!;
      expect(changes.movesFocus).toBe(true);
      act(() => changes.run());
      const tab = screen.getByRole("tab", { name: /^Changes/ });
      await waitFor(() => expect(tab).toHaveAttribute("aria-selected", "true"));
      await waitFor(() => expect(tab).toHaveFocus());
      // Comments are Overview's Activity now, which takes focus itself, every kind shown.
      const comments = sectionRows().find((command) => command.label.includes("comments"))!;
      act(() => comments.run());
      expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await waitFor(() => expect(screen.getByRole("region", { name: "Activity" })).toHaveFocus());

      // Inside an open commit, its own "Show all changes" is the only way to Changes.
      const list = await openCommits();
      await userEvent.click(within(list).getByRole("button", { name: /Add the parser/ }));
      await screen.findByRole("heading", { name: "Add the parser" });
      expect(sectionRows().map((command) => command.label)).toEqual([
        "Show the pull request’s overview",
        "Show the pull request’s comments",
      ]);
      expect(commitRows().map((command) => command.label)).toContain("Show all changes");
    });

    it("leaves the classic panel's tabs without the new page's section commands", async () => {
      installShell(CAPABLE);
      renderPanel();
      await screen.findByRole("tab", { name: "Summary" });
      expect(paletteGroup("pull-request-sections:conv_1")?.commands ?? []).toEqual([]);
    });

    it("offers only the shown pane's sections when two are mounted", async () => {
      pageV2.set(true);
      installShell(CAPABLE);
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(
        <QueryClientProvider client={client}>
          <PullRequestPanel conversationId="conv_1" active />
          {/* Another session's pane, mounted but hidden. */}
          <PullRequestPanel conversationId="conv_2" active={false} />
        </QueryClientProvider>,
      );
      await waitFor(() =>
        expect(screen.getAllByRole("tab", { name: "Commits 2" })).toHaveLength(2),
      );
      const groups = () =>
        Object.entries(usePaletteCommandsStore.getState().groups).filter(
          ([key, group]) => key.startsWith("pull-request-sections:") && group.commands.length > 0,
        );
      expect(groups()).toHaveLength(1);
      act(() =>
        groups()[0]![1]
          .commands.find((command) => command.label.includes("changes"))!
          .run(),
      );
      const [shown, hidden] = screen.getAllByRole("tab", { name: /^Changes/ });
      await waitFor(() => expect(shown).toHaveAttribute("aria-selected", "true"));
      expect(hidden).toHaveAttribute("aria-selected", "false");
    });

    it("doesn't take focus from the composer when the list loads after the person moved on", async () => {
      let releaseList: () => void = () => {};
      githubQuery.mockImplementation(async (operation: unknown) => {
        if (operation === "account_status") return { ok: true, data: { login: "dev" } };
        if (operation === "pull_request_commits") {
          await new Promise<void>((resolve) => {
            releaseList = resolve;
          });
          return { ok: true, data: { total_count: 2, commits: COMMITS } };
        }
        return { ok: false, error: { code: "invalid_request", message: "no" } };
      });
      installShell(CAPABLE);
      renderPanel();
      const composer = document.body.appendChild(document.createElement("textarea"));
      onTestFinished(() => composer.remove());
      await screen.findByRole("tab", { name: /^Commits/ });
      act(() => commitRow("Show commits").run());
      expect(await screen.findByText("Loading commits…")).toBeInTheDocument();
      // The person goes back to the composer and types while the list loads.
      composer.focus();
      await userEvent.type(composer, "hello");
      await act(async () => releaseList());
      const list = await screen.findByRole("list", { name: "Commits" });
      expect(within(list).getAllByRole("button")[0]).not.toHaveFocus();
      expect(composer).toHaveFocus();
    });

    it("moves focus to the list once, not again when the list mounts again", async () => {
      installShell(CAPABLE);
      renderPanel();
      await screen.findByRole("tab", { name: "Commits 2" });
      act(() => commitRow("Show commits").run());
      const first = async () =>
        within(await screen.findByRole("list", { name: "Commits" })).getByRole("button", {
          name: /Add the parser/,
        });
      await waitFor(async () => expect(await first()).toHaveFocus());
      fireEvent.mouseDown(screen.getByRole("tab", { name: "Summary" }));
      fireEvent.mouseDown(screen.getByRole("tab", { name: "Commits 2" }));
      expect(await first()).not.toHaveFocus();
    });

    it("offers nothing while the panel is hidden", async () => {
      installShell(CAPABLE);
      renderPanel(false, false);
      await screen.findByRole("tab", { name: /^Commits/ });
      expect(commitRows()).toEqual([]);
    });

    it("offers nothing where the pull request has no Commits tab", async () => {
      renderPanel();
      await screen.findByRole("tab", { name: "Changes" });
      expect(commitRows()).toEqual([]);
    });
  });

  it("shows a commit's files on the shared review under the Tabs design, and stacked under Classic", async () => {
    pageV2.set(true);
    installShell(CAPABLE);
    renderPanel();
    const list = await openCommits();
    await userEvent.click(within(list).getByRole("button", { name: /Add the parser/ }));
    expect(await screen.findByTestId("commit-review")).toBeInTheDocument();
    expect(document.querySelector('[data-github-file="src/parser.ts"]')).toBeNull();
    expect(commitReview.props).toMatchObject({
      conversationId: "conv_1",
      prUrl: PR_URL,
      sha: COMMITS[0].sha,
      layout: "unified",
      active: true,
    });
    expect((commitReview.props!.files as { path: string }[]).map((entry) => entry.path)).toEqual([
      "src/parser.ts",
    ]);
    // The next commit is its own review, with its own selection and find.
    await userEvent.click(screen.getByRole("button", { name: "Next commit" }));
    await waitFor(() => expect(commitReview.props!.sha).toBe(COMMITS[1].sha));
  });

  it("opens a commit's files where the PR's commits live, at that commit", async () => {
    pageV2.set(true);
    installShell(CAPABLE);
    const commitReviewFor = async (headRepositoryUrl: string | null | undefined) => {
      cleanup();
      commitReview.props = undefined;
      if (headRepositoryUrl === undefined) delete state.info!.data!.pr!.head_repository_url;
      else state.info!.data!.pr!.head_repository_url = headRepositoryUrl;
      renderPanel();
      const list = await openCommits();
      await userEvent.click(within(list).getByRole("button", { name: /Add the parser/ }));
      await screen.findByTestId("commit-review");
      // The selected commit stays the revision; only the repository comes from the PR.
      expect(commitReview.props!.sha).toBe(COMMITS[0].sha);
      return commitReview.props!.repositories as Record<string, unknown>;
    };
    expect(await commitReviewFor("https://github.com/fork/app")).toEqual({
      headRepositoryUrl: "https://github.com/fork/app",
    });
    // Known to be unknown is passed on, so the link is disabled rather than guessed.
    expect(await commitReviewFor(null)).toEqual({ headRepositoryUrl: null });
    // An older host's PR says nothing, which keeps the PR's own repository.
    const legacy = await commitReviewFor(undefined);
    expect(legacy).toEqual({});
    expect(legacy).not.toHaveProperty("headRepositoryUrl");
  });

  it("opens a stacked PR's own changes where each side's commits live", async () => {
    pageV2.set(true);
    state.info!.data!.pr!.head_repository_url = "https://github.com/child-fork/app";
    const original = await vi.importActual<typeof StackDiffModule>(
      "@/pullRequestPage/stackDiff/usePullRequestStackDiff",
    );
    const stub = {
      parent: { number: 100, label: "#100", headRepository: "parent-fork/app" },
      intent: "this-pr",
      choose: () => {},
      effective: { kind: "this-pr", comparison: stackComparison() },
      diagnostic: null,
      reload: () => {},
      retry: () => {},
    } as ReturnType<typeof usePullRequestStackDiff>;
    // The real hook still runs, so React sees the same hooks every render; only its answer differs.
    vi.mocked(usePullRequestStackDiff).mockImplementation((...args) => {
      original.usePullRequestStackDiff(...args);
      return stub;
    });
    try {
      renderControlled({
        selected: "https://example.com/pr/6000",
        showingDetails: true,
        subtab: "changes",
      });
      await screen.findByTestId("stack-only-review");
      expect(stackOnlyReview.props!.repositories).toEqual({
        headRepositoryUrl: "https://github.com/child-fork/app",
        parentHeadRepository: "parent-fork/app",
      });
    } finally {
      cleanup();
      vi.mocked(usePullRequestStackDiff).mockImplementation(original.usePullRequestStackDiff);
    }
  });

  describe("a stacked PR's Changes", () => {
    /** Renders Changes with the stack diff answering `effective`, under the real hook's calls. */
    type StackDiff = NonNullable<ReturnType<typeof usePullRequestStackDiff>>;
    const place = {
      selected: "https://example.com/pr/6000",
      showingDetails: true,
      subtab: "changes",
    } as const;
    /**
     * Renders Changes with the stack diff answering `effective`, under the real hook's calls.
     * `answers` says what a choice shows next; without it a choice changes nothing.
     */
    async function stackedChanges(
      effective: StackDiff["effective"],
      intent: StackDiff["intent"] = "this-pr",
      answers?: (intent: StackDiff["intent"]) => StackDiff["effective"],
    ) {
      pageV2.set(true);
      const original = await vi.importActual<typeof StackDiffModule>(
        "@/pullRequestPage/stackDiff/usePullRequestStackDiff",
      );
      let rerender = () => {};
      const stub: StackDiff = {
        parent: { number: 100, label: "#100", headRepository: "acme/app" },
        intent,
        choose: vi.fn((chosen: StackDiff["intent"]) => {
          if (!answers) return;
          stub.intent = chosen;
          stub.effective = answers(chosen);
          rerender();
        }),
        effective,
        diagnostic: null,
        reload: vi.fn(),
        retry: vi.fn(),
      };
      vi.mocked(usePullRequestStackDiff).mockImplementation((...args) => {
        original.usePullRequestStackDiff(...args);
        return { ...stub };
      });
      onTestFinished(() => {
        vi.mocked(usePullRequestStackDiff).mockImplementation(original.usePullRequestStackDiff);
      });
      const view = renderControlled(place);
      rerender = () => view.setView({ ...place });
      return Object.assign(stub, {
        /** Shows `next` as the comparison's new answer, as a read finishing would. */
        answer(next: StackDiff["effective"]) {
          stub.effective = next;
          rerender();
        },
      });
    }

    it("name their parent beside the file count, with no strip over them", async () => {
      const diff = await stackedChanges({ kind: "this-pr", comparison: stackComparison() });
      await screen.findByTestId("stack-only-review");
      expect(screen.queryByTestId("pull-request-stack-diff-bar")).toBeNull();
      const base = stackOnlyReview.props!.base as {
        label: string;
        description: string;
        choices: { id: string; checked: boolean; onSelect: () => void }[];
      };
      expect(base).toMatchObject({
        label: "vs #100",
        description: "Only this PR's changes, since #100",
      });
      expect(base.choices.find((choice) => choice.checked)?.id).toBe("this-pr");
      base.choices.find((choice) => choice.id === "all")!.onSelect();
      expect(diff.choose).toHaveBeenCalledWith("all");
    });

    it("offer Try Again in the menu by the file count after a chosen comparison failed", async () => {
      const diff = await stackedChanges({ kind: "all", fallback: "failed" });
      await screen.findByTestId("shared-review");
      expect(screen.queryByTestId("pull-request-stack-diff-bar")).toBeNull();
      const base = sharedReview.props!.base as {
        label: string;
        description: string;
        actions: { label: string; onSelect: () => void }[];
      };
      expect(base).toMatchObject({
        label: "vs base branch",
        description: "Couldn't compare this PR with #100. Showing all changes.",
      });
      expect(base.actions.map((action) => action.label)).toEqual(["Compare with #100 again"]);
      base.actions[0]!.onSelect();
      expect(diff.retry).toHaveBeenCalledOnce();
    });

    it("say they're comparing while the comparison is read", async () => {
      await stackedChanges({ kind: "comparing" });
      // The header's state pill keeps its own, empty, status beside this one.
      const comparing = await screen.findByText("Comparing with #100…");
      expect(comparing.closest('[role="status"]')).not.toBeNull();
      expect(screen.queryByTestId("pull-request-stack-diff-bar")).toBeNull();
    });

    it("offer Reload once the stack changed since it was compared", async () => {
      const diff = await stackedChanges({ kind: "stale" });
      expect(await screen.findByText("Stack Changed")).toBeVisible();
      expect(screen.getByText("This stack changed since it was compared.")).toBeVisible();
      await userEvent.click(screen.getByRole("button", { name: "Reload" }));
      expect(diff.reload).toHaveBeenCalledOnce();
    });

    it.each(["comparing", "stale"] as const)(
      "keep the comparison menu while %s, which can still switch to all changes",
      async (kind) => {
        const diff = await stackedChanges({ kind });
        await screen.findByTestId("pull-request-stack-diff-waiting");
        const menu = screen.getByTestId("diff-review-base");
        expect(menu).toHaveTextContent("vs #100");
        await userEvent.click(menu);
        await userEvent.click(await screen.findByRole("menuitemradio", { name: /^All changes/ }));
        expect(diff.choose).toHaveBeenCalledWith("all");
      },
    );

    it("keep a failed comparison's reason and Try Again where all changes have no files", async () => {
      state.changes = {
        data: { available: true, data: [] },
        isLoading: false,
        error: null,
        isFetching: false,
      };
      state.parsedFiles = [];
      state.diff = { object: "session.github.pr_diff", patch: "" };
      const diff = await stackedChanges({ kind: "all", fallback: "failed" });
      expect(await screen.findByText("There are no changes in this pull request.")).toBeVisible();
      const row = screen.getByTestId("pull-request-stack-diff-base-row");
      expect(row).toHaveTextContent("Couldn't compare this PR with #100. Showing all changes.");
      await userEvent.click(within(row).getByTestId("diff-review-base"));
      await userEvent.click(
        await screen.findByRole("menuitem", { name: "Compare with #100 again" }),
      );
      expect(diff.retry).toHaveBeenCalledOnce();
    });

    it("keep focus on the menu when a keyboard choice swaps the review that holds it", async () => {
      const diff = await stackedChanges(
        { kind: "this-pr", comparison: stackComparison() },
        "this-pr",
        (intent) =>
          intent === "all"
            ? { kind: "all", fallback: null }
            : { kind: "this-pr", comparison: stackComparison() },
      );
      const user = userEvent.setup();
      const trigger = within(await screen.findByTestId("stack-only-review")).getByTestId(
        "diff-review-base",
      );
      trigger.focus();
      await user.keyboard("{Enter}{End}{Enter}");
      expect(diff.choose).toHaveBeenCalledWith("all");
      const replacement = within(await screen.findByTestId("shared-review")).getByTestId(
        "diff-review-base",
      );
      expect(trigger.isConnected).toBe(false);
      await waitFor(() => expect(replacement).toHaveFocus());
    });

    it("keep focus on the menu through comparing, until only this PR's changes show", async () => {
      const diff = await stackedChanges({ kind: "all", fallback: null }, "all", (intent) =>
        intent === "all" ? { kind: "all", fallback: null } : { kind: "comparing" },
      );
      const user = userEvent.setup();
      within(await screen.findByTestId("shared-review"))
        .getByTestId("diff-review-base")
        .focus();
      await user.keyboard("{Enter}");
      await user.click(await screen.findByRole("menuitemradio", { name: /^This PR only/ }));
      const waiting = await screen.findByTestId("pull-request-stack-diff-waiting");
      await waitFor(() => expect(within(waiting).getByTestId("diff-review-base")).toHaveFocus());
      diff.answer({ kind: "this-pr", comparison: stackComparison() });
      const own = within(await screen.findByTestId("stack-only-review")).getByTestId(
        "diff-review-base",
      );
      await waitFor(() => expect(own).toHaveFocus());
    });
  });

  describe("a commit that may have left the pull request", () => {
    const HEAD = "f".repeat(40);
    const BASE = "e".repeat(40);
    /** Moves the PR to `revision` and re-renders, as a refetch after a push would. */
    const rereadPullRequest = vi.fn();
    function moveTo(rerender: () => void, revision: { head: string; base: string }) {
      const data = state.info!.data!;
      state.info = {
        ...state.info!,
        refetch: rereadPullRequest,
        data: { ...data, pr: { ...data.pr!, head_sha: revision.head, base_sha: revision.base } },
      } as typeof state.info;
      act(() => rerender());
    }
    function listing(commits: typeof COMMITS, extra: Record<string, unknown>) {
      githubQuery.mockImplementation(async (operation: unknown) => {
        if (operation === "account_status") return { ok: true, data: { login: "dev" } };
        if (operation === "pull_request_commits") {
          return { ok: true, data: { total_count: commits.length, commits, ...extra } };
        }
        if (operation === "commit_diff") return { ok: true, data: { sha: "x", patch: "PATCH" } };
        return { ok: false, error: { code: "invalid_request", message: "no" } };
      });
    }
    const listReads = () =>
      githubQuery.mock.calls.filter(([operation]) => operation === "pull_request_commits").length;

    async function openSecondCommit() {
      rereadPullRequest.mockClear();
      installShell(CAPABLE);
      listing(COMMITS, { head_sha: HEAD, base_sha: BASE, complete: true });
      const view = renderPanel();
      const list = await openCommits();
      await userEvent.click(within(list).getByRole("button", { name: /Fix the parser/ }));
      expect(await screen.findByRole("heading", { name: "Fix the parser" })).toBeVisible();
      return () => view.rerender(<PullRequestPanel conversationId="conv_1" />);
    }

    it("says the open commit is no longer in the pull request after a push drops it", async () => {
      const rerender = await openSecondCommit();
      const NEW_HEAD = "9".repeat(40);
      listing([COMMITS[0]!], { head_sha: NEW_HEAD, base_sha: BASE, complete: true });
      moveTo(rerender, { head: NEW_HEAD, base: BASE });
      expect(
        await screen.findByText("This commit is no longer in the pull request."),
      ).toBeVisible();
    });

    it("re-reads the list when only the base moves, and claims nothing from the old base", async () => {
      const rerender = await openSecondCommit();
      const reads = listReads();
      const NEW_BASE = "8".repeat(40);
      // The answer still names the old base, as a late read from before the change would.
      listing([COMMITS[0]!], { head_sha: HEAD, base_sha: BASE, complete: true });
      moveTo(rerender, { head: HEAD, base: NEW_BASE });
      await waitFor(() => expect(listReads()).toBeGreaterThan(reads));
      expect(await screen.findByText("This commit isn’t in the list shown here.")).toBeVisible();
      expect(screen.queryByText("This commit is no longer in the pull request.")).toBeNull();
      // The list and the PR disagree, so the PR is read again, once.
      await waitFor(() => expect(rereadPullRequest).toHaveBeenCalledOnce());
    });

    it("claims nothing past GitHub's 250-commit limit", async () => {
      const rerender = await openSecondCommit();
      const NEW_HEAD = "7".repeat(40);
      listing([COMMITS[0]!], { head_sha: NEW_HEAD, base_sha: BASE, complete: false });
      moveTo(rerender, { head: NEW_HEAD, base: BASE });
      expect(await screen.findByText("This commit isn’t in the list shown here.")).toBeVisible();
    });
  });

  it("explains how to sign in when the host's account isn't signed in on this Mac", async () => {
    githubQuery.mockImplementation(async () => ({
      ok: false,
      error: {
        code: "account_missing",
        message: "That GitHub account isn't signed in on this Mac. Run gh auth login.",
      },
    }));
    installShell(CAPABLE);
    renderPanel();
    fireEvent.mouseDown(await screen.findByRole("tab", { name: "Commits" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That GitHub account isn't signed in on this Mac. Run gh auth login.",
    );

    // After signing in, Try again checks the account again and lists the commits.
    githubQuery.mockImplementation(async (operation: unknown) => {
      if (operation === "account_status") return { ok: true, data: { login: "dev" } };
      if (operation === "pull_request_commits") {
        return { ok: true, data: { total_count: 2, commits: COMMITS } };
      }
      return { ok: false, error: { code: "invalid_request", message: "no" } };
    });
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("list", { name: "Commits" })).toHaveTextContent(
      "Add the parser",
    );
  });

  it("offers a retry when GitHub can't list the commits", async () => {
    let failing = true;
    githubQuery.mockImplementation(async (operation: unknown) => {
      if (operation === "account_status") return { ok: true, data: { login: "dev" } };
      if (failing) {
        return { ok: false, error: { code: "query_failed", message: "GitHub couldn't answer." } };
      }
      return { ok: true, data: { total_count: 2, commits: COMMITS } };
    });
    installShell(CAPABLE);
    renderPanel();
    fireEvent.mouseDown(await screen.findByRole("tab", { name: "Commits" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("GitHub couldn't answer.");
    failing = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("list", { name: "Commits" })).toBeInTheDocument();
  });

  it("stays hidden in a shell whose fast track only checks accounts", async () => {
    // The first fast-track shells answer account_status but not the commit reads.
    githubQuery.mockImplementation(async (operation: unknown) =>
      operation === "account_status"
        ? { ok: true, data: { login: "dev" } }
        : { ok: false, error: { code: "invalid_request", message: "Unknown operation." } },
    );
    installShell({ githubFastTrack: true });
    renderPanel();
    await screen.findByRole("tab", { name: "Changes" });
    await act(async () => {});
    expect(screen.queryByRole("tab", { name: /^Commits/ })).toBeNull();
    expect(githubQuery.mock.calls.map(([operation]) => operation)).not.toContain(
      "pull_request_commits",
    );
  });

  it("keeps a saved Changes-tab draft out of a commit's diff, and intact", async () => {
    state.changes = {
      data: { available: true, data: [file("src/parser.ts", "modified")] },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    const draftKey = ["github-new-review-thread", draftOwner(), "conv_1", PR_URL, "src/parser.ts"];
    const draft = {
      body: "Keep this draft",
      pending: false,
      error: null,
      // An earlier revision, so the draft shows above the file rather than inline.
      anchor: {
        path: "src/parser.ts",
        side: "RIGHT" as const,
        line: 1,
        start_line: null,
        head_sha: "0".repeat(40),
        base_sha: "e".repeat(40),
      },
    };
    installShell(CAPABLE);
    const { client } = renderPanel();
    client.setQueryData(draftKey, draft);

    fireEvent.mouseDown(screen.getByRole("tab", { name: "Changes" }));
    expect(await screen.findByRole("region", { name: "New review conversation" })).toBeVisible();

    const list = await openCommits();
    await userEvent.click(within(list).getByRole("button", { name: /Add the parser/ }));
    await waitFor(() =>
      expect(document.querySelector('[data-github-file="src/parser.ts"]')).not.toBeNull(),
    );
    const commitView = screen.getByRole("tabpanel");
    expect(
      within(commitView).queryByRole("region", { name: "New review conversation" }),
    ).toBeNull();
    expect(within(commitView).queryByDisplayValue("Keep this draft")).toBeNull();
    expect(within(commitView).queryByRole("button", { name: "Close comment composer" })).toBeNull();

    fireEvent.mouseDown(screen.getByRole("tab", { name: "Changes" }));
    const restored = await screen.findByRole("region", { name: "New review conversation" });
    expect(within(restored).getByDisplayValue("Keep this draft")).toBeInTheDocument();
    expect(client.getQueryData(draftKey)).toEqual(draft);
  });

  it("Refresh re-reads CI on the same head and retries a failed account check", async () => {
    let signedIn = false;
    let ciState = "PENDING";
    githubQuery.mockImplementation(async (operation: unknown) => {
      if (operation === "account_status") {
        return signedIn
          ? { ok: true, data: { login: "dev" } }
          : {
              ok: false,
              error: {
                code: "account_missing",
                message: "That GitHub account isn't signed in on this Mac. Run gh auth login.",
              },
            };
      }
      return {
        ok: true,
        data: { total_count: 1, commits: [{ ...COMMITS[0], ci_state: ciState }] },
      };
    });
    installShell(CAPABLE);
    renderPanel();
    fireEvent.mouseDown(await screen.findByRole("tab", { name: "Commits" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("isn't signed in on this Mac");

    signedIn = true;
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    const list = await screen.findByRole("list", { name: "Commits" });
    expect(await within(list).findByRole("img", { name: "Checks running" })).toBeInTheDocument();

    ciState = "SUCCESS";
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await within(list).findByRole("img", { name: "Checks passed" })).toBeInTheDocument();
  });

  it("polls running checks only while the panel is shown", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    onTestFinished(() => {
      vi.useRealTimers();
    });
    githubQuery.mockImplementation(async (operation: unknown) =>
      operation === "account_status"
        ? { ok: true, data: { login: "dev" } }
        : { ok: true, data: { total_count: 1, commits: [{ ...COMMITS[0], ci_state: "PENDING" }] } },
    );
    installShell(CAPABLE);
    const view = renderPanel(false, true);
    fireEvent.mouseDown(await screen.findByRole("tab", { name: /^Commits/ }));
    await screen.findByRole("list", { name: "Commits" });
    const reads = () =>
      githubQuery.mock.calls.filter(([operation]) => operation === "pull_request_commits").length;
    const first = reads();
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(reads()).toBe(first + 1);

    // A closed rail keeps the panel mounted but inactive; nothing may keep reading.
    view.rerender(<PullRequestPanel conversationId="conv_1" active={false} />);
    const whileHidden = reads();
    await act(() => vi.advanceTimersByTimeAsync(120_000));
    expect(reads()).toBe(whileHidden);
    expect(screen.getByRole("list", { name: "Commits" })).toBeInTheDocument();

    view.rerender(<PullRequestPanel conversationId="conv_1" active />);
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(reads()).toBeGreaterThan(whileHidden);
  });
});

describe("merge status from a registered source", () => {
  const PR_URL = "https://github.com/acme/app/pull/101";
  const HEAD = "f".repeat(40);
  const known = (
    status: "policy_failing" | "ready",
    failingRequirements: string[] = [],
  ): MergeReadinessView => ({
    kind: "known",
    readiness: { status, reasons: { failingJobs: [], failingRequirements } },
    headSha: HEAD,
  });
  const read = vi.fn<(url: string) => Promise<MergeReadinessView>>();
  withPullRequestIntegrations({ mergeReadiness: exampleMergeReadinessSource(read) });

  beforeEach(() => {
    read.mockReset();
    read.mockResolvedValue(known("policy_failing", ["SizeChecker"]));
    const data = state.info!.data!;
    state.info = {
      ...state.info!,
      data: {
        ...data,
        selected_pr_url: PR_URL,
        pr: { ...data.pr!, url: PR_URL, number: 101, head_sha: HEAD, state: "OPEN" },
      },
    };
    vi.mocked(usePullRequestInfo).mockImplementation(() => state.info as never);
  });

  it("shows the source's verdict in the header and Summary", async () => {
    renderPanel(false, true);
    expect(await screen.findByLabelText("Merge status: Policy checks failing")).toBeInTheDocument();
    const summary = screen.getByRole("region", { name: "Merge status" });
    expect(within(summary).getByText("Requirements failing: SizeChecker.")).toBeInTheDocument();
  });

  it("offers the source's renewal when it needs a credential, and reads again once renewed", async () => {
    read.mockResolvedValue({ kind: "unknown", failure: "credential" });
    renderPanel(false, true);
    expect(await screen.findByLabelText("Merge status: Renew your credential")).toBeInTheDocument();
    read.mockResolvedValue(known("ready"));
    fireEvent.click(screen.getByRole("button", { name: "Renew" }));
    expect(await screen.findByLabelText("Merge status: Ready")).toBeInTheDocument();
  });

  it("reads again when Refresh is pressed", async () => {
    renderPanel(false, true);
    await screen.findByLabelText("Merge status: Policy checks failing");
    read.mockResolvedValue(known("ready"));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByLabelText("Merge status: Ready")).toBeInTheDocument();
  });

  it("reads nothing while the panel is hidden, and reads again once it's shown", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    onTestFinished(() => {
      vi.useRealTimers();
    });
    const view = renderPanel(false, true);
    await screen.findByLabelText("Merge status: Policy checks failing");

    view.rerender(<PullRequestPanel conversationId="conv_1" active={false} />);
    const whileHidden = read.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(120_000));
    expect(read.mock.calls.length).toBe(whileHidden);

    read.mockResolvedValue(known("ready"));
    view.rerender(<PullRequestPanel conversationId="conv_1" active />);
    expect(await screen.findByLabelText("Merge status: Ready")).toBeInTheDocument();
    expect(read.mock.calls.length).toBeGreaterThan(whileHidden);
  });
});

describe("a PR linked from elsewhere", () => {
  it("opens the panel on it, though the panel mounts after the link finished", () => {
    const one = "https://github.com/example/one/pull/42";
    const two = "https://github.com/example/two/pull/7";
    const association = (url: string, repository: string, number: number) => ({
      url,
      host: "github.com",
      repository,
      number,
      title: repository,
      relationship: "attached" as const,
    });
    state.info = {
      isLoading: false,
      error: null,
      isFetching: false,
      data: {
        ...state.info!.data!,
        tracking_available: true,
        selected_pr_url: one,
        pr: { ...state.info!.data!.pr!, url: one, number: 42 },
        prs: [association(one, "example/one", 42), association(two, "example/two", 7)],
      },
    };
    // Earlier suites swap in their own info implementation; read this suite's state.
    vi.mocked(usePullRequestInfo).mockImplementation(() => state.info as never);
    // A chat link's menu linked `two` while the Pull Requests tab was closed.
    usePullRequestAttachStore.setState({ lastAttached: { sessionId: "conv_1", url: two } });
    renderPanel(false, true, (client) => showPullRequestInPanel(client, "conv_1", two));
    expect(usePullRequestInfo).toHaveBeenLastCalledWith("conv_1", {
      poll: true,
      prUrl: two,
      findBranch: true,
    });
    expect(screen.getByRole("button", { name: "All pull requests (2)" })).toBeVisible();
  });
});

describe("pull requests while the session's machine comes back", () => {
  const signedInNoRepo = (): PullRequestInfo => ({
    object: "session.github.info",
    available: true,
    provider: "github",
    branch: "feat/x",
    repo: null,
    pr: null,
    auth: {
      authenticated: true,
      hint: null,
      cli: { name: "gh", available: true },
      accounts: [{ login: "octo", host: "github.com", state: "success", active: true }],
      selected_account: "octo",
    },
  });
  const badGateway = () => new Response("<html>Bad Gateway</html>", { status: 502 });
  const json = (info: PullRequestInfo) => new Response(JSON.stringify(info));
  let answers: (() => Response)[] = [];
  let hostReads = 0;

  beforeEach(async () => {
    vi.useFakeTimers();
    state.realPrHooks = true;
    const actual = await vi.importActual<typeof UsePullRequestsModule>(
      "@/newPullRequestPage/hooks/usePullRequests",
    );
    vi.mocked(usePullRequestInfo).mockImplementation(actual.usePullRequestInfo);
    state.storesPrs = false;
    answers = [];
    hostReads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "http://localhost").pathname;
        if (!path.endsWith("/resources/github")) return new Response("{}", { status: 404 });
        hostReads += 1;
        return (answers.length > 1 ? answers.shift()! : answers[0])();
      }),
    );
    onTestFinished(() => {
      vi.useRealTimers();
      resetHostTransportRecovery();
      resetHostReturns();
    });
  });

  /** Let reads, retries and renders run for `ms`. */
  const wait = (ms = 0) => act(() => vi.advanceTimersByTimeAsync(ms));
  const blamesAccess = () => screen.queryByText(/can’t see|may not have access/);

  it("walks a resumed machine from asleep to its pull request without blaming access", async () => {
    // Stopped: neither the runner nor the host can answer.
    state.sandboxAsleep = true;
    state.serveable = false;
    answers = [badGateway, () => json(signedInNoRepo()), () => json(state.info!.data!)];
    const view = renderPanel();
    await wait();
    expect(hostReads).toBe(0);
    expect(screen.queryByText("Couldn’t open this repository")).toBeNull();

    // The resume brings the host back; the first read finds it not answering yet.
    state.sandboxAsleep = false;
    state.serveable = true;
    act(() => {
      observeSandboxResumes({ conv_1: Date.now() });
    });
    view.rerender(<PullRequestPanel conversationId="conv_1" />);
    await wait();
    expect(hostReads).toBe(1);
    expect(screen.getByText("Reconnecting to this session’s machine…")).toBeVisible();

    // The retry reaches the host, which can't open the repository yet.
    await wait(1_000);
    expect(hostReads).toBe(2);
    await wait(10);
    expect(screen.getByText("Checking this repository")).toBeVisible();
    expect(blamesAccess()).toBeNull();

    // The next poll finds the repository and its pull request.
    await wait(5_000);
    expect(screen.getByText(state.info!.data!.pr!.title)).toBeVisible();
  });

  it("says the machine isn't answering once retries run out, and Check again reads again", async () => {
    answers = [badGateway];
    renderPanel();
    await wait();
    expect(screen.getByText("Reconnecting to this session’s machine…")).toBeVisible();
    await wait(45_000);
    expect(hostReads).toBe(7);
    expect(screen.getByText("Can’t reach this session’s machine")).toBeVisible();
    expect(screen.getByText("It didn’t answer. Check again in a moment.")).toBeVisible();
    expect(blamesAccess()).toBeNull();
    await wait(120_000);
    expect(hostReads).toBe(7);

    answers = [() => json(state.info!.data!)];
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    await wait();
    expect(hostReads).toBe(8);
    expect(screen.getByText(state.info!.data!.pr!.title)).toBeVisible();
  });

  it("says it came back but isn't answering when retries run out right after a resume", async () => {
    answers = [badGateway];
    act(() => {
      observeSandboxResumes({ conv_1: Date.now() });
    });
    renderPanel();
    await wait(45_000);
    expect(
      screen.getByText("It came back online but isn’t answering yet. Check again in a moment."),
    ).toBeVisible();
  });

  it("keeps showing the loaded pull request, with a notice, while the machine can't answer", async () => {
    answers = [() => json(state.info!.data!), badGateway];
    const { client } = renderPanel();
    await wait();
    const title = screen.getByText(state.info!.data!.pr!.title);
    void client.invalidateQueries({ queryKey: ["pr-page-github-info", "conv_1"] });
    await wait(45_000);
    expect(title).toBeVisible();
    expect(
      screen.getByText(
        "Can’t reach this session’s machine right now. Showing what was loaded before.",
      ),
    ).toBeVisible();
  });

  it("notes the machine isn't answering from the first failed background read", async () => {
    answers = [() => json(state.info!.data!), badGateway];
    const { client } = renderPanel();
    await wait();
    void client.invalidateQueries({ queryKey: ["pr-page-github-info", "conv_1"] });
    await wait(10);
    expect(hostReads).toBe(2);
    expect(
      screen.getByText(
        "Can’t reach this session’s machine right now. Showing what was loaded before.",
      ),
    ).toBeVisible();
    expect(screen.getByText(state.info!.data!.pr!.title)).toBeVisible();
  });

  it("reads a repository that didn't open, with no return to explain it, as a neutral failure", async () => {
    answers = [() => json(signedInNoRepo())];
    renderPanel();
    await wait();
    expect(screen.getByText("Couldn’t open this repository")).toBeVisible();
    expect(screen.queryByText("Checking this repository")).toBeNull();
    expect(screen.queryByText(/can’t see/)).toBeNull();
  });

  it("stops saying the machine is starting once the window after its return closes", async () => {
    answers = [() => json(signedInNoRepo())];
    act(() => {
      observeSandboxResumes({ conv_1: Date.now() });
    });
    renderPanel();
    await wait();
    expect(screen.getByText("Checking this repository")).toBeVisible();
    await wait(60_000);
    expect(screen.getByText("Couldn’t open this repository")).toBeVisible();
  });

  it("reads a timed-out account check as a timeout, not a broken sign-in", async () => {
    const info = signedInNoRepo();
    info.auth = {
      ...info.auth!,
      authenticated: false,
      accounts: [{ login: "octo", host: "github.com", state: "timeout", active: true }],
    };
    answers = [() => json(info)];
    renderPanel();
    await wait();
    expect(screen.getByText("Checking GitHub timed out")).toBeVisible();
    expect(screen.queryByText(/sign-in/i)).toBeNull();
  });
});

describe("switching the merge account from a PR still loading its own read", () => {
  it("keeps the switched account after the dialog reopens", async () => {
    state.realPrHooks = true;
    state.storesPrs = false;
    const prUrl = "https://github.com/acme/app/pull/6000";
    const initial = {
      ...state.info!.data!,
      selected_pr_url: prUrl,
      tracking_available: true,
      prs: [
        {
          url: prUrl,
          host: "github.com",
          repository: "acme/app",
          number: 6000,
          relationship: "attached",
        },
      ],
      auth: {
        authenticated: true,
        hint: null,
        cli: { name: "gh", available: true },
        accounts: [
          { login: "octo", active: true, state: "success", host: "github.com" },
          { login: "other", active: false, state: "success", host: "github.com" },
        ],
        selected_account: "octo",
      },
      capabilities: {
        account_switching: true,
        merge_as_account: true,
        merging: true,
        line_counts: true,
        linked_pr_diff: true,
      },
      pr: {
        ...state.info!.data!.pr!,
        url: prUrl,
        head_sha: "a".repeat(40),
        merge_state_status: "CLEAN",
        auto_merge: null,
      },
    };
    const switched = { ...initial, auth: { ...initial.auth, selected_account: "other" } };
    // The PR's own read, and every read after the switch, stay unanswered.
    const releases: (() => void)[] = [];
    let switching = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const request = new URL(String(input), "http://localhost");
        if (request.pathname.endsWith("/merge-options")) {
          return new Response(
            JSON.stringify({
              object: "session.github.merge_options",
              methods: ["squash"],
              default: "squash",
            }),
          );
        }
        if (request.pathname.endsWith("/preferences")) {
          switching = true;
          return new Response(JSON.stringify(switched));
        }
        if (request.pathname.endsWith("/merge-as")) {
          return new Response(JSON.stringify({ detail: "Cannot merge as octo" }), { status: 400 });
        }
        if (request.pathname.endsWith("/resources/github")) {
          if (request.searchParams.has("pr_url") || switching) {
            return new Promise<Response>((resolve) => {
              releases.push(() =>
                resolve(new Response(JSON.stringify(switching ? switched : initial))),
              );
            });
          }
          return new Response(JSON.stringify(initial));
        }
        return new Response("{}", { status: 404 });
      }),
    );
    const view = renderPanel(false, true, (client) => {
      client.setQueryData(pullRequestPageViewKey("conv_1"), {
        showingDetails: true,
        subtab: "summary",
      });
    });
    try {
      // Merge shows from the session's read while the PR's own read is held.
      const open = await screen.findByRole("button", { name: "Squash and merge" });
      await waitFor(() => expect(releases.length).toBeGreaterThan(0));
      expect(view.client.getQueryData(["pr-page-github-info", "conv_1", prUrl])).toBeUndefined();

      fireEvent.click(open);
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("button", { name: "Squash and merge" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent("Cannot merge as octo");
      fireEvent.click(within(dialog).getByRole("combobox", { name: "Merge as" }));
      fireEvent.click(screen.getByRole("option", { name: "other" }));
      await waitFor(() =>
        expect(within(dialog).getByRole("button", { name: "Squash and merge" })).toBeEnabled(),
      );
      fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

      fireEvent.click(screen.getByRole("button", { name: "Squash and merge" }));
      const reopened = await screen.findByRole("dialog");
      expect(within(reopened).getByRole("combobox", { name: "Merge as" })).toHaveTextContent(
        "other",
      );
      // The PR's list is still the one the session's read gave it.
      expect(
        view.client.getQueryData<{ prs?: unknown }>(["pr-page-github-info", "conv_1", prUrl])?.prs,
      ).toEqual(initial.prs);
    } finally {
      view.unmount();
      await view.client.cancelQueries();
      for (const release of releases) release();
      view.client.clear();
    }
  });
});
