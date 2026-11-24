import type { ReactNode } from "react";

import type { Chord } from "@/lib/shortcuts/chord";

import type { MatchHighlight } from "./HighlightedText";
import type { MatchRange } from "./match";

/**
 * One selectable result. `value` is whatever the consumer acts on when accepted.
 * Treat items as immutable once shown: ranking caches each item's searchable text.
 */
export interface QuickPickItem<Value = unknown> {
  /** Stable across loads; used for selection and React keys. */
  id: string;
  readonly label: string;
  /** Quiet line under the label, such as where a session lives. */
  context?: ReactNode;
  /** Muted second line, such as where a search matched. */
  readonly description?: string;
  /** Hidden terms that also match the query, such as synonyms. */
  readonly keywords?: readonly string[];
  /** Section heading label; items sharing one appear together in source order. */
  group?: string;
  icon?: ReactNode;
  /** Quiet trailing text, such as a setting's current value. */
  detail?: string;
  /** Quiet trailing content, such as a badge or an agent name. */
  accessory?: ReactNode;
  /** Key caps shown at the trailing edge, such as `["⌘", "K"]`. */
  shortcut?: readonly string[];
  /** When set, the item is shown but cannot be accepted; the reason is announced. */
  disabledReason?: string;
  /** Ranges for provider-ranked items, which skip client matching. */
  highlights?: { label?: readonly MatchRange[]; description?: readonly MatchRange[] };
  /** How this row's matched text stands out; defaults to the picker's highlight. */
  matchHighlight?: MatchHighlight;
  /** Ends or removes something, so it's styled as a menu's destructive item is. */
  destructive?: boolean;
  /** Accepting the item opens a child page; a chevron marks it, and Right Arrow opens it. */
  opensPage?: boolean;
  /** Defaults to true; a free-text item is never remembered. */
  rememberInHistory?: boolean;
  /**
   * With `sourcesFirst`, whether this item matching the query can move the items ahead of the
   * sources. False for a label that names the user's data, such as a pull request's title, so
   * a word from that title doesn't push the matching sessions down. Defaults to true.
   */
  leadsSources?: boolean;
  /**
   * `card`, without a query, shows the item as a card (icon, label and shortcut, then the
   * description as a hint), two to a line when there's room; with a query it's a plain row.
   */
  layout?: "card";
  /**
   * Buttons beside the row, outside its option, shown while it's hovered or active, such as
   * Close. The pointer clicks them; the keyboard runs one by its `chord` on the active row.
   */
  actions?: readonly QuickPickRowAction[];
  value: Value;
}

/** A button beside one row, such as Close; see `QuickPickItem.actions`. */
export interface QuickPickRowAction {
  id: string;
  /** Its tooltip and the name its row announces it by, such as "Close tab". */
  label: string;
  icon: ReactNode;
  /** The keys that run it on the active row, while the query field has focus. */
  chord?: Chord;
  run: () => void;
}

/** A button in a group's heading, such as Close all; it takes Tab like the footer's buttons. */
export interface QuickPickGroupAction {
  id: string;
  label: string;
  /** The accessible name when the label alone is unclear, such as "Close all pull requests". */
  ariaLabel?: string;
  run: () => void;
}

/**
 * - `client`: the picker fuzzy-ranks items against the query and highlights matches.
 * - `provider`: the caller already filtered and ordered items for the query, such as a server search.
 */
export type QuickPickRanking = "client" | "provider";

export interface QuickPickLoadRequest<Value = unknown> {
  query: string;
  /** Aborted when a newer query, reload, or close supersedes this request. */
  signal: AbortSignal;
  /**
   * Shows a partial snapshot while loading continues, for results that stream
   * in. Each call replaces the previous snapshot; the settled value is final.
   * Calls after abort or completion are ignored.
   */
  publish: (items: readonly QuickPickItem<Value>[]) => void;
}

/** Items a picker loads itself, such as a search that answers each query. */
export interface QuickPickSource<Value = unknown> {
  /**
   * Stable identity; changing it resets the source's results. A new object with
   * the same `id` does not reload, so callers need not memoize sources.
   */
  id: string;
  load: (
    request: QuickPickLoadRequest<Value>,
  ) => readonly QuickPickItem<Value>[] | Promise<readonly QuickPickItem<Value>[]>;
  /** Run `load` again as the query changes. Defaults to false (load once per open). */
  queryDependent?: boolean;
  /**
   * Delay before a query-dependent reload. Previous results stay visible
   * meanwhile, except provider-ranked ones, which match only their own query.
   */
  debounceMs?: number;
  /** Changing this reloads the source and keeps its previous results visible. */
  reloadKey?: string;
  /**
   * What the rows answer besides the query, such as search options. Changing it
   * reloads after the debounce and, like a new query, hides the rows meanwhile.
   */
  resultKey?: string;
  ranking?: QuickPickRanking;
}

export type QuickPickSourceStatus = "loading" | "ready" | "error";

export interface QuickPickSourceState<Value = unknown> {
  status: QuickPickSourceStatus;
  /** Last successful results; retained while a newer load is in flight. */
  items: readonly QuickPickItem<Value>[];
  error?: string;
  /** Duration of the most recent completed load. */
  elapsedMs?: number;
}

export interface QuickPickHeadingRow {
  kind: "heading";
  key: string;
  label: string;
}

export interface QuickPickItemRow<Value = unknown> {
  kind: "item";
  key: string;
  item: QuickPickItem<Value>;
  labelRanges: readonly MatchRange[];
  descriptionRanges: readonly MatchRange[];
  /** The heading the row is listed under, absent for a row outside any group. */
  headingKey?: string;
  /** True for the item made from the typed query. */
  isFreeText: boolean;
  /** The item's group, shown on the row when ranked rows from several groups interleave. */
  sectionLabel?: string;
}

export type QuickPickRow<Value = unknown> = QuickPickHeadingRow | QuickPickItemRow<Value>;

/** Inline feedback when typed text cannot become an item. */
export interface QuickPickInputError {
  error: string;
}

export interface QuickPickStats {
  /** Selectable results currently shown. */
  resultCount: number;
  /** True while any source is still loading. */
  loading: boolean;
  /** Slowest completed source load for the current results. */
  elapsedMs?: number;
}

export interface QuickPickAcceptDetails {
  /** ⌘↵ / Ctrl+↵ or a modified click. */
  alternate: boolean;
}

/**
 * What an accept asks of the wrapper around the picker, which owns closing. `"keepOpen"` asks
 * it to stay open, such as after loading more rows; `{ afterClose }` asks it to run `afterClose`
 * once it has closed and restored focus, so an action that moves focus keeps it. The picker
 * itself only refreshes its recents on `"keepOpen"`.
 */
export type QuickPickAcceptResult = void | "keepOpen" | { afterClose: () => void };
