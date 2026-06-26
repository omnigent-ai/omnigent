import { describe, expect, it } from "vitest";
import { generateFlowText, nodeInLoop, type FlowGraph } from "./flowToText";

/** The "batch processing" chart shipped as the prototype's Load-example. */
function exampleGraph(): FlowGraph {
  return {
    nodes: [
      { id: "n1", type: "start", label: "Start batch", x: 300, y: 60 },
      { id: "n2", type: "process", label: "Fetch next order", x: 300, y: 190 },
      { id: "n3", type: "process", label: "Charge & ship", x: 300, y: 290 },
      { id: "n4", type: "decision", label: "More orders?", x: 290, y: 390 },
      { id: "n5", type: "end", label: "Batch done", x: 620, y: 420 },
    ],
    edges: [
      { id: "e1", from: "n1", to: "n2", label: "" },
      { id: "e2", from: "n2", to: "n3", label: "" },
      { id: "e3", from: "n3", to: "n4", label: "" },
      { id: "e4", from: "n4", to: "n2", label: "Yes" },
      { id: "e5", from: "n4", to: "n5", label: "No" },
    ],
    loops: [{ id: "L1", label: "while orders remain", x: 240, y: 160, w: 330, h: 400 }],
  };
}

describe("generateFlowText", () => {
  it("returns a hint for an empty graph", () => {
    const r = generateFlowText({ nodes: [], edges: [] });
    expect(r.summary).toBe("Add nodes first.");
    expect(r.narrative).toBe("");
    expect(r.mermaid).toBe("");
  });

  it("numbers nodes in DFS pre-order from the start", () => {
    const { outline } = generateFlowText(exampleGraph());
    expect(outline).toContain("1. [Start] Start batch");
    expect(outline).toContain("2. [Step] Fetch next order");
    expect(outline).toContain("3. [Step] Charge & ship");
    expect(outline).toContain("4. [Decision] More orders?");
    expect(outline).toContain("5. [End] Batch done");
  });

  it("renders decision branches in the outline", () => {
    const { outline } = generateFlowText(exampleGraph());
    expect(outline).toContain("↳ if “Yes” → step 2 (Fetch next order)");
    expect(outline).toContain("↳ if “No” → step 5 (Batch done)");
  });

  it("lists loop membership in the outline", () => {
    const { outline } = generateFlowText(exampleGraph());
    expect(outline).toContain("Loops:");
    // n2,n3,n4 sit inside the box (n1/n5 are outside) → steps 2, 3, 4.
    expect(outline).toContain("↻ while orders remain — repeats steps 2, 3, 4");
  });

  it("produces loop-aware narrative prose", () => {
    const { narrative } = generateFlowText(exampleGraph());
    expect(narrative).toContain("Begin: Start batch.");
    expect(narrative).toContain("↻ Repeat — while orders remain:");
    expect(narrative).toContain("⟲ (repeat while the loop condition holds)");
    // The "No" branch exits the loop to the End node.
    expect(narrative).toContain("exit loop → step 5 (Batch done)");
  });

  it("emits a valid mermaid flowchart with subgraph + shapes + edges", () => {
    const { mermaid } = generateFlowText(exampleGraph());
    expect(mermaid.startsWith("flowchart TD")).toBe(true);
    // start/end use stadium shape, decision uses rhombus
    expect(mermaid).toContain("([Start batch])");
    expect(mermaid).toContain("{More orders?}");
    expect(mermaid).toContain("[Charge & ship]");
    // loop members wrapped in a subgraph
    expect(mermaid).toContain('subgraph LOOP0["↻ while orders remain"]');
    expect(mermaid).toContain("end");
    // labelled + unlabelled edges
    expect(mermaid).toContain("-- Yes -->");
    expect(mermaid).toMatch(/N0 --> N1/); // start -> fetch (unlabelled)
  });

  it("summarizes node/edge/loop counts", () => {
    const { summary } = generateFlowText(exampleGraph());
    expect(summary).toContain("5 nodes · 5 edges · 1 loop");
    expect(summary).toContain("1 start");
    expect(summary).toContain("2 process");
  });

  it("escapes mermaid-breaking characters in labels", () => {
    const r = generateFlowText({
      nodes: [{ id: "a", type: "process", label: 'Do [x] {y} "z"', x: 0, y: 0 }],
      edges: [],
    });
    // brackets/braces become spaces; double quotes become single quotes
    expect(r.mermaid).toContain("[Do  x   y  'z']");
  });

  it("marks dead-end (non-end) nodes in the outline", () => {
    const r = generateFlowText({
      nodes: [
        { id: "s", type: "start", label: "Go", x: 0, y: 0 },
        { id: "p", type: "process", label: "Orphan step", x: 0, y: 0 },
      ],
      edges: [{ id: "e", from: "s", to: "p" }],
    });
    expect(r.outline).toContain("(dead end — no outgoing connection)");
  });

  it("falls back to indegree-0 nodes when no explicit start exists", () => {
    const r = generateFlowText({
      nodes: [
        { id: "p1", type: "process", label: "First", x: 0, y: 0 },
        { id: "p2", type: "process", label: "Second", x: 0, y: 0 },
      ],
      edges: [{ id: "e", from: "p1", to: "p2" }],
    });
    expect(r.outline).toContain("1. [Step] First");
    expect(r.outline).toContain("2. [Step] Second");
  });

  it("is deterministic — identical input yields identical output", () => {
    const a = generateFlowText(exampleGraph());
    const b = generateFlowText(exampleGraph());
    expect(a).toEqual(b);
  });
});

describe("nodeInLoop", () => {
  const loop = { id: "L", label: "l", x: 100, y: 100, w: 200, h: 200 };
  it("includes a node whose center is inside the box", () => {
    expect(nodeInLoop({ id: "n", type: "process", label: "", x: 150, y: 150, w: 40, h: 20 }, loop)).toBe(
      true,
    );
  });
  it("excludes a node whose center is outside the box", () => {
    expect(nodeInLoop({ id: "n", type: "process", label: "", x: 10, y: 10, w: 40, h: 20 }, loop)).toBe(
      false,
    );
  });
});
