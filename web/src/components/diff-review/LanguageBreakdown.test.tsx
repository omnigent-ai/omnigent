import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { diffReviewCommands, NO_LISTED_FILES } from "./diffReviewCommands";
import {
  DEFAULT_DIFF_REVIEW_FILTERS,
  type DiffFilterAction,
  type DiffReviewFilters,
  type LineBreakdown,
} from "./diffReviewModel";
import { LanguageBreakdown } from "./LanguageBreakdown";

const BREAKDOWN: LineBreakdown = {
  totals: { files: 6, additions: 1300, deletions: 400 },
  notViewed: { files: 6, additions: 1300, deletions: 400 },
  languages: [
    { language: "TypeScript", files: 3, additions: 1000, deletions: 200 },
    { language: "Python", files: 1, additions: 300, deletions: 200 },
    { language: "Plain Text", files: 2, additions: 0, deletions: 0 },
  ],
};

function command(listedFileCount: number) {
  return diffReviewCommands({
    treeShown: false,
    toggleTree: () => {},
    searchFiles: () => {},
    filtersActive: false,
    openFilters: () => {},
    listedFileCount,
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
  }).find((candidate) => candidate.id === "show-language-breakdown");
}

function Harness({
  breakdown = BREAKDOWN,
  filters = DEFAULT_DIFF_REVIEW_FILTERS,
  dispatchFilter,
  listedFileCount = 6,
  initiallyOpen = false,
}: {
  breakdown?: LineBreakdown;
  filters?: DiffReviewFilters;
  dispatchFilter: (action: DiffFilterAction) => void;
  listedFileCount?: number;
  initiallyOpen?: boolean;
}) {
  const [open, setOpen] = useState(initiallyOpen);
  return (
    <LanguageBreakdown
      command={command(listedFileCount)}
      breakdown={breakdown}
      filters={filters}
      dispatchFilter={dispatchFilter}
      open={open}
      onOpenChange={setOpen}
    />
  );
}

const trigger = () => screen.getByTestId("diff-review-line-counts");
const popover = () => screen.getByRole("dialog", { name: "Changes by language" });
const row = (language: string) =>
  within(popover()).getByRole("button", { name: new RegExp(`^${language}`) });

describe("LanguageBreakdown", () => {
  it("is the toolbar's line counts, named with what it opens", () => {
    render(<Harness dispatchFilter={vi.fn()} />);
    expect(trigger()).toHaveTextContent("+1,300−400");
    expect(trigger()).toHaveAccessibleName("1,300 added, 400 removed. Show changes by language");
  });

  it("counts only files not yet viewed on the toolbar, and both totals in the popover", async () => {
    const user = userEvent.setup();
    render(
      <Harness
        dispatchFilter={vi.fn()}
        breakdown={{ ...BREAKDOWN, notViewed: { files: 2, additions: 300, deletions: 200 } }}
      />,
    );
    expect(trigger()).toHaveTextContent("+300−200");
    expect(trigger()).toHaveAccessibleName(
      "300 added, 200 removed in files not viewed. Show changes by language",
    );
    await user.click(trigger());
    const totals = within(popover())
      .getAllByRole("term")
      .map((term) => `${term.textContent}: ${term.nextElementSibling?.textContent}`);
    expect(totals).toEqual(["Not viewed: 2 files+300−200", "All files: 6 files+1,300−400"]);
    // The languages keep counting every file, viewed or not.
    expect(row("TypeScript")).toHaveTextContent("TypeScript3 files+1,000−200");
  });

  it("lists each language's files, lines and share, most changed first, with one total", async () => {
    const user = userEvent.setup();
    render(<Harness dispatchFilter={vi.fn()} />);
    await user.click(trigger());
    // With no file viewed there's one total, not a Not viewed and an All files pair.
    expect(within(popover()).queryByRole("term")).toBeNull();
    expect(within(popover()).queryByText("Not viewed")).toBeNull();
    expect(within(popover()).getAllByText("6 files")).toHaveLength(1);
    const rows = within(screen.getByRole("group", { name: "Languages" })).getAllByRole("button");
    expect(rows.map((entry) => entry.dataset.language)).toEqual([
      "TypeScript",
      "Python",
      "Plain Text",
    ]);
    expect(rows[0]).toHaveTextContent("TypeScript3 files+1,000−200");
    // Bars: TypeScript has 1,200 of 1,700 changed lines; Plain Text none, an empty track.
    const share = (entry: HTMLElement) =>
      (entry.querySelector("[aria-hidden='true'] > span") as HTMLElement).style.width;
    expect(share(rows[0]!)).toBe(`${(1200 / 1700) * 100}%`);
    expect(share(rows[2]!)).toBe("0%");
    expect(rows[2]).not.toHaveAttribute("aria-disabled");
  });

  it("shows only a language when its row is chosen, and closes back to the trigger", async () => {
    const user = userEvent.setup();
    const dispatchFilter = vi.fn();
    render(<Harness dispatchFilter={dispatchFilter} />);
    await user.click(trigger());
    expect(row("Python")).toHaveAccessibleDescription("Show only Python");
    await user.click(row("Python"));
    expect(dispatchFilter).toHaveBeenCalledExactlyOnceWith({
      type: "showOnlyLanguage",
      language: "Python",
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("runs one action per Enter or Space, and Escape returns focus to the trigger", async () => {
    const user = userEvent.setup();
    const dispatchFilter = vi.fn();
    render(<Harness dispatchFilter={dispatchFilter} />);
    trigger().focus();
    await user.keyboard("{Enter}");
    row("TypeScript").focus();
    await user.keyboard(" ");
    expect(dispatchFilter).toHaveBeenCalledTimes(1);
    await user.keyboard("{Enter}");
    row("Python").focus();
    await user.keyboard("{Enter}");
    expect(dispatchFilter).toHaveBeenCalledTimes(2);
    await user.keyboard("{Enter}");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger());
    expect(dispatchFilter).toHaveBeenCalledTimes(2);
  });

  it("presses the language chosen alone, which shows every language again", async () => {
    const user = userEvent.setup();
    const dispatchFilter = vi.fn();
    render(
      <Harness
        dispatchFilter={dispatchFilter}
        filters={{ ...DEFAULT_DIFF_REVIEW_FILTERS, selectedLanguages: ["TypeScript"] }}
        breakdown={{ ...BREAKDOWN, languages: [BREAKDOWN.languages[0]!] }}
      />,
    );
    await user.click(trigger());
    expect(row("TypeScript")).toHaveAttribute("aria-pressed", "true");
    expect(row("TypeScript")).toHaveAccessibleDescription("Show all languages");
    await user.click(row("TypeScript"));
    expect(dispatchFilter).toHaveBeenCalledExactlyOnceWith({ type: "showAllLanguages" });
  });

  it("offers Show all languages only while a language filter is on", async () => {
    const user = userEvent.setup();
    const dispatchFilter = vi.fn();
    const { unmount } = render(<Harness dispatchFilter={dispatchFilter} />);
    await user.click(trigger());
    expect(within(popover()).queryByRole("button", { name: "Show all languages" })).toBeNull();
    unmount();
    render(
      <Harness
        dispatchFilter={dispatchFilter}
        filters={{ ...DEFAULT_DIFF_REVIEW_FILTERS, selectedLanguages: ["TypeScript", "Python"] }}
      />,
    );
    await user.click(trigger());
    // Several languages chosen: none is the one shown alone.
    expect(row("TypeScript")).toHaveAttribute("aria-pressed", "false");
    await user.click(within(popover()).getByRole("button", { name: "Show all languages" }));
    expect(dispatchFilter).toHaveBeenCalledExactlyOnceWith({ type: "showAllLanguages" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps a long list in a scrolling area whose last row the keyboard reaches", async () => {
    const user = userEvent.setup();
    const languages = Array.from({ length: 40 }, (_, index) => ({
      language: `Language ${String(index).padStart(2, "0")}`,
      files: 1,
      additions: 40 - index,
      deletions: 0,
    }));
    render(
      <Harness
        dispatchFilter={vi.fn()}
        breakdown={{
          totals: { files: 40, additions: 820, deletions: 0 },
          notViewed: { files: 40, additions: 820, deletions: 0 },
          languages,
        }}
      />,
    );
    await user.click(trigger());
    const list = screen.getByRole("group", { name: "Languages" });
    expect(list).toHaveClass("overflow-y-auto");
    expect(list.className).toMatch(/max-h-/);
    await user.keyboard("{Tab}".repeat(39));
    expect(document.activeElement).toBe(row("Language 39"));
  });

  it("can't open with no listed files, and says why", async () => {
    const user = userEvent.setup();
    render(
      <Harness
        dispatchFilter={vi.fn()}
        listedFileCount={0}
        breakdown={{
          totals: { files: 0, additions: 0, deletions: 0 },
          notViewed: { files: 0, additions: 0, deletions: 0 },
          languages: [],
        }}
      />,
    );
    expect(trigger()).toBeDisabled();
    expect(trigger()).toHaveAttribute("title", NO_LISTED_FILES);
    await user.click(trigger());
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
