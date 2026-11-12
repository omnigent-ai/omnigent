import { useMemo } from "react";
import {
  Background,
  Handle,
  type Node,
  type NodeProps,
  type NodeTypes,
  Position,
  ReactFlow,
} from "@xyflow/react";

import { GraphZoomControls } from "@/components/GraphZoomControls";
import {
  layoutWorkflowSteps,
  STEP_NODE_HEIGHT,
  STEP_NODE_WIDTH,
  type StepNodeData,
  type WorkflowStep,
} from "@/lib/workflowGraphLayout";

import "@xyflow/react/dist/style.css";

const HANDLE_CLASS = "!h-1.5 !w-1.5 !border-0 !bg-muted-foreground/40";
// The graph grows with its rows, up to a height that still leaves the runs in view.
const MIN_HEIGHT = 160;
const MAX_HEIGHT = 480;

function StepNode({ data }: NodeProps<Node<StepNodeData>>) {
  return (
    <>
      <Handle type="target" position={Position.Top} className={HANDLE_CLASS} />
      <div
        className="rounded-lg border border-muted-foreground/30 bg-card px-3 py-2 shadow-sm"
        style={{ width: STEP_NODE_WIDTH }}
        title={data.goal ?? undefined}
        data-testid="workflow-step-node"
      >
        <p className="truncate text-sm font-medium leading-tight">{data.name}</p>
        {data.goal && (
          <p className="mt-1 line-clamp-2 text-xs leading-snug text-muted-foreground">
            {data.goal}
          </p>
        )}
      </div>
      <Handle type="source" position={Position.Bottom} className={HANDLE_CLASS} />
    </>
  );
}

const nodeTypes: NodeTypes = { step: StepNode };

/**
 * A saved workflow's steps as a read-only graph, top to bottom by dependency.
 * Drag to pan; the corner buttons zoom. Scrolling over it scrolls the page.
 */
export function WorkflowStepsGraph({ steps }: { steps: readonly WorkflowStep[] }) {
  const { nodes, edges, rowCount } = useMemo(() => layoutWorkflowSteps(steps), [steps]);
  const height = Math.min(
    MAX_HEIGHT,
    Math.max(MIN_HEIGHT, rowCount * (STEP_NODE_HEIGHT + 40) + 64),
  );
  return (
    <div
      className="relative overflow-hidden rounded-xl border border-border bg-card"
      style={{ height }}
      data-testid="workflow-steps-graph"
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
        panOnDrag
        panOnScroll={false}
        zoomOnScroll={false}
        preventScrolling={false}
        zoomOnDoubleClick={false}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        proOptions={{ hideAttribution: true }}
        minZoom={0.2}
        maxZoom={2}
      >
        <Background bgColor="var(--card)" />
        <GraphZoomControls />
      </ReactFlow>
    </div>
  );
}
