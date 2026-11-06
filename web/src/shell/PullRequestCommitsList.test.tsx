import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PullRequestCommit } from "@/hooks/usePullRequests";
import { filterCommits, PullRequestCommitsList } from "./PullRequestCommitsList";

const commit = (sha: string, title: string, author: string | null): PullRequestCommit => ({
  object: "session.github.commit",
  sha,
  title,
  author,
  date: "2026-11-01T10:00:00Z",
});

const COMMITS = [
  commit("1a2b3c4d".padEnd(40, "0"), "Add the parser", "Ada"),
  commit("9f8e7d6c".padEnd(40, "0"), "Fix the typo", "Grace"),
];

describe("filterCommits", () => {
  it("matches a case-insensitive substring of the sha, title or author", () => {
    expect(filterCommits(COMMITS, "PARSER").map((item) => item.title)).toEqual(["Add the parser"]);
    expect(filterCommits(COMMITS, "grace").map((item) => item.title)).toEqual(["Fix the typo"]);
    expect(filterCommits(COMMITS, "9f8e7d").map((item) => item.title)).toEqual(["Fix the typo"]);
  });

  it("keeps every commit for a blank query, in list order", () => {
    expect(filterCommits(COMMITS, "  ").map((item) => item.title)).toEqual([
      "Add the parser",
      "Fix the typo",
    ]);
  });
});

describe("PullRequestCommitsList", () => {
  it("searches, selects a commit by sha, and clears the search with Escape", () => {
    const onSelect = vi.fn();
    render(
      <PullRequestCommitsList
        commits={COMMITS}
        hasMore={false}
        loading={false}
        errorMessage={null}
        onSelect={onSelect}
      />,
    );
    expect(screen.getByText("2 commits")).toBeInTheDocument();

    const search = screen.getByRole("searchbox", { name: "Search commits" });
    fireEvent.change(search, { target: { value: "nothing-matches" } });
    expect(screen.getByText('No commits match "nothing-matches"')).toBeInTheDocument();
    fireEvent.keyDown(search, { key: "Escape" });
    expect(search).toHaveValue("");

    fireEvent.click(screen.getByRole("button", { name: /Fix the typo/ }));
    expect(onSelect).toHaveBeenCalledWith(COMMITS[1]!.sha);
  });

  it("keeps Escape inside the search while it has a query, and passes it on when empty", () => {
    const enclosingKeyDown = vi.fn();
    render(
      <div onKeyDown={enclosingKeyDown}>
        <PullRequestCommitsList
          commits={COMMITS}
          hasMore={false}
          loading={false}
          errorMessage={null}
          onSelect={vi.fn()}
        />
      </div>,
    );
    const search = screen.getByRole("searchbox", { name: "Search commits" });

    fireEvent.change(search, { target: { value: "parser" } });
    fireEvent.keyDown(search, { key: "Escape" });
    expect(enclosingKeyDown).not.toHaveBeenCalled();

    fireEvent.keyDown(search, { key: "Escape" });
    expect(enclosingKeyDown).toHaveBeenCalledTimes(1);
  });

  it("says when GitHub's history is truncated", () => {
    render(
      <PullRequestCommitsList
        commits={COMMITS}
        hasMore
        loading={false}
        errorMessage={null}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("History limited to 250 commits")).toBeInTheDocument();
  });

  it("shows loading, error and empty states in place of the list", () => {
    const { rerender } = render(
      <PullRequestCommitsList
        commits={[]}
        hasMore={false}
        loading
        errorMessage={null}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("Loading commits…")).toBeInTheDocument();

    rerender(
      <PullRequestCommitsList
        commits={[]}
        hasMore={false}
        loading={false}
        errorMessage="GitHub could not load the PR's commits. Refresh to retry."
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("GitHub could not load");

    rerender(
      <PullRequestCommitsList
        commits={[]}
        hasMore={false}
        loading={false}
        errorMessage={null}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("No commits on this pull request.")).toBeInTheDocument();
  });
});
