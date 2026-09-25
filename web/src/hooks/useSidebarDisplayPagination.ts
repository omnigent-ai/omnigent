import { useCallback, useMemo, useState } from "react";

/** Page the display independently of the retained cache and backend page size. */
export function useSidebarDisplayPagination<T>(
  rows: T[],
  scopeKey: string,
  configuredSize: number | false | undefined,
  hasNextPage: boolean,
  fetchNextPage: () => unknown,
) {
  const pageSize =
    typeof configuredSize === "number" && Number.isSafeInteger(configuredSize) && configuredSize > 0
      ? configuredSize
      : undefined;
  const key = JSON.stringify([scopeKey, pageSize]);
  const [window, setWindow] = useState({ key, limit: pageSize ?? Infinity });
  const limit = window.key === key ? window.limit : (pageSize ?? Infinity);
  if (window.key !== key) setWindow({ key, limit });
  const visibleRows = useMemo(
    () => (pageSize === undefined ? rows : rows.slice(0, limit)),
    [rows, limit, pageSize],
  );
  const hasCachedRows = rows.length > limit;
  const loadMore = useCallback(() => {
    if (pageSize !== undefined) {
      setWindow({ key, limit: Math.max(limit, Math.min(rows.length, limit) + pageSize) });
    }
    // Reveal retained rows before requesting another backend page.
    return !hasCachedRows && hasNextPage ? fetchNextPage() : undefined;
  }, [pageSize, key, limit, rows.length, hasCachedRows, hasNextPage, fetchNextPage]);
  return {
    rows: visibleRows,
    hasMore: hasCachedRows || hasNextPage,
    loadMore,
    maxAutoLoads: pageSize === undefined ? undefined : 0,
  };
}
