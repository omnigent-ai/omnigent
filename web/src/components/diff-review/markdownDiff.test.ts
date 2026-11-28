import type { Element, Nodes } from "hast";
import { toHtml } from "hast-util-to-html";
import { describe, expect, it } from "vitest";
import {
  changedRanges,
  changedSpans,
  computeMarkdownDiff,
  longestTableLikeRun,
  MARKDOWN_DIFF_STATIC_PROPERTY,
  MARKDOWN_DIFF_WORD_CLASS,
  TOO_LARGE_MESSAGE,
  tokenize,
  WORKER_MARKDOWN_DIFF_LIMITS,
  type MarkdownDiffLimits,
  type MarkdownDiffSection,
} from "./markdownDiff";

function sections(
  before: string | null,
  after: string | null,
  limits?: Partial<MarkdownDiffLimits>,
) {
  const result = computeMarkdownDiff(before, after, { ...WORKER_MARKDOWN_DIFF_LIMITS, ...limits });
  if (result.status !== "ready") throw new Error(result.message);
  return result.sections;
}

function kinds(list: readonly MarkdownDiffSection[]) {
  return list.map((entry) => `${entry.kind}:${entry.text}`);
}

function* elements(node: Nodes): Generator<Element> {
  if (node.type === "element") yield node;
  if ("children" in node) for (const child of node.children) yield* elements(child);
}

/** The text of every changed-word highlight in a section. */
function changedWords(entry: MarkdownDiffSection): string[] {
  return [...elements(entry.block)]
    .filter((element) => element.properties.className?.toString() === MARKDOWN_DIFF_WORD_CLASS)
    .map((element) =>
      element.children.map((child) => (child.type === "text" ? child.value : "")).join(""),
    );
}

describe("computeMarkdownDiff blocks", () => {
  it("leaves an unchanged document unchanged", () => {
    expect(kinds(sections("# Title\n\nBody.", "# Title\n\nBody."))).toEqual([
      "unchanged:Title",
      "unchanged:Body.",
    ]);
  });

  it("aligns an inserted block between unchanged ones", () => {
    expect(kinds(sections("One.\n\nThree.", "One.\n\nTwo.\n\nThree."))).toEqual([
      "unchanged:One.",
      "added:Two.",
      "unchanged:Three.",
    ]);
  });

  it("shows every block as added for a new file and removed for a deleted one", () => {
    expect(kinds(sections(null, "# New\n\nText."))).toEqual(["added:New", "added:Text."]);
    expect(kinds(sections("# Old", null))).toEqual(["removed:Old"]);
  });

  it("returns no sections when both versions are empty", () => {
    expect(sections("", "  \n")).toEqual([]);
  });

  it("replaces a changed middle wholesale when it is too big to align", () => {
    const result = sections("Keep.\n\nA.\n\nB.\n\nEnd.", "Keep.\n\nB.\n\nC.\n\nEnd.", {
      maxAlignmentCells: 1,
    });
    expect(kinds(result)).toEqual([
      "unchanged:Keep.",
      "removed:A.",
      "removed:B.",
      "added:B.",
      "added:C.",
      "unchanged:End.",
    ]);
  });

  it("aligns a typical edit with repeated rules and headings block by block", () => {
    const before = [
      "# Guide",
      "Intro.",
      "---",
      "## Setup",
      "Install it.",
      "---",
      "## Usage",
      "Run it.",
      "---",
      "Footer.",
    ].join("\n\n");
    const after = [
      "# Guide",
      "Intro, revised.",
      "---",
      "## Setup",
      "---",
      "## Usage",
      "Run it.",
      "Then check it.",
      "---",
      "## Notes",
      "---",
      "Footer.",
    ].join("\n\n");
    expect(kinds(sections(before, after))).toEqual([
      "unchanged:Guide",
      "removed:Intro.",
      "added:Intro, revised.",
      "unchanged:",
      "unchanged:Setup",
      "removed:Install it.",
      "unchanged:",
      "unchanged:Usage",
      "unchanged:Run it.",
      "added:Then check it.",
      "added:",
      "added:Notes",
      "unchanged:",
      "unchanged:Footer.",
    ]);
  });

  it("chooses among equally short alignments of repeated blocks consistently", () => {
    const cases: [string, string][] = [
      ["A.\n\nB.\n\nA.", "B.\n\nA.\n\nB."],
      ["A.\n\nA.\n\nB.", "B.\n\nA.\n\nA."],
      ["A.\n\nB.\n\nC.\n\nB.", "B.\n\nC.\n\nB.\n\nA."],
    ];
    expect(cases.map(([before, after]) => kinds(sections(before, after)))).toEqual([
      ["removed:A.", "unchanged:B.", "unchanged:A.", "added:B."],
      ["added:B.", "unchanged:A.", "unchanged:A.", "removed:B."],
      ["removed:A.", "unchanged:B.", "unchanged:C.", "unchanged:B.", "added:A."],
    ]);
  });

  it("keeps a moved block's neighbours aligned", () => {
    expect(kinds(sections("A.\n\nB.\n\nC.", "B.\n\nC.\n\nA."))).toEqual([
      "removed:A.",
      "unchanged:B.",
      "unchanged:C.",
      "added:A.",
    ]);
  });
});

describe("computeMarkdownDiff words", () => {
  it("marks only the changed words in an edited paragraph", () => {
    const [removed, added] = sections("The quick brown fox.", "The slow brown fox.");
    expect(removed.kind).toBe("removed");
    expect(added.kind).toBe("added");
    expect(changedWords(removed)).toEqual(["quick"]);
    expect(changedWords(added)).toEqual(["slow"]);
  });

  it("keeps links and inline markup, and the text, while marking words inside them", () => {
    const [removed, added] = sections(
      "See the [setup guide](docs/setup.md) **now**.",
      "See the [install guide](docs/setup.md) **today**.",
    );
    expect(added.text).toBe("See the install guide today.");
    expect(removed.text).toBe("See the setup guide now.");
    expect(changedWords(added)).toEqual(["install", "today"]);
    const link = [...elements(added.block)].find((element) => element.tagName === "a");
    expect(link?.properties.href).toBe("docs/setup.md");
    expect(toHtml(added.block)).toContain("<strong>");
  });

  it("marks words in edited list items and headings", () => {
    const list = sections("- apples\n- pears", "- apples\n- plums");
    expect(list.map((entry) => entry.kind)).toEqual(["removed", "added"]);
    expect(changedWords(list[1])).toEqual(["plums"]);
    const heading = sections("## Getting started", "## Getting going");
    expect(changedWords(heading[1])).toEqual(["going"]);
  });

  it("pairs consecutive edited paragraphs one to one", () => {
    const result = sections("First one.\n\nSecond one.", "First two.\n\nSecond two.");
    expect(result.map((entry) => entry.kind)).toEqual(["removed", "removed", "added", "added"]);
    expect(result.map(changedWords)).toEqual([["one"], ["one"], ["two"], ["two"]]);
  });

  it("doesn't mark words across different kinds of block, or in tables and code", () => {
    expect(sections("Plain text.", "## Plain heading").flatMap(changedWords)).toEqual([]);
    expect(
      sections("| a |\n| - |\n| one |", "| a |\n| - |\n| two |").flatMap(changedWords),
    ).toEqual([]);
    expect(sections("```\nold\n```", "```\nnew\n```").flatMap(changedWords)).toEqual([]);
  });

  it("stops marking words when the budget runs out", () => {
    expect(sections("Old words.", "New words.", { wordBudget: 0 }).flatMap(changedWords)).toEqual(
      [],
    );
  });

  it("tokenizes words, whitespace, and punctuation separately", () => {
    expect(tokenize("Hello,  wörld_1!")).toEqual(["Hello", ",", "  ", "wörld_1", "!"]);
  });

  it("chooses among equally short word changes consistently", () => {
    const cases: [string, string][] = [
      ["the cat and the dog", "the dog and the cat"],
      ["a b a b", "b a b a"],
      ["one two two three", "one two three three"],
    ];
    expect(
      cases.map(([before, after]) => changedRanges(tokenize(before), tokenize(after))),
    ).toEqual([
      {
        oldRanges: [
          [4, 7],
          [16, 19],
        ],
        newRanges: [
          [4, 7],
          [16, 19],
        ],
      },
      { oldRanges: [[0, 1]], newRanges: [[6, 7]] },
      { oldRanges: [[8, 11]], newRanges: [[8, 13]] },
    ]);
  });

  it("lists each changed span of the shortest edit, a replacement as one span", () => {
    expect(changedSpans([], [])).toEqual([]);
    expect(changedSpans([1, 2, 3], [1, 2, 3])).toEqual([]);
    expect(changedSpans([1, 3], [1, 2, 3])).toEqual([[1, 1, 1, 2]]);
    expect(changedSpans([1, 2, 3], [3])).toEqual([[0, 2, 0, 0]]);
    expect(changedSpans([1, 2, 3, 4], [1, 5, 3, 6, 7])).toEqual([
      [1, 2, 1, 2],
      [3, 4, 3, 5],
    ]);
  });

  it("reports changed ranges without surrounding whitespace", () => {
    expect(changedRanges(tokenize("a b c"), tokenize("a x y c"))).toEqual({
      oldRanges: [[2, 3]],
      newRanges: [[2, 5]],
    });
  });
});

describe("computeMarkdownDiff safety", () => {
  it("renders through the file viewer's sanitizer", () => {
    const result = sections(
      null,
      '<script>alert(1)</script>\n\n<img src="x.png" onerror="alert(1)">\n\n[click](javascript:alert(1))',
    );
    const html = result.map((each) => toHtml(each.block)).join("");
    expect(html).toContain('src="x.png"');
    expect(html).toContain("click");
    expect(html).not.toContain("<script");
    expect(html).not.toContain("onerror");
    expect(html).not.toContain("javascript:");
  });

  it("strips the changed-word class and the static marker from authored HTML", () => {
    const html = sections(
      null,
      '<span class="markdown-diff-word">fake</span> <pre data-markdown-diff-static="true"><code class="language-mermaid">graph TD</code></pre>',
    )
      .map((entry) => toHtml(entry.block))
      .join("");
    expect(html).not.toContain(MARKDOWN_DIFF_WORD_CLASS);
    expect(html).not.toContain("data-markdown-diff-static");
  });

  it("keeps the alert classes the file viewer allows", () => {
    const [alert] = sections(null, "> [!NOTE]\n> Read this.");
    expect(toHtml(alert.block)).toContain("markdown-alert-note");
  });
});

describe("computeMarkdownDiff limits", () => {
  it("refuses a version longer than the length limit", () => {
    expect(
      computeMarkdownDiff("short", "x".repeat(11), {
        ...WORKER_MARKDOWN_DIFF_LIMITS,
        maxSideLength: 10,
      }),
    ).toEqual({ status: "tooLarge", message: TOO_LARGE_MESSAGE });
  });

  it("refuses a version with too many blocks or rendered nodes", () => {
    const many = Array.from({ length: 5 }, (_, index) => `Paragraph ${index}.`).join("\n\n");
    expect(
      computeMarkdownDiff(null, many, { ...WORKER_MARKDOWN_DIFF_LIMITS, maxBlocks: 4 }).status,
    ).toBe("tooLarge");
    expect(
      computeMarkdownDiff(null, many, { ...WORKER_MARKDOWN_DIFF_LIMITS, maxNodes: 9 }).status,
    ).toBe("tooLarge");
  });

  it("leaves a block past the node limit to the source diff", () => {
    const table = [
      "| a | b |",
      "| - | - |",
      ...Array.from({ length: 50 }, (_, row) => `| ${row} | x |`),
    ].join("\n");
    const result = sections(null, `Intro.\n\n${table}`, { maxBlockNodes: 100 });
    expect(result.map((entry) => [entry.kind, entry.oversized ?? false, entry.text])).toEqual([
      ["added", false, "Intro."],
      ["added", true, ""],
    ]);
    expect(result[0]!.nodes).toBe(2);
    expect(toHtml(result[1]!.block)).toBe("<div></div>");
  });

  it("doesn't parse a table too long to parse quickly", () => {
    const table = (rows: number) =>
      [
        "| a | b |",
        "| - | - |",
        ...Array.from({ length: rows }, (_, row) => `| ${row} | x |`),
      ].join("\n");
    const limits = { ...WORKER_MARKDOWN_DIFF_LIMITS, maxTableLines: 12 };
    expect(computeMarkdownDiff(null, table(10), limits).status).toBe("ready");
    expect(computeMarkdownDiff(table(11), null, limits)).toEqual({
      status: "tooLarge",
      message: TOO_LARGE_MESSAGE,
    });
  });

  it("measures the longest run of lines with a pipe", () => {
    expect(longestTableLikeRun("")).toBe(0);
    expect(longestTableLikeRun("no pipes\nhere")).toBe(0);
    expect(longestTableLikeRun("a|b\nc|d\n\ne|f\n")).toBe(2);
    expect(longestTableLikeRun("text\n|x|\n|y|\n|z|")).toBe(3);
  });

  it("typesets math within the limit and leaves it as TeX beyond it", () => {
    const typeset = sections(null, "$$\nx^2\n$$");
    expect(toHtml(typeset[0].block)).toContain("katex");
    const plain = sections(null, "$$\nx^2\n$$", { maxMath: 0 });
    expect(toHtml(plain[0].block)).not.toContain("katex");
    expect(plain[0].text).toContain("x^2");
    // Typeset math keeps its TeX in the page, so find's offsets include it.
    expect(typeset[0].text).toContain("x^2");
  });

  it("draws Mermaid diagrams up to the limit and shows the rest as code", () => {
    const diagram = "```mermaid\ngraph TD\n```";
    const result = sections(null, `${diagram}\n\n${diagram}`, { maxDiagrams: 1 });
    expect(
      result.map((entry) => (entry.block as Element).properties[MARKDOWN_DIFF_STATIC_PROPERTY]),
    ).toEqual([undefined, "true"]);
    // A drawn diagram shows no source text to find; one shown as code does.
    expect(result.map((entry) => entry.text)).toEqual(["", "graph TD\n"]);
  });
});
