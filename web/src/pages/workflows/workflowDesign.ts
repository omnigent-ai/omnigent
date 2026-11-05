import type {
  WorkflowDefinition,
  WorkflowMember,
  WorkflowTask,
  WorkflowTeam,
} from "@/lib/workflowsApi";

export const DEFAULT_TASK_OPTIONS = { allow_expansion: true, max_rounds: 3 } as const;
export const TASK_WIDTH = 224;
const ROW_SPACING = 190;

export interface WorkflowDesign extends Omit<WorkflowDefinition, "teams" | "nodes"> {
  nodes: TaskDesign[];
}

export type TaskExecution = Omit<WorkflowTeam, "id">;
export interface TaskDesign extends WorkflowTask {
  execution: TaskExecution;
}

export function newExecution(): TaskExecution {
  return { strategy: "owner", lead: "owner", members: [newMember("owner")] };
}

export function newMember(id: string): WorkflowMember {
  return {
    id,
    role: "Own the outcome and repair failed candidates.",
    agent_id: "replace-with-registered-agent-id",
    placement: { kind: "sandbox", provider: "replace-with-sandbox-provider" },
  };
}

export function starterWorkflow(): WorkflowDesign {
  return {
    name: "Investigate and propose a change",
    goal: "Produce an evidence-based recommendation.",
    nodes: [
      {
        ...DEFAULT_TASK_OPTIONS,
        execution: newExecution(),
        id: "investigate",
        name: "Investigate",
        goal: "Investigate the problem and report evidence.",
        depends_on: [],
        gates: [],
      },
      {
        ...DEFAULT_TASK_OPTIONS,
        execution: newExecution(),
        id: "propose",
        name: "Propose a change",
        goal: "Propose a change. Return an artifact named result with its revision.",
        depends_on: ["investigate"],
        gates: [{ id: "review", kind: "external", artifacts: ["result"] }],
      },
      {
        ...DEFAULT_TASK_OPTIONS,
        execution: newExecution(),
        id: "constraints",
        name: "Review constraints",
        goal: "Identify constraints and risks independently.",
        depends_on: ["investigate"],
        gates: [],
      },
      {
        ...DEFAULT_TASK_OPTIONS,
        execution: newExecution(),
        id: "summarize",
        name: "Summarize",
        goal: "Summarize the accepted recommendation and constraints.",
        depends_on: ["propose", "constraints"],
        gates: [],
      },
    ],
  };
}

export function executionDefinition(design: WorkflowDesign): WorkflowDefinition {
  const { nodes: _nodes, ...settings } = design;
  return {
    ...settings,
    teams: design.nodes.map((task) => ({ ...task.execution, id: task.id })),
    nodes: design.nodes.map(({ execution: _execution, ...task }) => ({
      ...task,
      allow_expansion: task.allow_expansion ?? DEFAULT_TASK_OPTIONS.allow_expansion,
      max_rounds: task.max_rounds ?? DEFAULT_TASK_OPTIONS.max_rounds,
      team: task.id,
    })),
  };
}

export function restoreDesign(definition: WorkflowDefinition): WorkflowDesign {
  const { nodes: _nodes, teams: _teams, ...settings } = definition;
  return {
    ...settings,
    nodes: definition.nodes.map(({ team, ...task }) => {
      const execution = definition.teams.find((item) => item.id === team);
      if (!execution) throw new Error(`Missing execution settings for ${task.id}`);
      const { id: _id, ...executionSettings } = execution;
      return { ...task, execution: structuredClone(executionSettings) };
    }),
  };
}

export function nextIdentifier(prefix: string, ids: string[]): string {
  let index = 1;
  while (ids.includes(`${prefix}_${index}`)) index++;
  return `${prefix}_${index}`;
}

export function canConnect(nodes: WorkflowTask[], source: string, target: string): boolean {
  if (!nodes.some((node) => node.id === source) || !nodes.some((node) => node.id === target))
    return false;
  if (source === target || nodes.find((node) => node.id === target)?.depends_on.includes(source))
    return false;
  const pending = [source];
  const visited = new Set<string>();
  while (pending.length) {
    const id = pending.pop()!;
    if (id === target) return false;
    if (visited.has(id)) continue;
    visited.add(id);
    pending.push(...(nodes.find((node) => node.id === id)?.depends_on ?? []));
  }
  return true;
}

export function removeTask(workflow: WorkflowDesign, id: string): WorkflowDesign {
  return {
    ...workflow,
    nodes: workflow.nodes
      .filter((node) => node.id !== id)
      .map((node) => ({
        ...node,
        depends_on: node.depends_on.filter((dependency) => dependency !== id),
      })),
  };
}

export function layoutTasks(nodes: WorkflowTask[]): Record<string, { x: number; y: number }> {
  const levels = new Map<string, number>();
  let remaining = [...nodes];
  while (remaining.length) {
    const ready = remaining.filter((node) => node.depends_on.every((id) => levels.has(id)));
    if (!ready.length) throw new Error("The workflow has a cycle or a missing task.");
    for (const node of ready)
      levels.set(node.id, Math.max(-1, ...node.depends_on.map((id) => levels.get(id)!)) + 1);
    remaining = remaining.filter((node) => !levels.has(node.id));
  }
  const groups = new Map<number, WorkflowTask[]>();
  for (const node of nodes) {
    const level = levels.get(node.id)!;
    groups.set(level, [...(groups.get(level) ?? []), node]);
  }
  return Object.fromEntries(
    [...groups].flatMap(([level, group]) =>
      group.map((node, index) => [
        node.id,
        { x: (index - (group.length - 1) / 2) * 280, y: level * ROW_SPACING },
      ]),
    ),
  );
}

export function dependencyDetours(
  tasks: WorkflowTask[],
  positions: Record<string, { x: number; y: number }>,
): Record<string, number> {
  const routes: Record<string, number> = {};
  let nextLane = Math.max(...Object.values(positions).map((point) => point.x)) + TASK_WIDTH + 40;
  for (const task of tasks) {
    for (const source of task.depends_on) {
      const distance = positions[task.id].y - positions[source].y;
      if (distance > 0 && distance <= ROW_SPACING) continue;
      // Route connections across multiple rows around the cards, in separate lanes.
      routes[`${source}->${task.id}`] = nextLane;
      nextLane += 32;
    }
  }
  return routes;
}
