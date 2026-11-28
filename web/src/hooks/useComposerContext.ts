import { useCallback, useEffect, useState } from "react";

import {
  EMPTY_COMPOSER_CONTEXT,
  normalizeComposerContextState,
  type ComposerContextState,
  type WorkingDirectoryGitState,
  type WorkingDirectorySelection,
  type WorktreeSelection,
} from "@/lib/composerContext";

export interface UseComposerContextOptions {
  initialState?: ComposerContextState;
  workingDirectoryGitState?: WorkingDirectoryGitState;
  onChange?: (state: ComposerContextState) => void;
}

export function useComposerContext({
  initialState = EMPTY_COMPOSER_CONTEXT,
  workingDirectoryGitState = "unknown",
  onChange,
}: UseComposerContextOptions = {}) {
  const [state, setState] = useState(() =>
    normalizeComposerContextState(initialState, workingDirectoryGitState),
  );

  const update = useCallback(
    (next: ComposerContextState | ((current: ComposerContextState) => ComposerContextState)) => {
      setState((current) => {
        const resolved = typeof next === "function" ? next(current) : next;
        return normalizeComposerContextState(resolved, workingDirectoryGitState);
      });
    },
    [workingDirectoryGitState],
  );

  useEffect(() => {
    setState((current) => normalizeComposerContextState(current, workingDirectoryGitState));
  }, [workingDirectoryGitState]);

  useEffect(() => {
    onChange?.(state);
  }, [onChange, state]);

  const setWorkingDirectory = useCallback(
    (workingDirectory: WorkingDirectorySelection) =>
      update((current) => ({ ...current, workingDirectory })),
    [update],
  );
  const setWorktree = useCallback(
    (worktree: WorktreeSelection) => update((current) => ({ ...current, worktree })),
    [update],
  );
  return {
    state,
    setState: update,
    setWorkingDirectory,
    setWorktree,
  };
}
