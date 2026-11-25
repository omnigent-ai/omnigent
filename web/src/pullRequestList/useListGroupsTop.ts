// Where the list's groups start inside it, below its column header and any message, for the
// virtualizer's scroll margin. Measured after each layout change, guarded against a loop.

import { type RefObject, useLayoutEffect, useRef, useState } from "react";

import {
  createListLayoutLoopGuard,
  type ListLayoutInputs,
  type ListLayoutShape,
} from "./listLayoutLoop";

export interface ListGroupsTopSource {
  /** Changes when the groups may have moved. */
  nothingMatches: boolean;
  layout: unknown;
  /** The layout's inputs by name, and the list's shape, for a loop's record. */
  describe: () => { inputs: ListLayoutInputs; shape: () => ListLayoutShape };
}

export function useListGroupsTop(
  groupsRef: RefObject<HTMLElement | null>,
  { nothingMatches, layout, describe }: ListGroupsTopSource,
): number {
  const [groupsTop, setGroupsTop] = useState(0);
  const shown = useRef(0);
  const [guard] = useState(() => createListLayoutLoopGuard());
  const describeRef = useRef(describe);
  describeRef.current = describe;
  useLayoutEffect(() => {
    const top = groupsRef.current?.offsetTop ?? 0;
    // The same top again sets nothing; one that keeps moving stops for the rest of the task.
    if (top === shown.current) return;
    const { inputs, shape } = describeRef.current();
    if (!guard.allows(top, inputs, shape)) return;
    shown.current = top;
    setGroupsTop(top);
  }, [groupsRef, guard, nothingMatches, layout]);
  return groupsTop;
}
