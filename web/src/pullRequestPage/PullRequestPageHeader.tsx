// The new pull request page's header, in two rows: the way back, the PR's name, state and merge
// status with where it's read in the top-right corner, then its sections and the panel's actions. It
// measures what fits (see `pullRequestHeaderFit.ts`), so at any width every section, count and
// status stays in view without scrolling.

import { type ReactNode, useLayoutEffect, useRef } from "react";
import {
  ArrowLeftIcon,
  FileDiffIcon,
  GitCommitHorizontalIcon,
  type LucideIcon,
  MessagesSquareIcon,
} from "lucide-react";

import {
  FoldingSectionTabs,
  type SectionTab,
  SectionTabsMeasure,
} from "@/components/sectionTabs/SectionTabs";
import { PullRequestFreshness } from "@/components/PullRequestFreshness";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs } from "@/components/ui/tabs";
import { PullRequestReviewMenu } from "@/pullRequestReviews/PullRequestReviewMenu";
import {
  PullRequestLifecycleBadge,
  pullRequestLifecycleVisual,
} from "@/shell/PullRequestLifecycleBadge";
import {
  PullRequestListToggle,
  type PullRequestListToggleState,
} from "@/shell/PullRequestListToggle";
import type { PullRequestSubtab } from "@/shell/pullRequestPanelView";
import { SHOW_ALL_PULL_REQUESTS_LABEL } from "@/shell/workspaceCommands/model";

import type { GitProviderCopy } from "@/lib/gitProviders";

import type { PullRequestDetailsState, PullRequestPreview } from "./detailsLayout";
import {
  PullRequestChangeNotice,
  PullRequestLifecycleOwner,
  PullRequestStateMenu,
  usePullRequestStatePillProgress,
} from "./PullRequestLifecycleControls";
import {
  type PullRequestHeaderName,
  type PullRequestHeaderSections,
  usePullRequestHeaderFit,
} from "./pullRequestHeaderFit";
import { type PullRequestPageSection, pullRequestPageSections } from "./pullRequestSections";
import {
  type HeaderMergeStatus,
  MergeStatusBadgeFace,
  mergeStatusBadgeClass,
  PullRequestMergeStatusBadge,
  revealMergeSection,
  useSteadyHeaderMergeStatus,
} from "./PullRequestMergeStatusBadge";
import {
  PullRequestStackDependency,
  PullRequestStackHeaderSizers,
  PullRequestStackMenu,
  PullRequestStackPending,
} from "./PullRequestStack";
import type { PullRequestStackView } from "./usePullRequestStack";
import "./pullRequestPageHeader.css";

/** Marks the section picker, so focus moves aimed at the sections find it. */
export const SECTION_PICKER_ATTRIBUTE = "data-pull-request-section-picker";

/**
 * Focus the control that shows the current section: its tab, or the picker standing in for
 * the tabs. False when neither is on screen, so the caller can fall back.
 */
export function focusSectionControl(root: ParentNode | null | undefined): boolean {
  const control =
    root?.querySelector<HTMLElement>(`[${SECTION_PICKER_ATTRIBUTE}]`) ??
    root?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]');
  if (!control || control.closest("[hidden]")) return false;
  control.focus();
  return document.activeElement === control;
}

const SECTION_ICONS: Record<PullRequestSubtab, LucideIcon> = {
  summary: MessagesSquareIcon,
  comments: MessagesSquareIcon,
  commits: GitCommitHorizontalIcon,
  changes: FileDiffIcon,
};

/** The sections as the shared tabs show them: each with its icon. */
function sectionTabs(sections: readonly PullRequestPageSection[]): SectionTab<PullRequestSubtab>[] {
  return sections.map((section) => ({ ...section, icon: SECTION_ICONS[section.id] }));
}

export interface PullRequestPageHeaderOptions {
  /** In a pull request's own tab: open the tab with all of them. */
  onShowAllPullRequests?: () => void;
  /** In a tab that can show the list beside the pull request; the shown list is the way back. */
  listToggle?: PullRequestListToggleState;
  /** The panel's own header actions, which the page's header takes over in a PR's tab. */
  actions?: ReactNode;
  /** Told when the page's header is on screen and when it leaves, so the panel defers to it. */
  onHeaderShown?: (shown: boolean) => void;
  /** A line under the header about its actions, such as a refresh that didn't go through. */
  notice?: ReactNode;
}

export function PullRequestPageHeader({
  state,
  options,
}: {
  state: PullRequestDetailsState;
  options: PullRequestPageHeaderOptions;
}) {
  const { copy, pr, tabs } = state;
  const fullName = `${state.repository} ${copy.prNumberPrefix}${pr.number}`;
  const href = /^https:\/\//.test(pr.url) ? pr.url : undefined;
  const actions = options.actions ?? state.refreshButton;
  const { onHeaderShown } = options;
  // Before paint, so the panel's own back row never shows beside this one.
  useLayoutEffect(() => {
    onHeaderShown?.(true);
    return () => onHeaderShown?.(false);
  }, [onHeaderShown]);
  const headerRef = useRef<HTMLDivElement>(null);
  const fit = usePullRequestHeaderFit(headerRef);
  const mergeStatus = useSteadyHeaderMergeStatus(pr.url, state.mergeStatus);
  const pillProgress = usePullRequestStatePillProgress(state.lifecycle);
  const openMergeSection = () => {
    tabs.select("summary");
    // The Overview may still be mounting; the section is found once it renders.
    revealMergeSection(headerRef.current?.closest<HTMLElement>('[data-slot="tabs"]') ?? null);
  };
  const sections = pullRequestPageSections(state);
  const names = pullRequestNames(state.repository, `${copy.prNumberPrefix}${pr.number}`);
  return (
    <div className="@container">
      <div
        ref={headerRef}
        className="pr-page-header"
        data-name={fit.name}
        data-lifecycle={fit.lifecycle}
        data-spacing={fit.spacing}
        data-status={fit.status}
        data-source={fit.source}
        data-sections={fit.sections}
      >
        <div data-header-row className="pr-page-header-top">
          <div className="pr-page-header-identity">
            <HeaderLead heading={state.heading} options={options} />
            {href ? (
              <a
                href={href}
                target="_blank"
                rel="noreferrer"
                // The link shows less of the name as the pane narrows; its name and tooltip keep it all.
                aria-label={`Open ${fullName} on ${copy.label}`}
                title={`Open ${fullName} on ${copy.label}`}
                className={`${NAME_CLASS} text-link hover:underline`}
              >
                {names[fit.name]}
              </a>
            ) : (
              <span title={fullName} aria-label={fullName} className={NAME_CLASS}>
                {names[fit.name]}
              </span>
            )}
          </div>
          <span data-header-part="badges" className="pr-page-header-badges">
            <PullRequestStateMenu
              pr={state.lifecycle}
              currentLabel={
                pr.state ? pullRequestLifecycleVisual(pr.state, pr.isDraft).label : undefined
              }
              badge={
                pr.state && (
                  <span data-header-lifecycle-live className="inline-flex">
                    <PullRequestLifecycleBadge
                      state={pr.state}
                      isDraft={pr.isDraft}
                      compact={fit.lifecycle === "icon"}
                      pending={pillProgress.kind === "running"}
                    />
                  </span>
                )
              }
            />
          </span>
          {!state.stack && state.stackLoading && (
            <span data-header-part="stack" className="pr-page-header-badges inline-flex gap-1">
              <PullRequestStackPending />
            </span>
          )}
          {state.stack && (
            // A stacked PR's place in its stack, beside its state; sized by its copies below.
            <span data-header-part="stack" className="pr-page-header-badges inline-flex gap-1">
              <PullRequestStackMenu
                stack={state.stack}
                active={state.active}
                compact={fit.stack === "compact"}
              />
              {fit.stack === "full" && <PullRequestStackDependency stack={state.stack} />}
            </span>
          )}
          {mergeStatus && (
            // Sized by its copies below, as its label shortens with the fit.
            <span className="pr-page-header-badges">
              <PullRequestMergeStatusBadge
                {...mergeStatus}
                length={fit.status}
                onOpen={openMergeSection}
                checks={state.headChecks}
              />
            </span>
          )}
        </div>
        {/* The confirmations and ⌘K rows, on every tab. */}
        <PullRequestLifecycleOwner pr={state.lifecycle} active={state.active} />
        <div className="pr-page-header-navigation">
          <SectionNavigation
            sections={sections}
            shown={tabs.shown}
            select={tabs.select}
            mode={fit.sections}
          />
          <div data-header-part="actions" className="ml-auto flex shrink-0 items-center gap-1">
            {/* Shorter as the header narrows, so the age stays beside the actions in any pane. */}
            {state.updatingToLatestPush && (
              <span role="status" className="shrink-0 text-ui text-muted-foreground">
                Updating to the latest push…
              </span>
            )}
            <PullRequestFreshness state={state.freshness ?? null} shortWhenNarrow />
            <PullRequestReviewMenu state={state} />
            {actions}
          </div>
        </div>
        {options.notice}
        {/* What a change to the PR came to, wherever it was started, on every tab. */}
        <PullRequestChangeNotice pr={state.lifecycle} />
        <HeaderSizers
          names={names}
          sections={sections}
          lifecycle={pr.state ? { state: pr.state, isDraft: pr.isDraft } : null}
          stack={state.stack}
          mergeStatus={mergeStatus}
        />
      </div>
    </div>
  );
}

const NAME_CLASS = "min-w-0 truncate text-ui font-semibold tabular-nums";

/** The header's lead: the panel's title, the list toggle, and the way back to every PR. */
function HeaderLead({
  heading,
  options,
}: {
  heading?: ReactNode;
  options: PullRequestPageHeaderOptions;
}) {
  const { listToggle } = options;
  // The way back stays beside a shown list too: it widens the list into the whole tab.
  const showAll = options.onShowAllPullRequests;
  if (!heading && !showAll && !listToggle) return null;
  return (
    <span data-header-part="lead" className="flex shrink-0 items-center gap-2">
      {heading}
      {listToggle && <PullRequestListToggle toggle={listToggle} />}
      {showAll && (
        <>
          {/* A PR's tab leads with the way back to all of them, as one breadcrumb. */}
          <Button
            variant="ghost"
            size="sm"
            className="-ml-1.5 shrink-0 px-1.5 text-muted-foreground @3xl:-ml-2 @3xl:px-2.5"
            // Named the same with or without its visible label, which a narrow pane hides.
            aria-label="Pull requests"
            title={SHOW_ALL_PULL_REQUESTS_LABEL}
            onClick={showAll}
          >
            <ArrowLeftIcon aria-hidden className="size-3.5" />
            <span className="hidden @3xl:inline">Pull requests</span>
          </Button>
          <span aria-hidden className="hidden text-foreground-tertiary @3xl:inline">
            /
          </span>
        </>
      )}
    </span>
  );
}

// Every section, with no counts: nothing has been read to count yet.
const PREVIEW_SECTIONS: PullRequestPageSection[] = pullRequestPageSections({
  tabs: {
    shown: "summary",
    select: () => {},
    commits: { shown: true },
  },
}).map(({ id, label }) => ({ id, label }));

// What the preview measures: room for two-digit Commits and Changes counts, which the loaded
// tabs most often show, so the preview folds its tabs where the loaded header will.
const PREVIEW_MEASURED_SECTIONS: PullRequestPageSection[] = PREVIEW_SECTIONS.map((section) =>
  section.id === "summary" ? section : { ...section, count: 10 },
);

/**
 * The header while the PR's details read: its name and state as the list knows them, and its
 * sections in their places but not yet open, so the details arrive in the same lines.
 */
export function PullRequestPagePreviewHeader({
  preview,
  copy,
  options,
  stack = null,
  section = "summary",
}: {
  preview: PullRequestPreview;
  copy: GitProviderCopy;
  options: PullRequestPageHeaderOptions;
  /** The stack the list nests the PR in, shown where the loaded header shows its own. */
  stack?: PullRequestStackView | null;
  /** The section the PR opens on, chosen in the tabs while it reads. */
  section?: PullRequestSubtab;
}) {
  const number = `${copy.prNumberPrefix}${preview.number}`;
  const fullName = `${preview.repository} ${number}`;
  const names = pullRequestNames(preview.repository, number);
  const lifecycle = preview.state ? { state: preview.state, isDraft: preview.isDraft } : null;
  const { onHeaderShown } = options;
  // Before paint, as the header it stands in for, so the panel's own back row never shows.
  useLayoutEffect(() => {
    onHeaderShown?.(true);
    return () => onHeaderShown?.(false);
  }, [onHeaderShown]);
  // Fitted as the loaded header is, so a narrow pane shortens and folds it the same way.
  const headerRef = useRef<HTMLDivElement>(null);
  const fit = usePullRequestHeaderFit(headerRef);
  return (
    <div className="@container">
      <Tabs value={section}>
        <div
          ref={headerRef}
          className="pr-page-header"
          data-name={fit.name}
          data-lifecycle={fit.lifecycle}
          data-spacing={fit.spacing}
          data-status={fit.status}
          data-source={fit.source}
          data-sections={fit.sections}
        >
          <div data-header-row className="pr-page-header-top">
            <div className="pr-page-header-identity">
              <HeaderLead options={options} />
              <span title={fullName} aria-label={fullName} className={NAME_CLASS}>
                {names[fit.name]}
              </span>
            </div>
            {lifecycle && (
              <span data-header-part="badges" className="pr-page-header-badges">
                {/* Marked as the loaded header's is, so the fit measures the badge only once. */}
                <span data-header-lifecycle-live className="inline-flex">
                  <PullRequestLifecycleBadge {...lifecycle} compact={fit.lifecycle === "icon"} />
                </span>
              </span>
            )}
            {stack && (
              // The list's stack, from the first frame, in the place the loaded header's takes.
              <span data-header-part="stack" className="pr-page-header-badges inline-flex gap-1">
                <PullRequestStackMenu
                  stack={stack}
                  active={false}
                  compact={fit.stack === "compact"}
                />
                {fit.stack === "full" && <PullRequestStackDependency stack={stack} />}
              </span>
            )}
          </div>
          <div className="pr-page-header-navigation">
            <SectionNavigation
              sections={PREVIEW_SECTIONS}
              shown={section}
              select={() => {}}
              mode={fit.sections}
              disabled
            />
            <div data-header-part="actions" className="ml-auto flex shrink-0 items-center gap-1">
              {options.actions}
            </div>
          </div>
          {options.notice}
          <HeaderSizers
            names={names}
            sections={PREVIEW_MEASURED_SECTIONS}
            lifecycle={lifecycle}
            stack={stack}
            mergeStatus={null}
          />
        </div>
      </Tabs>
    </div>
  );
}

/** `owner/repository #number`, shortened from the front. */
function pullRequestNames(
  repository: string,
  number: string,
): Record<PullRequestHeaderName, string> {
  const slash = repository.lastIndexOf("/");
  return {
    full: `${repository} ${number}`,
    repository: `${repository.slice(slash + 1)} ${number}`,
    number,
  };
}

/** The sections as tabs, or, where the header's fit says they don't fit, a picker. */
function SectionNavigation({
  sections,
  shown,
  select,
  mode,
  disabled = false,
}: {
  sections: PullRequestPageSection[];
  shown: PullRequestSubtab;
  select: (section: PullRequestSubtab) => void;
  mode: PullRequestHeaderSections;
  /** In their places but not yet open, as while the PR's details read. */
  disabled?: boolean;
}) {
  return (
    <FoldingSectionTabs
      tabs={sectionTabs(sections)}
      shown={shown}
      select={select}
      folded={mode === "menu"}
      disabled={disabled}
      label="Pull request"
      pickerLabel="Pull request section"
      className="pr-page-header-sections"
      listProps={HEADER_TABS_PROPS}
      pickerProps={HEADER_PICKER_PROPS}
    />
  );
}

const HEADER_TABS_PROPS = { "data-header-tabs": "" };
const HEADER_PICKER_PROPS = { [SECTION_PICKER_ATTRIBUTE]: "" } as const;

/**
 * Copies of what's measured that never take part in the layout: the name at each length, the
 * merge status at each label length, the read source at each width, and a row of section
 * labels styled as the tabs are. Plain text, so nothing here can take focus.
 */
function HeaderSizers({
  names,
  sections,
  lifecycle,
  stack,
  mergeStatus,
}: {
  names: Record<PullRequestHeaderName, string>;
  sections: PullRequestPageSection[];
  lifecycle: { state: string; isDraft: boolean } | null;
  stack: PullRequestStackView | null;
  mergeStatus: HeaderMergeStatus | null;
}) {
  return (
    <span aria-hidden className="pr-page-header-sizers" data-header-sizers>
      {(["full", "repository", "number"] as const).map((length) => (
        <span key={length} data-header-name={length} className="text-ui font-semibold tabular-nums">
          {names[length]}
        </span>
      ))}
      {lifecycle &&
        (["full", "icon"] as const).map((form) => (
          <span key={form} data-header-lifecycle={form} className="inline-flex">
            <PullRequestLifecycleBadge {...lifecycle} compact={form === "icon"} measuring />
          </span>
        ))}
      {stack && <PullRequestStackHeaderSizers stack={stack} />}
      {mergeStatus &&
        (["full", "short", "icon"] as const).map((length) => (
          <Badge
            key={length}
            data-header-status={length}
            className={mergeStatusBadgeClass(mergeStatus.status)}
          >
            <MergeStatusBadgeFace status={mergeStatus.status} length={length} />
          </Badge>
        ))}
      <SectionTabsMeasure tabs={sectionTabs(sections)} data-header-sections="" />
    </span>
  );
}
