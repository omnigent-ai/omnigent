import { createContext, type ReactNode, useContext, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import {
  Background,
  Handle,
  type Node,
  type NodeProps,
  type NodeTypes,
  Position,
  ReactFlow,
} from "@xyflow/react";

import { FullscreenIcon } from "@/components/ai-elements/MermaidBlock";
import { GraphZoomControls } from "@/components/GraphZoomControls";
import { Button } from "@/components/ui/button";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { getEmbedRoot } from "@/lib/host";
import { failedUpstream, type StepProgress, type StepState } from "@/lib/sessionJournal";
import { cn } from "@/lib/utils";
import {
  layoutWorkflowSteps,
  STEP_NODE_WIDTH,
  type StepNodeData,
  type WorkflowStep,
} from "@/lib/workflowGraphLayout";
import {
  CantStart,
  SEGMENT_COLORS,
  STATE_LABELS,
  StepGlyph,
  TaskCardDetails,
  TaskMenu,
  type TaskMenuAction,
} from "@/shell/TasksPanel";

import "@xyflow/react/dist/style.css";

const HANDLE_CLASS = "!h-1.5 !w-1.5 !border-0 !bg-muted-foreground/40";
// The graph grows with its steps, up to a share of the window that leaves the runs in view.
const MIN_HEIGHT = 160;
const MAX_HEIGHT = "40dvh";
// fitView's margin around the steps, as a share of their size.
const FIT_PADDING = 0.2;

// A node's border and tint, in the Tasks tab's colors for where the step stands.
const NODE_TINT: Record<StepState, string> = {
  waiting: "border-border-strong",
  ready: "border-session-active/50",
  running: "border-session-active bg-session-active/[0.06] ring-4 ring-session-active/10",
  stopped: "border-dashed border-status-gray",
  needs_input: "border-brand-accent bg-brand-accent/[0.06] ring-4 ring-brand-accent/10",
  done: "border-success/60 bg-success/[0.06]",
  failed: "border-destructive/60 bg-destructive/[0.06]",
};

// The states a run's legend explains, in the order work moves through them.
const LEGEND: readonly StepState[] = ["waiting", "running", "needs_input", "done", "failed"];

// A node's ⋯ button; React Flow leaves a press on it alone rather than panning.
const NODE_MENU_BUTTON =
  "nodrag nopan -my-0.5 -mr-1.5 shrink-0 cursor-pointer rounded-md p-0.5 text-muted-foreground hover:bg-foreground/[0.06] hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50";

/** What the nodes show beyond their layout: in a run, where each step stands and its ⋯ action. */
interface StepsGraphContextValue {
  /** How many steps the graph has, e.g. for "Task 2 of 5". */
  total: number;
  run?: {
    /** Each step's progress, by step ID. */
    progress: ReadonlyMap<string, StepProgress>;
    /** The steps each waits on that aren't done, by name, by step ID. */
    pending: ReadonlyMap<string, readonly string[]>;
    menuActionFor?: (item: StepProgress) => TaskMenuAction | undefined;
  };
}

const StepsGraphContext = createContext<StepsGraphContextValue>({ total: 0 });

/**
 * A step's box. Hovering it shows a card with all of the step, as a task's
 * timeline segment does; in a run, its ⋯ menu offers the task's action.
 */
function StepNode({ id, data }: NodeProps<Node<StepNodeData>>) {
  const graph = useContext(StepsGraphContext);
  const item = graph.run?.progress.get(id);
  const menuAction = item && graph.run?.menuActionFor?.(item);
  const [cardOpen, setCardOpen] = useState(false);
  // The card steps aside while the node's menu is open.
  const [menuOpen, setMenuOpen] = useState(false);
  return (
    <>
      <Handle type="target" position={Position.Top} className={HANDLE_CLASS} />
      <HoverCard
        open={cardOpen && !menuOpen}
        onOpenChange={setCardOpen}
        openDelay={300}
        closeDelay={100}
      >
        <HoverCardTrigger asChild>
          <div
            className={cn(
              "flex items-start gap-2 rounded-lg border bg-card px-3 py-2 shadow-sm transition-colors",
              NODE_TINT[data.state ?? "waiting"],
            )}
            style={{ width: STEP_NODE_WIDTH }}
            data-testid="workflow-step-node"
            data-state={data.state}
          >
            <StepGlyph state={data.state ?? "waiting"} number={data.number} />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium leading-[18px]">{data.name}</p>
              {/* A step that can't start says why in place of its goal, which its card shows. */}
              {data.failedNames ? (
                <p className="mt-0.5 line-clamp-2 text-xs leading-snug text-muted-foreground">
                  <CantStart failedNames={data.failedNames} />
                </p>
              ) : (
                data.goal && (
                  <p className="mt-0.5 line-clamp-2 text-xs leading-snug text-muted-foreground">
                    {data.goal}
                  </p>
                )
              )}
            </div>
            {menuAction && (
              <TaskMenu
                taskName={item.step.name}
                menuAction={menuAction}
                testIdPrefix="workflow-step-node"
                className={NODE_MENU_BUTTON}
                onOpenChange={setMenuOpen}
              />
            )}
          </div>
        </HoverCardTrigger>
        <HoverCardContent
          side="right"
          align="start"
          sideOffset={8}
          className="flex w-fit max-w-72 flex-col items-start gap-1 px-3 py-2 text-sm"
          data-testid="workflow-step-card"
        >
          {item ? (
            // The card names the step as its box does.
            <TaskCardDetails
              item={{ ...item, step: { ...item.step, name: data.name, goal: data.goal } }}
              number={data.number}
              total={graph.total}
              pending={graph.run?.pending.get(id) ?? []}
              failedNames={data.failedNames ?? []}
              full
            />
          ) : (
            <>
              <span className="text-muted-foreground">
                Step {data.number} of {graph.total}
              </span>
              <span className="break-words font-medium">{data.name}</span>
              {data.goal && <span className="break-words text-muted-foreground">{data.goal}</span>}
            </>
          )}
        </HoverCardContent>
      </HoverCard>
      <Handle type="source" position={Position.Bottom} className={HANDLE_CLASS} />
    </>
  );
}

const nodeTypes: NodeTypes = { step: StepNode };

/**
 * The steps drawn with React Flow; *full* means the view owns the wheel, to zoom.
 *
 * @param menuActionFor - In a run, what a step's ⋯ menu offers; none when absent.
 */
function StepsFlow({
  steps,
  progress,
  full = false,
  menuActionFor,
}: {
  steps: readonly WorkflowStep[];
  progress?: readonly StepProgress[];
  full?: boolean;
  menuActionFor?: (item: StepProgress) => TaskMenuAction | undefined;
}) {
  const { nodes, edges, pending } = useMemo(() => {
    if (!progress) return { ...layoutWorkflowSteps(steps), pending: undefined };
    // Name a failed step as its box does; a step the run added has only the run's name.
    const names = new Map([
      ...progress.map((item) => [item.step.id, item.step.name] as const),
      ...steps.map((step) => [step.id, step.name] as const),
    ]);
    const failedNames = new Map(
      [...failedUpstream(progress)].map(([id, failed]) => [
        id,
        failed.map((failedId) => names.get(failedId) ?? failedId),
      ]),
    );
    const done = new Set(
      progress.filter((item) => item.state === "done").map((item) => item.step.id),
    );
    return {
      ...layoutWorkflowSteps(
        steps,
        new Map(progress.map((item) => [item.step.id, item.state])),
        failedNames,
      ),
      pending: new Map(
        progress.map((item) => [
          item.step.id,
          item.step.depends_on.filter((id) => !done.has(id)).map((id) => names.get(id) ?? id),
        ]),
      ),
    };
  }, [steps, progress]);
  const graph = useMemo<StepsGraphContextValue>(
    () => ({
      total: steps.length,
      run:
        progress && pending
          ? {
              progress: new Map(progress.map((item) => [item.step.id, item])),
              pending,
              menuActionFor,
            }
          : undefined,
    }),
    [steps, progress, pending, menuActionFor],
  );
  return (
    <StepsGraphContext.Provider value={graph}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: FIT_PADDING, maxZoom: 1 }}
        panOnDrag
        panOnScroll={false}
        zoomOnScroll={full}
        preventScrolling={full}
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
    </StepsGraphContext.Provider>
  );
}

/**
 * A workflow's graph over the whole window, as a diagram's "View fullscreen"
 * opens it. Given a run's progress, each step shows where it stands, with a
 * legend, and given `menuActionFor`, its ⋯ menu. Escape or the corner button
 * closes it.
 */
export function WorkflowGraphOverlay({
  title,
  subtitle,
  steps,
  progress,
  menuActionFor,
  onClose,
}: {
  title: string;
  /** A line under the title, e.g. the run's session, start and state. */
  subtitle?: ReactNode;
  steps: readonly WorkflowStep[];
  progress?: readonly StepProgress[];
  /** What a step's ⋯ menu offers in the run, as its task's row does. */
  menuActionFor?: (item: StepProgress) => TaskMenuAction | undefined;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      // An open menu or card takes the first Escape.
      if (event.key === "Escape" && !event.defaultPrevented) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex flex-col bg-background/95 text-foreground backdrop-blur-sm"
      // Page-wide shortcuts (a pending question's keys) leave Escape to this view.
      data-fullscreen-overlay=""
      role="dialog"
      aria-modal="true"
      aria-label={title}
      data-testid="workflow-graph-overlay"
    >
      <div className="flex items-start justify-between gap-4 px-6 pt-5 pb-4">
        <div className="min-w-0">
          <h2 className="truncate text-lg font-semibold">{title}</h2>
          {subtitle && (
            <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-sm text-muted-foreground">
              {subtitle}
            </div>
          )}
        </div>
        <Button
          aria-label="Exit fullscreen"
          title="Exit fullscreen"
          onClick={onClose}
          size="icon-sm"
          type="button"
          variant="ghost"
        >
          <FullscreenIcon />
        </Button>
      </div>
      <div className="relative mx-6 mb-6 min-h-0 flex-1 overflow-hidden rounded-xl border border-border bg-card">
        <StepsFlow steps={steps} progress={progress} menuActionFor={menuActionFor} full />
        {progress && (
          <div className="absolute bottom-3 left-3 z-10 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border bg-card/90 px-2.5 py-1.5 text-xs text-muted-foreground shadow-sm backdrop-blur">
            {LEGEND.map((state) => (
              <span key={state} className="flex items-center gap-1.5">
                <span className={cn("h-1.5 w-3 rounded-full", SEGMENT_COLORS[state])} />
                {STATE_LABELS[state]}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>,
    getEmbedRoot() ?? document.body,
  );
}

/**
 * A saved workflow's steps as a read-only graph, top to bottom by dependency.
 * Drag to pan; the corner buttons zoom, and "View fullscreen" opens it over
 * the whole window. Scrolling over it scrolls the page.
 */
export function WorkflowStepsGraph({
  steps,
  title,
}: {
  steps: readonly WorkflowStep[];
  /** The full-screen view's title, e.g. the workflow's name. */
  title: string;
}) {
  const [fullscreen, setFullscreen] = useState(false);
  const { height: stepsHeight } = useMemo(() => layoutWorkflowSteps(steps), [steps]);
  // Full size when it fits, with room for fitView's margin; past the cap it zooms out.
  const height = Math.ceil(stepsHeight * (1 + FIT_PADDING)) + 32;
  return (
    <div
      className="relative overflow-hidden rounded-xl border border-border bg-card"
      style={{ height, minHeight: MIN_HEIGHT, maxHeight: MAX_HEIGHT }}
      data-testid="workflow-steps-graph"
    >
      <StepsFlow steps={steps} />
      <div className="absolute top-2 right-2 z-10 flex items-center rounded-md border border-sidebar bg-sidebar/80 px-1.5 py-1 supports-[backdrop-filter]:bg-sidebar/70 supports-[backdrop-filter]:backdrop-blur">
        <Button
          aria-label="View fullscreen"
          title="View fullscreen"
          className="size-6 bg-transparent text-muted-foreground hover:bg-transparent hover:text-foreground"
          onClick={() => setFullscreen(true)}
          size="icon-sm"
          type="button"
          variant="ghost"
        >
          <FullscreenIcon />
        </Button>
      </div>
      {fullscreen && (
        <WorkflowGraphOverlay title={title} steps={steps} onClose={() => setFullscreen(false)} />
      )}
    </div>
  );
}
