import { describe, expect, it } from "vitest";
import { rehypeMarkUnclosedFences, remarkFindUnclosedFences } from "./unclosedFences";

interface Node {
  type: string;
  data?: Record<string, unknown>;
  children?: Node[];
  position?: { start: { offset: number } };
}

type FenceHandler = (this: { stack: Node[] }) => void;

/** Install the remark plugin on a fake processor; returns its fence counter and transformer. */
function installRemarkPlugin() {
  const data: Record<string, unknown> = {};
  const transform = remarkFindUnclosedFences.call({ data: () => data });
  const [extension] = data.fromMarkdownExtensions as { enter: { codeFencedFence: FenceHandler } }[];
  return { countFence: extension.enter.codeFencedFence, transform };
}

const code = (offset: number, fences?: number): Node => ({
  type: "code",
  data: fences === undefined ? undefined : { fences },
  position: { start: { offset } },
});

describe("remarkFindUnclosedFences", () => {
  it("counts each fence on the innermost code block being parsed", () => {
    const { countFence } = installRemarkPlugin();
    const block = code(0);
    const contentBuffer = { type: "fragment" };

    // The code block is on top at the opening fence, and its content buffer at the closing one.
    countFence.call({ stack: [{ type: "root" }, block] });
    countFence.call({ stack: [{ type: "root" }, block, contentBuffer] });

    expect(block.data).toEqual({ fences: 2 });
  });

  it("records the starts of fenced blocks with only an opening fence", () => {
    const { transform } = installRemarkPlugin();
    const tree: Node = {
      type: "root",
      children: [
        code(0, 2),
        { type: "list", children: [{ type: "listItem", children: [code(20, 1)] }] },
        code(40),
      ],
    };
    const file = { data: {} as Record<string, unknown> };

    transform(tree, file);

    expect([...(file.data.omnigentUnclosedFenceStarts as Set<number>)]).toEqual([20]);
  });
});

describe("rehypeMarkUnclosedFences", () => {
  const pre = (offset: number) => ({
    type: "element",
    tagName: "pre",
    properties: {} as Record<string, unknown>,
    position: { start: { offset } },
  });

  it("marks only the pre whose fence the parser found unclosed", () => {
    const closed = pre(0);
    const unclosed = pre(20);
    const file = { data: { omnigentUnclosedFenceStarts: new Set([20]) } };

    rehypeMarkUnclosedFences()({ type: "root", children: [closed, unclosed] }, file);

    expect(closed.properties).toEqual({});
    expect(unclosed.properties).toEqual({ dataUnclosedFence: true });
  });

  it("leaves the tree alone when the remark plugin didn't run", () => {
    const node = pre(0);

    rehypeMarkUnclosedFences()({ type: "root", children: [node] }, { data: {} });

    expect(node.properties).toEqual({});
  });
});
