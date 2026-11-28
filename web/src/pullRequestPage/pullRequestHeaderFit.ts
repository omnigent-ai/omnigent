// How the pull request page's header fits the pane's width. It's always two rows: the pull
// request, its state and merge status, with where it's read in the top-right corner, then its
// sections and actions. Nothing scrolls or clips: each step down gives up the least useful
// thing that still doesn't fit, and the full name, status and account stay in their tooltips
// and accessible names.

import { type RefObject, useLayoutEffect, useState } from "react";

/** How much of `owner/repository #number` the link shows. */
export type PullRequestHeaderName = "full" | "repository" | "number";

/**
 * How much the read source shows: its place and whole account, its place with the account
 * truncating to fit, or just its icon, with the rest in its tooltip and accessible name.
 */
export type PullRequestHeaderSource = "full" | "short" | "icon";

/** The merge status badge's full label, its short one, or just its icon. */
export type PullRequestHeaderStatus = "full" | "short" | "icon";

/** The sections as tabs, or as a picker showing the current one. */
export type PullRequestHeaderSections = "segments" | "menu";

/** The lifecycle pill's label, or just its icon in the narrowest header. */
export type PullRequestHeaderLifecycle = "full" | "icon";

/** A stacked PR's switcher and depends-on chip, or the switcher's pancake alone when narrowest. */
export type PullRequestHeaderStack = "full" | "compact";

/** The top row's spacing: its usual gaps, or half of them in the narrowest header. */
export type PullRequestHeaderSpacing = "roomy" | "tight";

export interface PullRequestHeaderFit {
  name: PullRequestHeaderName;
  spacing: PullRequestHeaderSpacing;
  lifecycle: PullRequestHeaderLifecycle;
  stack: PullRequestHeaderStack;
  status: PullRequestHeaderStatus;
  source: PullRequestHeaderSource;
  sections: PullRequestHeaderSections;
}

/** Natural widths, in CSS pixels, that don't depend on the fit chosen. */
export interface PullRequestHeaderMeasurements {
  /** The header's content width. */
  available: number;
  /** The space between neighboring items in a row. */
  gap: number;
  /** What leads the name, such as the way back to all pull requests; zero when absent. */
  lead: number;
  /** The link's width at each name length. */
  names: Record<PullRequestHeaderName, number>;
  /** The lifecycle pill, with its menu's chevron, as a label and as an icon. */
  badges: Record<PullRequestHeaderLifecycle, number>;
  /** The stack's switcher and chip at each form; absent or zero for a PR that isn't stacked. */
  stack?: Record<PullRequestHeaderStack, number>;
  /** The merge status badge at each label length; zero when there's none to show. */
  status: Record<PullRequestHeaderStatus, number>;
  /** The read source at each width, short being its place alone; zero when there's no choice. */
  source: Record<PullRequestHeaderSource, number>;
  /** The tabs with every label. */
  sections: number;
  /** The panel's actions, such as Refresh, which travel with the sections; zero when absent. */
  actions: number;
}

export const DEFAULT_PULL_REQUEST_HEADER_FIT: PullRequestHeaderFit = {
  name: "full",
  spacing: "roomy",
  lifecycle: "full",
  stack: "full",
  status: "full",
  source: "full",
  sections: "segments",
};

/** The browser lays out in 64ths of a pixel, so widths agree to within one. */
const LAYOUT_UNIT = 1 / 64;

/** Keeps the read source in the corner visibly apart from the status, while there's room. */
const SOURCE_SEPARATION = 16;

interface TopRowStep {
  name: PullRequestHeaderName;
  lifecycle: PullRequestHeaderLifecycle;
  status: PullRequestHeaderStatus;
  source: PullRequestHeaderSource;
  /** Roomy spacing, or the top row's half gaps and no room before the read source. */
  tight: boolean;
}

const step = (
  name: PullRequestHeaderName,
  status: PullRequestHeaderStatus,
  source: PullRequestHeaderSource,
  lifecycle: PullRequestHeaderLifecycle = "full",
  tight = false,
): TopRowStep => ({ name, lifecycle, status, source, tight });

/**
 * In order, what the top row gives up as it narrows: the owner first, as it's usually the
 * same for every pull request open, then the status's long label, then the account, then the
 * repository, then the source's place and the status's label, leaving their icons. Last, the
 * lifecycle pill keeps its icon, a stack's switcher its pancake, and the spacing tightens.
 */
const TOP_ROW_STEPS: readonly TopRowStep[] = [
  step("full", "full", "full"),
  step("repository", "full", "full"),
  step("repository", "short", "full"),
  step("repository", "short", "short"),
  step("number", "short", "short"),
  step("number", "short", "icon"),
  step("number", "icon", "icon"),
  step("number", "icon", "icon", "icon", true),
];

/** The roomiest arrangement whose rows fit. */
export function fitPullRequestHeader(
  measurements: PullRequestHeaderMeasurements,
): PullRequestHeaderFit {
  const { available, gap, lead, names, badges, status, source, sections, actions } = measurements;
  const stack = measurements.stack ?? { full: 0, compact: 0 };
  const row = (widths: number[], spacing = gap) => {
    const present = widths.filter((width) => width > 0);
    return (
      present.reduce((total, width) => total + width, 0) + spacing * Math.max(0, present.length - 1)
    );
  };
  // To the browser's layout unit: a row even that much wider wraps.
  const fits = (width: number) => width <= available + LAYOUT_UNIT;
  // The sections keep every label in view, or offer them as a picker.
  const navigation: PullRequestHeaderSections = fits(row([sections, actions]))
    ? "segments"
    : "menu";
  // A part with nothing to give up keeps its full form.
  const statusStep = (length: PullRequestHeaderStatus): PullRequestHeaderStatus => {
    if (status.full === 0) return "full";
    if (length === "short" && status.short >= status.full) return "full";
    return length;
  };
  const steps = TOP_ROW_STEPS.map((candidate): TopRowStep => ({
    ...candidate,
    status: statusStep(candidate.status),
    source: source.full > 0 ? candidate.source : "full",
  }));
  const fitOf = ({
    name,
    lifecycle,
    status: statusLength,
    source: sourceWidth,
    tight,
  }: TopRowStep) => ({
    name,
    lifecycle,
    stack: lifecycle === "icon" ? ("compact" as const) : ("full" as const),
    status: statusLength,
    source: sourceWidth,
    spacing: tight ? ("tight" as const) : ("roomy" as const),
    sections: navigation,
  });
  for (const candidate of steps) {
    const sourceWidth = source[candidate.source];
    const separation = !candidate.tight && sourceWidth > 0 ? SOURCE_SEPARATION : 0;
    const width = row(
      [
        lead,
        names[candidate.name],
        badges[candidate.lifecycle],
        stack[candidate.lifecycle === "icon" ? "compact" : "full"],
        status[candidate.status],
        sourceWidth,
      ],
      candidate.tight ? gap / 2 : gap,
    );
    if (fits(width + separation)) return fitOf(candidate);
  }
  // Narrower still, the number truncates, beside the narrowest of everything else.
  return fitOf(steps.at(-1)!);
}

/**
 * Measures the header's parts and keeps the fit current as the pane resizes or the parts
 * change, such as a count arriving or the Commits section appearing.
 *
 * The header marks what's measured: hidden `[data-header-name]`, `[data-header-lifecycle]`,
 * `[data-header-status]` and `[data-header-source]` sizers for each form of those parts, a
 * hidden `[data-header-sections]` row of section labels, plus the live
 * `[data-header-part="lead" | "badges" | "actions"]`. The live pill's badge,
 * `[data-header-lifecycle-live]`, tells its menu's chevron apart from the badge.
 */
export function usePullRequestHeaderFit(
  header: RefObject<HTMLElement | null>,
): PullRequestHeaderFit {
  const [fit, setFit] = useState(DEFAULT_PULL_REQUEST_HEADER_FIT);
  useLayoutEffect(() => {
    const element = header.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const update = () => {
      const measurements = measurePullRequestHeader(element);
      // A hidden pane has no width; it re-fits when it shows again.
      if (!measurements) return;
      const next = fitPullRequestHeader(measurements);
      setFit((current) =>
        current.name === next.name &&
        current.lifecycle === next.lifecycle &&
        current.stack === next.stack &&
        current.spacing === next.spacing &&
        current.status === next.status &&
        current.source === next.source &&
        current.sections === next.sections
          ? current
          : next,
      );
    };
    let frame = 0;
    const schedule = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        update();
      });
    };
    const resizes = new ResizeObserver(schedule);
    const observeParts = () => {
      resizes.disconnect();
      resizes.observe(element);
      for (const part of element.querySelectorAll(
        "[data-header-name], [data-header-lifecycle], [data-header-status], [data-header-source], [data-header-sections], [data-header-part]",
      )) {
        resizes.observe(part);
      }
    };
    // Parts come and go, such as a count once it loads.
    const mutations =
      typeof MutationObserver === "undefined"
        ? undefined
        : new MutationObserver(() => {
            observeParts();
            schedule();
          });
    mutations?.observe(element, { childList: true, subtree: true, characterData: true });
    observeParts();
    update();
    return () => {
      cancelAnimationFrame(frame);
      resizes.disconnect();
      mutations?.disconnect();
    };
  }, [header]);
  return fit;
}

function measurePullRequestHeader(header: HTMLElement): PullRequestHeaderMeasurements | undefined {
  const style = getComputedStyle(header);
  // Fractional, as the parts are: a whole-pixel clientWidth can round up past what wraps.
  const available =
    header.getBoundingClientRect().width -
    pixels(style.paddingLeft) -
    pixels(style.paddingRight) -
    pixels(style.borderLeftWidth) -
    pixels(style.borderRightWidth);
  const width = (selector: string) =>
    header.querySelector<HTMLElement>(selector)?.getBoundingClientRect().width ?? 0;
  const sections = header.querySelector<HTMLElement>("[data-header-sections]");
  if (available <= 0 || !sections) return undefined;
  const pill = width('[data-header-part="badges"]');
  const lifecycleMenu = pill - width("[data-header-lifecycle-live]");
  // Without copies to measure, the pill keeps the width it has.
  const lifecycle = (form: PullRequestHeaderLifecycle) => {
    const sizer = width(`[data-header-lifecycle="${form}"]`);
    return sizer > 0 ? sizer + lifecycleMenu : pill;
  };
  return {
    available,
    // The rows' usual gap, from the measuring copies, whose spacing the fit never changes.
    gap: pixels(getComputedStyle(header.querySelector("[data-header-sizers]") ?? header).columnGap),
    lead: width('[data-header-part="lead"]'),
    names: {
      full: width('[data-header-name="full"]'),
      repository: width('[data-header-name="repository"]'),
      number: width('[data-header-name="number"]'),
    },
    // The pill's sizers hold its badge alone; the live pill adds its menu's chevron to either.
    badges: { full: lifecycle("full"), icon: lifecycle("icon") },
    stack: {
      full: width('[data-header-stack="full"]'),
      compact: width('[data-header-stack="compact"]'),
    },
    status: {
      full: width('[data-header-status="full"]'),
      short: width('[data-header-status="short"]'),
      icon: width('[data-header-status="icon"]'),
    },
    source: {
      full: width('[data-header-source="full"]'),
      short: width('[data-header-source="short"]'),
      icon: width('[data-header-source="icon"]'),
    },
    sections: sections.getBoundingClientRect().width,
    actions: width('[data-header-part="actions"]'),
  };
}

function pixels(value: string): number {
  const number = Number.parseFloat(value);
  return Number.isFinite(number) ? number : 0;
}
