// Ties a live-preview transaction to a picker's lifetime: one transaction per
// activation, cancelled when it deactivates or unmounts, and suspended while
// the window is in the background so an unattended preview never lingers.

import { useEffect, useMemo, useRef } from "react";

import { createLivePreview, type LivePreview } from "@/lib/livePreview";

export interface UseLivePreviewOptions<Value> {
  /** Whether the picker is showing; each activation starts a new transaction. */
  active: boolean;
  /** Reads the committed value when a transaction starts. */
  readCommitted: () => Value;
  /** Shows a value without persisting it. */
  apply: (value: Value) => void;
  /** Persists and shows the chosen value. */
  commit: (value: Value) => void;
}

export type LivePreviewHandle<Value> = Pick<LivePreview<Value>, "preview" | "commit" | "cancel">;

export function useLivePreview<Value>({
  active,
  readCommitted,
  apply,
  commit,
}: UseLivePreviewOptions<Value>): LivePreviewHandle<Value> {
  const callbacksRef = useRef({ readCommitted, apply, commit });
  callbacksRef.current = { readCommitted, apply, commit };
  const transactionRef = useRef<LivePreview<Value> | null>(null);

  useEffect(() => {
    if (!active) return;
    const transaction = createLivePreview<Value>({
      committed: callbacksRef.current.readCommitted(),
      apply: (value) => callbacksRef.current.apply(value),
      commit: (value) => callbacksRef.current.commit(value),
    });
    transactionRef.current = transaction;
    const suspend = () => transaction.suspend();
    const resume = () => transaction.resume();
    window.addEventListener("blur", suspend);
    window.addEventListener("focus", resume);
    return () => {
      window.removeEventListener("blur", suspend);
      window.removeEventListener("focus", resume);
      transaction.cancel();
      if (transactionRef.current === transaction) transactionRef.current = null;
    };
  }, [active]);

  return useMemo(
    () => ({
      preview: (value) => transactionRef.current?.preview(value),
      commit: (value) => transactionRef.current?.commit(value),
      cancel: () => transactionRef.current?.cancel(),
    }),
    [],
  );
}
