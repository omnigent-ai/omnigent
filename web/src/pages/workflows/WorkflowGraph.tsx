import { useMemo, useState } from "react";
import {
  Background,
  BaseEdge,
  Controls,
  getSmoothStepPath,
  Handle,
  MarkerType,
  Panel,
  Position,
  ReactFlow,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import { GitBranchIcon, LayoutGridIcon, ShieldCheckIcon, Trash2Icon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { WorkflowRun, WorkflowTask } from "@/lib/workflowsApi";
import { canConnect, dependencyDetours, layoutTasks, TASK_WIDTH } from "./workflowDesign";
import "@xyflow/react/dist/style.css";

type TaskNode = Node<{ task: WorkflowTask; status?: string; round?: number }, "task">;
type Dependency = Edge<{ detourX?: number }, "dependency">;
export const statusColor: Record<string, string> = {
  succeeded: "text-emerald-600 dark:text-emerald-400",
  failed: "text-destructive",
  running: "text-blue-600 dark:text-blue-400",
  waiting: "text-amber-600 dark:text-amber-400",
  interrupted: "text-amber-600 dark:text-amber-400",
};

function TaskCard({ data, selected, isConnectable }: NodeProps<TaskNode>) {
  return (
    <div
      className={cn(
        "rounded-xl border-2 bg-card p-3 text-card-foreground shadow-sm",
        selected ? "border-primary" : "border-border",
      )}
      style={{ width: TASK_WIDTH }}
    >
      <Handle
        type="target"
        position={Position.Top}
        isConnectable={isConnectable}
        aria-label={`Input for ${data.task.name ?? data.task.id}`}
        style={{ width: 12, height: 12, background: "var(--primary)" }}
      />
      <div className="flex items-center gap-2">
        <GitBranchIcon className="size-4 shrink-0 text-muted-foreground" />
        <strong className="truncate text-sm">{data.task.name || data.task.id}</strong>
      </div>
      <p className="my-2 line-clamp-2 text-xs text-muted-foreground">
        {data.task.goal || "Describe this task’s goal"}
      </p>
      {data.task.gates.length > 0 && (
        <span className="flex items-center gap-1 text-xs text-muted-foreground">
          <ShieldCheckIcon className="size-3" />
          Review
        </span>
      )}
      {data.status && (
        <p className={cn("mt-2 border-t pt-2 text-xs", statusColor[data.status])}>
          {data.status} · Round {data.round}
        </p>
      )}
      <Handle
        type="source"
        position={Position.Bottom}
        isConnectable={isConnectable}
        aria-label={`Output from ${data.task.name ?? data.task.id}`}
        style={{ width: 12, height: 12, background: "var(--primary)" }}
      />
    </div>
  );
}
const nodeTypes = { task: TaskCard };

function DependencyArrow(props: EdgeProps<Dependency>) {
  const { sourceX, sourceY, targetX, targetY, data } = props;
  const path =
    data?.detourX === undefined
      ? getSmoothStepPath(props)[0]
      : `M ${sourceX} ${sourceY} V ${sourceY + 12} H ${data.detourX} V ${targetY - 12} H ${targetX} V ${targetY}`;
  return (
    <BaseEdge
      id={props.id}
      path={path}
      markerEnd={props.markerEnd}
      style={props.style}
      interactionWidth={props.interactionWidth}
    />
  );
}
const edgeTypes = { dependency: DependencyArrow };

export function WorkflowGraph({
  tasks,
  selectedId,
  onSelect,
  onConnect,
  onDisconnect,
  states,
}: {
  tasks: WorkflowTask[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onConnect?: (source: string, target: string) => void;
  onDisconnect?: (source: string, target: string) => void;
  states?: WorkflowRun["nodes"];
}) {
  const layout = useMemo(() => layoutTasks(tasks), [tasks]);
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});
  const [instance, setInstance] = useState<ReactFlowInstance<TaskNode> | null>(null);
  const [selectedEdges, setSelectedEdges] = useState<Set<string>>(new Set());
  const nodes: TaskNode[] = tasks.map((task) => ({
    id: task.id,
    type: "task",
    deletable: false,
    position: positions[task.id] ?? layout[task.id],
    selected: task.id === selectedId,
    ariaRole: "button",
    ariaLabel: `${task.name || task.id}${states ? ` ${states[task.id]?.status ?? "pending"}` : ""}`,
    data: { task, status: states?.[task.id]?.status, round: states?.[task.id]?.round },
  }));
  const detours = dependencyDetours(
    tasks,
    Object.fromEntries(nodes.map((node) => [node.id, node.position])),
  );
  const edges = tasks.flatMap((task) =>
    task.depends_on.map((source) => ({
      id: `${source}->${task.id}`,
      source,
      target: task.id,
      type: "dependency",
      data: { detourX: detours[`${source}->${task.id}`] },
      selected: selectedEdges.has(`${source}->${task.id}`),
      interactionWidth: 28,
      ariaLabel: `${tasks.find((node) => node.id === source)?.name || source} → ${task.name || task.id}`,
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: selectedEdges.has(`${source}->${task.id}`)
          ? "var(--primary)"
          : "var(--muted-foreground)",
        width: 20,
        height: 20,
      },
      style: {
        stroke: selectedEdges.has(`${source}->${task.id}`)
          ? "var(--primary)"
          : "var(--muted-foreground)",
        strokeWidth: selectedEdges.has(`${source}->${task.id}`) ? 3 : 2,
        cursor: onDisconnect ? "pointer" : undefined,
      },
      animated: states?.[task.id]?.status === "running",
    })),
  );
  const selectedConnections = edges.filter((edge) => edge.selected);
  return (
    <div
      role="region"
      aria-label="Workflow DAG"
      className="relative h-[520px] min-w-0 overflow-hidden rounded-xl border bg-muted/20 sm:h-[620px]"
    >
      <ReactFlow<TaskNode>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onInit={setInstance}
        onNodeClick={(_event, node) => onSelect(node.id)}
        onNodeDragStop={(_event, node) =>
          setPositions((current) => ({ ...current, [node.id]: node.position }))
        }
        onNodesChange={(changes) => {
          for (const change of changes)
            if (change.type === "select" && change.selected) onSelect(change.id);
          if (!changes.some((change) => change.type === "position")) return;
          setPositions((current) => {
            const updated = { ...current };
            for (const change of changes)
              if (change.type === "position" && change.position)
                updated[change.id] = change.position;
            return updated;
          });
        }}
        onEdgesChange={(changes) =>
          setSelectedEdges((current) => {
            const updated = new Set(current);
            for (const change of changes)
              if (change.type === "select") {
                if (change.selected) updated.add(change.id);
                else updated.delete(change.id);
              } else if (change.type === "remove") {
                updated.delete(change.id);
              }
            return updated;
          })
        }
        onConnect={(connection) => {
          if (onConnect && canConnect(tasks, connection.source, connection.target))
            onConnect(connection.source, connection.target);
        }}
        isValidConnection={(connection) =>
          Boolean(
            connection.source &&
            connection.target &&
            canConnect(tasks, connection.source, connection.target),
          )
        }
        onEdgesDelete={(deleted) =>
          deleted.forEach((edge) => onDisconnect?.(edge.source, edge.target))
        }
        nodesDraggable={Boolean(onConnect)}
        nodesConnectable={Boolean(onConnect)}
        edgesReconnectable={false}
        edgesFocusable={Boolean(onDisconnect)}
        deleteKeyCode={onDisconnect ? ["Backspace", "Delete"] : null}
        fitView
        fitViewOptions={{ padding: 0.18 }}
        minZoom={0.25}
        maxZoom={1.5}
        zoomOnDoubleClick={false}
      >
        <Background gap={20} color="var(--border)" />
        <Controls showInteractive={false} />
        {onDisconnect && (
          <Panel
            position="bottom-right"
            className="max-w-[calc(100%_-_88px)] rounded-lg border bg-background p-3 shadow-sm"
          >
            {selectedConnections.length ? (
              <div className="w-56 max-w-full" role="region" aria-label="Selected arrows">
                <p className="text-xs font-medium">
                  Selected {selectedConnections.length === 1 ? "arrow" : "arrows"}
                </p>
                <ul className="mt-1 max-h-24 overflow-y-auto text-xs text-muted-foreground">
                  {selectedConnections.map((edge) => (
                    <li key={edge.id} className="break-words">
                      {edge.ariaLabel}
                    </li>
                  ))}
                </ul>
                <Button
                  size="sm"
                  variant="destructive"
                  className="mt-3 h-11 w-full sm:h-8"
                  onClick={() => void instance?.deleteElements({ edges: selectedConnections })}
                >
                  <Trash2Icon className="size-3" />
                  {selectedConnections.length === 1
                    ? "Remove arrow"
                    : `Remove ${selectedConnections.length} arrows`}
                </Button>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">Select an arrow to remove it.</p>
            )}
          </Panel>
        )}
        <Panel position="top-left">
          <span className="rounded-md border bg-background/95 px-2 py-1 text-xs text-muted-foreground">
            {tasks.length} tasks · {edges.length} dependencies
          </span>
        </Panel>
        <Panel position="top-right">
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setPositions({});
              requestAnimationFrame(() => {
                void instance?.fitView({ padding: 0.18, duration: 200 });
              });
            }}
          >
            <LayoutGridIcon className="size-3" />
            Arrange
          </Button>
        </Panel>
      </ReactFlow>
    </div>
  );
}
