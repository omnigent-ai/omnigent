import { useCallback, useEffect, useState } from "react";

import {
  EMPTY_COMPOSER_CONTEXT,
  normalizeComposerContextState,
  type ComposerContextState,
  type ComposerMcpSelection,
  type ComposerRepositorySelection,
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
  const setRepositories = useCallback(
    (repositories: ComposerRepositorySelection[]) =>
      update((current) => ({ ...current, repositories })),
    [update],
  );
  const setMcpContext = useCallback(
    (mcpContext: ComposerMcpSelection[]) => update((current) => ({ ...current, mcpContext })),
    [update],
  );

  return {
    state,
    setState: update,
    setWorkingDirectory,
    setWorktree,
    setRepositories,
    setMcpContext,
  };
}
