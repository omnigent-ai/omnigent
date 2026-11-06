import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { PULL_REQUEST_COMMITS_LIMIT, type PullRequestCommit } from "@/hooks/usePullRequests";
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

/** The list with its query held by the caller, as the Changes tab holds it. */
function ControlledList(props: Partial<React.ComponentProps<typeof PullRequestCommitsList>>) {
  const [query, setQuery] = useState("");
  return (
    <PullRequestCommitsList
      commits={COMMITS}
      hasMore={false}
      loading={false}
      unavailable={false}
      errorMessage={null}
      onSelect={vi.fn()}
      {...props}
      query={query}
      onQueryChange={setQuery}
    />
  );
}

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
    render(<ControlledList onSelect={onSelect} />);
    expect(screen.getByText("2 commits")).toBeInTheDocument();

    const search = screen.getByRole("searchbox", { name: "Search commits" });
    fireEvent.change(search, { target: { value: "nothing-matches" } });
    expect(screen.getByText('No commits match "nothing-matches"')).toBeInTheDocument();
    fireEvent.keyDown(search, { key: "Escape" });
    expect(search).toHaveValue("");

    fireEvent.click(screen.getByRole("button", { name: /Fix the typo/ }));
    expect(onSelect).toHaveBeenCalledWith(COMMITS[1]!.sha);
  });

  it("keeps the query in the caller, so it survives the list being rendered again", () => {
    const onQueryChange = vi.fn();
    const { rerender } = render(
      <PullRequestCommitsList
        commits={COMMITS}
        hasMore={false}
        loading={false}
        unavailable={false}
        errorMessage={null}
        query="parser"
        onQueryChange={onQueryChange}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByRole("searchbox", { name: "Search commits" })).toHaveValue("parser");

    fireEvent.change(screen.getByRole("searchbox", { name: "Search commits" }), {
      target: { value: "fix" },
    });
    expect(onQueryChange).toHaveBeenCalledWith("fix");
    rerender(
      <PullRequestCommitsList
        commits={COMMITS}
        hasMore={false}
        loading={false}
        unavailable={false}
        errorMessage={null}
        query="fix"
        onQueryChange={onQueryChange}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /Fix the typo/ })).toBeInTheDocument();
  });

  it("keeps Escape inside the search while it has a query, and passes it on when empty", () => {
    const enclosingKeyDown = vi.fn();
    render(
      <div onKeyDown={enclosingKeyDown}>
        <ControlledList />
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
    render(<ControlledList hasMore />);

    expect(
      screen.getByText(`History limited to ${PULL_REQUEST_COMMITS_LIMIT} commits`),
    ).toBeInTheDocument();
  });

  it("shows loading, error and empty states in place of the list", () => {
    const { rerender } = render(<ControlledList commits={[]} loading />);
    expect(screen.getByText("Loading commits…")).toBeInTheDocument();

    rerender(
      <ControlledList
        commits={[]}
        errorMessage="GitHub could not load the PR's commits. Refresh to retry."
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("GitHub could not load");

    rerender(<ControlledList commits={[]} />);
    expect(screen.getByText("No commits on this pull request.")).toBeInTheDocument();
  });

  it("keeps the loaded commits and notes a failed refresh above them", () => {
    render(<ControlledList errorMessage="GitHub could not load the PR's commits." />);

    expect(screen.getByRole("button", { name: /Fix the typo/ })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("GitHub could not load");
  });

  it("says the history is unavailable, rather than empty, when it cannot be read here", () => {
    render(<ControlledList commits={[]} unavailable />);

    expect(screen.getByText("Commits are unavailable.")).toBeInTheDocument();
    expect(screen.queryByText("No commits on this pull request.")).toBeNull();
  });
});
