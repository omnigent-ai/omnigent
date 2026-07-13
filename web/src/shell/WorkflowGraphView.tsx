import { useMemo } from "react";
import type { Node, NodeProps, NodeTypes } from "@xyflow/react";
import { Background, Handle, Position, ReactFlow } from "@xyflow/react";
import { Link, useLocation } from "@/lib/routing";
import { Badge } from "@/components/ui/badge";
import { RunningDot } from "@/components/RunningDot";
import { useWorkflows, type WorkflowNodeState } from "@/hooks/useWorkflows";
import { cn } from "@/lib/utils";
import {
  buildWorkflowGraphLayout,
  WORKFLOW_NODE_WIDTH,
  type WorkflowGraphNodeData,
} from "./workflowGraphLayout";

import "@xyflow/react/dist/style.css";

const STATE_STYLE: Record<WorkflowNodeState, string> = {
  pending: "border-muted-foreground/25 bg-muted/20",
  ready: "border-info/50 bg-info/5",
  running: "border-brand-accent bg-brand-accent/5",
  succeeded: "border-success/50 bg-success/5",
  failed: "border-destructive bg-destructive/5",
  blocked: "border-warning bg-warning/5",
  cancelled: "border-muted-foreground/30 bg-muted/30",
};

function WorkflowNode({ data }: NodeProps<Node<WorkflowGraphNodeData>>) {
  const { node } = data;
  const location = useLocation();
  const search = useMemo(() => {
    const params = new URLSearchParams(location.search);
    for (const key of ["file", "diff", "comment", "view"]) params.delete(key);
    const value = params.toString();
    return value ? `?${value}` : "";
  }, [location.search]);
  const card = (
    <div
      className={cn(
        "rounded-lg border px-3 py-2 shadow-sm transition-shadow",
        node.child_session_id && "cursor-pointer hover:shadow-md",
        STATE_STYLE[node.state],
      )}
      style={{ width: WORKFLOW_NODE_WIDTH }}
    >
      <div className="flex items-center gap-1.5">
        <span className="truncate text-xs font-medium">{node.title}</span>
        <span className="flex-1" />
        {node.state === "running" ? (
          <RunningDot />
        ) : (
          <span className="size-2 rounded-full bg-current opacity-50" />
        )}
      </div>
      <div className="mt-1 flex items-center gap-1.5 text-[10px] text-muted-foreground">
        <span>{node.agent}</span>
        <span>·</span>
        <span className="capitalize">{node.state}</span>
        {node.attempt_count > 0 && <span>· try {node.attempt_count}</span>}
      </div>
      {node.error && <p className="mt-1 truncate text-[10px] text-destructive">{node.error}</p>}
    </div>
  );
  return (
    <>
      <Handle type="target" position={Position.Left} className="!size-1.5 !border-0" />
      {node.child_session_id ? (
        <Link to={{ pathname: `/c/${node.child_session_id}`, search }}>{card}</Link>
      ) : (
        card
      )}
      <Handle type="source" position={Position.Right} className="!size-1.5 !border-0" />
    </>
  );
}

const nodeTypes: NodeTypes = { workflow: WorkflowNode };

export function WorkflowGraphView({ rootSessionId }: { rootSessionId: string }) {
  const { workflows, isLoading, error } = useWorkflows(rootSessionId, 15_000);
  const workflow = useMemo(
    () => [...workflows].sort((a, b) => b.updated_at - a.updated_at)[0],
    [workflows],
  );
  const layout = useMemo(
    () => (workflow ? buildWorkflowGraphLayout(workflow) : { nodes: [], edges: [] }),
    [workflow],
  );

  if (isLoading && !workflow)
    return <div className="p-4 text-xs text-muted-foreground">Loading…</div>;
  if (error || !workflow)
    return <div className="p-4 text-xs text-muted-foreground">No workflow DAG is available.</div>;

  return (
    <div className="flex h-full min-h-0 flex-col bg-card">
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <span className="min-w-0 flex-1 truncate text-xs font-medium">{workflow.name}</span>
        <Badge variant="outline" className="capitalize text-[10px]">
          {workflow.status}
        </Badge>
      </div>
      {workflow.blocked_reason && (
        <div className="border-b bg-warning/5 px-3 py-2 text-[10px] text-warning">
          {workflow.blocked_reason}
        </div>
      )}
      <div className="min-h-0 flex-1">
        <ReactFlow
          nodes={layout.nodes}
          edges={layout.edges}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.25 }}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          zoomOnDoubleClick={false}
          minZoom={0.35}
          maxZoom={1.5}
          proOptions={{ hideAttribution: true }}
        >
          <Background bgColor="var(--card)" />
        </ReactFlow>
      </div>
    </div>
  );
}
