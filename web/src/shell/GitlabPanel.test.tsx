import { describe, expect, it } from "vitest";
import { RunnerOfflineError } from "@/hooks/useWorkspaceChangedFiles";
import type { GitlabInfo } from "@/hooks/useGitlab";
import { deriveGitlabPanelState, relatedGitlabMergeRequests } from "./GitlabPanel";

function state(data?: GitlabInfo, error: unknown = null) {
  return deriveGitlabPanelState({ isLoading: false, error, data });
}

describe("deriveGitlabPanelState", () => {
  it("distinguishes a non-git workspace", () => {
    expect(
      state({ object: "session.gitlab.info", available: false, reason: "not_a_git_repo" }),
    ).toEqual({ kind: "not-a-git-repo" });
  });

  it("distinguishes missing CLI and unreachable upstream", () => {
    expect(
      state({
        object: "session.gitlab.info",
        available: true,
        glab_available: false,
      }),
    ).toEqual({ kind: "no-glab-cli" });
    expect(
      state({
        object: "session.gitlab.info",
        available: true,
        glab_available: true,
        authenticated: false,
      }),
    ).toEqual({ kind: "repo-unresolved" });
  });

  it("reports the branch when no merge request exists", () => {
    expect(
      state({
        object: "session.gitlab.info",
        available: true,
        glab_available: true,
        authenticated: true,
        branch: "feature",
        repo: { host: "gitlab.example", path_with_namespace: "group/project" },
        merge_request: null,
      }),
    ).toEqual({ kind: "no-mr", branch: "feature" });
  });

  it("handles runner and ready states", () => {
    expect(state(undefined, new RunnerOfflineError())).toEqual({ kind: "runner-offline" });
    expect(
      state({
        object: "session.gitlab.info",
        available: true,
        glab_available: true,
        authenticated: true,
        repo: { host: "gitlab.example", path_with_namespace: "group/project" },
        merge_request: { iid: 42 },
      }),
    ).toEqual({ kind: "ready" });
  });

  it("keeps merge requests from additional upstreams visible", () => {
    const mergeRequests = [
      {
        iid: 7,
        title: "Company",
        web_url: "https://gitlab.example/group/project/-/merge_requests/7",
      },
      {
        iid: 8,
        title: "Mirror",
        web_url: "https://gitlab.other.example/group/project/-/merge_requests/8",
      },
      { iid: 9, title: "Malformed without URL" },
    ];

    expect(relatedGitlabMergeRequests(mergeRequests, mergeRequests[0].web_url)).toEqual([
      mergeRequests[1],
    ]);
  });
});
