// Who the comment form says a post goes out as, by the PR's provider.

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PullRequestCommentForm } from "./PullRequestCommentComposer";

describe("who a comment posts as, without a named account", () => {
  const draft = { body: "hello", pending: false, error: null };

  it.each([
    ["GitHub's", undefined, "Comment using the host’s GitHub account"],
    [
      "Azure DevOps'",
      { id: "azure_devops", label: "Azure DevOps", requestName: "pull request" },
      "Comment using the host’s Azure DevOps account",
    ],
    [
      "an unknown provider's",
      { id: "", label: "Pull Requests", requestName: "pull request" },
      "Comment using the host’s account",
    ],
  ])("names %s account", (_name, provider, expected) => {
    render(
      <PullRequestCommentForm
        draft={draft}
        setBody={vi.fn()}
        post={vi.fn()}
        account={null}
        provider={provider}
      />,
    );
    expect(screen.getAllByText(expected, { exact: false }).length).toBeGreaterThan(0);
    if (provider) expect(screen.queryByText(/GitHub account/)).toBeNull();
  });
});
