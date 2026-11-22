// What a pane's strip row has room for at a width, for every pane kind. Actions fold first; below
// the row's own floor, only its controls stay.

/** The row's padding, both sides together (px-1). */
export const STRIP_PADDING = 8;
/** The gap between the row's parts (gap-1). */
export const STRIP_GAP = 4;
/** The pane icon with its margins (size-3.5, mx-1). */
export const STRIP_ICON_WIDTH = 22;
/** Each button (size-5). */
export const STRIP_BUTTON_WIDTH = 20;
/** The least the title shows at, above the floor. */
export const STRIP_TITLE_MIN_WIDTH = 48;
export interface PaneStripParts {
  /** How many actions the pane has, in the order they stay: Split Right, Split Down, Maximize, Close. */
  actions: number;
  /** The pane is maximized and can be restored: Restore always shows. */
  restore: boolean;
  /** Other parts that always show, such as a status or Show Chat, by width. */
  fixed?: readonly number[];
}

export interface PaneStripLayout {
  /** Below the floor: the icon and title (kept for screen readers) give way to the controls. */
  compact: boolean;
  /** How many of the pane's actions show as buttons, from the first; the rest give way. */
  actions: number;
  /** The title shows; otherwise it's for screen readers only. */
  title: boolean;
}

/** The row's width for `parts` widths: padding, each part and the gaps between them. */
export const stripRowWidth = (parts: readonly number[]) =>
  STRIP_PADDING + parts.reduce((sum, part) => sum + part, 0) + STRIP_GAP * (parts.length - 1);

/** The parts that always show, with the title at its least. */
function permanentParts(parts: PaneStripParts, title: boolean, icon = true): number[] {
  return [
    ...(icon ? [STRIP_ICON_WIDTH] : []),
    ...(title ? [STRIP_TITLE_MIN_WIDTH] : []),
    ...(parts.fixed ?? []),
    ...(parts.restore ? [STRIP_BUTTON_WIDTH] : []),
  ];
}

/** The least width a row keeps everything for: a split never leaves a pane narrower than this. */
export const paneStripMinWidth = (parts: PaneStripParts) =>
  stripRowWidth(permanentParts(parts, true));

/** The floor a split keeps for every row: Restore and room for one action button. */
export const PANE_STRIP_MIN_WIDTH = paneStripMinWidth({
  actions: 0,
  restore: true,
  fixed: [STRIP_BUTTON_WIDTH],
});

/** The row's parts for a pane `paneWidth` pixels wide. */
export function paneStripLayout(paneWidth: number, parts: PaneStripParts): PaneStripLayout {
  const width = Number.isFinite(paneWidth) ? Math.max(0, Math.floor(paneWidth)) : 0;
  const base = stripRowWidth(permanentParts(parts, true));
  if (width < base) return { compact: true, title: false, actions: 0 };
  const room = width - base;
  const actions = Math.min(parts.actions, Math.floor(room / (STRIP_BUTTON_WIDTH + STRIP_GAP)));
  return { compact: false, title: true, actions };
}

/** The pixels the layout's shown parts take, the title at its least; never more than the width above the floor. */
export function paneStripShownWidth(layout: PaneStripLayout, parts: PaneStripParts): number {
  const shown = [
    ...permanentParts(parts, layout.title && !layout.compact, !layout.compact),
    ...Array.from({ length: layout.actions }, () => STRIP_BUTTON_WIDTH),
  ];
  return stripRowWidth(shown);
}
