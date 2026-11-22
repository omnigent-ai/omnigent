import { describe, expect, it } from "vitest";
import {
  applyFilterAction,
  DEFAULT_DIFF_REVIEW_FILTERS,
  effectiveReviewFilters,
  filterReviewPaths,
  hasActiveFilters,
  hasTreeSearch,
  languageCounts,
  lineBreakdown,
  nextTestFileView,
  onlySelectedLanguage,
  ownedFileCount,
  pathFromReviewItemId,
  reviewFile,
  reviewItemId,
  treeOrderedFiles,
  visibleReviewFiles,
} from "./diffReviewModel";

const files = [
  reviewFile("web/src/App.tsx", "modified", { additions: 3, deletions: 1 }),
  reviewFile("web/src/App.test.tsx", "added"),
  reviewFile("omnigent/server/app.py", "modified"),
  reviewFile("web/pnpm-lock.yaml", "modified"),
];

const paths = (shown: { path: string }[]) => shown.map((file) => file.path);

describe("treeOrderedFiles", () => {
  const order = (list: readonly string[]) =>
    paths(treeOrderedFiles(list.map((path) => reviewFile(path, "modified"))));

  it("lists folders before files at each level, the way the tree draws them", () => {
    // GitHub lists a PR's files by path, byte-wise: capitals first, root files among folders.
    expect(
      order([
        "README.md",
        "tests/e2e_ui/test_tree.py",
        "web/src/shell/ProjectPicker.tsx",
        "web/src/shell/Sidebar.tsx",
        "web/src/shell/projectChoices.test.ts",
        "web/src/shell/projectChoices.ts",
        "web/a.ts",
      ]),
    ).toEqual([
      "tests/e2e_ui/test_tree.py",
      "web/src/shell/projectChoices.test.ts",
      "web/src/shell/projectChoices.ts",
      "web/src/shell/ProjectPicker.tsx",
      "web/src/shell/Sidebar.tsx",
      "web/a.ts",
      "README.md",
    ]);
  });

  it("puts dot entries first and ignores case", () => {
    expect(order(["b.ts", "A.ts", ".env", "src/x.ts", ".github/ci.yml"])).toEqual([
      ".github/ci.yml",
      "src/x.ts",
      ".env",
      "A.ts",
      "b.ts",
    ]);
  });

  it("keeps every file once, whatever order it was given in", () => {
    const given = ["z/y/x.ts", "a.ts", "z/b.ts", "m/n.ts"];
    expect(order(given)).toEqual(["m/n.ts", "z/y/x.ts", "z/b.ts", "a.ts"]);
    expect(order([...given].reverse())).toEqual(order(given));
    expect(order([])).toEqual([]);
  });

  it("keeps the files' own fields", () => {
    const owned = { ...reviewFile("b.ts", "added"), ownedByViewer: true };
    expect(treeOrderedFiles([owned, reviewFile("a.ts", "modified")])[1]).toBe(owned);
  });
});

describe("treeOrderedFiles by size", () => {
  const sized = (path: string, additions: number | null, deletions: number | null = 0) =>
    reviewFile(path, "modified", { additions, deletions });
  const bySize = (list: ReturnType<typeof sized>[]) => paths(treeOrderedFiles(list, "size"));

  it("puts the most changed lines first at each level, a folder weighing all its files", () => {
    expect(
      bySize([
        sized("README.md", 3),
        sized("src/app/main.py", 1),
        sized("src/app/session_list.py", 14, 6),
        sized("src/uploads.py", 5, 2),
        sized("docs/guide.md", 30),
      ]),
    ).toEqual([
      // docs/ weighs 30, src/ 28 (its app/ 21 before uploads.py's 7), then the 3-line README.
      "docs/guide.md",
      "src/app/session_list.py",
      "src/app/main.py",
      "src/uploads.py",
      "README.md",
    ]);
  });

  it("keeps the path order between rows of the same size", () => {
    const tied = [sized("c.ts", 2), sized("a.ts", 1, 1), sized("lib/d.ts", 2), sized("b.ts", 2)];
    // All weigh 2, so the tree's own order decides: folders first, then by name.
    expect(bySize(tied)).toEqual(["lib/d.ts", "a.ts", "b.ts", "c.ts"]);
    expect(bySize(tied)).toEqual(paths(treeOrderedFiles(tied, "path")));
  });

  it("puts a file without counts after every counted one, even an empty change", () => {
    expect(
      bySize([sized("image.png", null, null), sized("empty.txt", 0), sized("big.ts", 9)]),
    ).toEqual(["big.ts", "empty.txt", "image.png"]);
    // A folder of only uncounted files goes after counted rows, even an empty change.
    expect(bySize([sized("assets/logo.png", null, null), sized("a.ts", 1)])).toEqual([
      "a.ts",
      "assets/logo.png",
    ]);
    expect(bySize([sized("assets/image.png", null, null), sized("empty.txt", 0)])).toEqual([
      "empty.txt",
      "assets/image.png",
    ]);
    // Once one of its files has counts, the folder weighs what they changed.
    expect(
      bySize([sized("assets/image.png", null, null), sized("assets/a.svg", 2), sized("b.ts", 1)]),
    ).toEqual(["assets/a.svg", "assets/image.png", "b.ts"]);
  });

  it("keeps the path order unless asked for size", () => {
    const list = [sized("z.ts", 50), sized("a/b.ts", 1)];
    expect(paths(treeOrderedFiles(list))).toEqual(["a/b.ts", "z.ts"]);
    expect(paths(treeOrderedFiles(list, "path"))).toEqual(["a/b.ts", "z.ts"]);
  });
});

describe("reviewFile", () => {
  it("classifies language, tests, and generated files from the path", () => {
    expect(files.map((file) => [file.language, file.isTest, file.isGenerated])).toEqual([
      ["TSX", false, false],
      ["TSX", true, false],
      ["Python", false, false],
      ["YAML", false, true],
    ]);
    expect(files[0]).toMatchObject({ additions: 3, deletions: 1, isBinary: false });
    expect(files[1]).toMatchObject({ additions: null, deletions: null });
  });
});

describe("filters", () => {
  it("shows everything by default", () => {
    expect(hasActiveFilters(DEFAULT_DIFF_REVIEW_FILTERS)).toBe(false);
    expect(paths(visibleReviewFiles(files, DEFAULT_DIFF_REVIEW_FILTERS))).toHaveLength(4);
  });

  it("cycles the test-file view and filters by it", () => {
    expect(nextTestFileView("all")).toBe("skipTests");
    expect(nextTestFileView("skipTests")).toBe("onlyTests");
    expect(nextTestFileView("onlyTests")).toBe("all");
    const skip = applyFilterAction(
      DEFAULT_DIFF_REVIEW_FILTERS,
      { type: "setTestFileView", value: "skipTests" },
      files,
    );
    expect(paths(visibleReviewFiles(files, skip))).not.toContain("web/src/App.test.tsx");
    const only = { ...skip, testFileView: "onlyTests" as const };
    expect(paths(visibleReviewFiles(files, only))).toEqual(["web/src/App.test.tsx"]);
  });

  it("hides generated files", () => {
    const filters = applyFilterAction(
      DEFAULT_DIFF_REVIEW_FILTERS,
      { type: "setShowGeneratedFiles", value: false },
      files,
    );
    expect(hasActiveFilters(filters)).toBe(true);
    expect(paths(visibleReviewFiles(files, filters))).not.toContain("web/pnpm-lock.yaml");
  });

  it("toggles languages and clears the filter once every language is back", () => {
    let filters = applyFilterAction(
      DEFAULT_DIFF_REVIEW_FILTERS,
      { type: "toggleLanguage", language: "TSX" },
      files,
    );
    expect(filters.selectedLanguages).toEqual(["Python", "YAML"]);
    expect(paths(visibleReviewFiles(files, filters))).toEqual([
      "omnigent/server/app.py",
      "web/pnpm-lock.yaml",
    ]);
    filters = applyFilterAction(filters, { type: "toggleLanguage", language: "TSX" }, files);
    expect(filters.selectedLanguages).toBeNull();
    filters = applyFilterAction(filters, { type: "hideAllLanguages" }, files);
    expect(visibleReviewFiles(files, filters)).toEqual([]);
    filters = applyFilterAction(filters, { type: "clearFilters" }, files);
    expect(filters).toEqual(DEFAULT_DIFF_REVIEW_FILTERS);
  });

  it("keeps a linked file visible even when filters hide it", () => {
    const filters = { ...DEFAULT_DIFF_REVIEW_FILTERS, showGeneratedFiles: false };
    expect(paths(visibleReviewFiles(files, filters, "web/pnpm-lock.yaml"))).toContain(
      "web/pnpm-lock.yaml",
    );
  });

  it("counts files per language in name order", () => {
    expect(languageCounts(files)).toEqual([
      { language: "Python", count: 1 },
      { language: "TSX", count: 2 },
      { language: "YAML", count: 1 },
    ]);
  });
});

describe("files the reader owns", () => {
  const owned = files.map((file, index) => ({ ...file, ownedByViewer: index % 2 === 1 }));

  it("shows only owned files, and drops the filter once nothing listed is owned", () => {
    const only = applyFilterAction(
      DEFAULT_DIFF_REVIEW_FILTERS,
      { type: "setOwnedFilesOnly", value: true },
      owned,
    );
    expect(hasActiveFilters(only)).toBe(true);
    expect(ownedFileCount(owned)).toBe(2);
    expect(paths(visibleReviewFiles(owned, effectiveReviewFilters(owned, only)))).toEqual([
      "web/src/App.test.tsx",
      "web/pnpm-lock.yaml",
    ]);
    // Ownership went away (a new head, or the lookup turned off): every file shows again.
    expect(effectiveReviewFilters(files, only)).toEqual(DEFAULT_DIFF_REVIEW_FILTERS);
    expect(applyFilterAction(only, { type: "clearFilters" }, owned)).toEqual(
      DEFAULT_DIFF_REVIEW_FILTERS,
    );
  });
});

describe("tree search", () => {
  it("matches path text ignoring case and normalizes backslashes", () => {
    const all = files.map((file) => file.path);
    expect(filterReviewPaths(all, null)).toEqual(all);
    expect(filterReviewPaths(all, "  ")).toEqual(all);
    expect(filterReviewPaths(all, "APP.")).toEqual([
      "web/src/App.tsx",
      "web/src/App.test.tsx",
      "omnigent/server/app.py",
    ]);
    expect(filterReviewPaths(all, "web\\src")).toHaveLength(2);
    expect(hasTreeSearch(" x ")).toBe(true);
    expect(hasTreeSearch("")).toBe(false);
  });

  it("round-trips item ids", () => {
    expect(pathFromReviewItemId(reviewItemId("a/b.ts"))).toBe("a/b.ts");
  });
});

describe("lineBreakdown", () => {
  const sourceCounts = (file: { additions: number | null; deletions: number | null }) => ({
    additions: file.additions ?? 0,
    deletions: file.deletions ?? 0,
  });

  it("adds up to the totals, every file counted as its counts say, binary ones too", () => {
    const listed = [
      reviewFile("web/src/a.ts", "modified", { additions: 10, deletions: 2 }),
      reviewFile("web/src/b.ts", "added", { additions: 5, deletions: 0 }),
      reviewFile("omnigent/app.py", "modified", { additions: 1, deletions: 30 }),
      // A binary file whose source still reports lines: those lines count.
      reviewFile("assets/logo.png", "modified", { additions: 4, deletions: 4, isBinary: true }),
      reviewFile("assets/empty.bin", "added", { isBinary: true }),
    ];
    const { totals, languages } = lineBreakdown(listed, sourceCounts);
    expect(totals).toEqual({ files: 5, additions: 20, deletions: 36 });
    const sum = languages.reduce(
      (acc, entry) => ({
        files: acc.files + entry.files,
        additions: acc.additions + entry.additions,
        deletions: acc.deletions + entry.deletions,
      }),
      { files: 0, additions: 0, deletions: 0 },
    );
    expect(sum).toEqual(totals);
    expect(languages.reduce((count, entry) => count + entry.files, 0)).toBe(listed.length);
  });

  it("sorts by lines changed, ties by name, and keeps languages with no changed lines last", () => {
    const listed = [
      reviewFile("notes.txt", "modified", { additions: 0, deletions: 0 }),
      reviewFile("b.py", "modified", { additions: 3, deletions: 3 }),
      reviewFile("a.ts", "modified", { additions: 6, deletions: 0 }),
      reviewFile("c.rs", "modified", { additions: 50, deletions: 1 }),
    ];
    expect(lineBreakdown(listed, sourceCounts).languages.map((entry) => entry.language)).toEqual([
      "Rust",
      "Python",
      "TypeScript",
      "Plain Text",
    ]);
  });

  it("counts a renamed file under its new name's language, and an unknown extension as Plain Text", () => {
    // A rename is listed under its new path; its old one, here a .py file, lives in the diff.
    const listed = [
      reviewFile("src/moved.ts", "modified", { additions: 1, deletions: 1 }),
      reviewFile("Notes.unknownext", "added", { additions: 5, deletions: 0 }),
    ];
    expect(lineBreakdown(listed, sourceCounts).languages).toEqual([
      { language: "Plain Text", files: 1, additions: 5, deletions: 0 },
      { language: "TypeScript", files: 1, additions: 1, deletions: 1 },
    ]);
  });

  it("totals the files not viewed apart, while languages count every file", () => {
    const listed = [
      reviewFile("a.ts", "modified", { additions: 10, deletions: 2 }),
      reviewFile("b.ts", "modified", { additions: 5, deletions: 1 }),
      reviewFile("c.py", "added", { additions: 3, deletions: 0 }),
    ];
    const breakdown = lineBreakdown(listed, sourceCounts, (file) => file.path === "a.ts");
    expect(breakdown.totals).toEqual({ files: 3, additions: 18, deletions: 3 });
    expect(breakdown.notViewed).toEqual({ files: 2, additions: 8, deletions: 1 });
    expect(breakdown.languages[0]).toEqual({
      language: "TypeScript",
      files: 2,
      additions: 15,
      deletions: 3,
    });
    // Nothing viewed: both totals agree; everything viewed: none left.
    expect(lineBreakdown(listed, sourceCounts).notViewed).toEqual(
      lineBreakdown(listed, sourceCounts).totals,
    );
    expect(lineBreakdown(listed, sourceCounts, () => true).notViewed).toEqual({
      files: 0,
      additions: 0,
      deletions: 0,
    });
  });

  it("takes every file's counts from the one source the totals use, such as a loaded diff", () => {
    const listed = [reviewFile("a.ts", "modified", { additions: 100, deletions: 100 })];
    const loaded = lineBreakdown(listed, () => ({ additions: 7, deletions: 1 }));
    expect(loaded.totals).toEqual({ files: 1, additions: 7, deletions: 1 });
    expect(loaded.languages[0]).toMatchObject({ additions: 7, deletions: 1 });
  });
});

describe("showOnlyLanguage", () => {
  const mixed = [
    reviewFile("a.ts", "modified"),
    reviewFile("b.py", "modified"),
    reviewFile("c.rs", "modified"),
  ];
  const showOnly = (selectedLanguages: readonly string[] | null, language: string) =>
    applyFilterAction(
      { ...DEFAULT_DIFF_REVIEW_FILTERS, selectedLanguages },
      { type: "showOnlyLanguage", language },
      mixed,
    ).selectedLanguages;

  it("shows exactly that language from no filter, a subset, or none selected", () => {
    expect(showOnly(null, "Python")).toEqual(["Python"]);
    expect(showOnly(["TypeScript", "Rust"], "Python")).toEqual(["Python"]);
    expect(showOnly([], "Rust")).toEqual(["Rust"]);
  });

  it("is no language filter when that's the only language there", () => {
    const single = [reviewFile("a.ts", "modified"), reviewFile("b.ts", "added")];
    expect(
      applyFilterAction(
        DEFAULT_DIFF_REVIEW_FILTERS,
        { type: "showOnlyLanguage", language: "TypeScript" },
        single,
      ).selectedLanguages,
    ).toBeNull();
  });

  it("names the language shown alone only when the filter chose it alone", () => {
    const only = (selectedLanguages: readonly string[] | null) =>
      onlySelectedLanguage({ ...DEFAULT_DIFF_REVIEW_FILTERS, selectedLanguages });
    expect(only(["Python"])).toBe("Python");
    expect(only(null)).toBeNull();
    expect(only([])).toBeNull();
    expect(only(["Python", "Rust"])).toBeNull();
  });
});
