import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { MergeReadinessView } from "@/lib/mergeReadiness";
import type { MergeReadinessSource } from "@/lib/pullRequestIntegrations";
import type { PullRequestInfo } from "@/newPullRequestPage/hooks/usePullRequests";
import { withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

import {
  isPullRequestMergeReadinessKey,
  mergeReadinessGitHubOperation,
  ownedChangedPaths,
  refreshMergeReadiness,
  sessionMergeReadinessTarget,
  useMergeReadiness,
  useOwnedFiles,
} from "./useMergeReadiness";

const URL = "https://github.com/acme/app/pull/9";
const OPTIONS = { active: true, pollMs: false } as const;

function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: new QueryClient() }, children);
}

describe("sessionMergeReadinessTarget", () => {
  // Only the fields the target reads.
  const info = (url: string) =>
    ({
      object: "session.github.info",
      available: true,
      provider: "github",
      auth: { selected_account: "work-login" },
      pr: { url, number: 9, state: "OPEN" },
    }) as PullRequestInfo;

  it("reads a PR as the account in its own response", () => {
    expect(sessionMergeReadinessTarget(info(URL), URL, "abc")).toEqual({
      url: URL,
      account: { host: "github.com", login: "work-login" },
      headSha: "abc",
    });
  });

  it("gives no account from a response about another PR", () => {
    const other = "https://github.com/me/dotfiles/pull/3";
    expect(sessionMergeReadinessTarget(info(other), URL, undefined)).toEqual({
      url: URL,
      account: null,
      headSha: null,
    });
  });
});

describe("ownedChangedPaths", () => {
  it("keeps the owned changed paths, and marks nothing when none or all are owned", () => {
    const changed = ["a", "b", "c"];
    expect(ownedChangedPaths(changed, ["b", "elsewhere"])).toEqual(new Set(["b"]));
    expect(ownedChangedPaths(changed, ["elsewhere"])).toBeNull();
    expect(ownedChangedPaths(changed, ["a", "b", "c"])).toBeNull();
    expect(ownedChangedPaths(changed, [])).toBeNull();
    expect(ownedChangedPaths(changed, null)).toBeNull();
  });
});

describe("without a merge readiness source", () => {
  it("shows no readiness and marks no owned files", async () => {
    const readiness = renderHook(
      () => useMergeReadiness([{ url: URL, account: null, headSha: null }], OPTIONS),
      { wrapper },
    );
    expect(readiness.result.current.size).toBe(0);
    const owned = renderHook(
      () => useOwnedFiles({ url: URL, viewerLogin: "me", headSha: "a".repeat(40) }, OPTIONS),
      { wrapper },
    );
    expect(owned.result.current).toBeNull();
    expect(mergeReadinessGitHubOperation()).toBeNull();
    expect(isPullRequestMergeReadinessKey(["anything", "x", `\n${URL}`], URL)).toBe(false);
    await expect(refreshMergeReadiness(new QueryClient())).resolves.toBeUndefined();
  });
});

describe("with a merge readiness source", () => {
  const READY: MergeReadinessView = {
    kind: "known",
    readiness: { status: "ready", reasons: { failingJobs: [], failingRequirements: [] } },
    headSha: "a".repeat(40),
  };
  const source: MergeReadinessSource = {
    githubOperation: "example_readiness",
    useMergeReadiness: (targets) => new Map(targets.map((target) => [target.url, READY])),
    refresh: vi.fn(async () => {}),
    refreshPullRequest: vi.fn(async () => {}),
    readOf: (queryKey, prUrl) =>
      queryKey[0] === "example" && queryKey[1] === prUrl ? "readiness" : null,
    present: () => ({ label: "Ready", short: "Ready", tone: "green" }),
    CredentialRenewal: () => null,
    useOwnedFiles: () => ["src/owned.ts"],
  };
  withPullRequestIntegrations({ mergeReadiness: source });

  it("reads each target's readiness and owned files from the source", async () => {
    const readiness = renderHook(
      () => useMergeReadiness([{ url: URL, account: null, headSha: null }], OPTIONS),
      { wrapper },
    );
    expect(readiness.result.current.get(URL)).toBe(READY);
    const owned = renderHook(
      () => useOwnedFiles({ url: URL, viewerLogin: "me", headSha: "a".repeat(40) }, OPTIONS),
      { wrapper },
    );
    expect(owned.result.current).toEqual(["src/owned.ts"]);
    expect(mergeReadinessGitHubOperation()).toBe("example_readiness");
    expect(isPullRequestMergeReadinessKey(["example", URL], URL)).toBe(true);
    const client = new QueryClient();
    await refreshMergeReadiness(client);
    expect(source.refresh).toHaveBeenCalledWith(client);
  });
});
