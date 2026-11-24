import { CodeView } from "@pierre/diffs";
import { describe, expect, it } from "vitest";
import { reviewFile, reviewItemId } from "./diffReviewModel";
import { computeFileDiff } from "./fileDiffComputation";
import {
  buildReviewItems,
  changedLineCounts,
  fileLineCounts,
  isExpandable,
  reviewSearchLines,
  type DiffReviewContent,
} from "./reviewItems";

function diffOf(path: string, before: string | null, after: string | null) {
  return computeFileDiff({ path, before, after, ignoreWhitespace: false, revision: "r1" });
}

const app = reviewFile("src/app.ts", "modified", { additions: 9, deletions: 9 });
const lock = reviewFile("pnpm-lock.yaml", "modified");
const image = reviewFile("logo.png", "added", { isBinary: true });
const appDiff = diffOf("src/app.ts", "one\ntwo\nthree\n", "one\nTWO\nthree\nfour\n");

describe("buildReviewItems", () => {
  it("renders loaded diffs expanded and generated, binary, and pending files collapsed", () => {
    const contents = new Map<string, DiffReviewContent>([
      ["src/app.ts", { status: "ready", fileDiff: appDiff }],
      ["pnpm-lock.yaml", { status: "ready", fileDiff: diffOf("pnpm-lock.yaml", "a\n", "b\n") }],
    ]);
    const items = buildReviewItems({ files: [app, lock, image], contents, collapsed: new Map() });
    expect(items.map((item) => [item.id, item.type, item.collapsed])).toEqual([
      ["diff:src/app.ts", "diff", false],
      ["diff:pnpm-lock.yaml", "diff", true],
      ["diff:logo.png", "file", true],
    ]);
    expect(isExpandable(image, undefined)).toBe(false);
    expect(isExpandable(app, contents.get("src/app.ts"))).toBe(true);
  });

  it("shows why a file has no diff", () => {
    const contents = new Map<string, DiffReviewContent>([
      ["src/app.ts", { status: "failed", message: "Couldn't load this diff. 500" }],
      ["pnpm-lock.yaml", { status: "unavailable", message: "Original content unavailable" }],
    ]);
    const items = buildReviewItems({ files: [app, lock, image], contents, collapsed: new Map() });
    const labels = items.map((item) => (item.type === "file" ? item.file.contents : ""));
    expect(labels).toEqual([
      "Couldn't load this diff. 500",
      "Original content unavailable",
      "Binary file",
    ]);
    expect(
      buildReviewItems({ files: [app], contents: new Map(), collapsed: new Map() })[0],
    ).toMatchObject({ type: "file", file: { contents: "Loading diff…" } });
  });

  it("honors explicit collapse choices and reuses unchanged items", () => {
    const contents = new Map<string, DiffReviewContent>([
      ["src/app.ts", { status: "ready", fileDiff: appDiff }],
    ]);
    const first = buildReviewItems({ files: [app], contents, collapsed: new Map() });
    const again = buildReviewItems({
      files: [app],
      contents: new Map(contents),
      collapsed: new Map(),
    });
    expect(again[0]).toBe(first[0]);
    const collapsed = buildReviewItems({
      files: [app],
      contents,
      collapsed: new Map([["src/app.ts", true]]),
    });
    expect(collapsed[0]).toMatchObject({ collapsed: true });
    expect(collapsed[0]!.version).not.toBe(first[0]!.version);
  });

  it("names each item by the file it shows, even when two files share one diff", () => {
    const copy = reviewFile("src/copy.ts", "modified", { additions: 9, deletions: 9 });
    const ready: DiffReviewContent = { status: "ready", fileDiff: appDiff };
    const items = (file: typeof app) =>
      buildReviewItems({
        files: [file],
        contents: new Map([[file.path, ready]]),
        collapsed: new Map(),
      });
    expect(items(copy)[0]!.id).toBe(reviewItemId("src/copy.ts"));
    expect(items(app)[0]!.id).toBe(reviewItemId("src/app.ts"));
  });
});

describe("line counts", () => {
  it("counts the diff once loaded, else what the source reported", () => {
    expect(changedLineCounts(appDiff)).toEqual({ additions: 2, deletions: 1 });
    expect(fileLineCounts(app, { status: "ready", fileDiff: appDiff })).toEqual({
      additions: 2,
      deletions: 1,
    });
    expect(fileLineCounts(app, undefined)).toEqual({ additions: 9, deletions: 9 });
    expect(fileLineCounts(lock, undefined)).toEqual({ additions: 0, deletions: 0 });
  });
});

describe("reviewSearchLines", () => {
  it("searches paths, the whole new version, and deleted lines", () => {
    const contents = new Map<string, DiffReviewContent>([
      ["src/app.ts", { status: "ready", fileDiff: appDiff }],
    ]);
    const lines = reviewSearchLines([app, image], contents);
    expect(lines.map((line) => [line.text, line.lineNumber, line.side ?? line.target])).toEqual([
      ["src/app.ts", 1, "currentPath"],
      ["one\n", 1, "additions"],
      ["TWO\n", 2, "additions"],
      ["three\n", 3, "additions"],
      ["four\n", 4, "additions"],
      ["two\n", 2, "deletions"],
      ["logo.png", 1, "currentPath"],
    ]);
  });

  it("searches files still loading by path only", () => {
    expect(reviewSearchLines([app], new Map()).map((line) => line.target)).toEqual(["currentPath"]);
  });
});

describe("buildReviewItems with source annotations", () => {
  const contents = new Map<string, DiffReviewContent>([
    ["src/app.ts", { status: "ready", fileDiff: appDiff }],
  ]);
  const annotation = { side: "additions" as const, lineNumber: 2, metadata: "thread" };

  it("attaches a file's annotations and keeps the item while they're unchanged", () => {
    const annotations = new Map([["src/app.ts", [annotation]]]);
    const first = buildReviewItems({ files: [app], contents, collapsed: new Map(), annotations });
    expect(first[0]).toMatchObject({ annotations: [annotation] });
    const again = buildReviewItems({
      files: [app],
      contents,
      collapsed: new Map(),
      annotations: new Map(annotations),
    });
    expect(again[0]).toBe(first[0]);
  });

  it("rebuilds a file's item when its annotations change, and drops them when they're gone", () => {
    const plain = buildReviewItems({ files: [app], contents, collapsed: new Map() })[0]!;
    const annotated = buildReviewItems({
      files: [app],
      contents,
      collapsed: new Map(),
      annotations: new Map([["src/app.ts", [annotation]]]),
    })[0]!;
    expect(annotated).not.toBe(plain);
    expect(annotated.version).toBeGreaterThan(plain.version!);
    const cleared = buildReviewItems({ files: [app], contents, collapsed: new Map() })[0]!;
    expect(cleared).not.toHaveProperty("annotations");
  });

  it("leaves placeholders without annotations", () => {
    const [item] = buildReviewItems({
      files: [app],
      contents: new Map(),
      collapsed: new Map(),
      annotations: new Map([["src/app.ts", [annotation]]]),
    });
    expect(item).toMatchObject({ type: "file" });
    expect(item).not.toHaveProperty("annotations");
  });
});

describe("buildReviewItems with a real CodeView", () => {
  function mountedCodeView() {
    const view = new CodeView<undefined>({});
    view.setup(document.createElement("div"));
    return view;
  }
  const diffItem = (view: CodeView<undefined>) => {
    const item = view.getItem("diff:src/app.ts");
    return item?.type === "diff" ? item.fileDiff : undefined;
  };
  const input = (fileDiff: ReturnType<typeof diffOf>) => ({
    files: [app],
    contents: new Map<string, DiffReviewContent>([["src/app.ts", { status: "ready", fileDiff }]]),
    collapsed: new Map(),
  });

  it("shows a refreshed diff for a file whose content changed", () => {
    const view = mountedCodeView();
    view.setItems(buildReviewItems(input(diffOf("src/app.ts", "a\n", "b\n"))));
    const latest = diffOf("src/app.ts", "a\n", "c\n");
    view.setItems(buildReviewItems(input(latest)));
    expect(diffItem(view)).toBe(latest);
    view.cleanUp();
  });

  it("shows the recomputed diff when whitespace handling changes", () => {
    const view = mountedCodeView();
    const before = "if (x) {\n  run();\n}\n";
    const after = "if (x) {\n    run();\n}\n";
    const withWhitespace = diffOf("src/app.ts", before, after);
    view.setItems(buildReviewItems(input(withWhitespace)));
    const ignoring = computeFileDiff({
      path: "src/app.ts",
      before,
      after,
      ignoreWhitespace: true,
      revision: "r2",
    });
    view.setItems(buildReviewItems(input(ignoring)));
    expect(diffItem(view)).toBe(ignoring);
    expect(changedLineCounts(diffItem(view)!)).toEqual({ additions: 0, deletions: 0 });
    view.cleanUp();
  });

  it("swaps a loading placeholder for the diff and an error message for another", () => {
    const view = mountedCodeView();
    const placeholder = (content: DiffReviewContent) => ({
      files: [app],
      contents: new Map([["src/app.ts", content]]),
      collapsed: new Map(),
    });
    view.setItems(buildReviewItems(placeholder({ status: "failed", message: "Couldn't load. A" })));
    view.setItems(buildReviewItems(placeholder({ status: "failed", message: "Couldn't load. B" })));
    const item = view.getItem("diff:src/app.ts");
    expect(item?.type === "file" && item.file.contents).toBe("Couldn't load. B");
    view.cleanUp();
  });
});
