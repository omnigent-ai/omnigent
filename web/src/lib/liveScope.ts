// A toast can outlive the list that showed it, such as the Pull Requests page's table, which is
// replaced on each tab. Its Undo and Try Again run through the list mounted now, under that
// list's account and rows, and find nothing while none is mounted, so they can fail closed.
// Where two lists share a scope, each listing its own PRs, a recovery goes through the one that
// lists its PR now.

import { useEffect, useRef } from "react";

export interface LiveScopes<T> {
  /** Publish `current` as `scope`'s live value while the calling component stays mounted. */
  usePublish: (scope: string, current: T) => void;
  /** Publish a value outside React, e.g. in a test, until the returned function is called. */
  publish: (scope: string, current: { current: T }) => () => void;
  /** What the most recently mounted component for `scope` published; null while none is. */
  current: (scope: string) => T | null;
  /**
   * The most recently mounted publisher for `scope` that `matches`, e.g. the list showing a PR;
   * the most recently mounted one where none does; null while none is mounted.
   */
  currentFor: (scope: string, matches: (value: T) => boolean) => T | null;
}

export function createLiveScopes<T>(): LiveScopes<T> {
  const mounted = new Map<string, { current: T }[]>();
  const publish = (scope: string, ref: { current: T }) => {
    mounted.set(scope, [...(mounted.get(scope) ?? []), ref]);
    return () => {
      const remaining = (mounted.get(scope) ?? []).filter((entry) => entry !== ref);
      if (remaining.length) mounted.set(scope, remaining);
      else mounted.delete(scope);
    };
  };
  return {
    usePublish(scope, current) {
      const ref = useRef(current);
      ref.current = current;
      useEffect(() => publish(scope, ref), [scope]);
    },
    publish,
    current(scope) {
      return mounted.get(scope)?.at(-1)?.current ?? null;
    },
    currentFor(scope, matches) {
      const publishers = mounted.get(scope) ?? [];
      return (
        publishers.findLast((entry) => matches(entry.current))?.current ??
        publishers.at(-1)?.current ??
        null
      );
    },
  };
}
