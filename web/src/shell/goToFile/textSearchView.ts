// What Go to File's text mode shows for a search's state: the matching lines, what to say
// when there are none, and the footer's line, so a stopped or refused search never reads
// as "nothing matched".

import type { QuickPickItem } from "@/components/ui/quick-pick";
import type { ContentSearchInput, ContentSearchResult } from "@/lib/contentSearch";

import type { GoToFileTarget } from "./goToFileItems";
import { textSearchItems, textSearchSummary } from "./textSearchItems";

export type TextSearchState =
  | { phase: "idle" }
  /** The search can't run as typed; `problem` says how to fix it. */
  | { phase: "invalid"; problem: string }
  /**
   * A newer search runs; `previous` is the last answer, shown meanwhile and marked for the
   * search that produced it rather than what is typed now.
   */
  | {
      phase: "searching";
      previous: { result: ContentSearchResult; input: ContentSearchInput } | undefined;
    }
  | { phase: "error"; message: string }
  | { phase: "settled"; result: ContentSearchResult };

export interface TextSearchView {
  items: QuickPickItem<GoToFileTarget>[];
  emptyMessage: string;
  status: string | null;
}

export const TEXT_SEARCH_IDLE = "Type the text to find in this session's files.";
export const TEXT_SEARCH_SEARCHING = "Searching…";

/** The rows, empty message and footer line for `state`, searched as `input` says. */
export function textSearchView(state: TextSearchState, input: ContentSearchInput): TextSearchView {
  switch (state.phase) {
    case "idle":
      return { items: [], emptyMessage: TEXT_SEARCH_IDLE, status: null };
    case "invalid":
      return { items: [], emptyMessage: state.problem, status: null };
    case "error":
      return {
        items: [],
        emptyMessage: `Couldn't search this session's files. ${state.message}`,
        status: null,
      };
    case "searching":
      return {
        items: state.previous
          ? textSearchItems(state.previous.result.matches, state.previous.input)
          : [],
        emptyMessage: TEXT_SEARCH_SEARCHING,
        status: TEXT_SEARCH_SEARCHING,
      };
    case "settled": {
      const { result } = state;
      const note =
        result.warning ?? (result.truncated ? "Narrow the search to see the rest." : null);
      return {
        items: textSearchItems(result.matches, input),
        emptyMessage: `No lines match “${input.query}”.`,
        // With no lines, the empty message says so and the footer keeps only the note.
        status:
          result.matches.length === 0
            ? note
            : [textSearchSummary(result), note].filter(Boolean).join(". "),
      };
    }
  }
}
