import type { CodeViewItem } from "@pierre/diffs";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reviewFile, reviewItemId } from "./diffReviewModel";
import { computeFileDiff } from "./fileDiffComputation";
import type { ReviewSearchLine } from "./findMatches";
import { computeMarkdownDiff } from "./markdownDiff";
import type { MarkdownPreviewState } from "./markdownDiffStore";
import type { DiffReviewContent } from "./reviewItems";
import {
  canPreviewMarkdown,
  isMarkdownPreviewAnnotation,
  markdownDiffViewAction,
  shownRichPaths,
  withMarkdownPreviewItems,
  withMarkdownPreviewSearchLines,
  useMarkdownPreviews,
  type MarkdownPreviewItemCache,
} from "./useMarkdownPreviews";

const GUIDE = "docs/guide.md";
const CODE = "src/app.ts";
const ready = (path: string): DiffReviewContent => ({
  status: "ready",
  fileDiff: computeFileDiff({
    path,
    before: "a\n",
    after: "b\n",
    ignoreWhitespace: false,
    revision: "1",
  }),
});

function readyState(before: string, after: string): MarkdownPreviewState {
  const result = computeMarkdownDiff(before, after);
  if (result.status !== "ready") throw new Error("expected a ready diff");
  return { status: "ready", sections: result.sections };
}

describe("canPreviewMarkdown and shownRichPaths", () => {
  it("previews only loaded text Markdown files", () => {
    expect(canPreviewMarkdown(reviewFile(GUIDE, "modified"), ready(GUIDE))).toBe(true);
    expect(canPreviewMarkdown(reviewFile("notes.markdown", "added"), ready("notes.markdown"))).toBe(
      true,
    );
    expect(canPreviewMarkdown(reviewFile(CODE, "modified"), ready(CODE))).toBe(false);
    expect(canPreviewMarkdown(reviewFile("page.mdx", "modified"), ready("page.mdx"))).toBe(false);
    expect(canPreviewMarkdown(reviewFile(GUIDE, "modified"), { status: "loading" })).toBe(false);
    expect(
      canPreviewMarkdown(reviewFile(GUIDE, "modified", { isBinary: true }), ready(GUIDE)),
    ).toBe(false);
  });

  it("shows a rich diff only for chosen files that can preview", () => {
    const files = [reviewFile(GUIDE, "modified"), reviewFile("README.md", "modified")];
    const contents = new Map([
      [GUIDE, ready(GUIDE)],
      ["README.md", { status: "loading" } as DiffReviewContent],
    ]);
    expect([...shownRichPaths(files, contents, new Set([GUIDE, "README.md", "gone.md"]))]).toEqual([
      GUIDE,
    ]);
  });
});

describe("markdownDiffViewAction", () => {
  it("names the switch by what shows now, and leaves which way it flips to the toggle", () => {
    const toggled: string[] = [];
    const source = markdownDiffViewAction(GUIDE, false, (path) => toggled.push(path));
    expect(source).toMatchObject({ label: "Show rich diff", rich: false });
    const rich = markdownDiffViewAction(GUIDE, true, (path) => toggled.push(path));
    expect(rich).toMatchObject({ label: "Show source diff", rich: true });
    source.run();
    rich.run();
    expect(toggled).toEqual([GUIDE, GUIDE]);
  });
});

describe("withMarkdownPreviewItems", () => {
  const diffItem = (path: string, collapsed = false): CodeViewItem<undefined> =>
    ({
      id: reviewItemId(path),
      type: "diff",
      fileDiff: (ready(path) as { fileDiff: unknown }).fileDiff,
      collapsed,
      version: 1,
    }) as CodeViewItem<undefined>;

  it("swaps a rich file for a preview row and keeps other files as they are", () => {
    const code = diffItem(CODE);
    const [guide, untouched] = withMarkdownPreviewItems(
      [diffItem(GUIDE, true), code],
      new Set([GUIDE]),
      new Map(),
      new Map(),
    );
    expect(untouched).toBe(code);
    expect(guide).toMatchObject({ id: reviewItemId(GUIDE), type: "file", collapsed: true });
    const annotations = (guide as { annotations: { lineNumber: number; metadata: unknown }[] })
      .annotations;
    expect(annotations).toHaveLength(1);
    expect(annotations[0]!.lineNumber).toBe(0);
    expect(isMarkdownPreviewAnnotation(annotations[0]!.metadata)).toBe(true);
  });

  it("keeps a preview row until its file collapses or its preview changes", () => {
    const cache: MarkdownPreviewItemCache = new Map();
    const states = new Map<string, MarkdownPreviewState>([[GUIDE, { status: "loading" }]]);
    const rich = new Set([GUIDE]);
    const first = withMarkdownPreviewItems([diffItem(GUIDE)], rich, states, cache)[0]!;
    const again = withMarkdownPreviewItems([diffItem(GUIDE)], rich, states, cache)[0]!;
    expect(again).toBe(first);
    const collapsed = withMarkdownPreviewItems([diffItem(GUIDE, true)], rich, states, cache)[0]!;
    expect(collapsed).not.toBe(first);
    const loaded = withMarkdownPreviewItems(
      [diffItem(GUIDE, true)],
      rich,
      new Map([[GUIDE, readyState("A.", "B.")]]),
      cache,
    )[0]!;
    expect(loaded).not.toBe(collapsed);
    expect(loaded.version).toBeGreaterThan(collapsed.version!);
  });

  it("forgets a file's row, and its document, once the file shows its source diff", () => {
    const cache: MarkdownPreviewItemCache = new Map();
    const states = new Map([[GUIDE, readyState("A.", "B.")]]);
    withMarkdownPreviewItems([diffItem(GUIDE)], new Set([GUIDE]), states, cache);
    expect(cache.has(GUIDE)).toBe(true);
    withMarkdownPreviewItems([diffItem(GUIDE)], new Set(), states, cache);
    expect(cache.size).toBe(0);
  });
});

describe("withMarkdownPreviewSearchLines", () => {
  const id = reviewItemId(GUIDE);
  const lines: ReviewSearchLine[] = [
    { id, text: GUIDE, lineNumber: 1, target: "currentPath" },
    { id, text: "The *slow* fox.\n", lineNumber: 1, side: "additions" },
    { id: reviewItemId(CODE), text: CODE, lineNumber: 1, target: "currentPath" },
    { id: reviewItemId(CODE), text: "slow code\n", lineNumber: 1, side: "additions" },
  ];

  it("searches a ready rich file's rendered sections instead of its source", () => {
    const states = new Map([
      [GUIDE, readyState("# Guide\n\nThe quick fox.", "# Guide\n\nThe *slow* fox.")],
    ]);
    expect(withMarkdownPreviewSearchLines(lines, new Set([GUIDE]), states)).toEqual([
      lines[0],
      { id, text: "Guide", lineNumber: 1, target: "markdownPreview" },
      { id, text: "The quick fox.", lineNumber: 2, target: "markdownPreview" },
      { id, text: "The slow fox.", lineNumber: 3, target: "markdownPreview" },
      lines[2],
      lines[3],
    ]);
  });

  it("keeps the source lines searchable while a preview isn't ready", () => {
    const loading = new Map<string, MarkdownPreviewState>([[GUIDE, { status: "loading" }]]);
    expect(withMarkdownPreviewSearchLines(lines, new Set([GUIDE]), loading)).toEqual(lines);
    expect(withMarkdownPreviewSearchLines(lines, new Set([GUIDE]), new Map())).toEqual(lines);
  });
});

describe("useMarkdownPreviews", () => {
  it("starts another review from no previews", async () => {
    const content = ready(GUIDE);
    if (content.status !== "ready") throw new Error("expected a ready diff");
    const view = renderHook(({ scope }) => useMarkdownPreviews(scope, undefined), {
      initialProps: { scope: "first-review" },
    });
    await waitFor(() => expect(view.result.current.request).toBeDefined());
    act(() => view.result.current.request(GUIDE, content.fileDiff));
    await waitFor(() => expect(view.result.current.states.get(GUIDE)?.status).toBe("ready"));
    view.rerender({ scope: "second-review" });
    await waitFor(() => expect(view.result.current.states.size).toBe(0));
  });
});
