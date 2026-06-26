/**
 * Flow builder (`/jobs/flow/:jobId`).
 *
 * An interactive node/edge canvas (React Flow, via the shared `Canvas`
 * wrapper) for sketching a flow chart, plus a live output panel that turns the
 * chart into three textual renderings — Narrative prose, a numbered Outline,
 * and a Mermaid `flowchart TD` definition (rendered inline via the same
 * Streamdown pipeline the chat uses).
 *
 * The chart is loaded from / saved to a *job* (see {@link jobsStore}) keyed by
 * the `:jobId` route param — this is the editor reached from the Jobs page.
 * The drawing surface is React state; text generation is delegated to the
 * pure, deterministic {@link generateFlowText} in `@/lib/flowToText`. The page
 * maps React Flow's node/edge objects ↔ the generator's `FlowGraph` shape
 * (notably: loop containers are plain `loop` nodes here, and membership is the
 * generator's geometric "node center inside the box" test — independent of
 * React Flow parent/child relationships).
 *
 * Node kinds (start / process / decision / io / end) match the prototype's
 * palette; double-click a node to rename it, drag from a node's right handle to
 * connect, and connecting *from a decision* prompts for the branch label.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  addEdge,
  Handle,
  MarkerType,
  NodeResizer,
  Position,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
  type NodeTypes,
} from "@xyflow/react";
import { ArrowLeftIcon, CopyIcon, PlayIcon, PlusIcon, SaveIcon, Trash2Icon } from "lucide-react";
import { Canvas } from "@/components/ai-elements/canvas";
import { Controls } from "@/components/ai-elements/controls";
import { Panel } from "@/components/ai-elements/panel";
import { MessageResponse } from "@/components/ai-elements/message";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Link, useNavigate, useParams } from "@/lib/routing";
import { runJob, updateJob, useJob } from "@/lib/jobsStore";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import {
  generateFlowText,
  type FlowEdge,
  type FlowGraph,
  type FlowLoop,
  type FlowNode,
  type FlowNodeType,
} from "@/lib/flowToText";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Node data + per-kind presentation
// ---------------------------------------------------------------------------
interface FlowNodeData {
  kind: FlowNodeType;
  label: string;
  [key: string]: unknown;
}

const KIND_META: Record<FlowNodeType, { tag: string; defLabel: string; className: string }> = {
  start: { tag: "Start", defLabel: "Start", className: "border-emerald-500 bg-emerald-500/10" },
  process: { tag: "Process", defLabel: "Do something", className: "border-blue-500 bg-blue-500/10" },
  decision: {
    tag: "Decision",
    defLabel: "Condition?",
    className: "border-amber-500 bg-amber-500/10",
  },
  io: { tag: "Input/Output", defLabel: "Input / Output", className: "border-purple-500 bg-purple-500/10" },
  end: { tag: "End", defLabel: "End", className: "border-red-500 bg-red-500/10" },
};

const PALETTE: FlowNodeType[] = ["start", "process", "decision", "io", "end"];

let idCounter = 1;
const uid = (p: string) => `${p}_${idCounter++}`;

// ---------------------------------------------------------------------------
// Custom node renderers
// ---------------------------------------------------------------------------
function FlowNodeView({ data, selected }: NodeProps) {
  const d = data as FlowNodeData;
  const meta = KIND_META[d.kind];
  const isDecision = d.kind === "decision";
  return (
    <div
      className={cn(
        "flex min-h-[54px] min-w-[120px] max-w-[280px] flex-col items-center justify-center rounded-md border-2 px-3.5 py-2.5 text-center text-sm shadow-md transition-shadow",
        meta.className,
        selected && "ring-2 ring-ring ring-offset-2 ring-offset-background",
        isDecision && "rotate-0", // diamonds rendered as rounded rects for legibility
      )}
    >
      <Handle type="target" position={Position.Left} className="!size-2.5 !bg-foreground/40" />
      <span className="mb-0.5 text-[9.5px] font-bold tracking-wide text-muted-foreground uppercase">
        {meta.tag}
      </span>
      <span className="break-words">{d.label || meta.defLabel}</span>
      <Handle type="source" position={Position.Right} className="!size-2.5 !bg-foreground/40" />
    </div>
  );
}

function LoopNodeView({ data, selected }: NodeProps) {
  const d = data as FlowNodeData;
  return (
    <div
      className={cn(
        "size-full rounded-xl border-2 border-dashed border-primary/60 bg-primary/5",
        selected && "border-primary bg-primary/10",
      )}
    >
      <NodeResizer minWidth={160} minHeight={120} isVisible={selected} />
      <span className="absolute -top-3 left-3 rounded-md bg-primary px-2.5 py-0.5 text-[11px] font-bold text-primary-foreground">
        ↻ {d.label || "loop"}
      </span>
    </div>
  );
}

const nodeTypes: NodeTypes = { flow: FlowNodeView, loop: LoopNodeView };

// ---------------------------------------------------------------------------
// React Flow state → generator FlowGraph
// ---------------------------------------------------------------------------
function toFlowGraph(nodes: Node[], edges: Edge[]): FlowGraph {
  const flowNodes: FlowNode[] = [];
  const loops: FlowLoop[] = [];
  for (const n of nodes) {
    const d = n.data as FlowNodeData;
    const w = n.measured?.width ?? (typeof n.width === "number" ? n.width : undefined);
    const h = n.measured?.height ?? (typeof n.height === "number" ? n.height : undefined);
    if (n.type === "loop") {
      loops.push({
        id: n.id,
        label: d.label || "loop",
        x: n.position.x,
        y: n.position.y,
        w: w ?? 320,
        h: h ?? 200,
      });
    } else {
      flowNodes.push({
        id: n.id,
        type: d.kind,
        label: d.label,
        x: n.position.x,
        y: n.position.y,
        w,
        h,
      });
    }
  }
  const flowEdges: FlowEdge[] = edges.map((e) => ({
    id: e.id,
    from: e.source,
    to: e.target,
    label: typeof e.label === "string" ? e.label : undefined,
  }));
  return { nodes: flowNodes, edges: flowEdges, loops };
}

// ---------------------------------------------------------------------------
// generator FlowGraph → React Flow state (loading a saved job)
// ---------------------------------------------------------------------------
function fromFlowGraph(graph: FlowGraph): { nodes: Node[]; edges: Edge[] } {
  const loopNodes: Node[] = (graph.loops ?? []).map((L) => ({
    id: L.id,
    type: "loop",
    position: { x: L.x, y: L.y },
    data: { kind: "process", label: L.label },
    style: { width: L.w, height: L.h },
    zIndex: -1,
  }));
  const flowNodes: Node[] = graph.nodes.map((n) => ({
    id: n.id,
    type: "flow",
    position: { x: n.x, y: n.y },
    data: { kind: n.type, label: n.label },
    ...(n.w && n.h ? { style: { width: n.w, height: n.h } } : {}),
  }));
  // Loop boxes first so they render behind the nodes.
  const nodes = [...loopNodes, ...flowNodes];
  const edges: Edge[] = graph.edges.map((e) => ({
    id: e.id,
    source: e.from,
    target: e.to,
    ...(e.label ? { label: e.label } : {}),
    markerEnd: { type: MarkerType.ArrowClosed },
  }));
  return { nodes, edges };
}

// ---------------------------------------------------------------------------
// Example chart (the prototype's "batch processing" loop)
// ---------------------------------------------------------------------------
function exampleNodes(): Node[] {
  return [
    { id: "n1", type: "flow", position: { x: 360, y: 40 }, data: { kind: "start", label: "Start batch" } },
    { id: "L1", type: "loop", position: { x: 300, y: 150 }, data: { kind: "process", label: "while orders remain" }, style: { width: 360, height: 320 }, zIndex: -1, selectable: true, draggable: true },
    { id: "n2", type: "flow", position: { x: 360, y: 190 }, data: { kind: "process", label: "Fetch next order" } },
    { id: "n3", type: "flow", position: { x: 360, y: 300 }, data: { kind: "process", label: "Charge & ship" } },
    { id: "n4", type: "flow", position: { x: 360, y: 400 }, data: { kind: "decision", label: "More orders?" } },
    { id: "n5", type: "flow", position: { x: 720, y: 420 }, data: { kind: "end", label: "Batch done" } },
  ];
}
function exampleEdges(): Edge[] {
  const arrow = { markerEnd: { type: MarkerType.ArrowClosed } };
  return [
    { id: "e1", source: "n1", target: "n2", ...arrow },
    { id: "e2", source: "n2", target: "n3", ...arrow },
    { id: "e3", source: "n3", target: "n4", ...arrow },
    { id: "e4", source: "n4", target: "n2", label: "Yes", ...arrow },
    { id: "e5", source: "n4", target: "n5", label: "No", ...arrow },
  ];
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
type OutputTab = "narrative" | "outline" | "mermaid";

export function FlowchartPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();
  const { job, loading: jobLoading } = useJob(jobId);

  // Initial canvas: the saved job's graph if it has one, else the example
  // chart as a starting point. Computed once per mount (the builder owns the
  // working copy; Save writes it back to the job).
  const hasGraph = !!job && (job.graph.nodes.length > 0 || (job.graph.loops?.length ?? 0) > 0);
  const initial = useMemo(() => {
    if (job && hasGraph) {
      return fromFlowGraph(job.graph);
    }
    return { nodes: exampleNodes(), edges: exampleEdges() };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, job?.id, hasGraph]);

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>(initial.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(initial.edges);
  const [tab, setTab] = useState<OutputTab>("narrative");
  const [copied, setCopied] = useState(false);
  const [saved, setSaved] = useState(false);

  // Re-seed the canvas when navigating between jobs or when the job's graph
  // finishes loading from the API (the initial fetch resolves after mount).
  useEffect(() => {
    setNodes(initial.nodes);
    setEdges(initial.edges);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, job?.id, hasGraph]);

  // Live regeneration — the generator is pure & cheap, so recompute on any
  // node/edge change rather than gating behind a "Generate" button.
  const graph = useMemo(() => toFlowGraph(nodes, edges), [nodes, edges]);
  const result = useMemo(() => generateFlowText(graph), [graph]);

  const onSave = useCallback(() => {
    if (!jobId) return;
    void updateJob(jobId, { graph });
    setSaved(true);
    setTimeout(() => setSaved(false), 1200);
  }, [jobId, graph]);

  // Agent picker: the job runs as the chosen agent (its narrative becomes that
  // agent's first prompt). Persisted on the job via updateJob.
  const { data: agents } = useAvailableAgents();
  const onPickAgent = useCallback(
    (agentId: string) => {
      if (!jobId) return;
      void updateJob(jobId, { agentId: agentId || null });
    },
    [jobId],
  );

  const [running, setRunning] = useState(false);
  const onRun = useCallback(async () => {
    if (!jobId) return;
    // Persist the latest canvas first so the run uses the on-screen flow.
    await updateJob(jobId, { graph });
    setRunning(true);
    try {
      const run = await runJob(jobId);
      if (run.sessionId) navigate(`/c/${run.sessionId}`);
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Failed to run job");
    } finally {
      setRunning(false);
    }
  }, [jobId, graph, navigate]);

  const addNode = useCallback(
    (kind: FlowNodeType) => {
      const id = uid(kind);
      setNodes((nds) => [
        ...nds,
        {
          id,
          type: "flow",
          position: { x: 120 + (nds.length % 6) * 30, y: 100 + (nds.length % 6) * 30 },
          data: { kind, label: KIND_META[kind].defLabel },
        },
      ]);
    },
    [setNodes],
  );

  const addLoop = useCallback(() => {
    const label = window.prompt(
      "Loop label (the repeat condition, e.g. 'while not empty'):",
      "for each item",
    );
    if (label === null) return;
    setNodes((nds) => [
      ...nds,
      {
        id: uid("loop"),
        type: "loop",
        position: { x: 100, y: 100 },
        data: { kind: "process", label: label.trim() || "loop" },
        style: { width: 320, height: 200 },
        zIndex: -1,
      },
    ]);
  }, [setNodes]);

  // Connecting from a decision prompts for the branch label (Yes/No suggested).
  const onConnect = useCallback(
    (conn: Connection) => {
      const src = nodes.find((n) => n.id === conn.source);
      let label: string | undefined;
      if (src && (src.data as FlowNodeData).kind === "decision") {
        const existing = edges.filter((e) => e.source === conn.source).length;
        const suggest = existing === 0 ? "Yes" : existing === 1 ? "No" : "";
        const v = window.prompt("Branch label for this decision path:", suggest);
        label = v ? v.trim() : undefined;
      }
      setEdges((eds) =>
        addEdge(
          { ...conn, label, markerEnd: { type: MarkerType.ArrowClosed } },
          eds,
        ),
      );
    },
    [nodes, edges, setEdges],
  );

  // Double-click a node to rename it (loop boxes rename their condition).
  const onNodeDoubleClick = useCallback(
    (_e: React.MouseEvent, node: Node) => {
      const d = node.data as FlowNodeData;
      const v = window.prompt("Label:", d.label ?? "");
      if (v === null) return;
      setNodes((nds) =>
        nds.map((n) => (n.id === node.id ? { ...n, data: { ...n.data, label: v.trim() } } : n)),
      );
    },
    [setNodes],
  );

  const loadExample = useCallback(() => {
    setNodes(exampleNodes());
    setEdges(exampleEdges());
  }, [setNodes, setEdges]);

  const clear = useCallback(() => {
    if (nodes.length && !window.confirm("Clear the whole flow chart?")) return;
    setNodes([]);
    setEdges([]);
  }, [nodes.length, setNodes, setEdges]);

  const copyOutput = useCallback(() => {
    const text = tab === "mermaid" ? result.mermaid : tab === "outline" ? result.outline : result.narrative;
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    });
  }, [tab, result]);

  const hasContent = nodes.length > 0;

  // A jobId that doesn't resolve (stale link / deleted job): send the user
  // back to the list rather than silently editing an orphaned chart. Wait for
  // the API fetch to settle first so the loading window doesn't flash this.
  if (jobId && !job && !jobLoading) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
        <p className="text-sm font-medium">This flow no longer exists.</p>
        <Button variant="outline" onClick={() => navigate("/jobs")}>
          <ArrowLeftIcon className="size-4" /> Back to Jobs
        </Button>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Builder header — back to Jobs, job name, save */}
      <div className="flex items-center gap-3 border-b border-border px-4 py-2">
        <Button asChild variant="ghost" size="sm">
          <Link to="/jobs">
            <ArrowLeftIcon className="size-4" /> Jobs
          </Link>
        </Button>
        <span className="min-w-0 flex-1 truncate text-sm font-medium">
          {job?.name ?? "Flow builder"}
        </span>
        <select
          aria-label="Run as agent"
          data-testid="job-agent-select"
          value={job?.agentId ?? ""}
          onChange={(e) => onPickAgent(e.target.value)}
          disabled={!jobId}
          className="h-8 rounded-md border border-input bg-background px-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <option value="">Pick an agent…</option>
          {(agents ?? []).map((a) => (
            <option key={a.id} value={a.id}>
              {a.display_name}
            </option>
          ))}
        </select>
        <Button variant="outline" size="sm" onClick={onSave} disabled={!jobId}>
          <SaveIcon className="size-3.5" /> {saved ? "Saved!" : "Save"}
        </Button>
        <Button
          size="sm"
          onClick={onRun}
          disabled={!jobId || !job?.agentId || running}
          data-testid="job-run-button"
        >
          <PlayIcon className="size-3.5" /> {running ? "Running…" : "Run now"}
        </Button>
      </div>

      <div className="flex min-h-0 flex-1">
      {/* Canvas */}
      <div className="relative min-w-0 flex-1">
        <Canvas
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeDoubleClick={onNodeDoubleClick}
        >
          <Controls />
          <Panel position="top-left">
            <div className="flex flex-wrap items-center gap-1">
              {PALETTE.map((kind) => (
                <Button key={kind} variant="ghost" size="sm" onClick={() => addNode(kind)}>
                  <span className={cn("mr-1.5 inline-block size-2.5 rounded-sm border", KIND_META[kind].className)} />
                  {KIND_META[kind].tag}
                </Button>
              ))}
              <span className="mx-1 h-5 w-px bg-border" />
              <Button variant="ghost" size="sm" onClick={addLoop}>
                <PlusIcon className="size-3.5" /> Loop
              </Button>
              <Button variant="ghost" size="sm" onClick={loadExample}>
                Example
              </Button>
              <Button variant="ghost" size="sm" onClick={clear}>
                <Trash2Icon className="size-3.5" /> Clear
              </Button>
            </div>
          </Panel>
        </Canvas>
      </div>

      {/* Output panel */}
      <aside className="flex w-[420px] min-w-[300px] flex-col border-l border-border bg-card">
        <Tabs
          value={tab}
          onValueChange={(v) => setTab(v as OutputTab)}
          className="flex min-h-0 flex-1 flex-col gap-0"
        >
          <div className="flex items-center gap-2 border-b border-border px-3 py-2">
            <TabsList variant="line" className="flex-1">
              <TabsTrigger value="narrative">Narrative</TabsTrigger>
              <TabsTrigger value="outline">Outline</TabsTrigger>
              <TabsTrigger value="mermaid">Mermaid</TabsTrigger>
            </TabsList>
            <Button variant="outline" size="sm" onClick={copyOutput} disabled={!hasContent}>
              <CopyIcon className="size-3.5" /> {copied ? "Copied!" : "Copy"}
            </Button>
          </div>

          <div className="border-b border-border px-4 py-2 text-xs text-muted-foreground">
            {hasContent ? result.summary : "Add nodes from the toolbar to build a flow chart."}
          </div>

          <div className="min-h-0 flex-1 overflow-auto">
            <TabsContent value="narrative" className="p-4">
              {result.narrative ? (
                <pre className="font-sans text-sm leading-relaxed whitespace-pre-wrap">
                  {result.narrative}
                </pre>
              ) : (
                <p className="text-sm text-muted-foreground italic">Nothing to show yet.</p>
              )}
            </TabsContent>
            <TabsContent value="outline" className="p-4">
              {result.outline ? (
                <pre className="font-mono text-xs leading-relaxed whitespace-pre-wrap">
                  {result.outline}
                </pre>
              ) : (
                <p className="text-sm text-muted-foreground italic">Nothing to show yet.</p>
              )}
            </TabsContent>
            <TabsContent value="mermaid" className="flex flex-col gap-3 p-4">
              {result.mermaid ? (
                <>
                  {/* Rendered diagram (Streamdown's mermaid plugin). */}
                  <MessageResponse>{"```mermaid\n" + result.mermaid + "\n```"}</MessageResponse>
                  {/* Raw source for copy/paste. */}
                  <pre className="rounded-md border border-border bg-muted/40 p-3 font-mono text-xs whitespace-pre-wrap">
                    {result.mermaid}
                  </pre>
                </>
              ) : (
                <p className="text-sm text-muted-foreground italic">Nothing to show yet.</p>
              )}
            </TabsContent>
          </div>
        </Tabs>
      </aside>
      </div>
    </div>
  );
}
