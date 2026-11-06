// Pure helpers for the pull request Changes tab's review navigation.

import type { FileDiffMetadata } from "@pierre/diffs";
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

/** Files matching the path filter, excluding those whose current `path` is in `hiddenPaths`. */
export function selectVisibleFiles(
  files: PullRequestChangedFile[],
  opts: { pathFilter: string; hiddenPaths: ReadonlySet<string> },
): PullRequestChangedFile[] {
  const matching = filterChangedPaths(files, opts.pathFilter);
  return opts.hiddenPaths.size
    ? matching.filter((file) => !opts.hiddenPaths.has(file.path))
    : matching;
}

/** One searchable string of the fetched patch: a file path or a line of its hunks. */
export interface PatchMatchTarget {
  /** Current path of the file whose section reveals the match. */
  path: string;
  kind: "line" | "path";
  side?: "additions" | "deletions";
  /** New-file number for additions and context, old-file number for deletions. */
  line?: number;
  /** The line without its newline, as the diff renders it; the path for `kind: "path"`. */
  text: string;
}

export interface Match {
  target: PatchMatchTarget;
  /** Character offsets into `target.text`. */
  start: number;
  end: number;
}

/** Path targets for every file, then the lines of each file's fetched hunks. */
export function buildSearchCorpus(
  diffs: ReadonlyMap<string, FileDiffMetadata>,
  files: PullRequestChangedFile[],
): PatchMatchTarget[] {
  const corpus: PatchMatchTarget[] = [];
  for (const file of files) {
    const diff = diffs.get(file.path);
    const previousPath = file.previous_path ?? diff?.prevName;
    corpus.push({ path: file.path, kind: "path", text: file.path });
    if (previousPath && previousPath !== file.path) {
      corpus.push({ path: file.path, kind: "path", text: previousPath });
    }
    if (diff) pushPatchLines(corpus, file.path, diff);
  }
  return corpus;
}

function pushPatchLines(corpus: PatchMatchTarget[], path: string, file: FileDiffMetadata) {
  for (const hunk of file.hunks) {
    for (const content of hunk.hunkContent) {
      for (const side of ["deletions", "additions"] as const) {
        // Context lines exist once, on the additions side.
        const count =
          content.type === "context" ? (side === "additions" ? content.lines : 0) : content[side];
        const start = side === "additions" ? content.additionLineIndex : content.deletionLineIndex;
        const hunkIndex = side === "additions" ? hunk.additionLineIndex : hunk.deletionLineIndex;
        const first = side === "additions" ? hunk.additionStart : hunk.deletionStart;
        const source = side === "additions" ? file.additionLines : file.deletionLines;
        for (let offset = 0; offset < count; offset++) {
          const text = source[start + offset];
          if (text === undefined) break;
          corpus.push({
            path,
            kind: "line",
            side,
            line: first + start - hunkIndex + offset,
            text: text.replace(/\r?\n$/, ""),
          });
        }
      }
    }
  }
}

/** Matches beyond this count are dropped and the result is marked truncated. */
export const MATCH_LIMIT = 10_000;

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Every non-empty occurrence of `query` in the corpus, in corpus order. */
export function findMatches(
  corpus: readonly PatchMatchTarget[],
  query: string,
  opts: { matchCase: boolean; regex: boolean; limit: number },
): { matches: Match[]; truncated: boolean; invalid: boolean } {
  const matches: Match[] = [];
  // Whitespace-only queries are blank, so a stray space doesn't match every line.
  if (!query.trim()) return { matches, truncated: false, invalid: false };
  let pattern: RegExp;
  try {
    pattern = new RegExp(opts.regex ? query : escapeRegExp(query), opts.matchCase ? "g" : "gi");
  } catch {
    return { matches, truncated: false, invalid: true };
  }
  for (const target of corpus) {
    for (const found of target.text.matchAll(pattern)) {
      if (!found[0]) continue;
      if (matches.length === opts.limit) return { matches, truncated: true, invalid: false };
      matches.push({ target, start: found.index, end: found.index + found[0].length });
    }
  }
  return { matches, truncated: false, invalid: false };
}

/** Index of the first match in a file at or below `topPath` (in `filePaths` order), else 0. */
export function firstMatchAtOrBelow(
  matches: readonly Match[],
  filePaths: readonly string[],
  topPath: string | null,
): number {
  const order = new Map(filePaths.map((path, index) => [path, index]));
  const top = topPath === null ? 0 : (order.get(topPath) ?? 0);
  return Math.max(
    matches.findIndex((match) => (order.get(match.target.path) ?? 0) >= top),
    0,
  );
}

/**
 * Index in a refreshed match list for the previous match: the same target if it
 * still exists, else the nearest line in its file, else 0.
 */
export function refreshedMatchIndex(matches: readonly Match[], previous: Match): number {
  const { target, start } = previous;
  const same = matches.findIndex(
    (match) =>
      match.start === start &&
      match.target.path === target.path &&
      match.target.kind === target.kind &&
      match.target.side === target.side &&
      match.target.line === target.line &&
      match.target.text === target.text,
  );
  if (same >= 0) return same;
  // A path target has no line to measure from, so take its file's path match.
  if (target.kind === "path") {
    const pathMatch = matches.findIndex(
      (match) => match.target.path === target.path && match.target.kind === "path",
    );
    return pathMatch >= 0 ? pathMatch : 0;
  }
  // Line numbers only compare within a side: old and new numbering drift apart.
  const sameFile = (match: Match) => match.target.path === target.path;
  const sameSide = (match: Match) =>
    match.target.kind === target.kind && match.target.side === target.side;
  const candidates = matches.flatMap((match, index) => (sameFile(match) ? [{ index, match }] : []));
  const preferred = candidates.filter(({ match }) => sameSide(match));
  let nearest = 0;
  let nearestDistance = Infinity;
  for (const { index, match } of preferred.length ? preferred : candidates) {
    const distance = Math.abs((match.target.line ?? 0) - (target.line ?? 0));
    if (distance < nearestDistance) {
      nearest = index;
      nearestDistance = distance;
    }
  }
  return nearest;
}

const PR_PATH_MARKERS: Record<string, string> = {
  github: "/pull/",
  gitlab: "/-/merge_requests/",
  azure_devops: "/pullrequest/",
};

/** The web URL of the repository a PR URL belongs to, or null when the PR URL doesn't match the provider. */
export function pullRequestRepoUrl(provider: string | null, prUrl: string): string | null {
  const marker =
    provider && Object.hasOwn(PR_PATH_MARKERS, provider) ? PR_PATH_MARKERS[provider] : "";
  if (!marker) return null;
  // The marker must be followed by the PR number at the end, so a repository path
  // that contains the marker (e.g. `pull/issues/7`) doesn't match.
  const escaped = marker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`^(https?://.+)${escaped}\\d+(?:[/?#].*)?$`).exec(prUrl);
  return match ? match[1] : null;
}

function patchFileStatus(
  diff: FileDiffMetadata,
  previousPath: string | null,
): PullRequestChangedFile["status"] {
  if (diff.type === "new") return "created";
  if (diff.type === "deleted") return "deleted";
  return previousPath ? "renamed" : "modified";
}

/** The files one commit's patch changes, in patch order, with their line counts. */
export function changedFilesFromPatch(
  diffs: ReadonlyMap<string, FileDiffMetadata>,
): PullRequestChangedFile[] {
  return [...diffs.values()].map((diff) => {
    const previousPath = diff.prevName && diff.prevName !== diff.name ? diff.prevName : null;
    return {
      path: diff.name,
      name: diff.name.split("/").pop() ?? diff.name,
      status: patchFileStatus(diff, previousPath),
      lines_added: diff.hunks.reduce((sum, hunk) => sum + hunk.additionLines, 0),
      lines_removed: diff.hunks.reduce((sum, hunk) => sum + hunk.deletionLines, 0),
      previous_path: previousPath,
      bytes: null,
      modified_at: null,
    };
  });
}

/**
 * The provider page for a changed file at the revision the list shows. Deleted files
 * link to the base repository at the base revision; others use the head (fork)
 * repository. Null when the repository or revision is unknown or the provider is unsupported.
 */
export function pullRequestFileUrl({
  provider,
  headRepoUrl,
  baseRepoUrl,
  headSha,
  baseSha,
  path,
  previousPath,
  status,
}: {
  provider: string | null;
  /** Absent from hosts that predate the field, so treat it as unknown. */
  headRepoUrl?: string | null;
  baseRepoUrl: string | null;
  headSha: string | null | undefined;
  baseSha: string | null | undefined;
  path: string;
  previousPath?: string | null;
  status: PullRequestChangedFile["status"];
}): string | null {
  const deleted = status === "deleted";
  const repo = deleted ? baseRepoUrl : headRepoUrl;
  const sha = deleted ? baseSha : headSha;
  if (!repo || !sha || !repo.startsWith("https://")) return null;
  const filePath = deleted ? (previousPath ?? path) : path;
  const encodedPath = filePath.split("/").map(encodeURIComponent).join("/");
  switch (provider) {
    case "github":
      return `${repo}/blob/${sha}/${encodedPath}`;
    case "gitlab":
      return `${repo}/-/blob/${sha}/${encodedPath}`;
    case "azure_devops":
      return `${repo}?path=/${encodedPath}&version=GC${sha}`;
    default:
      return null;
  }
}
