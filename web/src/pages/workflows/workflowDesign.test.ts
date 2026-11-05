import { expect, it } from "vitest";
import {
  canConnect,
  dependencyDetours,
  executionDefinition,
  layoutTasks,
  nextIdentifier,
  removeTask,
  restoreDesign,
  starterWorkflow,
  TASK_WIDTH,
} from "./workflowDesign";

it("allows branches and joins but prevents duplicate, missing, self, and cyclic connections", () => {
  const { nodes } = starterWorkflow();
  expect(canConnect(nodes, "propose", "constraints")).toBe(true);
  expect(canConnect(nodes, "investigate", "summarize")).toBe(true);
  for (const [source, target] of [
    ["investigate", "propose"],
    ["summarize", "investigate"],
    ["summarize", "constraints"],
    ["propose", "propose"],
    ["missing", "propose"],
  ]) {
    expect(canConnect(nodes, source, target)).toBe(false);
  }
});

it("deleting a task removes only its incident dependencies", () => {
  const workflow = removeTask(starterWorkflow(), "propose");
  expect(workflow.nodes.map((node) => node.id)).not.toContain("propose");
  expect(workflow.nodes.find((node) => node.id === "summarize")?.depends_on).toEqual([
    "constraints",
  ]);
  expect(workflow.nodes.find((node) => node.id === "constraints")?.depends_on).toEqual([
    "investigate",
  ]);
});

it("lays out branches at the same depth and joins after all parents, regardless of input order", () => {
  const positions = layoutTasks(starterWorkflow().nodes.reverse());
  expect(positions.propose.y).toBe(positions.constraints.y);
  expect(positions.propose.x).not.toBe(positions.constraints.x);
  expect(positions.investigate.y).toBeLessThan(positions.propose.y);
  expect(positions.summarize.y).toBeGreaterThan(positions.constraints.y);
});

it("layout rejects cycles instead of looping", () => {
  const workflow = starterWorkflow();
  workflow.nodes[0].depends_on = ["summarize"];
  expect(() => layoutTasks(workflow.nodes)).toThrow("cycle");
});

it("routes dependencies that skip rows outside the cards, with distinct selectable lanes", () => {
  const { nodes } = starterWorkflow();
  expect(dependencyDetours(nodes, layoutTasks(nodes))).toEqual({});
  nodes.find((node) => node.id === "constraints")!.depends_on.push("propose");
  const positions = layoutTasks(nodes);
  const detours = dependencyDetours(nodes, positions);
  expect(Object.keys(detours).sort()).toEqual(["investigate->constraints", "propose->summarize"]);
  const rightBoundary = Math.max(...Object.values(positions).map((point) => point.x)) + TASK_WIDTH;
  for (const lane of Object.values(detours)) expect(lane).toBeGreaterThan(rightBoundary);
  expect(
    Math.abs(detours["investigate->constraints"] - detours["propose->summarize"]),
  ).toBeGreaterThan(28);
});

it("routes around cards when dragging places a dependent above its prerequisite", () => {
  const { nodes } = starterWorkflow();
  const positions = layoutTasks(nodes);
  positions.propose = { x: positions.investigate.x, y: -200 };
  expect(dependencyDetours(nodes, positions)["investigate->propose"]).toBeGreaterThan(
    positions.investigate.x + TASK_WIDTH,
  );
});

it("preserves per-task agent placement in the API definition", () => {
  const design = starterWorkflow();
  for (const task of design.nodes)
    expect(task).toMatchObject({ allow_expansion: true, max_rounds: 3 });
  design.nodes[0].allow_expansion = false;
  design.nodes[0].max_rounds = 1;
  delete design.nodes[1].allow_expansion;
  delete design.nodes[1].max_rounds;
  design.nodes[0].execution.members[0].agent_id = "investigation-agent";
  expect(design.nodes[1].execution.members[0].agent_id).toBe("replace-with-registered-agent-id");
  design.nodes[0].execution.members[0].placement.provider = "analysis-sandbox";
  expect(design.nodes[1].execution.members[0].placement.provider).toBe(
    "replace-with-sandbox-provider",
  );
  const before = structuredClone(design);
  const definition = executionDefinition(design);
  expect(definition).toEqual({
    name: design.name,
    goal: design.goal,
    teams: design.nodes.map((task) => ({ ...task.execution, id: task.id })),
    nodes: design.nodes.map(({ execution: _execution, ...task }) => ({
      allow_expansion: true,
      max_rounds: 3,
      ...task,
      team: task.id,
    })),
  });
  expect(design).toEqual(before);
  for (const task of design.nodes) expect(task).not.toHaveProperty("team");
  expect(definition.teams[0].members[0].placement.kind).toBe("sandbox");
});

it("allocates stable identifiers without colliding with existing tasks", () => {
  expect(nextIdentifier("task", ["task_1", "task_3"])).toBe("task_2");
});

it("restores a saved DAG with independent task execution settings", () => {
  const definition = executionDefinition(starterWorkflow());
  definition.max_parallel_members = 8;
  definition.member_timeout_seconds = 90;
  definition.nodes[1].team = definition.nodes[0].team;
  const restored = restoreDesign(definition);
  expect(executionDefinition(restored).max_parallel_members).toBe(8);
  expect(executionDefinition(restored).member_timeout_seconds).toBe(90);
  expect(restored.nodes[1].depends_on).toEqual(["investigate"]);
  restored.nodes[0].execution.members[0].placement.provider = "changed";
  expect(restored.nodes[1].execution.members[0].placement.provider).not.toBe("changed");
  expect(definition.teams[0].members[0].placement.provider).not.toBe("changed");
  expect(() => restoreDesign({ ...definition, teams: [] })).toThrow(/Missing execution/);
});
