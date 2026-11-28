// The contract between the pull request details body and a page layout that fills its
// slots. The body keeps its Tabs root, focus boundary, controllers and ⌘K commands; a
// layout only replaces the header with its tab triggers and the Overview tab's contents.

import type { ComponentProps, ReactNode } from "react";

import type {
  PullRequestChangedFile,
  PullRequestChecks,
  PullRequestReviewer,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type { GitProviderCopy } from "@/lib/gitProviders";
import type { PullRequestFreshnessState } from "@/lib/pullRequestFreshness";
import type { ListedStack } from "@/lib/pullRequestStacks/listedStack";
import type { PullRequestStatusDisplay } from "@/lib/pullRequestStatusDisplay";
import type { PullRequestChecksSection } from "@/shell/PullRequestChecks";
import type { PullRequestMerge } from "@/newPullRequestPage/shell/PullRequestMerge";
import type { PullRequestSubtab } from "@/shell/pullRequestPanelView";

import type { LifecyclePullRequest } from "./usePullRequestLifecycleActions";
import type { PullRequestStackView } from "./usePullRequestStack";

/** What the details body knows about its PR, read by the layout in the same render. */
export interface PullRequestDetailsState {
  conversationId: string;
  copy: GitProviderCopy;
  /** `owner/name`, which can differ from the session's repository. */
  repository: string;
  pr: {
    number: number;
    url: string;
    state: string | null | undefined;
    isDraft: boolean;
    title: string;
    author?: string | null;
    /** Its reviewers, where the provider reports them; Review ▾ offers pinging them. */
    reviewers?: readonly PullRequestReviewer[];
    headRef?: string | null;
    baseRef?: string | null;
  };
  /** The GitHub account the session's host acts as on this PR, where it reports one. */
  hostAccount?: string | null;
  /** The PR edits apply to, or null when the session has none selected. */
  selectedPrUrl: string | null;
  titleEditable: boolean;
  /** The complete changed-file list, or undefined while it loads or when it's partial. */
  files?: readonly PullRequestChangedFile[];
  /** Lines added and removed across `files`, when every file reported them. */
  lineTotals?: { added: number; removed: number };
  /**
   * The PR's status as the composer's chip names it: an integration's merge readiness where it
   * applies, else GitHub's. The header shows it beside the lifecycle pill where it adds to it.
   */
  mergeStatus?: PullRequestStatusDisplay;
  /** The head commit's checks, which a checks status in the header lists, as the table's CI does. */
  headChecks?: PullRequestChecks;
  /** The panel's own title, shown where the panel has no heading of its own. */
  heading?: ReactNode;
  refreshButton?: ReactNode;
  /** Changes shows the previous push's files and patch while the latest push's are read. */
  updatingToLatestPush?: boolean;
  /** How old the kept data shown is, while any shows; the header's freshness mark reads it. */
  freshness?: PullRequestFreshnessState | null;
  tabs: {
    shown: PullRequestSubtab;
    /** Show a section, as choosing its tab does. */
    select: (section: PullRequestSubtab) => void;
    commits: { shown: boolean; count?: number };
    /** The changed files, once their read answers; `partial` where it lists only some. */
    changes?: { count?: number; partial?: boolean };
  };
  /** Today's Summary sections, each absent where it doesn't apply to this PR. */
  sections: {
    reviewers: ReactNode;
    labels: ReactNode;
    /** An integration's merge status, shown above the CI checks. */
    mergeReadiness: ReactNode;
    /** A stacked PR's advice about landing it, shown with the merge status. */
    stackLanding: ReactNode;
    description: ReactNode;
  };
  /** What the CI checks show, for a layout that renders them with its own footer. */
  checks: Omit<ComponentProps<typeof PullRequestChecksSection>, "footer">;
  /** The merge controls' inputs, or null where this PR can't be merged from here. */
  merge: Omit<ComponentProps<typeof PullRequestMerge>, "readyCaption"> | null;
  /** The PR's Activity timeline, where anything on the page that opens an entry shows it. */
  activity: ReactNode;
  /** The box for a new comment, or null where this PR can't be commented on from here. */
  composer: ReactNode;
  /** What draft, ready and auto-merge read about the PR. */
  lifecycle: LifecyclePullRequest;
  /** False while the panel is hidden, so the page's own ⌘K rows leave with it. */
  active: boolean;
  /** The stack this PR's description lists, or null when it isn't stacked. */
  stack: PullRequestStackView | null;
  /** No stack shows yet, but one is likely: the header holds its place. */
  stackLoading?: boolean;
}

/**
 * What a list already knows about a PR, shown in the details' place while they read, so the
 * page paints its header and title at once and the details arrive in the same lines.
 */
export interface PullRequestPreview {
  url: string;
  /** `owner/name`. */
  repository: string;
  number: number;
  /** Null until a read names it. */
  title: string | null;
  /** GitHub's state, such as `OPEN` or `MERGED`; null where the list doesn't know it. */
  state: string | null;
  isDraft: boolean;
  author: string | null;
  /** The stack the list nests it in, shown from the first frame until its own reads say. */
  stack?: ListedStack;
}

/**
 * Slots a page layout fills. Both render inside the body's Tabs root, so the layout's
 * triggers pair with the body's tab panels, and inside its focus boundary.
 */
export interface PullRequestDetailsLayout {
  /** Replaces the header and the tab strip; must render the TabsList and its triggers. */
  renderHeader: (state: PullRequestDetailsState) => ReactNode;
  /** Replaces the contents of the Summary tab, which the layout may name Overview. */
  renderSummary: (state: PullRequestDetailsState) => ReactNode;
  /** Pinned below the Summary tab's scrolling contents, such as a comment box. */
  renderSummaryFooter?: (state: PullRequestDetailsState) => ReactNode;
  /** The page as `preview` lays it out while the details read, in place of a spinner. */
  renderPreview?: (
    preview: PullRequestPreview,
    copy: GitProviderCopy,
    /** Show another PR of its stack where this one shows; false where it can't. */
    selectInPlace: (url: string) => boolean,
    /** The section the PR opens on. */
    section: PullRequestSubtab,
  ) => ReactNode;
}
