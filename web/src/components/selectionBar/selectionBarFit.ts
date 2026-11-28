// How a list's selection bar fits the list's width. Nothing clips: each step down gives up the
// least useful thing that still doesn't fit, and every action stays a click away in More, with
// its full label kept in its tooltip and accessible name.

import { type RefObject, useLayoutEffect, useRef, useState } from "react";

export interface SelectionBarFit {
  /** Every action button shows its short label, with how many it acts on as a badge. */
  compact: boolean;
  /** "Select All 12", or "All 12". */
  selectAll: "full" | "short";
  /** How many actions, from the end of the bar's order, are listed in More instead. */
  folded: number;
  /** "3 selected", or just "3". */
  count: "full" | "number";
}

export const ROOMY_SELECTION_BAR: SelectionBarFit = {
  compact: false,
  selectAll: "full",
  folded: 0,
  count: "full",
};

/** Natural widths, in CSS pixels, each with its horizontal margins. */
export interface SelectionBarMeasurements {
  /** The widest the bar's content may be: the list less the bar's insets, padding and border. */
  available: number;
  /** The bar's gap, paid between each pair of items it renders. */
  gap: number;
  count: { full: number; number: number };
  /** Absent while every row shown is selected. */
  selectAll: { full: number; short: number } | null;
  /** What the bar always shows whatever its fit: the divider, Remove, Clear. */
  fixed: readonly number[];
  /** The More trigger. */
  more: number;
  /** Whether More lists anything without a fold, such as Open and Copy. */
  staticMore: boolean;
  /** Each action button in the bar's order, at its full and short labels. */
  actions: readonly { full: number; compact: number }[];
}

/** The browser lays out in 64ths of a pixel, so widths agree to within one. */
const LAYOUT_UNIT = 1 / 64;

/** The width an arrangement's items take, with the bar's gap between each pair. */
export function selectionBarWidth(
  measurements: SelectionBarMeasurements,
  fit: SelectionBarFit,
): number {
  const { actions, count, selectAll, fixed, more, staticMore, gap } = measurements;
  const shown = actions.length - fit.folded;
  const items = [
    count[fit.count],
    ...(selectAll ? [selectAll[fit.selectAll]] : []),
    ...fixed,
    ...actions.slice(0, shown).map((action) => (fit.compact ? action.compact : action.full)),
    ...(staticMore || fit.folded > 0 ? [more] : []),
  ];
  return items.reduce((total, width) => total + width, 0) + gap * Math.max(0, items.length - 1);
}

/**
 * The roomiest arrangement that fits, in order: everything at full length; every action at
 * its short label; Select All as "All"; actions into More from the end of the bar; the count as
 * a bare number. The last is used even if it still doesn't fit.
 */
export function fitSelectionBar(measurements: SelectionBarMeasurements): SelectionBarFit {
  const steps: SelectionBarFit[] = [
    ROOMY_SELECTION_BAR,
    { ...ROOMY_SELECTION_BAR, compact: true },
    { ...ROOMY_SELECTION_BAR, compact: true, selectAll: "short" },
  ];
  for (let folded = 1; folded <= measurements.actions.length; folded++) {
    steps.push({ compact: true, selectAll: "short", folded, count: "full" });
  }
  const last: SelectionBarFit = {
    compact: true,
    selectAll: "short",
    folded: measurements.actions.length,
    count: "number",
  };
  const fits = (fit: SelectionBarFit) =>
    selectionBarWidth(measurements, fit) <= measurements.available + LAYOUT_UNIT;
  return steps.find(fits) ?? last;
}

/** A fit as the bar applies it: which actions are in More, by id, so a change to the actions
 * can't move a surviving one there before the bar has measured again. */
export interface SelectionBarLayout extends Omit<SelectionBarFit, "folded"> {
  folded: ReadonlySet<string>;
}

export const ROOMY_SELECTION_BAR_LAYOUT: SelectionBarLayout = {
  ...ROOMY_SELECTION_BAR,
  folded: new Set(),
};

const sameLayout = (one: SelectionBarLayout, other: SelectionBarLayout) =>
  one.compact === other.compact &&
  one.selectAll === other.selectAll &&
  one.count === other.count &&
  one.folded.size === other.folded.size &&
  [...one.folded].every((id) => other.folded.has(id));

/**
 * The bar's layout, kept current as the list resizes, a label or count changes, the actions
 * change or More opens and closes. The bar marks what's measured: inert `[data-bar-sizers]`
 * copies of the count, Select All, More and each action (`[data-bar-action-size]`) at each
 * form, and the live `[data-bar-part="fixed"]` parts. A focused action button
 * (`[data-bar-action]`) that the next layout folds hands its focus to the More trigger.
 */
export function useSelectionBarFit({
  bar,
  more,
  staticMore,
  moreOpen,
  actionIds,
}: {
  bar: RefObject<HTMLElement | null>;
  more: RefObject<HTMLElement | null>;
  staticMore: boolean;
  /** An open More keeps its trigger, so the bar pays for it until it closes. */
  moreOpen: boolean;
  /** The bar's actions, in its order. */
  actionIds: readonly string[];
}): SelectionBarLayout {
  const [layout, setLayout] = useState(ROOMY_SELECTION_BAR_LAYOUT);
  const latest = useRef({ layout, more: staticMore || moreOpen });
  latest.current = { layout, more: staticMore || moreOpen };
  const focusMore = useRef(false);
  const refit = useRef(() => {});
  useLayoutEffect(() => {
    const element = bar.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const update = () => {
      const measured = measureSelectionBar(element, latest.current.more);
      // A hidden list has no width; the bar keeps its layout until it shows again.
      if (!measured) return;
      const fit = fitSelectionBar(measured.measurements);
      const { ids } = measured;
      const next: SelectionBarLayout = {
        compact: fit.compact,
        selectAll: fit.selectAll,
        count: fit.count,
        folded: new Set(ids.slice(ids.length - fit.folded)),
      };
      if (sameLayout(latest.current.layout, next)) return;
      // Before the button goes: once it's unmounted, focus has already fallen to the page.
      const focused = document.activeElement?.closest<HTMLElement>("[data-bar-action]");
      const focusedId = focused && element.contains(focused) ? focused.dataset.barAction : null;
      if (focusedId && next.folded.has(focusedId)) focusMore.current = true;
      latest.current = { ...latest.current, layout: next };
      setLayout(next);
    };
    refit.current = update;
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
      if (element.parentElement) resizes.observe(element.parentElement);
      for (const part of element.querySelectorAll(
        "[data-bar-size], [data-bar-part], [data-bar-action-size] [data-form]",
      )) {
        resizes.observe(part);
      }
    };
    // Labels and counts change as the selection does ("Remove Me 9/20", "Approving 1 of 3…"):
    // the copies hold them in `data-text`. Parts come and go too.
    const mutations =
      typeof MutationObserver === "undefined"
        ? undefined
        : new MutationObserver(() => {
            observeParts();
            schedule();
          });
    mutations?.observe(element, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ["data-text"],
    });
    observeParts();
    update();
    return () => {
      cancelAnimationFrame(frame);
      resizes.disconnect();
      mutations?.disconnect();
      refit.current = () => {};
    };
  }, [bar]);
  // The actions changed, or More opened or closed: fit again before paint, so the bar never
  // shows them at the last layout, and a focused button that now folds still hands its focus on.
  const actionsKey = actionIds.join("\n");
  const moreShown = staticMore || moreOpen;
  useLayoutEffect(() => {
    refit.current();
  }, [actionsKey, moreShown]);
  // After the fold commits, before paint: the trigger exists by now.
  useLayoutEffect(() => {
    if (!focusMore.current) return;
    focusMore.current = false;
    more.current?.focus();
  }, [layout, more]);
  return layout;
}

const pixels = (value: string) => Number.parseFloat(value) || 0;

/** An element's width with its horizontal margins, or 0 when it isn't there. */
function outerWidth(element: Element | null): number {
  if (!element) return 0;
  const style = getComputedStyle(element);
  return (
    element.getBoundingClientRect().width + pixels(style.marginLeft) + pixels(style.marginRight)
  );
}

function measureSelectionBar(
  bar: HTMLElement,
  staticMore: boolean,
): { measurements: SelectionBarMeasurements; ids: string[] } | undefined {
  const list = bar.parentElement;
  if (!list) return undefined;
  const style = getComputedStyle(bar);
  const listWidth = list.getBoundingClientRect().width;
  // The bar's inset from each side of the list: its max-width leaves that much.
  const inset = pixels(style.getPropertyValue("--selection-bar-inset"));
  const available =
    listWidth -
    2 * inset -
    pixels(style.paddingLeft) -
    pixels(style.paddingRight) -
    pixels(style.borderLeftWidth) -
    pixels(style.borderRightWidth);
  if (listWidth <= 0 || available <= 0) return undefined;
  const size = (name: string) => outerWidth(bar.querySelector(`[data-bar-size="${name}"]`));
  const hasSelectAll = bar.querySelector('[data-bar-size="select-all-full"]') !== null;
  const actions = [...bar.querySelectorAll<HTMLElement>("[data-bar-action-size]")];
  const measurements: SelectionBarMeasurements = {
    available,
    gap: pixels(style.columnGap),
    count: { full: size("count-full"), number: size("count-number") },
    selectAll: hasSelectAll
      ? { full: size("select-all-full"), short: size("select-all-short") }
      : null,
    fixed: [...bar.querySelectorAll('[data-bar-part="fixed"]')].map(outerWidth),
    more: size("more"),
    staticMore,
    actions: actions.map((sizer) => ({
      full: outerWidth(sizer.querySelector('[data-form="full"]')),
      compact: outerWidth(sizer.querySelector('[data-form="compact"]')),
    })),
  };
  return { measurements, ids: actions.map((sizer) => sizer.dataset.barActionSize ?? "") };
}
