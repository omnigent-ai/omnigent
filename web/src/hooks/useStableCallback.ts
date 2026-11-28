import { useCallback, useLayoutEffect, useRef } from "react";

/**
 * `callback` behind one identity for the component's lifetime, always running the latest one.
 * For event handlers handed to memoized children, so a parent's re-render doesn't re-render
 * them; never call it during render, which would read the previous render's closure.
 */
export function useStableCallback<Args extends unknown[], Result>(
  callback: (...args: Args) => Result,
): (...args: Args) => Result {
  const latest = useRef(callback);
  useLayoutEffect(() => {
    latest.current = callback;
  });
  return useCallback((...args: Args) => latest.current(...args), []);
}
