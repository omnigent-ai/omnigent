import { useCallback, useMemo, useState } from "react";

/** One page of a picker that opens child lists, such as Themes > Appearance. */
export interface QuickPickPage<PageId extends string = string> {
  id: PageId;
  query: string;
  /** The active item's `id`; null leaves the choice to the picker, starting at the first item. */
  activeId: string | null;
}

/** The QuickPick props that follow the current page. */
export interface QuickPickPageProps {
  query: string;
  onQueryChange: (query: string) => void;
  activeId: string | null;
  onActiveIdChange: (id: string) => void;
  /** Present on a child page. */
  onBack?: () => void;
  /** New on every page change and reset, so the picker forgets a pick made on another page. */
  selectionScope: string;
}

export interface QuickPickPages<PageId extends string> {
  page: QuickPickPage<PageId>;
  /** 1 on the root page. */
  depth: number;
  /** Opens a child page with an empty query. */
  push: (id: PageId, activeId?: string | null) => void;
  /** Returns to the parent page with its query and active item as they were. */
  pop: () => void;
  /** Returns to a fresh root page, such as when the picker reopens. */
  reset: (activeId?: string | null) => void;
  pickerProps: QuickPickPageProps;
}

/** A page as the stack holds it, with the opening it belongs to. */
type StackedPage<PageId extends string> = QuickPickPage<PageId> & { opening: number };

let nextOpening = 0;

function openPage<PageId extends string>(id: PageId, activeId: string | null): StackedPage<PageId> {
  nextOpening += 1;
  return { id, query: "", activeId, opening: nextOpening };
}

function updateTop<PageId extends string>(
  stack: StackedPage<PageId>[],
  patch: Partial<QuickPickPage<PageId>>,
): StackedPage<PageId>[] {
  const top = stack[stack.length - 1]!;
  return [...stack.slice(0, -1), { ...top, ...patch }];
}

/**
 * A stack of pages for one QuickPick: each keeps its own query and active item,
 * so going back restores the parent where the user left it. Spread
 * `pickerProps` onto the QuickPick and render the items for `page.id`.
 */
export function useQuickPickPages<PageId extends string>(
  rootId: PageId,
  rootActiveId: string | null = null,
): QuickPickPages<PageId> {
  const [stack, setStack] = useState(() => [openPage(rootId, rootActiveId)]);
  const top = stack[stack.length - 1]!;
  const { opening } = top;
  const page = useMemo<QuickPickPage<PageId>>(
    () => ({ id: top.id, query: top.query, activeId: top.activeId }),
    [top],
  );
  const depth = stack.length;

  const push = useCallback((id: PageId, activeId: string | null = null) => {
    setStack((current) => [...current, openPage(id, activeId)]);
  }, []);
  const pop = useCallback(() => {
    setStack((current) => (current.length > 1 ? current.slice(0, -1) : current));
  }, []);
  const reset = useCallback(
    (activeId: string | null = rootActiveId) => setStack([openPage(rootId, activeId)]),
    [rootId, rootActiveId],
  );
  const setQuery = useCallback((query: string) => {
    // A new query starts from the best match, as on a single page.
    setStack((current) => updateTop(current, { query, activeId: null }));
  }, []);
  const setActiveId = useCallback((activeId: string) => {
    setStack((current) =>
      current[current.length - 1]!.activeId === activeId
        ? current
        : updateTop(current, { activeId }),
    );
  }, []);

  const pickerProps = useMemo<QuickPickPageProps>(
    () => ({
      query: page.query,
      onQueryChange: setQuery,
      activeId: page.activeId,
      onActiveIdChange: setActiveId,
      onBack: depth > 1 ? pop : undefined,
      selectionScope: `page-${opening}`,
    }),
    [page.query, page.activeId, depth, opening, setQuery, setActiveId, pop],
  );

  return { page, depth, push, pop, reset, pickerProps };
}
