// The comment form's notice for a draft that was sent on this Mac before the workspace
// user changed: it keeps the text, links to the pull request, and can be dismissed.

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PullRequestCommentDraft } from "@/newPullRequestPage/hooks/usePullRequests";
import { PullRequestCommentForm } from "./PullRequestCommentComposer";

const PR = "https://github.com/acme/app/pull/42";

function renderForm(draft: PullRequestCommentDraft, dismissNotice?: () => void) {
  const post = vi.fn();
  render(
    <PullRequestCommentForm
      draft={draft}
      setBody={vi.fn()}
      post={post}
      dismissNotice={dismissNotice}
      account="@mac-octo"
    />,
  );
  return { post };
}

describe("a draft that may have been sent before a switch of accounts", () => {
  it("keeps the text editable and says to check GitHub, with a link and Dismiss", () => {
    const dismiss = vi.fn();
    renderForm({ body: "hello", pending: false, error: null, abandoned: { url: PR } }, dismiss);
    expect(screen.getByRole("textbox", { name: "New PR comment" })).toHaveValue("hello");
    expect(screen.getByRole("textbox", { name: "New PR comment" })).toBeEnabled();
    expect(screen.getByRole("status")).toHaveTextContent(
      "This may have been sent before you switched accounts. Check the pull request on GitHub before sending it again.",
    );
    expect(screen.getByRole("link", { name: "Open on GitHub" })).toHaveAttribute("href", PR);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(dismiss).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Post comment" })).toBeEnabled();
  });

  it("shows no notice for an ordinary draft", () => {
    renderForm({ body: "hello", pending: false, error: null });
    expect(screen.queryByRole("link", { name: "Open on GitHub" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
  });
});

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
