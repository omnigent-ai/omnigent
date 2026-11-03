// Pure helpers for the pull request Changes tab's review navigation.

import type { PullRequestChangedFile } from "@/hooks/usePullRequests";

/** Files whose path or previous path contains `query`, ignoring case and slash direction. */
export function filterChangedPaths(
  files: PullRequestChangedFile[],
  query: string,
): PullRequestChangedFile[] {
  const needle = query.trim().replaceAll("\\", "/").toLowerCase();
  if (!needle) return files;
  return files.filter((file) =>
    [file.path, file.previous_path].some((path) =>
      path?.replaceAll("\\", "/").toLowerCase().includes(needle),
    ),
  );
}

/** The files the Changes tab shows: those matching the path filter and not hidden by current path. */
export function selectVisibleFiles(
  files: PullRequestChangedFile[],
  opts: { pathFilter: string; hiddenPaths: ReadonlySet<string> },
): PullRequestChangedFile[] {
  const matching = filterChangedPaths(files, opts.pathFilter);
  return opts.hiddenPaths.size
    ? matching.filter((file) => !opts.hiddenPaths.has(file.path))
    : matching;
}
