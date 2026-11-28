// The confirmation a copy button gives: a check for a moment, or the failure a little longer.
// Among several targets, such as each commit's SHA, only the last one copied confirms.

import { useCallback, useEffect, useRef, useState } from "react";

export type CopyState = "idle" | "copied" | "failed";

/** How long a button confirms a copy, and the longer it leaves a failure up to be read. */
export const COPIED_MS = 1_500;
export const COPY_FAILED_MS = 2_500;

export function useCopyFeedback<Target = undefined>(): {
  stateOf: (target?: Target) => CopyState;
  /** Runs `perform` and confirms it for `target`; a rejection is a failure. */
  copy: (perform: () => Promise<unknown>, target?: Target) => void;
} {
  const [feedback, setFeedback] = useState<{
    target?: Target;
    state: Exclude<CopyState, "idle">;
  }>();
  const reset = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // Each copy's number: only the latest one's result, and its timer, may change the feedback.
  const generation = useRef(0);
  useEffect(
    () => () => {
      generation.current += 1;
      clearTimeout(reset.current);
    },
    [],
  );
  const copy = useCallback((perform: () => Promise<unknown>, target?: Target) => {
    const current = ++generation.current;
    clearTimeout(reset.current);
    void perform()
      .then(
        () => true,
        () => false,
      )
      .then((copied) => {
        if (current !== generation.current) return;
        clearTimeout(reset.current);
        setFeedback({ target, state: copied ? "copied" : "failed" });
        reset.current = setTimeout(
          () => {
            if (current === generation.current) setFeedback(undefined);
          },
          copied ? COPIED_MS : COPY_FAILED_MS,
        );
      });
  }, []);
  const stateOf = useCallback(
    (target?: Target): CopyState =>
      feedback && feedback.target === target ? feedback.state : "idle",
    [feedback],
  );
  return { stateOf, copy };
}
