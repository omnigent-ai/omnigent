/**
 * Deterministic flow-chart → text generator.
 *
 * Turns a flow chart ({@link FlowGraph}: typed nodes, directed edges, and
 * optional loop containers) into three textual renderings:
 *
 * - **outline**  — a numbered, always-unambiguous list of every node and its
 *   outgoing connections (DFS pre-order numbering).
 * - **narrative** — indented, loop-aware prose that reads like a procedure,
 *   following branches and folding loop bodies under their repeat condition.
 * - **mermaid**   — a `flowchart TD` definition (loop members wrapped in
 *   `subgraph`s) that renders anywhere mermaid is supported.
 *
 * The traversal is fully deterministic (no randomness, stable iteration over
 * the input arrays) so the same chart always produces byte-identical output —
 * which is what makes the generator unit-testable. It is intentionally pure
 * and framework-agnostic: it operates on a plain {@link FlowGraph}, not on
 * React Flow's node/edge objects, so the page maps RF state → FlowGraph before
 * calling {@link generateFlowText}.
 *
 * Ported from the standalone "Flow → Text" prototype, preserving its exact
 * numbering, branch handling, and loop semantics.
 */

export type FlowNodeType = "start" | "process" | "decision" | "io" | "end";

export interface FlowNode {
  id: string;
  type: FlowNodeType;
  label: string;
  /** Layout geometry (canvas coords). Used only for loop-membership tests. */
  x: number;
  y: number;
  w?: number;
  h?: number;
}

export interface FlowEdge {
  id: string;
  from: string;
  to: string;
  /** Branch/condition label (decisions) or a plain transition note. */
  label?: string;
}

/** A loop container — a box that groups the nodes geometrically inside it. */
export interface FlowLoop {
  id: string;
  label: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface FlowGraph {
  nodes: FlowNode[];
  edges: FlowEdge[];
  loops?: FlowLoop[];
}

export interface FlowText {
  narrative: string;
  outline: string;
  mermaid: string;
  /** One-line stats summary, e.g. "5 nodes · 5 edges · 1 loop · 1 start, …". */
  summary: string;
}

const TYPE_WORD: Record<FlowNodeType, string> = {
  start: "Start",
  process: "Step",
  decision: "Decision",
  io: "Input/Output",
  end: "End",
};

const lbl = (n: FlowNode): string => (n.label && n.label.trim() ? n.label.trim() : "(unlabeled)");

/** A node belongs to a loop box when its center falls inside the box. */
export function nodeInLoop(n: FlowNode, loop: FlowLoop): boolean {
  const cx = n.x + (n.w ?? 130) / 2;
  const cy = n.y + (n.h ?? 54) / 2;
  return cx >= loop.x && cx <= loop.x + loop.w && cy >= loop.y && cy <= loop.y + loop.h;
}

interface Graph {
  byId: Record<string, FlowNode>;
  out: Record<string, FlowEdge[]>;
  indeg: Record<string, number>;
}

function buildGraph(g: FlowGraph): Graph {
  const byId: Record<string, FlowNode> = {};
  const out: Record<string, FlowEdge[]> = {};
  const indeg: Record<string, number> = {};
  for (const n of g.nodes) {
    byId[n.id] = n;
    out[n.id] = [];
    indeg[n.id] = 0;
  }
  for (const e of g.edges) {
    if (out[e.from] && byId[e.to]) out[e.from].push(e);
  }
  for (const e of g.edges) {
    if (indeg[e.to] != null && byId[e.from]) indeg[e.to]++;
  }
  return { byId, out, indeg };
}

function findStarts(g: FlowGraph, indeg: Record<string, number>): FlowNode[] {
  let s = g.nodes.filter((n) => n.type === "start");
  if (!s.length) s = g.nodes.filter((n) => indeg[n.id] === 0);
  if (!s.length && g.nodes.length) s = [g.nodes[0]];
  return s;
}

/** Number nodes by DFS pre-order from the entry points (1-based). */
function numberNodes(
  g: FlowGraph,
  out: Record<string, FlowEdge[]>,
  starts: FlowNode[],
): { num: Record<string, number>; order: string[] } {
  const num: Record<string, number> = {};
  const order: string[] = [];
  const seen = new Set<string>();
  function dfs(id: string) {
    if (seen.has(id)) return;
    seen.add(id);
    num[id] = order.length + 1;
    order.push(id);
    for (const e of out[id]) dfs(e.to);
  }
  starts.forEach((s) => dfs(s.id));
  g.nodes.forEach((n) => {
    if (!seen.has(n.id)) dfs(n.id);
  });
  return { num, order };
}

/** Mermaid label escaping — neutralizes characters that break node syntax. */
function mmEsc(s: string): string {
  return String(s)
    .replace(/"/g, "'")
    .replace(/[[\]{}()|]/g, " ");
}

/**
 * Generate the narrative / outline / mermaid renderings for a flow chart.
 * Pure and deterministic — safe to call on every edit.
 */
export function generateFlowText(graph: FlowGraph): FlowText {
  const loops = graph.loops ?? [];

  if (!graph.nodes.length) {
    return { narrative: "", outline: "", mermaid: "", summary: "Add nodes first." };
  }

  const { byId, out, indeg } = buildGraph(graph);
  const starts = findStarts(graph, indeg);
  const { num, order } = numberNodes(graph, out, starts);

  // ---- Loop containers: map each node to its innermost enclosing loop box ----
  const loopLabel = (L: FlowLoop) => (L.label && L.label.trim() ? L.label.trim() : "loop");
  function loopForNode(n: FlowNode): FlowLoop | null {
    let best: FlowLoop | null = null;
    let bestArea = Infinity;
    for (const L of loops) {
      if (nodeInLoop(n, L)) {
        const area = L.w * L.h;
        if (area < bestArea) {
          bestArea = area;
          best = L;
        }
      }
    }
    return best;
  }
  const loopOf: Record<string, FlowLoop | null> = {};
  const membersOf: Record<string, string[]> = {};
  loops.forEach((L) => {
    membersOf[L.id] = [];
  });
  graph.nodes.forEach((n) => {
    const L = loopForNode(n);
    if (L) {
      loopOf[n.id] = L;
      membersOf[L.id].push(n.id);
    }
  });

  // ---- Outline (numbered, always unambiguous) ----
  const outline: string[] = [];
  for (const id of order) {
    const n = byId[id];
    outline.push(`${num[id]}. [${TYPE_WORD[n.type]}] ${lbl(n)}`);
    const outs = out[id];
    if (!outs.length) {
      if (n.type !== "end") outline.push(`     ↳ (dead end — no outgoing connection)`);
    } else if (n.type === "decision") {
      for (const e of outs)
        outline.push(`     ↳ if “${e.label || "—"}” → step ${num[e.to]} (${lbl(byId[e.to])})`);
    } else {
      for (const e of outs)
        outline.push(
          `     ↳ ${e.label ? "“" + e.label + "” → " : "→ "}step ${num[e.to]} (${lbl(byId[e.to])})`,
        );
    }
  }
  if (loops.length) {
    outline.push("");
    outline.push("Loops:");
    for (const L of loops) {
      const mem = (membersOf[L.id] || []).map((id) => num[id]).sort((a, b) => a - b);
      outline.push(
        mem.length
          ? `  ↻ ${loopLabel(L)} — repeats steps ${mem.join(", ")}`
          : `  ↻ ${loopLabel(L)} — (empty: drag process nodes inside this box)`,
      );
    }
  }

  // ---- Narrative (loop-aware, indented prose with branch handling) ----
  const narr: string[] = [];
  const described = new Set<string>();
  const pad = (d: number) => "  ".repeat(d);
  function sentence(n: FlowNode): string {
    switch (n.type) {
      case "start":
        return `Begin${n.label ? `: ${lbl(n)}.` : "."}`;
      case "process":
        return `${lbl(n)}.`;
      case "io":
        return `Input/Output — ${lbl(n)}.`;
      case "decision":
        return `Decision — ${lbl(n)}`;
      case "end":
        return `End${n.label && n.label !== "End" ? `: ${lbl(n)}.` : "."}`;
    }
  }
  // walk the body of loop L; edges that leave L are deferred into `exits`
  function walkInLoop(id: string, depth: number, L: FlowLoop, exits: FlowEdge[]) {
    const n = byId[id];
    if (described.has(id)) {
      narr.push(`${pad(depth)}↪ (loop back to step ${num[id]}: ${lbl(n)})`);
      return;
    }
    described.add(id);
    narr.push(`${pad(depth)}${num[id]}. ${sentence(n)}`);
    const outs = out[id];
    if (n.type === "decision" && outs.length) {
      for (const e of outs) {
        if (loopOf[e.to] === L) {
          narr.push(`${pad(depth)}  • If ${e.label || "(branch)"}:`);
          walkInLoop(e.to, depth + 2, L, exits);
        } else {
          narr.push(
            `${pad(depth)}  • If ${e.label || "(branch)"}: exit loop → step ${num[e.to]} (${lbl(byId[e.to])})`,
          );
          exits.push(e);
        }
      }
    } else {
      const internal = outs.filter((e) => loopOf[e.to] === L);
      outs
        .filter((e) => loopOf[e.to] !== L)
        .forEach((e) => {
          narr.push(`${pad(depth)}  ↦ exit loop → step ${num[e.to]} (${lbl(byId[e.to])})`);
          exits.push(e);
        });
      if (internal.length === 1) {
        walkInLoop(internal[0].to, depth, L, exits);
      } else if (internal.length > 1) {
        for (const e of internal) {
          narr.push(`${pad(depth)}  • ${e.label ? e.label + ":" : "then:"}`);
          walkInLoop(e.to, depth + 2, L, exits);
        }
      }
    }
  }
  function walk(id: string, depth: number, ctxLoop: FlowLoop | null) {
    const n = byId[id];
    if (described.has(id)) {
      narr.push(`${pad(depth)}↪ (continue from step ${num[id]}: ${lbl(n)})`);
      return;
    }
    const L = loopOf[id] || null;
    if (L && L !== ctxLoop) {
      // entering a loop block
      narr.push(`${pad(depth)}↻ Repeat — ${loopLabel(L)}:`);
      const exits: FlowEdge[] = [];
      walkInLoop(id, depth + 1, L, exits);
      narr.push(`${pad(depth)}  ⟲ (repeat while the loop condition holds)`);
      const seenExit = new Set<string>();
      for (const e of exits) {
        // continue past the loop via its exits
        if (seenExit.has(e.to)) continue;
        seenExit.add(e.to);
        narr.push(`${pad(depth)}Then${e.label ? ` (${e.label})` : ""}:`);
        walk(e.to, depth, ctxLoop);
      }
      return;
    }
    described.add(id);
    narr.push(`${pad(depth)}${num[id]}. ${sentence(n)}`);
    const outs = out[id];
    if (n.type === "decision" && outs.length) {
      for (const e of outs) {
        narr.push(`${pad(depth)}  • If ${e.label || "(unlabeled branch)"}:`);
        walk(e.to, depth + 2, ctxLoop);
      }
    } else if (outs.length === 1) {
      walk(outs[0].to, depth, ctxLoop);
    } else if (outs.length > 1) {
      for (const e of outs) {
        narr.push(`${pad(depth)}  • ${e.label ? e.label + ":" : "then:"}`);
        walk(e.to, depth + 2, ctxLoop);
      }
    }
  }
  starts.forEach((s, i) => {
    if (starts.length > 1) narr.push(`Flow ${i + 1}:`);
    walk(s.id, 0, null);
    narr.push("");
  });
  // any unreachable components
  graph.nodes.forEach((n) => {
    if (!described.has(n.id)) {
      narr.push("Disconnected:");
      walk(n.id, 0, null);
      narr.push("");
    }
  });

  // ---- Mermaid (loop members wrapped in subgraphs) ----
  const shape: Record<FlowNodeType, (s: string) => string> = {
    start: (s) => `([${s}])`,
    end: (s) => `([${s}])`,
    process: (s) => `[${s}]`,
    decision: (s) => `{${s}}`,
    io: (s) => `[/${s}/]`,
  };
  const mid: Record<string, string> = {};
  let mi = 0;
  graph.nodes.forEach((n) => {
    mid[n.id] = "N" + mi++;
  });
  const decl = (n: FlowNode) => `${mid[n.id]}${shape[n.type](mmEsc(lbl(n)))}`;
  const mer: string[] = ["flowchart TD"];
  graph.nodes.forEach((n) => {
    if (!loopOf[n.id]) mer.push(`    ${decl(n)}`);
  });
  loops.forEach((L, idx) => {
    const mem = membersOf[L.id] || [];
    if (!mem.length) return;
    mer.push(`    subgraph LOOP${idx}["↻ ${mmEsc(loopLabel(L))}"]`);
    mem.forEach((id) => mer.push(`        ${decl(byId[id])}`));
    mer.push(`    end`);
  });
  graph.edges.forEach((e) => {
    if (!mid[e.from] || !mid[e.to]) return;
    mer.push(`    ${mid[e.from]} ${e.label ? `-- ${mmEsc(e.label)} -->` : "-->"} ${mid[e.to]}`);
  });

  const counts: Partial<Record<FlowNodeType, number>> = {};
  graph.nodes.forEach((n) => {
    counts[n.type] = (counts[n.type] || 0) + 1;
  });
  const summary =
    `${graph.nodes.length} nodes · ${graph.edges.length} edges` +
    (loops.length ? ` · ${loops.length} loop${loops.length > 1 ? "s" : ""}` : "") +
    " · " +
    Object.entries(counts)
      .map(([k, v]) => `${v} ${k}`)
      .join(", ");

  return {
    narrative: narr.join("\n").trim(),
    outline: outline.join("\n"),
    mermaid: mer.join("\n"),
    summary,
  };
}
