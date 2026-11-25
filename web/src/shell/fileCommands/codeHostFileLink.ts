// Where a file lives on its code host, so a diff review's header menu, ⌘K and
// the file viewer can open it there. Plain facts in, no React. A working-copy
// file links through its workspace's repository context, where its branch is pushed.

import type { SelectedLineRange } from "@pierre/diffs";

import type { DiffReviewChange } from "@/components/diff-review/diffReviewModel";
import { codeHostProvider, parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { gitProviderCopy } from "@/lib/gitProviders";
import {
  PushUnresolved,
  RepositoryContextProblem,
  type ResolveRepositoryContextResponse,
} from "@/lib/workspaceSidebarApi";

/**
 * The "Open in GitHub" action for one file: where it opens, or why it can't, such as a
 * deleted file or a pull request whose source repository is gone.
 */
export type CodeHostFileLink =
  | { label: string; url: string; unavailableReason?: undefined }
  | { label: string; url: null; unavailableReason: string };

/** The GitHub repository segments a source-repository URL may name, as GitHub allows them. */
const REPOSITORY_SEGMENT = /^[A-Za-z0-9._-]+$/;

/**
 * The repository a pull request's commits live in, from its `head_repository_url`, when that
 * names one on the pull request's own host: `undefined` falls back to the pull request's
 * repository (a host too old to say), and null or anything else means it isn't known.
 */
export function sourceRepository(
  headRepositoryUrl: string | null | undefined,
  pullRequestHost: string,
): { owner: string; repo: string } | "unknown" | "same" {
  if (headRepositoryUrl === undefined) return "same";
  if (typeof headRepositoryUrl !== "string") return "unknown";
  let parsed: URL;
  try {
    parsed = new URL(headRepositoryUrl);
  } catch {
    return "unknown";
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.port ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash ||
    parsed.hostname.toLowerCase() !== pullRequestHost
  ) {
    return "unknown";
  }
  const segments = parsed.pathname.replace(/\/$/, "").split("/").slice(1);
  if (segments.length !== 2) return "unknown";
  const [owner, repo] = segments;
  if (!owner || !repo || ![owner, repo].every((segment) => REPOSITORY_SEGMENT.test(segment))) {
    return "unknown";
  }
  if (repo === "." || repo === "..") return "unknown";
  return { owner, repo };
}

/**
 * A repository GitHub names as `owner/repo`, such as a stack member's head repository: absent
 * from data too old to say, which is the pull request's own; null when it's gone.
 */
export function namedRepository(
  nameWithOwner: string | null | undefined,
): { owner: string; repo: string } | "unknown" | "same" {
  if (nameWithOwner === undefined) return "same";
  if (typeof nameWithOwner !== "string") return "unknown";
  const segments = nameWithOwner.split("/");
  if (segments.length !== 2) return "unknown";
  const [owner, repo] = segments;
  if (!owner || !repo || ![owner, repo].every((segment) => REPOSITORY_SEGMENT.test(segment))) {
    return "unknown";
  }
  if (repo === "." || repo === "..") return "unknown";
  return { owner, repo };
}

/** The repository a source names, the pull request's own when "same". */
function repositoryOf(
  source: { owner: string; repo: string } | "same",
  pullRequestRepository: string,
): { owner: string; repo: string } {
  if (source !== "same") return source;
  const [owner, repo] = pullRequestRepository.split("/");
  return { owner, repo };
}

const UNKNOWN_SOURCE = "The pull request's source repository isn't known.";

interface BlobLocation {
  host: string;
  owner: string;
  repo: string;
  /** A commit, or a branch name such as `feature/x`. */
  revision: string;
  path: string;
  /** The lines to highlight, already on the side `revision` shows. */
  lines?: { start: number; end: number } | null;
}

function encodeSegments(value: string): string {
  return value.split("/").map(encodeURIComponent).join("/");
}

/** `https://<host>/<owner>/<repo>/blob/<revision>/<path>#L<a>-L<b>`, as GitHub links a file. */
export function codeHostBlobUrl({
  host,
  owner,
  repo,
  revision,
  path,
  lines,
}: BlobLocation): string {
  const base = `https://${host}/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/blob/${encodeSegments(revision)}/${encodeSegments(path)}`;
  if (!lines) return base;
  const start = Math.min(lines.start, lines.end);
  const end = Math.max(lines.start, lines.end);
  return start === end ? `${base}#L${start}` : `${base}#L${start}-L${end}`;
}

/** The one side a selection is on, or "both" when it spans the old and new lines. */
function selectionSide(range: SelectedLineRange): "additions" | "deletions" | "both" {
  const startSide = range.side ?? "additions";
  const endSide = range.endSide ?? startSide;
  return startSide === endSide ? startSide : "both";
}

/** `range`'s lines when the whole selection is on the new side, the one the head shows. */
function newSideLines(
  range: SelectedLineRange | null | undefined,
): { start: number; end: number } | null {
  if (!range) return null;
  return selectionSide(range) === "additions" ? { start: range.start, end: range.end } : null;
}

function providerLabel(provider: string): string {
  return gitProviderCopy(provider).label;
}

export interface PullRequestFileLinkFacts {
  prUrl: string | null | undefined;
  /** The revision the diff on screen was read at. */
  headSha: string | null | undefined;
  /**
   * Where the pull request's commits live, such as a fork. Absent from hosts too old to say,
   * which opens the pull request's own repository; null when it isn't known.
   */
  headRepositoryUrl?: string | null;
  path: string;
  change: DiffReviewChange;
  /** The review's selected lines, when they're in this file. */
  selection?: SelectedLineRange | null;
  /** Hosts known to serve GitHub Enterprise. */
  githubHosts?: Iterable<string>;
}

/**
 * "Open in GitHub" for a pull request file, in the repository its commits live in
 * at the revision the diff was read at. Null when the PR's host isn't known. A deleted file
 * can't be opened: its old text is at the merge base, which the review doesn't know, not at
 * the target branch's tip.
 */
export function pullRequestFileLink(facts: PullRequestFileLinkFacts): CodeHostFileLink | null {
  const pullRequest = parseCodeHostPullRequestUrl(facts.prUrl, facts.githubHosts);
  if (!pullRequest) return null;
  const provider = providerLabel(pullRequest.provider);
  const label = `Open in ${provider}`;
  if (facts.change === "deleted") {
    return { label, url: null, unavailableReason: `Deleted files can't be opened in ${provider}.` };
  }
  if (!facts.headSha) {
    return {
      label,
      url: null,
      unavailableReason: "The pull request's latest commit isn't known yet.",
    };
  }
  const source = sourceRepository(facts.headRepositoryUrl, pullRequest.host);
  if (source === "unknown") return { label, url: null, unavailableReason: UNKNOWN_SOURCE };
  const { owner, repo } = repositoryOf(source, pullRequest.repository);
  return {
    label,
    url: codeHostBlobUrl({
      host: pullRequest.host,
      owner,
      repo,
      revision: facts.headSha,
      path: facts.path,
      lines: newSideLines(facts.selection),
    }),
  };
}

export interface StackComparisonFileLinkFacts {
  prUrl: string | null | undefined;
  /** The stacked PR's head, the comparison's new side. */
  childHead: string;
  /** Its parent's head, the comparison's old side. */
  parentHead: string;
  path: string;
  /** The file's path at the parent's head, when it was renamed or copied. */
  previousPath: string | null;
  /**
   * Where the stacked PR's own commits live, as `head_repository_url`: absent from hosts too
   * old to say, null when it isn't known.
   */
  headRepositoryUrl?: string | null;
  /** Where the parent's commits live, as `owner/repo`: absent when unknown to say, null when gone. */
  parentHeadRepository?: string | null;
  change: DiffReviewChange;
  selection?: SelectedLineRange | null;
  githubHosts?: Iterable<string>;
}

/**
 * "Open in GitHub" for a file of a stacked PR's own changes. Old lines open at the
 * parent's head in the parent's repository, where they are, and new lines at the PR's head in
 * its own, either of which may be a fork; a deleted file only has old lines, an added one only
 * new. A selection across both sides has no one place to open.
 */
export function stackComparisonFileLink(
  facts: StackComparisonFileLinkFacts,
): CodeHostFileLink | null {
  const pullRequest = parseCodeHostPullRequestUrl(facts.prUrl, facts.githubHosts);
  if (!pullRequest) return null;
  const selected = facts.selection ? selectionSide(facts.selection) : null;
  if (selected === "both") return null;
  const side = selected ?? (facts.change === "deleted" ? "deletions" : "additions");
  if (side === "deletions" && facts.change === "added") return null;
  if (side === "additions" && facts.change === "deleted") return null;
  const label = `Open in ${providerLabel(pullRequest.provider)}`;
  const source =
    side === "deletions"
      ? namedRepository(facts.parentHeadRepository)
      : sourceRepository(facts.headRepositoryUrl, pullRequest.host);
  if (source === "unknown") {
    return {
      label,
      url: null,
      unavailableReason:
        side === "deletions"
          ? "The parent pull request's source repository isn't known."
          : UNKNOWN_SOURCE,
    };
  }
  const { owner, repo } = repositoryOf(source, pullRequest.repository);
  const lines = facts.selection ? { start: facts.selection.start, end: facts.selection.end } : null;
  return {
    label,
    url: codeHostBlobUrl({
      host: pullRequest.host,
      owner,
      repo,
      revision: side === "deletions" ? facts.parentHead : facts.childHead,
      path: side === "deletions" ? (facts.previousPath ?? facts.path) : facts.path,
      lines,
    }),
  };
}

/** What `workspaceFileLink` reads from the host's repository context for the workspace. */
export type WorkspaceRepositoryContext = Pick<
  ResolveRepositoryContextResponse,
  "problem" | "branch" | "headSha" | "push" | "pushUnresolved" | "remotes" | "workspacePrefix"
>;

export interface WorkspaceFileLinkFacts {
  context: WorkspaceRepositoryContext | null | undefined;
  /** The file's path in the workspace, as the viewer names it. */
  path: string;
  lines?: { start: number; end: number } | null;
  /** Hosts known to serve GitHub Enterprise. */
  githubHosts?: Iterable<string>;
}

const PUSH_BRANCH_FIRST = "Push this branch to open the file in GitHub.";

/** `path` below the repository's root, or null for a path outside the workspace. */
function repositoryPathOf(workspacePrefix: string, path: string): string | null {
  if (path.startsWith("/") || path.startsWith("~") || /^[A-Za-z]:[\\/]/.test(path)) return null;
  const segments = path.split("/");
  if (segments.some((segment) => segment === "" || segment === "." || segment === "..")) {
    return null;
  }
  return `${workspacePrefix}${path}`;
}

/**
 * "Open in GitHub" for a workspace file, in the repository its branch pushes to: at HEAD
 * when the push remote has it, so line anchors match, otherwise at the pushed branch without
 * them. Null
 * when the workspace isn't a GitHub repository, the path is outside it, or the host is too
 * old to say where the workspace sits in its repository.
 */
export function workspaceFileLink(facts: WorkspaceFileLinkFacts): CodeHostFileLink | null {
  const { context } = facts;
  if (!context || context.problem !== RepositoryContextProblem.UNSPECIFIED) return null;
  if (context.workspacePrefix === undefined) return null;
  const path = repositoryPathOf(context.workspacePrefix, facts.path);
  if (path === null) return null;
  const onGitHub = (host: string) => codeHostProvider(host, facts.githubHosts) === "github";
  const label = `Open in ${providerLabel("github")}`;
  const { push } = context;
  if (push) {
    if (!onGitHub(push.host)) return null;
    // No tracking commit: the push remote may not have the branch yet.
    if (!push.trackingSha) return { label, url: null, unavailableReason: PUSH_BRANCH_FIRST };
    const atHead = push.trackingSha === context.headSha;
    return {
      label,
      url: codeHostBlobUrl({
        host: push.host,
        owner: push.owner,
        repo: push.repo,
        revision: atHead ? context.headSha : push.branch,
        path,
        // Line numbers only match the remote's copy at HEAD.
        lines: atHead ? facts.lines : null,
      }),
    };
  }
  if (!context.remotes.some((remote) => onGitHub(remote.host))) return null;
  if (!context.branch) {
    return {
      label,
      url: null,
      unavailableReason: "Check out a branch to open the file in GitHub.",
    };
  }
  if (context.pushUnresolved !== PushUnresolved.UNSPECIFIED) {
    return {
      label,
      url: null,
      unavailableReason: "This branch doesn't push to one GitHub repository.",
    };
  }
  return { label, url: null, unavailableReason: PUSH_BRANCH_FIRST };
}
