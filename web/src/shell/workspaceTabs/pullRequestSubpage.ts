// A pull request sub-page link (files, commits, checks) as the pull request's
// own URL plus the view it names, on GitHub, GitHub Enterprise, GitLab and
// Azure DevOps. Pure URL syntax, so the link parser can apply it
// before an explicit `view` would be indistinguishable from the default.

import { canonicalPullRequestUrl } from "@/lib/pullRequestReference";

import type { PullRequestView, WorkspacePlace } from "./workspacePlace";

export interface PullRequestSubpage {
  url: string;
  view?: PullRequestView;
  focus?: "checks";
  commit?: string;
}

const GITHUB_PULL = /^(\/[^/]+\/[^/]+\/pull\/\d+)(\/.*)?$/;
const GITLAB_MERGE_REQUEST = /^(.*\/-\/merge_requests\/\d+)(\/.*)?$/;
const AZURE_PULL_REQUEST = /^(.*\/pullrequest\/\d+)\/?$/i;

function trimSlashes(value: string | undefined): string {
  return (value ?? "").replace(/^\/+|\/+$/g, "");
}

/** Splits a provider's pull request sub-page into the pull request's own URL and a view. */
export function foldPullRequestSubpage(url: string): PullRequestSubpage {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return { url };
  }
  const origin = parsed.origin;
  const github = GITHUB_PULL.exec(parsed.pathname);
  if (github) {
    const rest = trimSlashes(github[2]);
    const own = `${origin}${github[1]}`;
    if (rest === "files") return { url: own, view: "changes" };
    if (rest === "commits") return { url: own, view: "commits" };
    if (rest === "checks") return { url: own, view: "summary", focus: "checks" };
    const commit = /^commits\/([0-9a-f]{4,64})$/i.exec(rest);
    if (commit) return { url: own, view: "commits", commit: commit[1].toLowerCase() };
    return { url: own };
  }
  const gitlab = GITLAB_MERGE_REQUEST.exec(parsed.pathname);
  if (gitlab) {
    const rest = trimSlashes(gitlab[2]);
    const own = `${origin}${gitlab[1]}`;
    if (rest === "diffs") return { url: own, view: "changes" };
    if (rest === "commits") return { url: own, view: "commits" };
    if (rest === "pipelines") return { url: own, view: "summary", focus: "checks" };
    return { url: own };
  }
  const azure = AZURE_PULL_REQUEST.exec(parsed.pathname);
  if (azure) {
    const own = `${origin}${azure[1]}`;
    const tab = parsed.searchParams.get("_a");
    if (tab === "files") return { url: own, view: "changes" };
    if (tab === "commits") return { url: own, view: "commits" };
    return { url: own };
  }
  return { url };
}

/**
 * One pull request's tab place, from any of its URLs, canonical as links make it: on the
 * section the URL names, else `view`.
 */
export function pullRequestTabPlace(
  url: string,
  view: PullRequestView = "summary",
): WorkspacePlace {
  const folded = foldPullRequestSubpage(url);
  return { kind: "pull", url: canonicalPullRequestUrl(folded.url), view: folded.view ?? view };
}
