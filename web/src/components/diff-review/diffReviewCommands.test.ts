import { describe, expect, it, vi } from "vitest";
import {
  diffReviewCommands,
  diffReviewPaletteCommands,
  type DiffReviewCommandState,
} from "./diffReviewCommands";
import { applySettingChoice } from "@/shell/settingChoiceFlow";
import { currentChoiceLabel } from "@/store/paletteSettings";

function state(overrides: Partial<DiffReviewCommandState> = {}): DiffReviewCommandState {
  return {
    treeShown: true,
    toggleTree: vi.fn(),
    searchFiles: vi.fn(),
    filtersActive: false,
    openFilters: vi.fn(),
    listedFileCount: 2,
    openLanguageBreakdown: vi.fn(),
    testFileView: "all",
    setTestFileView: vi.fn(),
    layout: "unified",
    setLayout: vi.fn(),
    ignoreWhitespace: false,
    setIgnoreWhitespace: vi.fn(),
    wrapLines: false,
    setWrapLines: vi.fn(),
    fileOrder: "path",
    setFileOrder: vi.fn(),
    allCollapsed: false,
    canCollapse: true,
    toggleAllFiles: vi.fn(),
    ...overrides,
  };
}

const byId = (commands: ReturnType<typeof diffReviewCommands>, id: string) =>
  commands.find((command) => command.id === id)!;

describe("diffReviewCommands", () => {
  it("lists every view command in toolbar order", () => {
    expect(diffReviewCommands(state()).map((command) => command.id)).toEqual([
      "toggle-tree",
      "search-files",
      "filter-files",
      "show-language-breakdown",
      "cycle-test-files",
      "toggle-layout",
      "toggle-whitespace",
      "toggle-wrap",
      "sort-files",
      "toggle-collapse-all",
    ]);
  });

  it("sorts files by size or path through one command, a ⌘K setting with both orders", () => {
    const byPath = state();
    const sort = byId(diffReviewCommands(byPath), "sort-files");
    expect(sort).toMatchObject({ label: "Sort files by size", pressed: false });
    expect(sort.setting).toMatchObject({
      label: "Sort changed files",
      current: "path",
      choices: [
        { value: "path", label: "By path" },
        { value: "size", label: "By size, largest first" },
      ],
    });
    sort.run();
    expect(byPath.setFileOrder).toHaveBeenCalledWith("size");
    sort.setting!.choose("path");
    expect(byPath.setFileOrder).toHaveBeenLastCalledWith("path");
    sort.setting!.choose("newest");
    expect(byPath.setFileOrder).toHaveBeenCalledTimes(2);

    const bySize = state({ fileOrder: "size" });
    const sorted = byId(diffReviewCommands(bySize), "sort-files");
    expect(sorted).toMatchObject({ label: "Sort files by path", pressed: true });
    sorted.run();
    expect(bySize.setFileOrder).toHaveBeenCalledWith("path");
  });

  it("withholds the commands whose setting the source doesn't offer", () => {
    const ids = diffReviewCommands(
      state({ setLayout: undefined, setIgnoreWhitespace: undefined, setWrapLines: undefined }),
    ).map((command) => command.id);
    expect(ids).not.toContain("toggle-layout");
    expect(ids).not.toContain("toggle-whitespace");
    expect(ids).not.toContain("toggle-wrap");
  });

  it("names each toggle by what running it does, and reports whether it's on", () => {
    const off = diffReviewCommands(state());
    expect(byId(off, "toggle-layout")).toMatchObject({
      label: "Switch to split view",
      pressed: false,
    });
    expect(byId(off, "toggle-whitespace")).toMatchObject({
      label: "Hide whitespace changes",
      pressed: false,
    });
    expect(byId(off, "toggle-wrap")).toMatchObject({ label: "Wrap lines", pressed: false });
    expect(byId(off, "toggle-tree")).toMatchObject({ label: "Hide changed files", pressed: true });

    const on = diffReviewCommands(
      state({ layout: "split", ignoreWhitespace: true, wrapLines: true, treeShown: false }),
    );
    expect(byId(on, "toggle-layout")).toMatchObject({
      label: "Switch to unified view",
      pressed: true,
    });
    expect(byId(on, "toggle-whitespace")).toMatchObject({
      label: "Show whitespace changes",
      pressed: true,
    });
    expect(byId(on, "toggle-wrap")).toMatchObject({ label: "Don’t wrap lines", pressed: true });
    expect(byId(on, "toggle-tree")).toMatchObject({ label: "Show changed files", pressed: false });
  });

  it("flips each setting to the other value", () => {
    const current = state({ layout: "split", ignoreWhitespace: true, wrapLines: false });
    const commands = diffReviewCommands(current);
    byId(commands, "toggle-layout").run();
    byId(commands, "toggle-whitespace").run();
    byId(commands, "toggle-wrap").run();
    expect(current.setLayout).toHaveBeenCalledWith("unified");
    expect(current.setIgnoreWhitespace).toHaveBeenCalledWith(false);
    expect(current.setWrapLines).toHaveBeenCalledWith(true);
  });

  it("cycles the test-file view and names the view it moves to", () => {
    const current = state({ testFileView: "skipTests" });
    const cycle = byId(diffReviewCommands(current), "cycle-test-files");
    expect(cycle).toMatchObject({ label: "Show only test files", pressed: true });
    cycle.run();
    expect(current.setTestFileView).toHaveBeenCalledWith("onlyTests");
  });

  it("offers expand all once every file is collapsed, and explains why it can't run with nothing to collapse", () => {
    expect(
      byId(diffReviewCommands(state({ allCollapsed: true })), "toggle-collapse-all").label,
    ).toBe("Expand all files");
    const none = diffReviewCommands(state({ canCollapse: false }));
    expect(byId(none, "toggle-collapse-all").unavailableReason).toBe(
      "No listed file has a diff to collapse.",
    );
    expect(diffReviewPaletteCommands("a", none).at(-1)!.availability).toEqual({
      available: false,
      reason: "No listed file has a diff to collapse.",
    });
    expect(
      diffReviewPaletteCommands("a", diffReviewCommands(state())).at(-1)!.availability,
    ).toEqual({ available: true });
  });
});

describe("diffReviewPaletteCommands", () => {
  it("gives each review its own ids and keeps labels, state, and handlers", () => {
    const current = state();
    const commands = diffReviewCommands(current);
    const first = diffReviewPaletteCommands("a", commands);
    const second = diffReviewPaletteCommands("b", commands);
    expect(first.map((command) => command.id)).not.toEqual(second.map((command) => command.id));
    const search = first.find((command) => command.label === "Search changed files")!;
    expect(search.movesFocus).toBe(true);
    expect(search.keywords).toContain("diff");
    search.run();
    expect(current.searchFiles).toHaveBeenCalled();
  });
});

describe("the opt-in filter commands", () => {
  const languages = (counts: { language: string; count: number }[], showOnly = vi.fn()) => ({
    counts,
    selected: null,
    showOnly,
  });

  it("are absent unless the review supplies their state", () => {
    const ids = diffReviewCommands(state()).map((command) => command.id);
    expect(ids).not.toContain("toggle-generated-files");
    expect(ids).not.toContain("filter-languages");
  });

  it("offers files you own only while the reader owns some, and toggles showing only them", () => {
    expect(diffReviewCommands(state()).map((command) => command.id)).not.toContain(
      "toggle-owned-files",
    );
    const setOnly = vi.fn();
    const all = byId(
      diffReviewCommands(state({ ownedFiles: { only: false, count: 3, setOnly } })),
      "toggle-owned-files",
    );
    expect(all).toMatchObject({ label: "Show only files you own", pressed: false });
    expect(all.setting?.choices.map((choice) => choice.label)).toEqual([
      "Show all files",
      "Show only files you own (3)",
    ]);
    all.run();
    expect(setOnly).toHaveBeenLastCalledWith(true);
    const only = byId(
      diffReviewCommands(state({ ownedFiles: { only: true, count: 3, setOnly } })),
      "toggle-owned-files",
    );
    expect(only).toMatchObject({ label: "Show all changed files", pressed: true });
    only.setting?.choose("all");
    expect(setOnly).toHaveBeenLastCalledWith(false);
  });

  it("offers Generated files, unavailable while no file is generated", () => {
    const setShown = vi.fn();
    const none = byId(
      diffReviewCommands(state({ generatedFiles: { shown: true, count: 0, setShown } })),
      "toggle-generated-files",
    );
    expect(none.unavailableReason).toBe("None of the changed files are generated.");
    const some = byId(
      diffReviewCommands(state({ generatedFiles: { shown: true, count: 2, setShown } })),
      "toggle-generated-files",
    );
    expect(some.unavailableReason).toBeUndefined();
    some.setting?.choose("hidden");
    expect(setShown).toHaveBeenCalledWith(false);
  });

  it("offers Languages only with more than one, and shows one or all", () => {
    expect(
      diffReviewCommands(
        state({ languages: languages([{ language: "TypeScript", count: 2 }]) }),
      ).map((command) => command.id),
    ).not.toContain("filter-languages");
    const showOnly = vi.fn();
    const command = byId(
      diffReviewCommands(
        state({
          languages: languages(
            [
              { language: "Python", count: 1 },
              { language: "TypeScript", count: 2 },
            ],
            showOnly,
          ),
        }),
      ),
      "filter-languages",
    );
    expect(command.setting?.choices.map((choice) => choice.label)).toEqual([
      "All languages",
      "Python (1)",
      "TypeScript (2)",
    ]);
    command.setting?.choose("Python");
    expect(showOnly).toHaveBeenLastCalledWith("Python");
    command.setting?.choose("all");
    expect(showOnly).toHaveBeenLastCalledWith(null);
  });
});

describe("the Languages row through the palette's choice flow", () => {
  const COUNTS = [
    { language: "Python", count: 1 },
    { language: "TypeScript", count: 1 },
    { language: "JSON", count: 1 },
  ];
  const page = () => ({
    isCurrent: () => true,
    isBound: () => true,
    setWaiting: vi.fn(),
    setStatus: vi.fn(),
    leave: vi.fn(),
    gone: vi.fn(),
  });
  function languagesRow(selected: readonly string[] | null, showOnly = vi.fn()) {
    const commands = diffReviewCommands(
      state({ languages: { counts: COUNTS, selected, showOnly } }),
    );
    const row = diffReviewPaletteCommands("test", commands).find(
      (command) => command.label === "Languages",
    );
    return { setting: row!.setting!, showOnly };
  }

  it.each([
    { selected: [] as string[], shown: "No languages" },
    { selected: ["Python", "TypeScript"], shown: "2 of 3 languages" },
  ])("names $shown truthfully, and All languages shows them all again", ({ selected, shown }) => {
    const { setting, showOnly } = languagesRow(selected);
    expect(currentChoiceLabel(setting)).toBe(shown);
    applySettingChoice(1, setting, "all", page());
    expect(showOnly.mock.calls).toEqual([[null]]);
  });

  it("names one shown language, and leaves an unchanged choice alone", () => {
    const { setting, showOnly } = languagesRow(["JSON"]);
    expect(currentChoiceLabel(setting)).toBe("JSON (1)");
    applySettingChoice(1, setting, "JSON", page());
    expect(showOnly).not.toHaveBeenCalled();
    const all = languagesRow(null);
    expect(currentChoiceLabel(all.setting)).toBe("All languages");
    expect(all.setting.choices.map((choice) => choice.label)).not.toContain("No languages");
  });
});
