// Split tree for the main terminal view: leaves are terminal tab keys
// (``terminalTabKey`` values), splits divide their area by ``ratio``.

export type TerminalSplitAxis = "columns" | "rows";
export type TerminalLayout =
  string | { axis: TerminalSplitAxis; ratio: number; children: [TerminalLayout, TerminalLayout] };

export interface TerminalSplitState {
  layout: TerminalLayout;
  /** The pane the header's split actions target. */
  focusedKey: string;
}

/** Percent-based rectangle inside the terminal view. */
export interface TerminalRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface TerminalDivider {
  /** Child indexes from the root to the split this divider resizes. */
  path: number[];
  axis: TerminalSplitAxis;
  ratio: number;
  /** Area of the split being resized. */
  area: TerminalRect;
}

// Neither side of a split may shrink below a quarter of its parent.
export const MIN_SPLIT_RATIO = 0.25;
export const MAX_SPLIT_RATIO = 0.75;

export function clampSplitRatio(ratio: number): number {
  // A zero-size container yields NaN; never persist a ratio the parser would reject.
  if (!Number.isFinite(ratio)) return 0.5;
  return Math.min(MAX_SPLIT_RATIO, Math.max(MIN_SPLIT_RATIO, ratio));
}

export function terminalLayoutKeys(layout: TerminalLayout): string[] {
  return typeof layout === "string" ? [layout] : layout.children.flatMap(terminalLayoutKeys);
}

/** Split ``targetKey`` in two, placing ``newKey`` right of or below it. */
export function splitTerminalLayout(
  layout: TerminalLayout,
  targetKey: string,
  newKey: string,
  axis: TerminalSplitAxis,
): TerminalLayout {
  if (typeof layout === "string") {
    return layout === targetKey ? { axis, ratio: 0.5, children: [layout, newKey] } : layout;
  }
  return {
    ...layout,
    children: [
      splitTerminalLayout(layout.children[0], targetKey, newKey, axis),
      splitTerminalLayout(layout.children[1], targetKey, newKey, axis),
    ],
  };
}

/** Drop ``key``'s pane; its sibling takes the freed space. Null when nothing is left. */
export function removeTerminalLayoutKey(
  layout: TerminalLayout,
  key: string,
): TerminalLayout | null {
  if (typeof layout === "string") return layout === key ? null : layout;
  const first = removeTerminalLayoutKey(layout.children[0], key);
  const second = removeTerminalLayoutKey(layout.children[1], key);
  if (first === null) return second;
  if (second === null) return first;
  if (first === layout.children[0] && second === layout.children[1]) return layout;
  return { ...layout, children: [first, second] };
}

export function replaceTerminalLayoutKey(
  layout: TerminalLayout,
  oldKey: string,
  newKey: string,
): TerminalLayout {
  if (typeof layout === "string") return layout === oldKey ? newKey : layout;
  return {
    ...layout,
    children: [
      replaceTerminalLayoutKey(layout.children[0], oldKey, newKey),
      replaceTerminalLayoutKey(layout.children[1], oldKey, newKey),
    ],
  };
}

export function resizeTerminalSplit(
  layout: TerminalLayout,
  path: readonly number[],
  ratio: number,
): TerminalLayout {
  if (typeof layout === "string") return layout;
  if (path.length === 0) return { ...layout, ratio: clampSplitRatio(ratio) };
  const [index, ...rest] = path;
  const children: [TerminalLayout, TerminalLayout] = [...layout.children];
  children[index] = resizeTerminalSplit(children[index], rest, ratio);
  return { ...layout, children };
}

/**
 * Drop panes whose terminal is gone and move a stale agent pane onto the live
 * agent terminal. ``removable`` says whether a missing key is known gone, as
 * opposed to not yet delivered. Returns ``state`` itself when nothing changes.
 */
export function reconcileTerminalSplit(
  state: TerminalSplitState,
  liveKeys: ReadonlySet<string>,
  {
    liveAgentKey,
    isAgentKey,
    removable,
  }: {
    liveAgentKey: string | null;
    isAgentKey: (key: string) => boolean;
    removable: (key: string) => boolean;
  },
): TerminalSplitState | undefined {
  let layout: TerminalLayout | null = state.layout;
  for (const key of terminalLayoutKeys(state.layout)) {
    if (layout === null || liveKeys.has(key)) continue;
    if (isAgentKey(key)) {
      if (liveAgentKey && !terminalLayoutKeys(layout).includes(liveAgentKey)) {
        layout = replaceTerminalLayoutKey(layout, key, liveAgentKey);
      }
    } else if (removable(key)) {
      layout = removeTerminalLayoutKey(layout, key);
    }
  }
  if (layout === state.layout) return state;
  if (layout === null || typeof layout === "string") return undefined;
  const keys = terminalLayoutKeys(layout);
  return { layout, focusedKey: keys.includes(state.focusedKey) ? state.focusedKey : keys[0] };
}

/** Flatten the tree into pane and divider rectangles (percent of the view). */
export function terminalLayoutGeometry(
  layout: TerminalLayout,
  area: TerminalRect = { left: 0, top: 0, width: 100, height: 100 },
  path: number[] = [],
): { panes: (TerminalRect & { key: string })[]; dividers: TerminalDivider[] } {
  if (typeof layout === "string") return { panes: [{ key: layout, ...area }], dividers: [] };
  const { axis, ratio } = layout;
  const [firstArea, secondArea]: [TerminalRect, TerminalRect] =
    axis === "columns"
      ? [
          { ...area, width: area.width * ratio },
          { ...area, left: area.left + area.width * ratio, width: area.width * (1 - ratio) },
        ]
      : [
          { ...area, height: area.height * ratio },
          { ...area, top: area.top + area.height * ratio, height: area.height * (1 - ratio) },
        ];
  const first = terminalLayoutGeometry(layout.children[0], firstArea, [...path, 0]);
  const second = terminalLayoutGeometry(layout.children[1], secondArea, [...path, 1]);
  return {
    panes: [...first.panes, ...second.panes],
    dividers: [{ path, axis, ratio, area }, ...first.dividers, ...second.dividers],
  };
}

/** Validate a persisted split; anything malformed restores as unsplit. */
export function parseTerminalSplitState(value: unknown): TerminalSplitState | undefined {
  const validLayout = (node: unknown): node is TerminalLayout => {
    if (typeof node === "string") return node.length > 0;
    if (!node || typeof node !== "object") return false;
    const { axis, ratio, children } = node as Record<string, unknown>;
    return (
      (axis === "columns" || axis === "rows") &&
      typeof ratio === "number" &&
      ratio >= MIN_SPLIT_RATIO &&
      ratio <= MAX_SPLIT_RATIO &&
      Array.isArray(children) &&
      children.length === 2 &&
      children.every(validLayout)
    );
  };
  if (!value || typeof value !== "object") return undefined;
  const { layout, focusedKey } = value as Record<string, unknown>;
  if (!validLayout(layout) || typeof layout === "string" || typeof focusedKey !== "string") {
    return undefined;
  }
  const keys = terminalLayoutKeys(layout);
  if (new Set(keys).size !== keys.length || !keys.includes(focusedKey)) return undefined;
  return { layout, focusedKey };
}
