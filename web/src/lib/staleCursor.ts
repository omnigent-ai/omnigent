import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/sessionsApi";

/**
 * Server error code for a pagination cursor whose row no longer exists.
 *
 * A cursor-paginated list route resolves `after`/`before` to a keyset bound
 * by looking the row up. When that row was deleted mid-scroll the bound is
 * unknowable, so the server answers 400 `stale_cursor` rather than silently
 * dropping the rest of the enumeration.
 */
export const STALE_CURSOR_CODE = "stale_cursor";

/**
 * Restarts allowed per query before a stale cursor is surfaced as an error.
 * A list churning fast enough to invalidate three consecutive page-1 walks
 * is a real failure, not a race worth hiding.
 */
export const STALE_CURSOR_MAX_RESTARTS = 3;

/** True when `error` is the server's 400 `stale_cursor` for a dead cursor. */
export function isStaleCursorError(error: unknown): boolean {
  return error instanceof ApiError && error.code === STALE_CURSOR_CODE;
}

/**
 * Restart an infinite query from its first page when its cursor goes stale.
 *
 * A stale cursor is not retryable in place — reissuing the same dead cursor
 * fails identically — but it is fully recoverable by dropping the loaded
 * pages and walking again from page 1. Without this an infinite-scroll list
 * whose oldest loaded row is deleted mid-scroll would surface the raw 400
 * where it used to just keep scrolling.
 *
 * Watches the query cache rather than the hook's result object: reading a
 * property off a query result during render narrows TanStack's tracked-props
 * set, which would silently stop callers re-rendering on `data` changes.
 *
 * The restart budget resets once the query succeeds again.
 *
 * @param queryKey - Key of the infinite query to reset, matched exactly.
 */
export function useRestartOnStaleCursor(queryKey: readonly unknown[]): void {
  const queryClient = useQueryClient();
  const keyRef = useRef(queryKey);
  keyRef.current = queryKey;
  const keyId = JSON.stringify(queryKey);
  useEffect(() => {
    const cache = queryClient.getQueryCache();
    let restarts = 0;
    const check = (): void => {
      const query = cache.find({ queryKey: keyRef.current, exact: true });
      if (!query) return;
      if (query.state.status === "success") {
        restarts = 0;
        return;
      }
      if (!isStaleCursorError(query.state.error)) return;
      if (restarts >= STALE_CURSOR_MAX_RESTARTS) return;
      restarts += 1;
      void queryClient.resetQueries({ queryKey: keyRef.current, exact: true });
    };
    check();
    return cache.subscribe(check);
  }, [keyId, queryClient]);
}
