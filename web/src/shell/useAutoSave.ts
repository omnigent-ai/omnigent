// Debounced auto-save for the markdown editor: a debounce timer plus
// single-flight (never two writes at once) and trailing-save (edits during a
// write get one follow-up) guards. Standalone so these timing invariants are
// unit-testable with fake timers, no TipTap needed.

import { useCallback, useEffect, useMemo, useRef } from "react";

interface UseAutoSaveOptions {
  /** Debounce delay (ms) between the last edit and an auto-save. */
  delayMs: number;
  /** False suppresses auto-save entirely — read-only, offline runner, or unresolved conflict. */
  enabled: boolean;
  /** Content to persist, read lazily at save time (e.g. editor.getMarkdown()). */
  getContent: () => string;
  /** True when there are unsaved edits worth persisting. */
  isDirty: () => boolean;
  /** Persists content; must resolve even on failure so the single-flight guard releases. */
  save: (content: string) => Promise<void>;
}

interface UseAutoSaveResult {
  /** (Re)arm the debounce timer; call on every edit. */
  schedule: () => void;
  /** Cancel the pending timer and save immediately if enabled + dirty. */
  flush: () => void;
  /** Cancel the pending timer without saving. */
  cancel: () => void;
}

/**
 * Debounced auto-save with single-flight + trailing-save semantics. Returns
 * stable schedule/flush/cancel — safe as an effect dependency.
 */
export function useAutoSave({
  delayMs,
  enabled,
  getContent,
  isDirty,
  save,
}: UseAutoSaveOptions): UseAutoSaveResult {
  // Latest values in refs so the callbacks below stay stable (they bind to
  // editor events and must not re-bind on every keystroke).
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const getContentRef = useRef(getContent);
  getContentRef.current = getContent;
  const isDirtyRef = useRef(isDirty);
  isDirtyRef.current = isDirty;
  const saveRef = useRef(save);
  saveRef.current = save;
  const delayRef = useRef(delayMs);
  delayRef.current = delayMs;

  const timerRef = useRef<number>(0);
  // True while a save() is in flight — prevents overlapping writes.
  const inFlightRef = useRef(false);
  // Set when an edit lands mid-write; triggers one follow-up save after it settles.
  const resaveQueuedRef = useRef(false);

  const cancel = useCallback(() => {
    window.clearTimeout(timerRef.current);
    timerRef.current = 0;
  }, []);

  // Save now, serialising callers into one in-flight write + an optional
  // trailing re-save.
  const runSave = useCallback(async () => {
    if (!enabledRef.current || !isDirtyRef.current()) return;
    if (inFlightRef.current) {
      // Write in progress — save again once it settles so mid-write edits aren't dropped.
      resaveQueuedRef.current = true;
      return;
    }
    inFlightRef.current = true;
    try {
      await saveRef.current(getContentRef.current());
    } finally {
      inFlightRef.current = false;
      const shouldResave = resaveQueuedRef.current && enabledRef.current && isDirtyRef.current();
      resaveQueuedRef.current = false;
      // Trailing save: edits landed mid-write; persist the latest content.
      if (shouldResave) void runSave();
    }
  }, []);

  const flush = useCallback(() => {
    cancel();
    void runSave();
  }, [cancel, runSave]);

  const schedule = useCallback(() => {
    cancel();
    timerRef.current = window.setTimeout(() => {
      timerRef.current = 0;
      void runSave();
    }, delayRef.current);
  }, [cancel, runSave]);

  // Clear the pending timer on unmount (flushing on unmount is the component's job).
  useEffect(
    () => () => {
      window.clearTimeout(timerRef.current);
    },
    [],
  );

  // Stable identity (schedule/flush/cancel are stable) — safe as an effect dep.
  return useMemo(() => ({ schedule, flush, cancel }), [schedule, flush, cancel]);
}
