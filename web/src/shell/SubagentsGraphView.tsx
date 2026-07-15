import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { NodeTypes, NodeProps, Node } from "@xyflow/react";
import { ReactFlow, Background, Position, Handle } from "@xyflow/react";
import { Link, useLocation } from "@/lib/routing";
import { RunningDot } from "@/components/RunningDot";
import { Badge } from "@/components/ui/badge";
import { MAX_TREE_DEPTH, useChildSessions, type ChildSessionInfo } from "@/hooks/useChildSessions";
import { useSession } from "@/hooks/useSession";
import { cn } from "@/lib/utils";
import { nativeCodingAgentForWrapper, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";
import {
  buildGraphLayout,
  NODE_WIDTH,
  type AgentActivity,
  type AgentNodeData,
} from "./subagentGraphLayout";

import "@xyflow/react/dist/style.css";

const ACTIVITY_COLORS: Record<AgentActivity, { border: string; bg: string; dot: string }> = {
  working: { border: "border-brand-accent", bg: "bg-brand-accent/5", dot: "" },
  awaiting: { border: "border-warning", bg: "bg-warning/5", dot: "" },
  failed: { border: "border-destructive", bg: "bg-destructive/5", dot: "bg-destructive" },
  launching: {
    border: "border-muted-foreground/40",
    bg: "bg-muted/30",
    dot: "bg-muted-foreground/70",
  },
  done: { border: "border-muted-foreground/30", bg: "bg-card", dot: "bg-muted-foreground/55" },
  idle: { border: "border-muted-foreground/30", bg: "bg-card", dot: "bg-muted-foreground/55" },
  other: { border: "border-muted-foreground/30", bg: "bg-card", dot: "bg-muted-foreground/55" },
};

function NodeStatusDot({ activity }: { activity: AgentActivity }) {
  if (activity === "working") return <RunningDot />;
  if (activity === "awaiting") {
    return (
      <Badge className="border-transparent bg-warning/15 text-warning text-[9px] px-1 py-0">
        !
      </Badge>
    );
  }
  const colors = ACTIVITY_COLORS[activity];
  return <span className={cn("inline-block size-2 shrink-0 rounded-full", colors.dot)} />;
}

function AgentNodeComponent({ data }: NodeProps<Node<AgentNodeData>>) {
  const { label, activity, statusLabel, isActive, preview } = data;
  const colors = ACTIVITY_COLORS[activity];
  const location = useLocation();
  const search = useMemo(() => {
    const params = new URLSearchParams(location.search);
    for (const key of ["file", "diff", "comment", "view"]) params.delete(key);
    const next = params.toString();
    return next ? `?${next}` : "";
  }, [location.search]);

  return (
    <Link to={{ pathname: `/c/${data.sessionId}`, search }} className="block">
      <Handle
        type="target"
        position={Position.Top}
        className="!bg-muted-foreground/40 !w-1.5 !h-1.5 !border-0"
      />
      <div
        className={cn(
          "rounded-lg border px-3 py-2 shadow-sm transition-colors hover:shadow-md cursor-pointer",
          colors.border,
          colors.bg,
          isActive && "ring-2 ring-ring ring-offset-1 ring-offset-background",
        )}
        style={{ width: NODE_WIDTH }}
      >
        <div className="flex items-center gap-1.5">
          <span className="truncate text-xs font-medium leading-tight">{label}</span>
          <span className="flex-1" />
          <NodeStatusDot activity={activity} />
        </div>
        {preview && (
          <p className="mt-1 truncate text-[10px] leading-tight text-muted-foreground">{preview}</p>
        )}
        {!["idle", "done"].includes(activity) && (
          <p className="mt-0.5 text-[10px] text-muted-foreground">{statusLabel}</p>
        )}
      </div>
      <Handle
        type="source"
        position={Position.Bottom}
        className="!bg-muted-foreground/40 !w-1.5 !h-1.5 !border-0"
      />
    </Link>
  );
}

const nodeTypes: NodeTypes = { agent: AgentNodeComponent };

function ChildCollector({
  parentId,
  depth,
  onCollected,
}: {
  parentId: string;
  depth: number;
  onCollected: (parentId: string, children: ChildSessionInfo[]) => void;
}) {
  const { children } = useChildSessions(depth < MAX_TREE_DEPTH ? parentId : null);

  useEffect(() => {
    onCollected(parentId, children);
  }, [parentId, children, onCollected]);

  if (depth >= MAX_TREE_DEPTH) return null;
  return (
    <>
      {children.map((child) => (
        <ChildCollector
          key={child.id}
          parentId={child.id}
          depth={depth + 1}
          onCollected={onCollected}
        />
      ))}
    </>
  );
}

interface SubagentsGraphViewProps {
  conversationId: string;
  rootSessionId: string;
}

export function SubagentsGraphView({ conversationId, rootSessionId }: SubagentsGraphViewProps) {
  const { session } = useSession(rootSessionId);
  const { children: rootChildren } = useChildSessions(rootSessionId);

  const [childrenMap, setChildrenMap] = useState<Map<string, ChildSessionInfo[]>>(() => new Map());

  const prevRootRef = useRef<ChildSessionInfo[] | undefined>(undefined);
  if (prevRootRef.current !== rootChildren) {
    prevRootRef.current = rootChildren;
    if (childrenMap.get(rootSessionId) !== rootChildren) {
      const next = new Map(childrenMap);
      next.set(rootSessionId, rootChildren);
      setChildrenMap(next);
    }
  }

  const handleCollected = useCallback((parentId: string, children: ChildSessionInfo[]) => {
    setChildrenMap((prev) => {
      if (prev.get(parentId) === children) return prev;
      const next = new Map(prev);
      next.set(parentId, children);
      return next;
    });
  }, []);

  const wrapper = session?.labels?.[WRAPPER_LABEL_KEY];
  const nativeAgent = nativeCodingAgentForWrapper(wrapper);
  const rootLabel = nativeAgent?.displayName ?? session?.agentName ?? "main";
  const rootActivity: AgentActivity =
    session?.status === "running" ? "working" : session?.status === "failed" ? "failed" : "idle";

  const { nodes: layoutNodes, edges: layoutEdges } = useMemo(
    () =>
      buildGraphLayout(
        rootSessionId,
        rootLabel,
        rootActivity,
        rootActivity === "working" ? "Working" : "Idle",
        null,
        childrenMap,
        conversationId,
      ),
    [rootSessionId, rootLabel, rootActivity, childrenMap, conversationId],
  );

  return (
    <div className="flex h-full min-h-0 flex-1 flex-col overflow-hidden bg-card">
      <div className="h-full w-full" style={{ minHeight: 200 }}>
        <ReactFlow
          nodes={layoutNodes}
          edges={layoutEdges}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.3 }}
          panOnDrag
          panOnScroll
          zoomOnDoubleClick={false}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          proOptions={{ hideAttribution: true }}
          minZoom={0.3}
          maxZoom={1.5}
        >
          <Background bgColor="var(--card)" />
        </ReactFlow>
      </div>
      {rootChildren.map((child) => (
        <ChildCollector
          key={child.id}
          parentId={child.id}
          depth={1}
          onCollected={handleCollected}
        />
      ))}
    </div>
  );
}
