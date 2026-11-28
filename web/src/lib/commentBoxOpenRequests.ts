// Asks a mounted comment box to open, show Write, and take the caret at the end of its draft,
// as Quote Reply does once it has added its quote. Boxes are named by their draft's key.

import { useEffect, useRef } from "react";

const listeners = new Map<string, Set<() => void>>();

/** The name a box answers to: its draft's key, as one string. */
export function commentBoxName(draftKey: readonly string[]): string {
  return JSON.stringify(draftKey);
}

/** Ask the box for `name` to open; true when one is mounted to hear it. */
export function requestCommentBoxOpen(name: string): boolean {
  const heard = listeners.get(name);
  if (!heard?.size) return false;
  for (const listener of heard) listener();
  return true;
}

/** Run `open` whenever the box for `name` is asked to open. */
export function useCommentBoxOpenRequests(name: string | undefined, open: () => void): void {
  const latest = useRef(open);
  latest.current = open;
  useEffect(() => {
    if (!name) return;
    const listener = () => latest.current();
    let heard = listeners.get(name);
    if (!heard) {
      heard = new Set();
      listeners.set(name, heard);
    }
    heard.add(listener);
    return () => {
      heard.delete(listener);
      if (heard.size === 0) listeners.delete(name);
    };
  }, [name]);
}
