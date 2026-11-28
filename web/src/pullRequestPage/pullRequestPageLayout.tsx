// The new pull request page: a header with the repository link, state and sections, and an
// Overview that leads with the title and branches. It fills the slots of the pull request
// details, which keep their tabs, controllers and commands.

import { forwardRef, type ReactNode, useRef, useState } from "react";

import { cn } from "@/lib/utils";

import { TabsContent } from "@/components/ui/tabs";
import { reviewShowsTreeColumn, useElementWidth } from "@/components/diff-review/reviewLayout";
import { PULL_REQUEST_REVIEW_TOOLBAR_HEIGHTS } from "@/components/diff-review/reviewToolbarHeight";
import { PULL_REQUEST_REVIEW_TREE_WIDTH } from "@/components/diff-review/reviewTreeWidth";
import { readDiffReviewTreeVisible } from "@/components/diff-review/treeVisibility";
import { useRememberedPaneWidth } from "@/hooks/useRememberedPaneWidth";
import { PullRequestChecksSection } from "@/shell/PullRequestChecks";
import {
  PullRequestPageTitleHeading,
  PullRequestTitle,
} from "@/newPullRequestPage/shell/PullRequestDetails";
import {
  PullRequestMerge,
  type MergeReadyAction,
} from "@/newPullRequestPage/shell/PullRequestMerge";

import {
  ConvertToDraftPrompt,
  EnableAutoMergeSlot,
  ReadyForReviewPrompt,
} from "./PullRequestLifecycleControls";
import { PullRequestSectionHeader } from "@/shell/PullRequestSection";
import { PullRequestSectionStyleContext } from "@/shell/pullRequestSectionStyle";
import type { GitProviderCopy } from "@/lib/gitProviders";
import type { PullRequestSubtab } from "@/shell/pullRequestPanelView";

import type {
  PullRequestDetailsLayout,
  PullRequestDetailsState,
  PullRequestPreview,
} from "./detailsLayout";
import { PullRequestStackCard } from "./PullRequestStack";
import { type PullRequestStackView, useListedStack } from "./usePullRequestStack";
import { MERGE_SECTION_ATTRIBUTE } from "./PullRequestMergeStatusBadge";
import {
  PullRequestPageHeader,
  type PullRequestPageHeaderOptions,
  PullRequestPagePreviewHeader,
} from "./PullRequestPageHeader";

/**
 * The details' header: the layout's header when one is given, else `children`, today's.
 * The element keeps `ref` either way, so focus moves aimed at the tab strip still land.
 */
export const PullRequestHeaderSlot = forwardRef<
  HTMLDivElement,
  {
    layout?: PullRequestDetailsLayout;
    state: PullRequestDetailsState;
    className: string;
    children: ReactNode;
  }
>(function PullRequestHeaderSlot({ layout, state, className, children }, ref) {
  return (
    <div ref={ref} className={layout ? "shrink-0 border-b border-border" : className}>
      {layout ? layout.renderHeader(state) : children}
    </div>
  );
});

/** The Summary tab: the layout's Overview when one is given, else `children`, today's. */
export function PullRequestSummarySlot({
  layout,
  state,
  children,
}: {
  layout?: PullRequestDetailsLayout;
  state: PullRequestDetailsState;
  children: ReactNode;
}) {
  const footer = layout?.renderSummaryFooter?.(state);
  if (!footer) {
    return (
      <TabsContent value="summary" className="min-h-0 flex-1 overflow-y-auto [container-type:size]">
        {layout ? layout.renderSummary(state) : children}
      </TabsContent>
    );
  }
  return (
    // The footer stays in view below the contents, which scroll on their own.
    <TabsContent
      value="summary"
      className="group/summary flex min-h-0 flex-1 flex-col overflow-hidden"
    >
      <div className="min-h-0 flex-1 overflow-y-auto [container-type:size]">
        {layout!.renderSummary(state)}
      </div>
      {/* The description's editor takes the room while it's open; the footer keeps its state. */}
      <div className="contents group-has-[[data-pull-request-description-editor]]/summary:hidden">
        {footer}
      </div>
    </TabsContent>
  );
}

function BranchChip({ name, title }: { name: string; title?: string }) {
  return (
    <code
      title={title ?? name}
      className="max-w-full min-w-0 truncate rounded-md bg-muted px-2 py-0.5 font-mono text-ui text-foreground"
    >
      {name}
    </code>
  );
}

function PullRequestTitleBlock({ state }: { state: PullRequestDetailsState }) {
  const { pr, files, lineTotals } = state;
  return (
    <div className="space-y-2">
      <PullRequestTitle
        // A different PR starts fresh, so an open title edit can't outlive its PR.
        key={state.selectedPrUrl ?? pr.url}
        conversationId={state.conversationId}
        prUrl={state.selectedPrUrl}
        title={pr.title}
        editable={state.titleEditable}
        size="page"
      />
      {pr.author && <p className="text-ui text-muted-foreground">Opened by {pr.author}</p>}
      {/* The branches and the size of the change; where it's read is in the header. */}
      <p className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 pt-1 text-ui text-muted-foreground">
        {pr.headRef && (
          <span className="flex max-w-full min-w-0 items-center gap-2">
            <BranchChip name={pr.headRef} />
            <span aria-label="into">→</span>
            <BranchChip name={pr.baseRef ?? "its base branch"} title={pr.baseRef ?? undefined} />
          </span>
        )}
        {files && (
          <span className="flex shrink-0 items-center gap-1.5 tabular-nums">
            <span>{files.length === 1 ? "1 file" : `${files.length} files`}</span>
            {lineTotals && (
              <>
                <span className="font-semibold text-status-green">+{lineTotals.added}</span>
                <span className="font-semibold text-status-red">−{lineTotals.removed}</span>
              </>
            )}
          </span>
        )}
      </p>
    </div>
  );
}

/** What the checks card's merge button would do, shown only while it would act. */
function MergeReadyCaption({
  action,
  baseRef,
}: {
  action: MergeReadyAction;
  baseRef?: string | null;
}) {
  return (
    <>
      <p className="text-ui font-semibold text-foreground">
        {action === "queue" ? "Ready for the merge queue" : "Ready to merge"}
      </p>
      {baseRef && (
        <p className="text-xs text-muted-foreground">
          {action === "queue"
            ? `Merges into ${baseRef} through its queue`
            : `Merges into ${baseRef}`}
        </p>
      )}
    </>
  );
}

function PullRequestOverview({ state }: { state: PullRequestDetailsState }) {
  const { reviewers, labels, mergeReadiness, stackLanding, description } = state.sections;
  const { merge } = state;
  return (
    // A pinned comment box below leaves less of the end to scroll clear of.
    <div
      className={cn(
        "mx-auto w-full max-w-5xl px-4 pt-5 @lg:px-8 @lg:pt-7",
        state.composer ? "pb-8" : "pb-16",
      )}
    >
      <PullRequestTitleBlock state={state} />
      {(labels || reviewers) && (
        <PullRequestSectionStyleContext.Provider value="overline">
          {/* Side by side where the page is wide enough; stacked in a narrow pane. */}
          <div className="mt-5 grid gap-6 border-t border-border pt-5 @2xl:grid-cols-2 @2xl:gap-10">
            {labels && <div className="min-w-0">{labels}</div>}
            {reviewers && <div className="min-w-0">{reviewers}</div>}
          </div>
        </PullRequestSectionStyleContext.Provider>
      )}
      <PullRequestSectionStyleContext.Provider value="heading">
        <div className="mt-6 border-t border-border pt-6">{description}</div>
        {/* The merge status and the checks behind it, which the header's status badge opens. */}
        <div
          {...{ [MERGE_SECTION_ATTRIBUTE]: "" }}
          tabIndex={-1}
          className="mt-8 scroll-mt-4 space-y-4 outline-none"
        >
          {mergeReadiness}
          {stackLanding}
          {/* The merge controls close the checks card, where the reference puts them. */}
          <PullRequestChecksSection
            // Each PR starts with its own completed checks folded or open.
            key={state.pr.url}
            {...state.checks}
            footer={
              merge && (
                <PullRequestMerge
                  // A different PR starts fresh, so an open dialog can't outlive its PR.
                  key={merge.prUrl}
                  {...merge}
                  beforeMerge={<EnableAutoMergeSlot pr={state.lifecycle} />}
                  readyCaption={{
                    merge: <MergeReadyCaption action="merge" baseRef={state.pr.baseRef} />,
                    queue: <MergeReadyCaption action="queue" baseRef={state.pr.baseRef} />,
                  }}
                />
              )
            }
          />
          <ConvertToDraftPrompt pr={state.lifecycle} />
          <ReadyForReviewPrompt pr={state.lifecycle} />
        </div>
        {/* Last, just above the comment box, where a posted comment shows up. */}
        <div className="mt-8 border-t border-border pt-6">{state.activity}</div>
      </PullRequestSectionStyleContext.Provider>
    </div>
  );
}

/** A line of what's still reading, as tall as what it stands in for. */
function Placeholder({ className }: { className: string }) {
  return (
    <div aria-hidden className={cn("rounded-md bg-muted motion-safe:animate-pulse", className)} />
  );
}

/**
 * The Overview while the PR's details read: the title and author as the list knows them, and
 * each section's heading in its place, so the details fill in without moving the page.
 */
function PullRequestOverviewPreview({
  preview,
  stack,
}: {
  preview: PullRequestPreview;
  stack: PullRequestStackView | null;
}) {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 pt-5 pb-16 @lg:px-8 @lg:pt-7">
      <div className="space-y-2">
        {preview.title ? (
          <PullRequestPageTitleHeading>{preview.title}</PullRequestPageTitleHeading>
        ) : (
          <Placeholder className="h-8 w-2/3" />
        )}
        {preview.author && (
          <p className="text-ui text-muted-foreground">Opened by {preview.author}</p>
        )}
        {/* The branches' chips, read with the details: a chip is one text line plus its padding. */}
        <p className="flex min-w-0 flex-wrap items-center pt-1 text-ui">
          <span className="block py-0.5">
            <Placeholder className="h-[1lh] w-72 max-w-full" />
          </span>
        </p>
      </div>
      <PullRequestSectionStyleContext.Provider value="overline">
        <div className="mt-5 grid gap-6 border-t border-border pt-5 @2xl:grid-cols-2 @2xl:gap-10">
          {/* Each value's loaded row sets its type and padding; the placeholder line takes the same. */}
          {[
            { title: "Labels", valueRowClass: "text-ui" },
            { title: "Reviewers", valueRowClass: "py-0.5 text-xs" },
          ].map(({ title, valueRowClass }) => (
            <div key={title} className="min-w-0 space-y-1.5">
              <PullRequestSectionHeader id={`preview-${title.toLowerCase()}`} title={title} />
              <div className={valueRowClass}>
                <Placeholder className="h-[1lh] w-32" />
              </div>
            </div>
          ))}
        </div>
      </PullRequestSectionStyleContext.Provider>
      <PullRequestSectionStyleContext.Provider value="heading">
        <div className="mt-6 space-y-1.5 border-t border-border pt-6">
          {/* As tall as the real heading row, which holds the description's Edit button. */}
          <div className="flex h-7 items-center">
            <PullRequestSectionHeader id="preview-description" title="Description" />
          </div>
          {/* GitHub's stack leads the description, as it will once the details read. */}
          {stack && <PullRequestStackCard stack={stack} />}
          <Placeholder className="h-4 w-full" />
          <Placeholder className="h-4 w-5/6" />
          <Placeholder className="h-4 w-2/3" />
        </div>
        <div className="mt-8 space-y-1.5">
          <PullRequestSectionHeader id="preview-checks" title="Checks" />
          <Placeholder className="h-24 w-full" />
        </div>
      </PullRequestSectionStyleContext.Provider>
    </div>
  );
}

/** The page while `preview`'s details read, laid out as they'll show, on `section`. */
export function PullRequestPagePreview({
  preview,
  copy,
  options,
  selectInPlace,
  section = "summary",
}: {
  preview: PullRequestPreview;
  copy: GitProviderCopy;
  options: PullRequestPageOptions;
  selectInPlace: (url: string) => boolean;
  /** The section the PR opens on, whose tab shows chosen while it reads. */
  section?: PullRequestSubtab;
}) {
  // The header and the Overview show the same stack the list nests the PR in.
  const stack = useListedStack(preview.stack, preview.url, selectInPlace);
  return (
    <div
      className="flex h-full min-h-0 flex-col"
      aria-busy="true"
      data-pull-request-preview={preview.url}
    >
      <div className="shrink-0 border-b border-border">
        <PullRequestPagePreviewHeader
          preview={preview}
          copy={copy}
          options={options}
          stack={stack}
          section={section}
        />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto [container-type:size]">
        {section === "summary" ? (
          <PullRequestOverviewPreview preview={preview} stack={stack} />
        ) : (
          <PullRequestSectionPreview section={section} />
        )}
      </div>
      <span role="status" className="sr-only">
        Loading {copy.requestName}…
      </span>
    </div>
  );
}

/**
 * Changes while they read, in the review's own frame: its toolbar as tall as it last drew at
 * this width, then the files beside the diff where the review shows them as a column, so the
 * review fills in without moving.
 */
function PullRequestChangesPreview() {
  const [frame, setFrame] = useState<HTMLDivElement | null>(null);
  const frameRef = useRef<HTMLDivElement | null>(null);
  frameRef.current = frame;
  const width = useElementWidth(frame);
  const [treeVisible] = useState(() => readDiffReviewTreeVisible(true));
  const treeColumn = treeVisible && reviewShowsTreeColumn(width);
  // The loaded review's tree width, so the files column doesn't move when it arrives.
  const treeWidth = useRememberedPaneWidth(
    PULL_REQUEST_REVIEW_TREE_WIDTH,
    frameRef,
    treeColumn ? width : null,
  ).width;
  // As tall as the review's toolbar drew at this width, rows it wrapped to included.
  const toolbarHeight = PULL_REQUEST_REVIEW_TOOLBAR_HEIGHTS.at(width);
  return (
    <div ref={setFrame} data-diff-review-placeholder className="flex h-full min-h-0 flex-col">
      <div
        data-diff-review-placeholder-toolbar
        style={toolbarHeight === null ? undefined : { height: `${toolbarHeight}px` }}
        className="flex min-h-9 shrink-0 items-center border-b border-border px-2 py-0.5"
      >
        <Placeholder className="h-4 w-40" />
      </div>
      <div className="relative flex min-h-0 flex-1">
        {treeColumn && (
          <div
            data-diff-review-tree-column
            style={{ width: `${treeWidth}px` }}
            className="shrink-0 space-y-2 border-r border-border p-3"
          >
            {["w-40", "w-32", "w-44", "w-28", "w-36"].map((rowWidth) => (
              <Placeholder key={rowWidth} className={cn("h-4", rowWidth)} />
            ))}
          </div>
        )}
        <div data-diff-review-content className="relative min-w-0 flex-1 space-y-3 p-4">
          <Placeholder className="h-6 w-1/3" />
          <Placeholder className="h-40 w-full" />
          <Placeholder className="h-6 w-1/4" />
          <Placeholder className="h-24 w-full" />
        </div>
      </div>
    </div>
  );
}

/** Changes, Commits or Comments while they read: their files beside a diff, or a list of rows. */
function PullRequestSectionPreview({
  section,
}: {
  section: Exclude<PullRequestSubtab, "summary">;
}) {
  if (section === "changes") return <PullRequestChangesPreview />;
  return (
    <div className="mx-auto w-full max-w-5xl space-y-4 px-4 pt-5 @lg:px-8 @lg:pt-7">
      {["w-2/3", "w-1/2", "w-3/5", "w-2/5"].map((width) => (
        <div key={width} className="space-y-1.5">
          <Placeholder className={cn("h-4", width)} />
          <Placeholder className="h-3 w-24" />
        </div>
      ))}
    </div>
  );
}

/** What the panel hands the page beyond the details' own state. */
export type PullRequestPageOptions = PullRequestPageHeaderOptions;

/** The new page's layout, closing over what the panel hands it. */
export function createPullRequestPageLayout(
  options: PullRequestPageOptions = {},
): PullRequestDetailsLayout {
  return {
    renderHeader: (state) => <PullRequestPageHeader state={state} options={options} />,
    renderSummary: (state) => <PullRequestOverview state={state} />,
    renderSummaryFooter: (state) => state.composer,
    renderPreview: (preview, copy, selectInPlace, section) => (
      <PullRequestPagePreview
        preview={preview}
        copy={copy}
        options={options}
        selectInPlace={selectInPlace}
        section={section}
      />
    ),
  };
}
