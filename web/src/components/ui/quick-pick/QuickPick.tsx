import {
  type KeyboardEvent,
  type MouseEvent,
  type MutableRefObject,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AlertCircleIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  SearchIcon,
  XIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { CompactShortcutKeys } from "@/components/ui/kbd";
import { Spinner } from "@/components/ui/spinner";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { pickListInputProps, readlineListStep } from "@/lib/listNavigationKeys";
import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { type Chord, chordAriaShortcut, chordKeys, chordMatches } from "@/lib/shortcuts/chord";
import { cn } from "@/lib/utils";

import { readQuickPickHistory, recordQuickPickHistory } from "./history";
import { HighlightedText, type MatchHighlight } from "./HighlightedText";
import type {
  QuickPickAcceptDetails,
  QuickPickAcceptResult,
  QuickPickGroupAction,
  QuickPickInputError,
  QuickPickItem,
  QuickPickItemRow,
  QuickPickRow,
  QuickPickRowAction,
  QuickPickSource,
  QuickPickStats,
} from "./model";
import {
  buildQuickPickRows,
  navigateQuickPick,
  type QuickPickNavigation,
  type QuickPickSegment,
  reconcileActiveIndex,
} from "./presentation";
import { useQuickPickSources } from "./useQuickPickSources";

const NO_SEGMENTS: readonly QuickPickSegment<never>[] = [];
const NO_SOURCES: readonly QuickPickSource<never>[] = [];
const NO_GROUP_ACTIONS: readonly QuickPickGroupAction[] = [];

export interface QuickPickProps<Value> {
  /** Accessible name for the query field and results, such as "Command palette". */
  label: string;
  placeholder?: string;
  /** Runs of items in display order, such as search results and then commands. */
  segments?: readonly QuickPickSegment<Value>[];
  /** Loaders for items that arrive asynchronously, listed after `segments`. */
  sources?: readonly QuickPickSource<Value>[];
  /**
   * List `sources`' rows before `segments`, for a picker whose async results lead, unless an
   * item matches the whole query from a word's start, such as a typed command name.
   */
  sourcesFirst?: boolean;
  query: string;
  onQueryChange: (query: string) => void;
  onAccept: (item: QuickPickItem<Value>, details: QuickPickAcceptDetails) => QuickPickAcceptResult;
  /**
   * ⌘↵ for the whole picker instead of the active row, such as handing its search to a
   * panel; it works with no row active. Without it, ⌘↵ accepts the active row as alternate.
   */
  onAlternate?: () => QuickPickAcceptResult;
  /** Called for Escape when the picker is not inside a dialog. */
  onDismiss?: () => void;
  /** The footer's Close button, for a picker whose Escape is handled around it, such as a dialog. */
  onClose?: () => void;
  /** Footer text summarizing results, such as `formatQuickPickSummary`. */
  summary?: (stats: QuickPickStats) => ReactNode;
  /** Footer hint for Enter; defaults to Select. */
  acceptLabel?: string;
  /**
   * Footer hint for ⌘↵ / Ctrl+↵; omit when there is no alternate action. A function names the
   * active row's alternate, or returns undefined for a row without one.
   */
  alternateLabel?: string | ((item: QuickPickItem<Value>) => string | undefined);
  /**
   * Shown on a child page, such as one opened from a row: a back button, and
   * Escape or Backspace in an empty query return to the parent page.
   */
  onBack?: () => void;
  /** Accessible name for the back button, such as "Back to themes". */
  backLabel?: string;
  /**
   * What the rows are, such as one opening of a child page. A new scope forgets the row the
   * user picked, as a new query does, so a page doesn't inherit another page's pick.
   */
  selectionScope?: string;
  /**
   * Right Arrow with the caret at the end of the query: return true to have handled it for
   * the active item, such as opening a related child page. Rows that `opensPage` open their
   * page on Right Arrow even without this.
   */
  onExpand?: (item: QuickPickItem<Value>) => boolean;
  /**
   * Turns typed text into a selectable first row, such as a new project's name.
   * Return `{ error }` to explain why the text can't be used, or null to ignore it.
   */
  createItem?: (query: string) => QuickPickItem<Value> | QuickPickInputError | null;
  /** Remembers accepted items under this namespace and lists them first while the query is empty. */
  historyKey?: string;
  /** How rows' matched text stands out, unless a row sets its own. */
  matchHighlight?: MatchHighlight;
  /** Shown and announced when no row is listed, such as "No results found"; null shows nothing. */
  emptyMessage: ReactNode;
  /**
   * Shown above the rows, such as a load error with Retry. It isn't announced; mark the
   * caller's text `role="status"` and keep buttons outside it.
   */
  status?: ReactNode;
  /** Content below the rows inside the scrolling area, such as a button that loads more. */
  listEnd?: ReactNode;
  /** Trailing content beside the query field, such as a close button. */
  inputEnd?: ReactNode;
  /** Buttons in a group's heading, such as Close all, by the group's label. */
  groupActions?: (group: string) => readonly QuickPickGroupAction[];
  /** Each group's heading shows how many of its rows are listed. */
  groupCounts?: boolean;
  /** Content under the query field across the picker's width, such as a search's filters. */
  belowInput?: ReactNode;
  autoFocus?: boolean;
  /**
   * A row the caller wants active, such as the last row before ones still loading.
   * It stays active while set, so clear it in `onActiveIdChange` once the user moves.
   */
  activeId?: string | null;
  onActiveIdChange?: (id: string) => void;
  /**
   * Called whenever a different row becomes active, by the user or as results change, such
   * as a query filtering the active one out, for a caller previewing it. Not while none is.
   */
  onActiveRowShown?: (id: string) => void;
  /**
   * Ctrl-N/Ctrl-P step through rows instead of moving the caret, as macOS text fields do.
   * Off by default, so the query keeps its readline caret keys.
   */
  stepsWithCtrlNP?: boolean;
  /** `data-testid` for the query field. */
  inputTestId?: string;
  /** Arrow keys wrap around at the ends; off by default, as in a menu. */
  loop?: boolean;
  /** Grow the results to fill the container, such as a full-screen sheet on phones. */
  fillHeight?: boolean;
  /**
   * The active row's preview, shown beside the list, such as a file's contents. Without it the
   * list takes the picker's whole width.
   */
  renderPreview?: (item: QuickPickItem<Value>) => ReactNode;
  /**
   * `sidebar` is a search field in a sidebar: results show only while there is a query, as
   * two-line rows, with the field in a muted well and a button that clears it. `switcher` is a
   * compact list for a transient switcher driven by shortcuts: no visible query field, and the
   * list sized to its rows.
   */
  presentation?: QuickPickPresentation;
  /** Receives the query field, such as to focus it from outside the picker. */
  inputRef?: MutableRefObject<HTMLInputElement | null>;
  className?: string;
}

export type QuickPickPresentation = "default" | "switcher" | "sidebar";

function readRecents(historyKey: string | undefined): string[] {
  return historyKey ? readQuickPickHistory(historyKey) : [];
}

function isInputError(value: unknown): value is QuickPickInputError {
  return typeof value === "object" && value !== null && "error" in value;
}

interface RowSection<Value> {
  heading?: { key: string; label: string };
  rows: { row: QuickPickItemRow<Value>; index: number }[];
}

// A heading starts a named section for its group's rows; rows outside any
// group, such as ungrouped commands after a group, form an unnamed one.
function sectionsOf<Value>(rows: readonly QuickPickRow<Value>[]): RowSection<Value>[] {
  const sections: RowSection<Value>[] = [];
  rows.forEach((row, index) => {
    if (row.kind === "heading") {
      sections.push({ heading: row, rows: [] });
      return;
    }
    const current = sections.at(-1);
    // Consecutive ungrouped rows share one unnamed section, as a heading's rows share its.
    const belongs = current !== undefined && (current.heading?.key ?? undefined) === row.headingKey;
    if (current && belongs) current.rows.push({ row, index });
    else sections.push({ rows: [{ row, index }] });
  });
  return sections;
}

interface RowRun<Value> {
  cards: boolean;
  rows: RowSection<Value>["rows"];
}

// Consecutive card items form one run, laid out as a grid; every other row stands alone.
function runsOf<Value>(rows: RowSection<Value>["rows"], cardsShown: boolean): RowRun<Value>[] {
  const runs: RowRun<Value>[] = [];
  for (const entry of rows) {
    const card = cardsShown && entry.row.item.layout === "card";
    const last = runs.at(-1);
    if (card && last?.cards) last.rows.push(entry);
    else runs.push({ cards: card, rows: [entry] });
  }
  return runs;
}

function resultCountText(resultCount: number): string {
  return resultCount === 1 ? "1 result" : `${resultCount.toLocaleString()} results`;
}

/** Default footer summary, such as "2,000 results · 42 ms". */
export function formatQuickPickSummary({
  resultCount,
  elapsedMs,
  loading,
}: QuickPickStats): string {
  const count = resultCountText(resultCount);
  if (loading) return `${count} · Searching…`;
  return elapsedMs === undefined ? count : `${count} · ${Math.round(elapsedMs)} ms`;
}

/** A footer hint that also works by click: it does what its keys do, keeping focus in the query. */
function FooterAction({
  keys,
  shortcut,
  label,
  onClick,
  disabled = false,
}: {
  keys: readonly string[];
  /** The keys as `aria-keyshortcuts` names them, such as "Meta+Enter". */
  shortcut: string;
  label: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  // The key caps are hidden from assistive tech, so the name says the key.
  const spokenKey = shortcut.replace("Meta+", "⌘").replace("Control+", "Ctrl+");
  return (
    <Button
      type="button"
      variant="ghost"
      size="xs"
      data-slot="quick-pick-footer-action"
      aria-label={`${label} (${spokenKey})`}
      aria-keyshortcuts={shortcut}
      disabled={disabled}
      onMouseDown={(event) => event.preventDefault()}
      onClick={onClick}
      className="-mx-1 shrink-0 gap-1.5 px-1 font-normal text-muted-foreground hover:text-foreground"
    >
      <CompactShortcutKeys keys={keys} />
      <span>{label}</span>
    </Button>
  );
}

/** The `aria-keyshortcuts` of a row's buttons, space-separated as the attribute lists them. */
function rowActionShortcuts(actions: readonly QuickPickRowAction[] | undefined) {
  const shortcuts = (actions ?? []).flatMap((action) =>
    action.chord ? [chordAriaShortcut(action.chord)] : [],
  );
  return shortcuts.length > 0 ? shortcuts.join(" ") : undefined;
}

/**
 * Whether `chord` would edit the query instead, as ⌘⌫ or Ctrl+Backspace deletes text before the
 * caret, so the field keeps it while there's something there to delete.
 */
function chordEditsField(chord: Chord, field: HTMLInputElement): boolean {
  const key = chord.key?.toLowerCase();
  if (key !== "backspace" && key !== "delete") return false;
  const start = field.selectionStart ?? field.value.length;
  const end = field.selectionEnd ?? start;
  if (start !== end) return true;
  return key === "backspace" ? start > 0 : start < field.value.length;
}

/** A button beside a row, for the pointer: it never takes focus, which stays in the query. */
function RowActionButton({
  action,
  isMac,
  onRun,
}: {
  action: QuickPickRowAction;
  isMac: boolean;
  onRun: () => void;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          tabIndex={-1}
          aria-label={action.label}
          data-slot="quick-pick-row-action"
          data-action={action.id}
          onMouseDown={(event) => event.preventDefault()}
          onClick={onRun}
          className="text-muted-foreground hover:text-foreground"
        >
          {action.icon}
        </Button>
      </TooltipTrigger>
      <TooltipContent shortcut={action.chord ? chordKeys(action.chord, isMac) : undefined}>
        {action.label}
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * Keyboard-first picker: a query field over fuzzy-ranked rows grouped under
 * headings. The caller owns the query and what accepting a row does.
 */
// Meta jumps to the ends and Alt moves between groups; Shift+Home/End stay with the query field.
function navigationFor(
  event: KeyboardEvent<HTMLInputElement>,
  pageSize: () => number,
  stepsWithCtrlNP: boolean,
): QuickPickNavigation | null {
  // An opted-in picker steps with Ctrl-N/Ctrl-P, as menus with a filter do.
  const readlineStep = stepsWithCtrlNP ? readlineListStep(event) : null;
  if (readlineStep) return { type: readlineStep };
  switch (event.key) {
    case "ArrowDown":
    case "ArrowUp": {
      const forward = event.key === "ArrowDown";
      if (event.metaKey) return { type: forward ? "last" : "first" };
      if (event.altKey) return { type: forward ? "nextGroup" : "previousGroup" };
      return { type: forward ? "next" : "previous" };
    }
    case "Home":
      return event.shiftKey ? null : { type: "first" };
    case "End":
      return event.shiftKey ? null : { type: "last" };
    case "PageDown":
      return { type: "pageDown", pageSize: pageSize() };
    case "PageUp":
      return { type: "pageUp", pageSize: pageSize() };
    default:
      return null;
  }
}

/** The list's and the preview's height side by side, so neither jumps as the rows change. */
const PREVIEW_HEIGHT = "h-[min(24rem,60vh)]";

/** The result list's height: the container's, a switcher's rows, beside a preview, or capped. */
function listHeightClass({
  fillHeight,
  switcher,
  preview,
}: {
  fillHeight: boolean;
  switcher: boolean;
  preview: boolean;
}): string {
  if (fillHeight) return "min-h-0 flex-1";
  if (switcher) return "max-h-[min(24rem,60vh)] min-h-16";
  return preview ? PREVIEW_HEIGHT : "max-h-72";
}

/**
 * The list alone, or beside the active row's preview when the picker has one. A row with
 * nothing to preview, or no active row, shows "No preview" in the same place.
 */
function PreviewBeside({
  enabled,
  preview,
  fillHeight,
  hidden,
  children,
}: {
  enabled: boolean;
  preview: ReactNode;
  fillHeight: boolean;
  hidden: boolean;
  children: ReactNode;
}) {
  if (!enabled) return children;
  return (
    <div className={cn("flex min-h-0", fillHeight && "flex-1", hidden && "hidden")}>
      {children}
      <div
        data-slot="quick-pick-preview"
        className={cn(
          "min-w-0 flex-1 overflow-auto border-l border-border",
          fillHeight ? "min-h-0" : PREVIEW_HEIGHT,
        )}
      >
        {preview === undefined || preview === null || preview === false ? (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            No preview
          </div>
        ) : (
          preview
        )}
      </div>
    </div>
  );
}

export function QuickPick<Value>({
  label,
  placeholder,
  segments = NO_SEGMENTS,
  sources = NO_SOURCES,
  sourcesFirst = false,
  query,
  onQueryChange,
  onAccept,
  onAlternate,
  onDismiss,
  onClose = onDismiss,
  onBack,
  backLabel = "Back",
  onExpand,
  selectionScope,
  summary,
  acceptLabel = "Select",
  alternateLabel,
  createItem,
  historyKey,
  matchHighlight = "emphasis",
  emptyMessage,
  status,
  listEnd,
  inputEnd,
  groupActions,
  groupCounts = false,
  autoFocus = true,
  activeId = null,
  onActiveIdChange,
  onActiveRowShown,
  stepsWithCtrlNP = false,
  inputTestId,
  loop = false,
  fillHeight = false,
  renderPreview,
  belowInput,
  presentation = "default",
  inputRef,
  className,
}: QuickPickProps<Value>) {
  const baseId = useId();
  const listId = `${baseId}-list`;
  const errorId = `${baseId}-error`;
  // Keyed by the row, not its position, so an option keeps its ID when results above it change.
  const optionId = (row: QuickPickRow<Value>) => `${baseId}-option-${encodeURIComponent(row.key)}`;
  const sidebar = presentation === "sidebar";
  const switcher = presentation === "switcher";
  // A sidebar search shows only its field until there is something to search for, so
  // no row is listed (or can be picked) while collapsed.
  const collapsed = sidebar && query.trim().length === 0;

  // Recents are read again when the namespace changes, such as switching scopes,
  // and after an accept that keeps the picker open.
  const [history, setHistory] = useState(() => ({ key: historyKey, ids: readRecents(historyKey) }));
  if (history.key !== historyKey) setHistory({ key: historyKey, ids: readRecents(historyKey) });
  const recentIds = history.ids;
  const { states, retry } = useQuickPickSources(sources, query);
  const allSegments = useMemo(
    () => [
      ...segments,
      ...sources.map((source): QuickPickSegment<Value> => ({
        sourceId: source.id,
        ranking: source.ranking ?? "client",
        origin: "source",
        items: states[source.id]?.items ?? [],
      })),
    ],
    [segments, sources, states],
  );
  const freeText = useMemo(() => {
    const trimmed = query.trim();
    if (!createItem || trimmed.length === 0) return { item: null, error: null };
    const created = createItem(trimmed);
    if (isInputError(created)) return { item: null, error: created.error };
    return { item: created, error: null };
  }, [createItem, query]);
  const rows = useMemo(
    () =>
      collapsed
        ? []
        : buildQuickPickRows({
            segments: allSegments,
            query,
            recentIds,
            freeTextItem: freeText.item,
            sourcesFirst,
          }),
    [collapsed, allSegments, query, recentIds, freeText.item, sourcesFirst],
  );
  const sourceStates = sources.map((source) => states[source.id]);
  const loading = sourceStates.some((state) => !state || state.status === "loading");
  const sourceError = sourceStates.find((state) => state?.status === "error")?.error ?? null;
  const elapsedValues = sourceStates.flatMap((state) =>
    state?.elapsedMs === undefined ? [] : [state.elapsedMs],
  );
  const isMac = useMemo(() => isMacPlatform(), []);
  const sections = useMemo(() => sectionsOf(rows), [rows]);
  const headingCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of rows) {
      if (row.kind === "item" && row.headingKey) {
        counts.set(row.headingKey, (counts.get(row.headingKey) ?? 0) + 1);
      }
    }
    return counts;
  }, [rows]);
  const headingActions = useMemo(() => {
    const byKey = new Map<string, readonly QuickPickGroupAction[]>();
    if (!groupActions) return byKey;
    for (const row of rows) {
      if (row.kind !== "heading") continue;
      const actions = groupActions(row.label);
      if (actions.length > 0) byKey.set(row.key, actions);
    }
    return byKey;
  }, [rows, groupActions]);
  // Card items show as cards only until the user types; results are plain rows in rank order.
  const cardsShown = query.trim().length === 0;
  const resultCount = rows.filter((row) => row.kind === "item").length;
  const showEmpty = !collapsed && resultCount === 0;
  const queryFieldRef = useRef<HTMLInputElement | null>(null);
  const listboxRef = useRef<HTMLDivElement | null>(null);
  // Where each mounted heading with buttons sits in the list, so its buttons, kept outside the
  // listbox, line up with it, and how wide they are, which the heading leaves free for them;
  // measured after every render, as rows come and go.
  const [headingActionBars, setHeadingActionBars] = useState<
    readonly { key: string; top: number; height: number; width: number }[]
  >([]);
  // The last measurement written: an unchanged one sets no state, since even a same-value
  // update renders the list again, which would measure again after every commit.
  const measuredBars = useRef("[]");
  useLayoutEffect(() => {
    const listbox = listboxRef.current;
    const bars: { key: string; top: number; height: number; width: number }[] = [];
    if (listbox && headingActions.size > 0) {
      const origin = listbox.getBoundingClientRect();
      const barWidths = new Map<string, number>();
      for (const bar of listbox.parentElement?.querySelectorAll<HTMLElement>(
        "[data-actions-for]",
      ) ?? []) {
        barWidths.set(bar.dataset.actionsFor!, bar.getBoundingClientRect().width);
      }
      for (const heading of listbox.querySelectorAll<HTMLElement>("[data-heading-key]")) {
        const key = heading.dataset.headingKey!;
        if (!headingActions.has(key)) continue;
        const box = heading.getBoundingClientRect();
        bars.push({
          key,
          top: box.top - origin.top,
          height: box.height,
          width: barWidths.get(key) ?? 0,
        });
      }
    }
    const measured = JSON.stringify(bars);
    if (measured === measuredBars.current) return;
    measuredBars.current = measured;
    setHeadingActionBars(bars);
  });
  const setQueryField = useCallback(
    (node: HTMLInputElement | null) => {
      queryFieldRef.current = node;
      if (inputRef) inputRef.current = node;
    },
    [inputRef],
  );

  // Until the user picks a row for this query, the top row stays active as results arrive.
  // Any query or scope change forgets the pick, so returning to an earlier query starts at the top.
  const [activeChoice, setActiveChoice] = useState<{
    query: string;
    scope: string | undefined;
    id: string | null;
  }>({ query, scope: selectionScope, id: null });
  const choiceCurrent = activeChoice.query === query && activeChoice.scope === selectionScope;
  if (!choiceCurrent) setActiveChoice({ query, scope: selectionScope, id: null });
  // A row whose button may remove it, such as Close, hands the selection to its neighbour once
  // it's gone, so the list doesn't jump back to its first row.
  const [leavingRow, setLeavingRow] = useState<{ id: string; successorId: string | null } | null>(
    null,
  );
  const listedIds = useMemo(
    () => new Set(rows.flatMap((row) => (row.kind === "item" ? [row.item.id] : []))),
    [rows],
  );
  const wantedId = activeId ?? (choiceCurrent ? activeChoice.id : null);
  const leftId =
    leavingRow &&
    wantedId === leavingRow.id &&
    !listedIds.has(leavingRow.id) &&
    leavingRow.successorId !== null &&
    listedIds.has(leavingRow.successorId)
      ? leavingRow.successorId
      : null;
  const activeIndex = reconcileActiveIndex(rows, leftId ?? wantedId);
  const pick = (id: string) => {
    // A newer pick, by key or pointer, takes over from a row still closing.
    setLeavingRow(null);
    setActiveChoice({ query, scope: selectionScope, id });
    onActiveIdChange?.(id);
  };
  const activeRow = activeIndex === -1 ? null : (rows[activeIndex] as QuickPickItemRow<Value>);
  const shownRowId = activeRow?.item.id ?? null;
  const onActiveRowShownRef = useRef(onActiveRowShown);
  onActiveRowShownRef.current = onActiveRowShown;
  useEffect(() => {
    if (shownRowId !== null) onActiveRowShownRef.current?.(shownRowId);
  }, [shownRowId]);

  // Whenever the active row changes, by keys, a new query or new results, scroll it
  // into view; a row the pointer picked is already visible, so the list stays put.
  const scrollRef = useRef<HTMLDivElement>(null);
  const revealedIdRef = useRef<string | null>(null);
  const pointerPickRef = useRef(false);
  const activeItemId = activeRow?.item.id ?? null;
  useLayoutEffect(() => {
    // A pointer pick only covers the render it caused, even when the row was already active.
    const pickedByPointer = pointerPickRef.current;
    pointerPickRef.current = false;
    if (activeItemId === revealedIdRef.current) return;
    revealedIdRef.current = activeItemId;
    if (activeItemId === null || pickedByPointer) return;
    if (activeRow)
      document.getElementById(optionId(activeRow))?.scrollIntoView?.({ block: "nearest" });
  });

  // Once the leaving row is gone, its neighbour becomes the pick, which a caller holding the
  // active row hears too.
  useEffect(() => {
    if (!leavingRow || listedIds.has(leavingRow.id)) return;
    setLeavingRow(null);
    const { successorId } = leavingRow;
    // Only while the closed row still held the selection: a newer query or scope chose anew.
    if (wantedId !== leavingRow.id) return;
    if (successorId === null || !listedIds.has(successorId)) return;
    setActiveChoice({ query, scope: selectionScope, id: successorId });
    onActiveIdChange?.(successorId);
  }, [leavingRow, listedIds, wantedId, query, selectionScope, onActiveIdChange]);

  /** Run one of `row`'s buttons; if it removes the active row, its neighbour takes over. */
  const runRowAction = (row: QuickPickItemRow<Value>, action: QuickPickRowAction) => {
    const index = rows.indexOf(row);
    if (index !== -1 && index === activeIndex) {
      const isItem = (candidate: QuickPickRow<Value> | undefined) => candidate?.kind === "item";
      const after = rows.slice(index + 1).find(isItem);
      const before = rows.slice(0, index).reverse().find(isItem);
      const successor = (after ?? before) as QuickPickItemRow<Value> | undefined;
      // A row highlighted only by default becomes the pick, so the handoff applies to it too.
      if (wantedId !== row.item.id) pick(row.item.id);
      setLeavingRow({ id: row.item.id, successorId: successor?.item.id ?? null });
    }
    action.run();
  };

  const navigate = (navigation: QuickPickNavigation) => {
    const next = navigateQuickPick(rows, activeIndex, navigation, { loop });
    const row = rows[next];
    if (row?.kind !== "item") return;
    pointerPickRef.current = false;
    pick(row.item.id);
  };

  // A page is the options that fit in the list, less one kept for context.
  const pageSize = () => {
    const list = scrollRef.current;
    const option = list?.querySelector<HTMLElement>('[role="option"]');
    if (!list?.clientHeight || !option?.offsetHeight) return 10;
    return Math.max(1, Math.floor(list.clientHeight / option.offsetHeight) - 1);
  };

  const accept = (row: QuickPickItemRow<Value>, alternate = false) => {
    if (row.item.disabledReason) return;
    if (historyKey && !row.isFreeText && row.item.rememberInHistory !== false) {
      recordQuickPickHistory(historyKey, row.item.id);
    }
    if (onAccept(row.item, { alternate }) === "keepOpen") {
      setHistory({ key: historyKey, ids: readRecents(historyKey) });
    }
  };
  // ⌘↵ or a modified click runs the picker's own alternate where there is one.
  const acceptWith = (row: QuickPickItemRow<Value> | null, alternate: boolean) => {
    if (alternate && onAlternate) onAlternate();
    else if (row) accept(row, alternate);
  };

  // The back button leaves with the child page, so focus returns to the query field.
  const goBack = () => {
    onBack?.();
    queryFieldRef.current?.focus();
  };

  /** Escape goes back on a child page, else dismisses, wherever focus is in the picker. */
  const handleEscape = (event: KeyboardEvent<HTMLElement>) => {
    // A dialog around the picker already handled it, such as going back.
    if (event.defaultPrevented) return;
    if (onBack) {
      event.preventDefault();
      goBack();
    } else if (onDismiss) {
      event.preventDefault();
      onDismiss();
    }
  };

  // Right Arrow at the end of the query opens the active row's page, so it still moves the caret.
  const handleExpand = (event: KeyboardEvent<HTMLInputElement>) => {
    const field = event.currentTarget;
    const atEnd = field.selectionStart === query.length && field.selectionEnd === query.length;
    if (!atEnd || event.shiftKey || event.altKey || event.metaKey || event.ctrlKey) return;
    if (!activeRow || activeRow.item.disabledReason) return;
    if (onExpand?.(activeRow.item)) {
      event.preventDefault();
    } else if (activeRow.item.opensPage) {
      event.preventDefault();
      accept(activeRow);
    }
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    // Keys that drive an IME composition, including its Enter, belong to the composition.
    if (isImeCompositionKeyEvent(event)) return;
    const rowAction = activeRow?.item.disabledReason
      ? undefined
      : activeRow?.item.actions?.find(
          (action) =>
            action.chord &&
            chordMatches(action.chord, event.nativeEvent, isMac) &&
            !chordEditsField(action.chord, event.currentTarget),
        );
    if (rowAction && activeRow) {
      event.preventDefault();
      runRowAction(activeRow, rowAction);
      return;
    }
    const navigation = navigationFor(event, pageSize, stepsWithCtrlNP);
    if (navigation) {
      event.preventDefault();
      navigate(navigation);
    } else if (event.key === "Enter") {
      event.preventDefault();
      acceptWith(activeRow, hasCommandModifier(event, isMac));
    } else if (event.key === "Escape") {
      handleEscape(event);
    } else if (event.key === "ArrowRight") {
      handleExpand(event);
    } else if (event.key === "Backspace" && onBack && query === "") {
      event.preventDefault();
      goBack();
    }
  };

  // The back and clear buttons take Tab too; the query handles its own Escape before this.
  const handleHeaderKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Escape" || isImeCompositionKeyEvent(event)) return;
    if (event.target === queryFieldRef.current) return;
    handleEscape(event);
    // Escape may remove the button that had focus, so focus returns to the query.
    if (event.defaultPrevented) queryFieldRef.current?.focus();
  };

  // The footer's buttons take focus from Tab, so Escape works from them as from the query.
  const handleFooterKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape" && !isImeCompositionKeyEvent(event)) handleEscape(event);
  };

  // Controls among the results, such as Try again or Show more, take Tab too. Escape from
  // one may remove it, so focus returns to the query.
  const handleResultsKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Escape" || isImeCompositionKeyEvent(event)) return;
    handleEscape(event);
    if (event.defaultPrevented) queryFieldRef.current?.focus();
  };

  const stats: QuickPickStats = {
    resultCount,
    loading,
    elapsedMs: elapsedValues.length > 0 ? Math.max(...elapsedValues) : undefined,
  };
  // An inline picker with no way to close, and nothing to summarize, keeps to its rows.
  const showFooter =
    !sidebar &&
    (onClose !== undefined ||
      onBack !== undefined ||
      summary !== undefined ||
      freeText.error !== null);
  // The footer's accept buttons do what Enter does, so they need a row Enter would accept.
  const canAcceptActive = activeRow !== null && !activeRow.item.disabledReason;
  const shownAlternateLabel =
    typeof alternateLabel === "function"
      ? activeRow
        ? alternateLabel(activeRow.item)
        : undefined
      : alternateLabel;

  const optionProps = (row: QuickPickItemRow<Value>, index: number) => {
    const { item } = row;
    const isActive = index === activeIndex;
    const disabled = Boolean(item.disabledReason);
    return {
      key: row.key,
      id: optionId(row),
      role: "option",
      "aria-selected": isActive,
      "aria-disabled": disabled || undefined,
      // The row's buttons sit outside the option, so it names the keys that run them.
      "aria-keyshortcuts": rowActionShortcuts(item.actions),
      "data-active": isActive || undefined,
      // Hovering an enabled row makes it the user's pick, as in a menu, even when it
      // is already the top row, so a result arriving above it doesn't take over.
      onPointerMove: () => {
        if (disabled || (activeId ?? (choiceCurrent ? activeChoice.id : null)) === item.id) return;
        pointerPickRef.current = true;
        pick(item.id);
      },
      // Keep focus in the query field.
      onMouseDown: (event: { preventDefault: () => void }) => event.preventDefault(),
      onClick: (event: MouseEvent<HTMLDivElement>) =>
        acceptWith(row, hasCommandModifier(event, isMac)),
    };
  };

  const renderCard = (row: QuickPickItemRow<Value>, index: number) => {
    const { item } = row;
    const { key, ...props } = optionProps(row, index);
    return (
      <div
        key={key}
        {...props}
        data-layout="card"
        className={cn(
          "@container/card flex cursor-pointer items-start gap-2.5 rounded-lg border border-border bg-muted/30 p-2.5 text-ui select-none",
          "data-active:border-ring/50 data-active:bg-muted data-active:text-foreground",
          item.disabledReason && "cursor-default opacity-50",
        )}
      >
        {item.icon && (
          <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-muted text-foreground [&_svg]:size-4">
            {item.icon}
          </span>
        )}
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="flex items-center gap-1.5">
            <span data-slot="quick-pick-label" className="min-w-0 truncate font-medium">
              {item.label}
            </span>
            {/* A tight card keeps its name whole; ⌘K and the menus still show the chord. */}
            {item.shortcut && (
              <CompactShortcutKeys
                keys={item.shortcut}
                className="ml-auto hidden shrink-0 @[13rem]/card:flex"
              />
            )}
          </span>
          {/* Two lines are kept for every hint, so the cards in a line match. */}
          <span
            data-slot={item.disabledReason ? "quick-pick-disabled-reason" : undefined}
            title={item.disabledReason ?? item.description}
            className="line-clamp-2 min-h-[2lh] text-xs text-muted-foreground"
          >
            {item.disabledReason ?? item.description}
          </span>
        </span>
      </div>
    );
  };

  // A group's heading: its label, which names the group, then any count. Its buttons sit
  // outside the listbox, laid over the heading's trailing edge.
  const renderHeading = (heading: { key: string; label: string }, labelId?: string) => {
    // A picker without counts or heading buttons keeps the plain caption.
    if (!groupCounts && !groupActions) {
      return (
        <div
          id={labelId}
          role="presentation"
          className="px-2 py-1.5 text-sm font-medium text-muted-foreground"
        >
          {heading.label}
        </div>
      );
    }
    const count = groupCounts ? headingCounts.get(heading.key) : undefined;
    // The label gives way to the buttons laid over the heading, and the count stays whole.
    const barWidth = headingActionBars.find((bar) => bar.key === heading.key)?.width ?? 0;
    return (
      <div
        role="presentation"
        data-slot="quick-pick-group-heading"
        data-heading-key={heading.key}
        style={barWidth > 0 ? { paddingRight: barWidth + 8 } : undefined}
        className="flex min-h-8 items-center gap-2 px-2 py-1 text-sm font-medium text-muted-foreground"
      >
        <span
          id={labelId}
          data-slot="quick-pick-group-label"
          className="flex min-w-0 items-baseline gap-1.5"
        >
          <span className="truncate">{heading.label}</span>
          {count !== undefined && (
            <>
              {" "}
              <span className="shrink-0 font-normal tabular-nums text-muted-foreground/80">
                {count.toLocaleString()}
              </span>
            </>
          )}
        </span>
      </div>
    );
  };

  // A row with buttons: the option, then its buttons beside it, outside the option so it stays
  // one thing to assistive tech; they're for the pointer, and the option names their keys.
  const renderOption = (row: QuickPickItemRow<Value>, index: number) => {
    const actions = row.item.actions;
    if (!actions?.length || sidebar) return renderOptionBody(row, index);
    // The row paints the active tint behind the option and its buttons; the tint is
    // translucent, so the option paints none of its own.
    const option = renderOptionBody(row, index, true);
    return (
      <div
        key={row.key}
        role="presentation"
        data-slot="quick-pick-row"
        className="group/row flex items-center rounded-lg has-[[data-active]]:bg-muted"
      >
        <div role="presentation" className="min-w-0 flex-1">
          {option}
        </div>
        <span
          aria-hidden
          className="invisible flex shrink-0 items-center gap-0.5 pr-1 group-hover/row:visible group-has-[[data-active]]/row:visible"
        >
          {actions.map((action) => (
            <RowActionButton
              key={action.id}
              action={action}
              isMac={isMac}
              onRun={() => runRowAction(row, action)}
            />
          ))}
        </span>
      </div>
    );
  };

  const renderOptionBody = (row: QuickPickItemRow<Value>, index: number, inRow = false) => {
    const { item } = row;
    const disabled = Boolean(item.disabledReason);
    const highlight = item.matchHighlight ?? matchHighlight;
    const { key, ...props } = optionProps(row, index);
    return (
      <div
        key={key}
        {...props}
        className={cn(
          "flex cursor-pointer select-none rounded-lg px-2 text-ui",
          sidebar
            ? "items-center gap-2 py-1.5 data-active:bg-[var(--sidebar-active)] data-active:text-[var(--sidebar-active-foreground)]"
            : cn(
                "items-start gap-2 py-1.5 data-active:text-foreground",
                !inRow && "data-active:bg-muted",
              ),
          // Matches the menus' destructive item: red text, a red tint while active.
          !sidebar &&
            item.destructive &&
            "text-destructive data-active:bg-destructive/10 data-active:text-destructive dark:data-active:bg-destructive/20",
          disabled && "cursor-default opacity-50",
        )}
        data-variant={item.destructive ? "destructive" : undefined}
      >
        {sidebar ? (
          <>
            {item.icon ? (
              <span className="flex size-4 shrink-0 items-center justify-center text-muted-foreground [&_svg]:size-4">
                {item.icon}
              </span>
            ) : null}
            <div className="flex min-w-0 flex-1 flex-col">
              <span data-slot="quick-pick-label" className="truncate">
                <HighlightedText text={item.label} ranges={row.labelRanges} highlight={highlight} />
              </span>
              {/* The second line holds the place and any accessory; the accessory keeps its width. */}
              {(item.description || item.accessory || item.shortcut) && (
                <span className="flex min-w-0 items-center gap-2">
                  {item.description && (
                    <span className="min-w-0 flex-1 truncate text-sm text-muted-foreground">
                      <HighlightedText
                        text={item.description}
                        ranges={row.descriptionRanges}
                        highlight={highlight}
                      />
                    </span>
                  )}
                  {item.accessory ? (
                    <span className="shrink-0">{item.accessory}</span>
                  ) : item.shortcut ? (
                    <CompactShortcutKeys keys={item.shortcut} className="shrink-0" />
                  ) : null}
                </span>
              )}
            </div>
          </>
        ) : null}
        {!sidebar && (
          <>
            {item.icon ? (
              <span
                className={cn(
                  "flex size-4 shrink-0 items-center justify-center self-center text-muted-foreground [&_svg]:size-4",
                  item.destructive && "text-destructive",
                )}
              >
                {item.icon}
              </span>
            ) : null}
            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              {row.sectionLabel ? (
                <span className="flex min-w-0 items-baseline gap-1">
                  {/* Rows ranked across groups name theirs, as a heading would; the heading isn't announced either. */}
                  <span
                    aria-hidden
                    data-slot="quick-pick-section"
                    className="shrink-0 text-muted-foreground"
                  >
                    {row.sectionLabel}:
                  </span>
                  <span data-slot="quick-pick-label" className="min-w-0 truncate">
                    <HighlightedText
                      text={item.label}
                      ranges={row.labelRanges}
                      highlight={highlight}
                    />
                  </span>
                </span>
              ) : (
                <span data-slot="quick-pick-label" className="truncate">
                  <HighlightedText
                    text={item.label}
                    ranges={row.labelRanges}
                    highlight={highlight}
                  />
                </span>
              )}
              {item.context}
              {item.description && (
                <span className="truncate text-sm text-muted-foreground">
                  <HighlightedText
                    text={item.description}
                    ranges={row.descriptionRanges}
                    highlight={highlight}
                  />
                </span>
              )}
            </div>
            {item.disabledReason && (
              <span
                data-slot="quick-pick-disabled-reason"
                title={item.disabledReason}
                className="min-w-0 shrink truncate self-center text-sm text-muted-foreground"
              >
                {item.disabledReason}
              </span>
            )}
            {!item.disabledReason && item.detail && (
              <span className="shrink-0 self-center text-sm text-muted-foreground">
                {item.detail}
              </span>
            )}
            {item.accessory && (
              <span className="flex shrink-0 items-center gap-2 self-center">{item.accessory}</span>
            )}
            {item.shortcut && (
              <CompactShortcutKeys keys={item.shortcut} className="shrink-0 self-center" />
            )}
            {item.opensPage && (
              <ChevronRightIcon
                aria-hidden
                className="size-4 shrink-0 self-center text-muted-foreground"
              />
            )}
          </>
        )}
      </div>
    );
  };

  return (
    <div
      data-slot="quick-pick"
      className={cn(
        "flex min-h-0 flex-col",
        !sidebar && "bg-popover text-popover-foreground",
        fillHeight && "flex-1",
        className,
      )}
    >
      {/* A switcher has no visible query; the field still holds focus and the list's keys, and
          the header shows no other control, which would take focus where nothing shows. */}
      <div
        onKeyDown={handleHeaderKeyDown}
        className={cn(
          "flex items-center gap-2",
          sidebar ? "rounded-lg bg-muted px-2" : "border-b border-border px-3 py-2",
          switcher && "sr-only",
        )}
      >
        {onBack && !switcher ? (
          // No tooltip: an open tooltip would take Escape from the dialog. The footer shows esc Back.
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label={backLabel}
            data-slot="quick-pick-back"
            onMouseDown={(event) => event.preventDefault()}
            onClick={goBack}
            className="-ml-1 text-muted-foreground"
          >
            <ChevronLeftIcon />
          </Button>
        ) : (
          <SearchIcon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
        )}
        <input
          ref={setQueryField}
          role="combobox"
          {...pickListInputProps(stepsWithCtrlNP)}
          aria-label={label}
          aria-expanded={!collapsed}
          aria-controls={collapsed ? undefined : listId}
          aria-autocomplete="list"
          aria-invalid={freeText.error ? true : undefined}
          aria-describedby={freeText.error && showFooter ? errorId : undefined}
          data-testid={inputTestId}
          aria-activedescendant={activeRow === null ? undefined : optionId(activeRow)}
          autoFocus={autoFocus}
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
          placeholder={placeholder}
          readOnly={switcher}
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          onKeyDown={handleKeyDown}
          className="h-8 min-w-0 flex-1 bg-transparent text-ui outline-hidden placeholder:text-muted-foreground"
        />
        {sidebar && query !== "" && (
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Clear search"
            data-slot="quick-pick-clear"
            // Keep focus in the query field, as the rows do.
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => {
              onQueryChange("");
              queryFieldRef.current?.focus();
            }}
            className="-mr-1 text-muted-foreground"
          >
            <XIcon />
          </Button>
        )}
        {!switcher && inputEnd}
      </div>
      {belowInput}
      <PreviewBeside
        enabled={renderPreview !== undefined}
        preview={renderPreview && activeRow ? renderPreview(activeRow.item) : null}
        fillHeight={fillHeight}
        hidden={collapsed}
      >
        <div
          ref={scrollRef}
          data-slot="quick-pick-scroll"
          onKeyDown={handleResultsKeyDown}
          className={cn(
            "overflow-y-auto p-1",
            listHeightClass({ fillHeight, switcher, preview: renderPreview !== undefined }),
            renderPreview && "w-[45%] shrink-0",
            collapsed && "hidden",
          )}
        >
          {status && <div data-slot="quick-pick-status">{status}</div>}
          {/* Announces the empty message, shown, or the result count, visually hidden, with a
            failure beside rows that remain. */}
          <div
            role="status"
            aria-live="polite"
            data-slot={showEmpty ? "quick-pick-empty" : undefined}
            className={
              showEmpty && (loading || sourceError || emptyMessage)
                ? "flex items-center justify-center gap-2 py-6 text-center text-ui"
                : "sr-only"
            }
          >
            {collapsed ? null : showEmpty ? (
              loading ? (
                <>
                  <Spinner aria-hidden role="presentation" />
                  Loading results…
                </>
              ) : (
                (sourceError ?? emptyMessage)
              )
            ) : sourceError ? (
              `${resultCountText(resultCount)}. ${sourceError}`
            ) : (
              resultCountText(resultCount)
            )}
          </div>
          {sourceError && !collapsed && (
            // Outside the status region, which announces the failure itself.
            <div
              className={cn(
                "flex items-center justify-center gap-2 text-sm",
                showEmpty ? "pb-4" : "px-2 py-1.5 text-destructive",
              )}
            >
              {!showEmpty && <span>{sourceError}</span>}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  retry();
                  // The button leaves once the retry starts, so the query keeps the keys.
                  queryFieldRef.current?.focus();
                }}
              >
                Try again
              </Button>
            </div>
          )}
          <div role="presentation" className="relative">
            <div ref={listboxRef} id={listId} role="listbox" aria-label={label}>
              {sections.map((section, sectionIndex) => {
                const headingId = section.heading ? `${baseId}-heading-${sectionIndex}` : undefined;
                return (
                  <div
                    key={section.heading?.key ?? `section-${sectionIndex}`}
                    role="group"
                    aria-labelledby={headingId}
                    className="pb-1"
                  >
                    {section.heading && renderHeading(section.heading, headingId)}
                    {runsOf(section.rows, cardsShown).map((run) =>
                      run.cards ? (
                        // Cards flow two to a line where the list is wide enough; arrows keep reading order.
                        <div
                          key={run.rows[0].row.key}
                          role="presentation"
                          data-slot="quick-pick-cards"
                          className="@container/cards"
                        >
                          <div className="grid grid-cols-1 gap-1 p-0.5 @[22rem]/cards:grid-cols-2">
                            {run.rows.map(({ row, index }) => renderCard(row, index))}
                          </div>
                        </div>
                      ) : (
                        run.rows.map(({ row, index }) => renderOption(row, index))
                      ),
                    )}
                  </div>
                );
              })}
            </div>
            {headingActionBars.map((bar) => (
              <div
                key={bar.key}
                data-slot="quick-pick-group-actions"
                data-actions-for={bar.key}
                style={{ top: bar.top, height: bar.height }}
                className="absolute right-1 flex items-center gap-0.5"
              >
                {(headingActions.get(bar.key) ?? NO_GROUP_ACTIONS).map((action) => (
                  <Button
                    key={action.id}
                    type="button"
                    variant="ghost"
                    size="xs"
                    data-slot="quick-pick-group-action"
                    aria-label={action.ariaLabel}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => {
                      action.run();
                      // The button may leave with its group, so the query keeps the keys.
                      queryFieldRef.current?.focus();
                    }}
                    className="font-normal text-muted-foreground hover:text-foreground"
                  >
                    {action.label}
                  </Button>
                ))}
              </div>
            ))}
          </div>
          {listEnd}
        </div>
      </PreviewBeside>
      {showFooter && (
        <div
          onKeyDown={handleFooterKeyDown}
          data-slot="quick-pick-footer"
          className="flex min-h-9 items-center gap-3 border-t border-border px-3 py-1.5 text-sm text-muted-foreground"
        >
          <span data-slot="quick-pick-summary" className="min-w-0 flex-1 truncate">
            {/* An error shares the summary's fixed slot, so it never shifts the list. */}
            {freeText.error ? (
              <span
                id={errorId}
                title={freeText.error}
                className="flex items-center gap-1.5 text-destructive"
              >
                <AlertCircleIcon aria-hidden className="size-3.5 shrink-0" />
                <span className="truncate">{freeText.error}</span>
              </span>
            ) : (
              summary?.(stats)
            )}
          </span>
          <FooterAction
            keys={["↵"]}
            shortcut="Enter"
            label={acceptLabel}
            disabled={!canAcceptActive}
            onClick={() => activeRow && accept(activeRow)}
          />
          {activeRow?.item.actions?.map(
            (action) =>
              action.chord && (
                <FooterAction
                  key={action.id}
                  keys={chordKeys(action.chord, isMac)}
                  shortcut={chordAriaShortcut(action.chord, isMac)}
                  label={action.label}
                  onClick={() => activeRow && runRowAction(activeRow, action)}
                />
              ),
          )}
          {shownAlternateLabel && (
            <FooterAction
              keys={[isMac ? "⌘" : "Ctrl", "↵"]}
              shortcut={isMac ? "Meta+Enter" : "Control+Enter"}
              label={shownAlternateLabel}
              disabled={!onAlternate && !canAcceptActive}
              onClick={() => acceptWith(activeRow, true)}
            />
          )}
          <FooterAction
            keys={["esc"]}
            shortcut="Escape"
            label={onBack ? "Back" : "Close"}
            disabled={!onBack && !onClose}
            onClick={() => (onBack ? goBack() : onClose?.())}
          />
        </div>
      )}
    </div>
  );
}
