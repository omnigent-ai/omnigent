import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  MarkdownDiffPreview,
  NODES_IN_FIRST_FRAME,
  NODES_PER_FRAME,
  NODES_PER_GROUP,
  nextBatchEnd,
  sectionGroups,
} from "./MarkdownDiffPreview";
import { computeMarkdownDiff, type MarkdownDiffSection } from "./markdownDiff";
import { heightModel, rangeHeight, UNCALIBRATED } from "./sectionHeightEstimate";

const sized = (...nodes: number[]): MarkdownDiffSection[] =>
  nodes.map((count) => ({
    kind: "unchanged",
    block: { type: "text", value: "" },
    text: "",
    nodes: count,
  }));

describe("nextBatchEnd", () => {
  it("fills a batch up to its node budget", () => {
    expect(nextBatchEnd(sized(400, 400, 400), 0, 1_000)).toBe(2);
    expect(nextBatchEnd(sized(400, 600, 1), 0, 1_000)).toBe(2);
    expect(nextBatchEnd(sized(400, 400, 400), 2, 1_000)).toBe(3);
  });

  it("always takes at least one section, however large", () => {
    expect(nextBatchEnd(sized(5_000, 1), 0, 1_000)).toBe(1);
  });

  it("ends at the last section", () => {
    expect(nextBatchEnd(sized(1, 1), 0, 1_000)).toBe(2);
    expect(nextBatchEnd(sized(1, 1), 2, 1_000)).toBe(2);
  });
});

describe("sectionGroups", () => {
  it("splits the sections into groups within the node budget, in order and without gaps", () => {
    const sections = sized(300, 300, 300, 100, 900, 50, 50);
    expect(sectionGroups(sections, 600)).toEqual([
      { start: 0, end: 2 },
      { start: 2, end: 4 },
      // One section larger than the budget is a group of its own.
      { start: 4, end: 5 },
      { start: 5, end: 7 },
    ]);
  });

  it("has no groups for no sections", () => {
    expect(sectionGroups([], 600)).toEqual([]);
  });

  it("groups more finely than the render batches, so a batch can end inside a group", () => {
    expect(NODES_PER_GROUP).toBeLessThan(NODES_PER_FRAME);
  });
});

function ready(before: string, after: string, maxBlockNodes = 4_000) {
  const result = computeMarkdownDiff(before, after, {
    maxSideLength: 1_000_000,
    maxBlocks: 5_000,
    maxNodes: 60_000,
    maxMath: 200,
    maxDiagrams: 20,
    maxAlignmentCells: 4_000_000,
    wordBudget: 20_000,
    maxBlockNodes,
    maxTableLines: 1_500,
  });
  if (result.status !== "ready") throw new Error("expected a ready diff");
  return { status: "ready" as const, sections: result.sections };
}

const noop = () => undefined;

describe("MarkdownDiffPreview", () => {
  it("renders a long document a batch at a time until it is complete", async () => {
    const paragraphs = Array.from({ length: 4_000 }, (_, index) => `Paragraph ${index}.`);
    const state = ready("", paragraphs.join("\n\n"));
    const { container } = render(
      <MarkdownDiffPreview
        path="docs/long.md"
        state={state}
        onRequest={noop}
        onRetry={noop}
        onShowSource={noop}
        hasSourceComments={false}
      />,
    );
    const shown = () => container.querySelectorAll("[data-markdown-diff-section]").length;
    expect(shown()).toBeGreaterThan(0);
    expect(shown()).toBeLessThan(paragraphs.length);
    expect(container.querySelector("[data-markdown-diff-complete]")).toBeNull();
    await waitFor(() =>
      expect(container.querySelector("[data-markdown-diff-complete]")).not.toBeNull(),
    );
    expect(shown()).toBe(paragraphs.length);
  });

  it("renders a smaller first batch, then the rest a frame at a time", async () => {
    const paragraphs = Array.from({ length: 4_000 }, (_, index) => `Paragraph ${index}.`);
    const state = ready("", paragraphs.join("\n\n"));
    const { container } = render(
      <MarkdownDiffPreview
        path="docs/long.md"
        state={state}
        onRequest={noop}
        onRetry={noop}
        onShowSource={noop}
        hasSourceComments={false}
      />,
    );
    const shown = container.querySelectorAll("[data-markdown-diff-section]").length;
    const nodes = state.sections.slice(0, shown).reduce((sum, section) => sum + section.nodes, 0);
    expect(shown).toBe(nextBatchEnd(state.sections, 0, NODES_IN_FIRST_FRAME));
    expect(nodes).toBeLessThanOrEqual(NODES_IN_FIRST_FRAME);
    expect(NODES_IN_FIRST_FRAME).toBeLessThan(NODES_PER_FRAME);
    await waitFor(() =>
      expect(container.querySelectorAll("[data-markdown-diff-section]")).toHaveLength(
        state.sections.length,
      ),
    );
  });

  it("wraps the sections in groups that keep their elements as later batches fill them", async () => {
    const paragraphs = Array.from({ length: 4_000 }, (_, index) => `Paragraph ${index}.`);
    const state = ready("", paragraphs.join("\n\n"));
    const { container } = render(
      <MarkdownDiffPreview
        path="docs/long.md"
        state={state}
        onRequest={noop}
        onRetry={noop}
        onShowSource={noop}
        hasSourceComments={false}
      />,
    );
    const groups = () => [...container.querySelectorAll<HTMLElement>(".markdown-diff-group")];
    const firstBatch = groups();
    const lastOfFirstBatch = firstBatch.at(-1)!;
    const sectionsInIt = lastOfFirstBatch.querySelectorAll("[data-markdown-diff-section]").length;
    await waitFor(() =>
      expect(container.querySelector("[data-markdown-diff-complete]")).not.toBeNull(),
    );
    // The groups already shown are the same elements, and the open one only gained sections.
    expect(groups().slice(0, firstBatch.length)).toEqual(firstBatch);
    expect(lastOfFirstBatch.isConnected).toBe(true);
    expect(
      lastOfFirstBatch.querySelectorAll("[data-markdown-diff-section]").length,
    ).toBeGreaterThanOrEqual(sectionsInIt);
    // Every section is in exactly one group, in document order, and groups reserve their height.
    const expected = sectionGroups(state.sections);
    expect(groups()).toHaveLength(expected.length);
    groups().forEach((group, index) => {
      const sections = [...group.querySelectorAll("[data-markdown-diff-section]")];
      expect(sections.map((section) => section.getAttribute("data-markdown-diff-section"))).toEqual(
        Array.from({ length: expected[index]!.end - expected[index]!.start }, (_, offset) =>
          String(expected[index]!.start + offset + 1),
        ),
      );
      expect(group.style.containIntrinsicSize).toBe(
        `auto ${rangeHeight(state.sections, expected[index]!.start, expected[index]!.end, heightModel(UNCALIBRATED), 720)}px`,
      );
    });
  });

  it("points to the source diff for a block too large to render", () => {
    const rows = Array.from({ length: 200 }, (_, index) => `| ${index} | row |`).join("\n");
    const table = `| n | name |\n| - | - |\n${rows}\n`;
    const state = ready("Intro.\n", `Intro.\n\n${table}`, 500);
    render(
      <MarkdownDiffPreview
        path="docs/table.md"
        state={state}
        onRequest={noop}
        onRetry={noop}
        onShowSource={noop}
        hasSourceComments={false}
      />,
    );
    expect(screen.getByRole("group", { name: "Added" })).toHaveTextContent(
      "This block is too large to show in the rich diff. The source diff shows it.",
    );
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByText("Intro.")).toBeVisible();
  });
});
