import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { BranchCommitsView } from "@/lib/branchCommits";

import { BranchCommitList } from "./ChangesReviewCommits";

const NEWEST = "a".repeat(40);
const OLDER = "b".repeat(40);

function listedCommits(overrides: Partial<Extract<BranchCommitsView, { kind: "listed" }>> = {}) {
  return {
    kind: "listed" as const,
    commits: [
      {
        oid: NEWEST,
        subject: "Add the parser",
        authorName: "Ada",
        authoredAtMs: Date.now() - 60_000,
        root: false,
      },
      { oid: OLDER, subject: "Start the lexer", authorName: "Bo", authoredAtMs: null, root: true },
    ],
    truncated: false,
    baseDisplay: "origin/main",
    ...overrides,
  };
}

function list(view: BranchCommitsView, shown: string | null = null) {
  const onChoose = vi.fn();
  const retry = vi.fn();
  const element = (next: BranchCommitsView) => (
    <BranchCommitList view={next} retry={retry} shown={shown} onChoose={onChoose} />
  );
  const { rerender } = render(element(view));
  return { onChoose, retry, rerender: (next: BranchCommitsView) => rerender(element(next)) };
}

const section = () => screen.getByTestId("changes-review-commits");
const filter = () => screen.getByRole("combobox", { name: "Filter commits" });

describe("the Commits tab's list", () => {
  it("lists the branch's commits, newest first, and chooses one with a click", async () => {
    const { onChoose } = list(listedCommits());
    const rows = screen.getAllByRole("option");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("Add the parser"),
      expect.stringContaining("Start the lexer"),
    ]);
    expect(rows[0]!.textContent).toContain("aaaaaaa");
    expect(rows[0]!.textContent).toContain("Ada");
    await userEvent.click(screen.getByRole("option", { name: /Start the lexer/ }));
    expect(onChoose).toHaveBeenCalledWith(expect.objectContaining({ oid: OLDER, root: true }));
  });

  it("marks the commit on screen", () => {
    list(listedCommits(), NEWEST);
    expect(screen.getByRole("option", { name: /Add the parser/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(screen.getByRole("option", { name: /Start the lexer/ })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("filters by subject, sha or author, and chooses with Enter", async () => {
    const { onChoose } = list(listedCommits());
    await userEvent.click(filter());
    await userEvent.type(filter(), "bo");
    expect(screen.getAllByRole("option").map((row) => row.textContent)).toEqual([
      expect.stringContaining("Start the lexer"),
    ]);
    await userEvent.keyboard("{Enter}");
    expect(onChoose).toHaveBeenCalledWith(expect.objectContaining({ oid: OLDER }));
    await userEvent.clear(filter());
    await userEvent.type(filter(), "nothing like it");
    expect(screen.getByText("No commits match.")).toBeTruthy();
  });

  it("says when the branch has no commits of its own, or only the newest are shown", () => {
    const { rerender } = list(listedCommits({ commits: [] }));
    expect(
      screen.getByText("This branch has no commits of its own since it left origin/main."),
    ).toBeTruthy();
    rerender(listedCommits({ truncated: true }));
    expect(
      screen.getByText("Showing the newest 200 commits since this branch left origin/main."),
    ).toBeTruthy();
  });

  it("says why commits can't be listed, with Try Again only when it may help", async () => {
    const { retry, rerender } = list({
      kind: "unavailable",
      message: "Couldn't list this branch's commits.",
      canRetry: true,
    });
    await userEvent.click(screen.getByRole("button", { name: "Try Again" }));
    expect(retry).toHaveBeenCalledTimes(1);
    rerender({
      kind: "unavailable",
      message: "Update Omnigent on this session's machine to list this branch's commits.",
      canRetry: false,
    });
    expect(screen.queryByRole("button", { name: "Try Again" })).toBeNull();
  });

  it("hands focus from the list to the filter once commits arrive", () => {
    const { rerender } = list({ kind: "loading" });
    expect(screen.getByRole("status").textContent).toBe("Listing this branch's commits…");
    section().focus();
    rerender(listedCommits());
    expect(filter()).toHaveFocus();
  });

  it("never pulls focus to the filter when the reader moved away before they arrived", () => {
    const outside = document.createElement("button");
    document.body.append(outside);
    const { rerender } = list({ kind: "loading" });
    section().focus();
    outside.focus();
    rerender(listedCommits());
    expect(outside).toHaveFocus();
    outside.remove();
  });
});
