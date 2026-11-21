// The review toolbar's status slot: a source's progress sits just left of the line counts,
// inside the group that anchors them right, so they and every control after them keep their
// place as it comes and goes.

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";

import { diffReviewCommands } from "./diffReviewCommands";
import { DEFAULT_DIFF_REVIEW_FILTERS, type LineBreakdown } from "./diffReviewModel";
import { DiffReviewToolbar, DiffReviewToolbarProgress } from "./DiffReviewToolbar";

/** One TypeScript file's worth of line counts. */
function breakdownOf(additions: number, deletions: number): LineBreakdown {
  return {
    totals: { files: 1, additions, deletions },
    notViewed: { files: 1, additions, deletions },
    languages: [{ language: "TypeScript", files: 1, additions, deletions }],
  };
}

function toolbar(active: boolean) {
  return (
    <TooltipProvider>
      <DiffReviewToolbar
        commands={[]}
        fileCount={2}
        breakdown={breakdownOf(126, 125)}
        languagesOpen={false}
        setLanguagesOpen={() => {}}
        filters={DEFAULT_DIFF_REVIEW_FILTERS}
        dispatchFilter={() => {}}
        languages={[]}
        testCount={0}
        generatedCount={0}
        filtersOpen={false}
        setFiltersOpen={() => {}}
        status={<DiffReviewToolbarProgress label="Loading review threads" active={active} />}
        extra={<button type="button">Find in changes</button>}
      />
    </TooltipProvider>
  );
}

describe("DiffReviewToolbar's status", () => {
  it("shows a spinner named by its label just before the line counts, in their group", () => {
    render(toolbar(true));
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Loading review threads");
    const lineCounts = screen.getByTestId("diff-review-line-counts");
    expect(lineCounts).toHaveTextContent("+126−125");
    expect(status.nextElementSibling).toBe(lineCounts);
    // The group the counts anchor to the right, so what the status adds grows leftward.
    expect(status.parentElement).toHaveClass("ml-auto");
  });

  it("keeps an empty live region of the spinner's size while idle, so nothing rewraps", () => {
    const { rerender } = render(toolbar(false));
    const status = screen.getByRole("status");
    expect(status).toBeEmptyDOMElement();
    // The same box idle and busy: a wrapping toolbar breaks its rows at the same place.
    expect(status).toHaveClass("size-3.5", "shrink-0");
    rerender(toolbar(true));
    expect(screen.getByRole("status")).toBe(status);
    expect(status).toHaveTextContent("Loading review threads");
  });
});

describe("DiffReviewToolbar's owned files filter", () => {
  /** The toolbar with the shared filter and owned-files commands, the latter `only` or not. */
  const ownedToolbar = (only: boolean, setOnly: (only: boolean) => void) => {
    const dispatchFilter = vi.fn();
    const commands = diffReviewCommands({
      treeShown: false,
      toggleTree: () => {},
      searchFiles: () => {},
      filtersActive: only,
      openFilters: () => {},
      listedFileCount: 2,
      openLanguageBreakdown: () => {},
      ownedFiles: { only, count: 2, setOnly },
      testFileView: "all",
      setTestFileView: () => {},
      layout: "unified",
      ignoreWhitespace: false,
      wrapLines: false,
      allCollapsed: false,
      canCollapse: false,
      fileOrder: "path",
      setFileOrder: () => {},
      toggleAllFiles: () => {},
    }).filter((command) => command.id === "filter-files" || command.id === "toggle-owned-files");
    render(
      <TooltipProvider>
        <DiffReviewToolbar
          commands={commands}
          fileCount={2}
          breakdown={breakdownOf(1, 0)}
          languagesOpen={false}
          setLanguagesOpen={() => {}}
          filters={DEFAULT_DIFF_REVIEW_FILTERS}
          dispatchFilter={dispatchFilter}
          languages={[]}
          testCount={0}
          generatedCount={0}
          filtersOpen
          setFiltersOpen={() => {}}
        />
      </TooltipProvider>,
    );
    return dispatchFilter;
  };

  it("turns the filter on and off through the shared command, not its own dispatch", async () => {
    const user = userEvent.setup();
    const setOnly = vi.fn();
    const dispatchFilter = ownedToolbar(false, setOnly);
    const item = screen.getByRole("menuitemcheckbox", { name: "Show only files you own (2)" });
    expect(item).toHaveAttribute("aria-checked", "false");
    await user.click(item);
    expect(setOnly).toHaveBeenLastCalledWith(true);

    cleanup();
    const dispatchWhenOn = ownedToolbar(true, setOnly);
    const checked = screen.getByRole("menuitemcheckbox", { name: "Show only files you own (2)" });
    expect(checked).toHaveAttribute("aria-checked", "true");
    await user.click(checked);
    expect(setOnly).toHaveBeenLastCalledWith(false);
    expect(dispatchFilter).not.toHaveBeenCalled();
    expect(dispatchWhenOn).not.toHaveBeenCalled();
  });

  it("offers no owned files row without the command", () => {
    render(toolbar(false));
    expect(screen.queryByRole("menuitemcheckbox", { name: /files you own/ })).toBeNull();
  });
});

describe("DiffReviewToolbar's line counts in a narrow review", () => {
  it("stays a button that opens the breakdown when the view controls fold into a menu", async () => {
    const user = userEvent.setup();
    const setLanguagesOpen = vi.fn();
    const commands = diffReviewCommands({
      treeShown: false,
      toggleTree: () => {},
      searchFiles: () => {},
      filtersActive: false,
      openFilters: () => {},
      listedFileCount: 1,
      openLanguageBreakdown: () => {},
      testFileView: "all",
      setTestFileView: () => {},
      layout: "unified",
      ignoreWhitespace: false,
      wrapLines: false,
      allCollapsed: false,
      canCollapse: false,
      fileOrder: "path",
      setFileOrder: () => {},
      toggleAllFiles: () => {},
      // The tree toggle opens its own popover; this toolbar shows the rest.
    }).filter((command) => command.id !== "toggle-tree");
    render(
      <TooltipProvider>
        <DiffReviewToolbar
          commands={commands}
          fileCount={1}
          breakdown={breakdownOf(12, 3)}
          languagesOpen={false}
          setLanguagesOpen={setLanguagesOpen}
          filters={DEFAULT_DIFF_REVIEW_FILTERS}
          dispatchFilter={() => {}}
          languages={[]}
          testCount={0}
          generatedCount={0}
          filtersOpen={false}
          setFiltersOpen={() => {}}
          compact
        />
      </TooltipProvider>,
    );
    expect(screen.getByRole("button", { name: "More view options" })).toBeInTheDocument();
    await user.click(screen.getByTestId("diff-review-line-counts"));
    expect(setLanguagesOpen).toHaveBeenCalledWith(true);
  });
});
