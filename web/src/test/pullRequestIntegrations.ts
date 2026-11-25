// Pull Requests page integrations for tests: register some for one test file, and stand-ins
// for a named code host, a review site, a stack format and a merge readiness source.

import GithubMono from "@lobehub/icons/es/Github/components/Mono";
import { useQueries } from "@tanstack/react-query";
import { createElement } from "react";
import { afterEach, beforeEach } from "vitest";

import {
  MERGE_READINESS_DETAILS,
  type MergeReadinessView,
  type MergeReadinessWording,
  presentMergeReadinessView,
} from "@/lib/mergeReadiness";
import {
  type MergeReadinessSource,
  type NamedCodeHost,
  type PullRequestIntegrations,
  type PullRequestReviewSite,
  pullRequestIntegrations,
  registerPullRequestIntegrations,
  restorePullRequestIntegrations,
} from "@/lib/pullRequestIntegrations";
import { markdownListStackFormat } from "@/lib/pullRequestStacks/formats/markdownListStack";
import { quoteShellArgument } from "@/lib/shell";

/** Registers `added` before each test in the file, and puts the previous ones back after. */
export function withPullRequestIntegrations(added: Partial<PullRequestIntegrations>): void {
  let saved: PullRequestIntegrations | null = null;
  beforeEach(() => {
    saved = pullRequestIntegrations();
    registerPullRequestIntegrations(added);
  });
  afterEach(() => {
    if (saved) restorePullRequestIntegrations(saved);
  });
}

/** A GitSpoke stand-in: numbered servers under `spoke.example.com`, read-only from the Mac. */
export const EXAMPLE_SPOKE_HOST: NamedCodeHost = {
  provider: "gitspoke",
  matchesHost: (host) => /^[1-9][0-9]*\.spoke\.example\.com$/.test(host.toLowerCase()),
  copy: {
    id: "gitspoke",
    label: "GitSpoke",
    requestName: "pull request",
    Icon: GithubMono,
    prNumberPrefix: "#",
    prUrlPlaceholder: "https://1.spoke.example.com/owner/repo/pull/123",
    defaultHost: null,
    authHint: "Renew this Mac's certificate, then try again.",
    cliLabel: "spoke CLI",
  },
  hostedOnGitHub: true,
};

/** A review site at `review.example.com` that takes a PR's owner, repository and number. */
export const EXAMPLE_REVIEW_SITE: PullRequestReviewSite = {
  name: "Example Review",
  reviewUrl: (pullRequestUrl) => {
    const match = /^https:\/\/github\.com\/([^/]+)\/([^/]+)\/pull\/(\d+)$/.exec(pullRequestUrl);
    return match ? `https://review.example.com/${match[1]}/${match[2]}/pull/${match[3]}` : null;
  },
  useEnabled: () => true,
};

/**
 * A stack format whose section opens with `## Stacked changes`, written and landed by an example `stk`
 * tool: `stk land` lands the stack up to a branch, `stk sync` drops a merged parent's commits.
 */
export const EXAMPLE_STACK_FORMAT = markdownListStackFormat({
  id: "example-stack",
  header: "## Stacked changes",
  headerSuffix: " (_written by stk_)",
  introLine: /^Review the changes since the PR below \[here\]\([^)\s]*\)\.$/,
  landing: {
    name: "stk",
    landName: "stk land",
    checkName: "stk land --check",
    syncName: "stk sync",
    landCommand: (branch, { check }) =>
      `git switch -- ${quoteShellArgument(branch)} && stk land${check ? " --check" : ""}`,
    syncCommand: "stk sync && stk push",
  },
});

const EXAMPLE_READINESS_KEY = "example-merge-readiness";

/** The stand-in source's wording: the generic reasons, and its own name for its failures. */
export const EXAMPLE_READINESS_WORDING: MergeReadinessWording = {
  details: MERGE_READINESS_DETAILS,
  failures: {
    credential: {
      label: "Renew your credential",
      short: "Renew",
      tone: "yellow",
      detail: "The merge status service needs a valid credential.",
    },
    github: {
      label: "Merge status unknown",
      short: "Unknown",
      tone: "gray",
      detail: "GitHub couldn't answer for this pull request. The app will try again.",
    },
    service: {
      label: "Merge status unknown",
      short: "Unknown",
      tone: "gray",
      detail: "Couldn't reach the merge status service. The app will try again.",
    },
  },
};

/**
 * A merge readiness source that asks `read` for each PR's view, through react-query like a
 * real one: paused while hidden, polled, and read again on refresh. A credential failure
 * offers a Renew button.
 */
export function exampleMergeReadinessSource(
  read: (url: string) => Promise<MergeReadinessView>,
): MergeReadinessSource {
  return {
    githubOperation: "example_merge_readiness",
    useMergeReadiness(targets, { active, pollMs }) {
      const results = useQueries({
        queries: targets.map((target) => ({
          queryKey: [EXAMPLE_READINESS_KEY, target.url],
          queryFn: () => read(target.url),
          enabled: active,
          refetchInterval: pollMs,
          refetchIntervalInBackground: false,
        })),
      });
      return new Map(
        targets.flatMap((target, index) => {
          const data = results[index]?.data;
          return data ? [[target.url, data] as const] : [];
        }),
      );
    },
    refresh: (queryClient) => queryClient.invalidateQueries({ queryKey: [EXAMPLE_READINESS_KEY] }),
    refreshPullRequest: (queryClient, prUrl) =>
      queryClient.invalidateQueries({ queryKey: [EXAMPLE_READINESS_KEY, prUrl] }),
    readOf: (queryKey, prUrl) =>
      queryKey[0] === EXAMPLE_READINESS_KEY && queryKey[1] === prUrl
        ? queryKey[2] === "checks"
          ? "checks"
          : "readiness"
        : null,
    present: (view) => presentMergeReadinessView(view, EXAMPLE_READINESS_WORDING),
    CredentialRenewal: ({ onRenewed }) =>
      createElement("button", { type: "button", onClick: onRenewed }, "Renew"),
    useOwnedFiles: () => null,
  };
}
