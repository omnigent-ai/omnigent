import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { NodeTypes, Node } from "@xyflow/react";
import { ReactFlow, Background, useReactFlow } from "@xyflow/react";
import { useLocation, useNavigate } from "@/lib/routing";
import { ZoomInIcon, ZoomOutIcon, Maximize2Icon } from "lucide-react";
import { MAX_TREE_DEPTH, useChildSessions, type ChildSessionInfo } from "@/hooks/useChildSessions";
import { useSession } from "@/hooks/useSession";
import { nativeCodingAgentForWrapper, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";
import { AgentGraphNode } from "./AgentGraphNode";
import { buildGraphLayout, type AgentNodeData } from "./subagentGraphLayout";
import { sessionStatus } from "./subagentStatus";

import "@xyflow/react/dist/style.css";

function ZoomControls() {
  const { zoomIn, zoomOut, fitView } = useReactFlow();
  const btnClass =
    "flex items-center justify-center size-7 rounded-md border bg-card text-muted-foreground shadow-sm hover:bg-accent hover:text-accent-foreground transition-colors";
  return (
    <div className="absolute bottom-3 right-3 z-10 flex flex-col gap-1">
      <button
        type="button"
        className={btnClass}
        onClick={() => zoomIn({ duration: 200 })}
        aria-label="Zoom in"
      >
        <ZoomInIcon className="size-4" />
      </button>
      <button
        type="button"
        className={btnClass}
        onClick={() => zoomOut({ duration: 200 })}
        aria-label="Zoom out"
      >
        <ZoomOutIcon className="size-4" />
      </button>
      <button
        type="button"
        className={btnClass}
        onClick={() => fitView({ duration: 200, padding: 0.3 })}
        aria-label="Fit view"
      >
        <Maximize2Icon className="size-4" />
      </button>
    </div>
  );
}

const nodeTypes: NodeTypes = { agent: AgentGraphNode };

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
  // Mirror the list view's ``sessionStatus`` so the root node honors
  // launching / disconnected (not just running / failed / idle).
  const rootStatus = sessionStatus(session?.status, session?.lastTaskError);
  const rootActivity = rootStatus.activity;
  const rootStatusLabel = rootStatus.label;

  const rootAgentName = session?.agentName ?? null;
  const rootHarness = session?.harness ?? null;

  const { nodes: layoutNodes, edges: layoutEdges } = useMemo(
    () =>
      buildGraphLayout(
        {
          id: rootSessionId,
          label: rootLabel,
          activity: rootActivity,
          statusLabel: rootStatusLabel,
          preview: null,
          wrapper: wrapper ?? null,
          agentName: rootAgentName,
          harness: rootHarness,
        },
        childrenMap,
        conversationId,
      ),
    [
      rootSessionId,
      rootLabel,
      rootActivity,
      rootStatusLabel,
      wrapper,
      rootAgentName,
      rootHarness,
      childrenMap,
      conversationId,
    ],
  );

  const navigate = useNavigate();
  const location = useLocation();
  const handleNodeClick = useCallback(
    (_event: React.MouseEvent, node: Node<AgentNodeData>) => {
      const params = new URLSearchParams(location.search);
      for (const key of ["file", "diff", "comment", "view"]) params.delete(key);
      const search = params.toString();
      navigate({
        pathname: `/c/${node.data.sessionId}`,
        search: search ? `?${search}` : "",
      });
    },
    [navigate, location.search],
  );

  return (
    <div
      data-workspace-panel-surface
      className="flex h-full min-h-0 flex-1 flex-col overflow-hidden bg-card"
    >
      <div className="h-full w-full" style={{ minHeight: 200 }}>
        <ReactFlow
          nodes={layoutNodes}
          edges={layoutEdges}
          nodeTypes={nodeTypes}
          onNodeClick={handleNodeClick}
          fitView
          fitViewOptions={{ padding: 0.3 }}
          panOnDrag
          panOnScroll
          zoomOnDoubleClick={false}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          proOptions={{ hideAttribution: true }}
          minZoom={0.1}
          maxZoom={3}
        >
          <Background bgColor="var(--card)" />
          <ZoomControls />
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
