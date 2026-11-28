// The end of a Pull Requests list that holds fewer rows than its search found: one block for the
// rows not loaded yet, sized to them, which reads the next page when it comes into view; while
// the list is filtered, only a button that loads 50 more to search through; and once the list
// has reached its end, why it may hold fewer rows than GitHub counted.

import { type RefObject, useEffect, useRef } from "react";

import { LIST_ROW_HEIGHT } from "./listWindow";

export interface PullRequestListMore {
  /** Positions the list hasn't read yet; one trailing block holds them all. */
  remainder: number;
  /** The positions a page being read covers, from 1. */
  loading: { from: number; to: number; of: number } | null;
  /** Why the last page couldn't be read; Retry reads it again. */
  failure: string | null;
  /** How the list ended, once it reached its end: short of GitHub's count, or at its 1,000. */
  end: "incomplete" | "capped" | null;
  /** How many the search found, as the tab's count says it, for "Searching 150 of 420 loaded". */
  countLabel: string | null;
  /** The trailing block is in view, or not: while it is, pages read one after another. */
  inView: (shown: boolean) => void;
  /** Read one more page, as the app may: the keyboard moved past the last loaded row. */
  request: () => void;
  /** Read one more page now, as the user asked. */
  loadMore: () => void;
  /** Read the list again from its first page. */
  refresh: () => void;
}

/** How many PRs a page reads; the button says what one click brings. */
const PAGE_SIZE = 50;

export function PullRequestListMoreBlock({
  more,
  filtering,
  loaded,
  grouped,
  scrollRoot,
}: {
  more: PullRequestListMore;
  /** A search, chip or filter narrows the list: nothing loads unless asked. */
  filtering: boolean;
  /** How many rows the list has loaded. */
  loaded: number;
  /** The list is grouped, so its rows not loaded yet sit after every group. */
  grouped: boolean;
  scrollRoot: RefObject<HTMLElement | null>;
}) {
  const block = useRef<HTMLDivElement>(null);
  const { inView } = more;
  const shown = more.remainder > 0 && !filtering;
  // Seen in the list's own scroll area, a little before it's on screen.
  useEffect(() => {
    const element = block.current;
    if (!shown || !element) return;
    const observer = new IntersectionObserver(
      (entries) => inView(entries.some((entry) => entry.isIntersecting)),
      { root: scrollRoot.current, rootMargin: `${LIST_ROW_HEIGHT * 4}px 0px` },
    );
    observer.observe(element);
    return () => {
      observer.disconnect();
      inView(false);
    };
  }, [shown, inView, scrollRoot]);

  if (more.remainder > 0 && filtering) {
    return (
      <div className="pr-list-more pr-list-more-filtered" role="row">
        <div role="gridcell" className="pr-list-more-cell">
          <span className="pr-list-more-caption">
            {more.countLabel
              ? `Searching ${loaded} of ${more.countLabel} loaded`
              : `Searching ${loaded} loaded`}
          </span>
          <MoreStatus more={more} />
          {!more.loading && (
            <button type="button" className="pr-list-quiet-button" onClick={more.loadMore}>
              {more.failure ? "Retry" : `Load ${PAGE_SIZE} More to Search`}
            </button>
          )}
        </div>
      </div>
    );
  }
  if (shown) {
    return (
      <div ref={block} className="pr-list-more" data-testid="pull-request-list-more">
        <div className="pr-list-more-header" role="row">
          <div role="gridcell" className="pr-list-more-cell">
            {!more.loading && !more.failure && (
              <span className="pr-list-more-caption">
                {grouped ? "Not loaded yet" : `${more.remainder} more not loaded yet`}
              </span>
            )}
            <MoreStatus more={more} />
            {more.failure && (
              <button type="button" className="pr-list-quiet-button" onClick={more.loadMore}>
                Retry
              </button>
            )}
          </div>
        </div>
        {/* Placeholders for the rows still to read: never focusable, selectable or navigable. */}
        <div
          className="pr-list-more-rows"
          aria-hidden
          style={{ height: Math.max(0, more.remainder - 1) * LIST_ROW_HEIGHT }}
        />
      </div>
    );
  }
  if (more.end === "incomplete") {
    return (
      <div className="pr-list-more pr-list-more-end" role="row">
        <div role="gridcell" className="pr-list-more-cell">
          <span>Some pull requests may have moved while this list loaded.</span>
          <button type="button" className="pr-list-quiet-button" onClick={more.refresh}>
            Refresh
          </button>
        </div>
      </div>
    );
  }
  if (more.end === "capped") {
    return (
      <div className="pr-list-more pr-list-more-end" role="row">
        <div role="gridcell" className="pr-list-more-cell">
          <span>
            GitHub lists only the first 1,000 pull requests a search finds. Narrow the search to see
            the rest.
          </span>
        </div>
      </div>
    );
  }
  return null;
}

/** What's reading or went wrong, said politely so a screen reader hears it once. */
function MoreStatus({ more }: { more: PullRequestListMore }) {
  const text = more.loading
    ? `Loading ${more.loading.from}–${more.loading.to} of ${more.loading.of}…`
    : more.failure
      ? `Couldn’t load more pull requests. ${more.failure}`
      : "";
  return (
    <span className="pr-list-more-status" role="status" aria-live="polite">
      {text}
    </span>
  );
}
