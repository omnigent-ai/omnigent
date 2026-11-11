import type { ReactNode } from "react";

import type { MatchHighlight } from "./HighlightedText";
import type { MatchRange } from "./match";

/** One selectable result. `value` is whatever the consumer acts on when accepted. */
export interface QuickPickItem<Value = unknown> {
  /** Stable across loads; used for selection and React keys. */
  id: string;
  label: string;
  /** Muted second line, such as where a search matched. */
  description?: string;
  /** Hidden terms that also match the query, such as synonyms. */
  keywords?: readonly string[];
  /** Section heading label; items sharing one appear together in source order. */
  group?: string;
  icon?: ReactNode;
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
  value: Value;
}

/**
 * - `client`: the picker fuzzy-ranks items against the query and highlights matches.
 * - `provider`: the caller already filtered and ordered items for the query, such as a server search.
 */
export type QuickPickRanking = "client" | "provider";

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
}

export type QuickPickRow<Value = unknown> = QuickPickHeadingRow | QuickPickItemRow<Value>;

/** Returning `"keepOpen"` from `onAccept` leaves a dialog open, such as after loading more rows. */
export type QuickPickAcceptResult = void | "keepOpen";
