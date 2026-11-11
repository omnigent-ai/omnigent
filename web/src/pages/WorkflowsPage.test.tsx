import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import type { WorkflowBackend, WorkflowDefinition, WorkflowRun } from "@/lib/workflowsApi";
import { executionDefinition, starterWorkflow } from "./workflows/workflowDesign";
import { WorkflowsPage } from "./WorkflowsPage";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@xyflow/react", () => ({
  ReactFlow: ({
    nodes,
    edges,
    onNodeClick,
    children,
  }: {
    nodes: { id: string; ariaLabel: string }[];
    edges: { id: string; ariaLabel: string }[];
    onNodeClick: (event: unknown, node: { id: string }) => void;
    children: ReactNode;
  }) => (
    <div>
      {nodes.map((node) => (
        <button type="button" key={node.id} onClick={() => onNodeClick({}, node)}>
          {node.ariaLabel}
        </button>
      ))}
      {edges.map((edge) => (
        <span key={edge.id}>{edge.ariaLabel}</span>
      ))}
      {children}
    </div>
  ),
  Handle: () => null,
  BaseEdge: () => null,
  Background: () => null,
  Controls: () => null,
  Panel: ({ children }: { children: ReactNode }) => children,
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Top: "top", Bottom: "bottom" },
}));

let run: WorkflowRun;
let posts: { path: string; body: Record<string, unknown> }[];
let enabledBackends: WorkflowBackend[];
beforeEach(() => {
  posts = [];
  enabledBackends = ["simulation"];
  run = {
    run_id: "run-one",
    status: "waiting",
    active: false,
    controller_error: null,
    binding: { mode: "simulation", server: null },
    workflow: executionDefinition(starterWorkflow()),
    nodes: {},
    events: [],
  };
  for (const node of run.workflow.nodes)
    run.nodes[node.id] = {
      status: "waiting",
      round: 1,
      summary: null,
      error: null,
      candidate_id: node.id + "-1",
      candidate: { summary: "Candidate evidence", status: "ready", artifacts: [] },
      candidates: {},
      artifacts: [],
      members: { owner: { session_id: "session-one", cleanup_error: null, turns: {} } },
      gate_results: [],
    };
  vi.mocked(authenticatedFetch).mockImplementation(async (url, init) => {
    const path = String(url);
    if (init?.method === "POST") {
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      posts.push({ path, body });
      if (path.includes("/designs/"))
        return Response.json({
          workflow_id: path.split("/").pop(),
          root_session_id: "root-one",
          workflow: body.workflow,
        });
      if (path.endsWith("/definition"))
        return Response.json({ definition: "name: generated\nnodes: []\n" });
      return Response.json(run);
    }
    if (path.endsWith("/options"))
      return Response.json({
        backends: [
          { id: "simulation", enabled: true, reason: null, example: "" },
          {
            id: "server",
            enabled: enabledBackends.includes("server"),
            reason: "Configure workflow server",
            example: "",
          },
        ],
      });
    if (path.endsWith("/run-one")) return Response.json(run);
    throw new Error("Unexpected request: " + path);
  });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <WorkflowsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
async function openTest() {
  await waitFor(() => expect(screen.getByRole("button", { name: "Test workflow" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Test workflow" }));
  await screen.findByRole("heading", { name: "Test workflow" });
  fireEvent.click(screen.getByRole("button", { name: "Propose a change waiting" }));
}

it("shows accepted outputs separately from a pending candidate", async () => {
  run.nodes.investigate = {
    ...run.nodes.investigate,
    status: "succeeded",
    summary: "Validated findings",
    outputs: { result: { count: 3 } },
  };
  renderPage();
  await openTest();
  expect(screen.getByText("Candidate propose-1")).toBeInTheDocument();
  expect(screen.queryByText("Accepted outcome")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Investigate succeeded" }));
  expect(screen.getByText("Accepted outcome")).toBeInTheDocument();
  expect(screen.getByText("Validated findings")).toBeInTheDocument();
  expect(screen.getByText("Outputs for dependent tasks")).toBeInTheDocument();
  expect(screen.getByText(/"count": 3/)).toBeInTheDocument();
  expect(screen.queryByText("Candidate evidence")).not.toBeInTheDocument();
});

it("opens a designer with branches and joins, without loading runs or a YAML editor", async () => {
  renderPage();
  expect(screen.getByRole("heading", { name: "Create workflow" })).toBeInTheDocument();
  expect(screen.getByText("Investigate → Propose a change")).toBeInTheDocument();
  expect(screen.getByText("Review constraints → Summarize")).toBeInTheDocument();
  expect(screen.queryByText("Your runs")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Generated YAML")).not.toBeInTheDocument();
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalled());
  expect(
    vi.mocked(authenticatedFetch).mock.calls.every(([path]) => path === "/v1/workflows/options"),
  ).toBe(true);
});

it("edits tasks and dependencies, prevents cycles, and exports the structured design", async () => {
  renderPage();
  const graph = screen.getByRole("region", { name: "Workflow DAG" });
  fireEvent.click(within(graph).getByRole("button", { name: "Propose a change" }));
  expect(screen.getByRole("checkbox", { name: "Summarize" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: "Review constraints" }));
  fireEvent.change(screen.getByLabelText("Task name"), {
    target: { value: "Implement the change" },
  });
  fireEvent.change(screen.getByLabelText("Task goal"), {
    target: { value: "Handle errors: preserve evidence" },
  });
  fireEvent.click(screen.getByText("Task options"));
  expect(screen.getByRole("checkbox", { name: "Allow follow-ups" })).toBeChecked();
  expect(screen.getByLabelText("Retries")).toHaveValue(2);
  fireEvent.click(screen.getByRole("checkbox", { name: "Allow follow-ups" }));
  fireEvent.change(screen.getByLabelText("Retries"), { target: { value: "0" } });
  fireEvent.click(screen.getByRole("button", { name: "Export YAML" }));
  const yaml = await screen.findByLabelText("Generated YAML");
  expect(yaml).toHaveAttribute("readonly");
  expect(yaml).toHaveValue("name: generated\nnodes: []\n");
  const design = posts[0].body.workflow as WorkflowDefinition;
  expect(design.nodes.find((node) => node.id === "propose")).toMatchObject({
    name: "Implement the change",
    goal: "Handle errors: preserve evidence",
    depends_on: ["investigate", "constraints"],
    allow_expansion: false,
    max_rounds: 1,
  });
});

it("creates a task with its own execution settings and exports them independently", async () => {
  renderPage();
  expect(
    within(screen.getByRole("region", { name: "Workflow settings" })).queryByText(
      "Execution settings",
    ),
  ).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("Execution settings"));
  fireEvent.change(screen.getByLabelText("owner agent ID"), { target: { value: "agent-own" } });
  fireEvent.change(screen.getByLabelText("owner sandbox provider"), {
    target: { value: "analysis-sandbox" },
  });
  fireEvent.click(screen.getByText("Execution settings"));
  fireEvent.click(screen.getByRole("button", { name: "Add task" }));
  const modal = screen.getByRole("dialog", { name: "Add task" });
  const dialog = within(modal);
  expect(modal).not.toHaveTextContent(/\bteam\b/i);
  fireEvent.click(dialog.getByText("Execution settings"));
  expect(dialog.getByLabelText("owner agent ID")).toHaveValue("replace-with-registered-agent-id");
  fireEvent.change(dialog.getByLabelText("owner agent ID"), {
    target: { value: "acceptance-agent" },
  });
  expect(dialog.getByLabelText("owner sandbox provider")).toHaveValue(
    "replace-with-sandbox-provider",
  );
  fireEvent.change(dialog.getByLabelText("owner sandbox provider"), {
    target: { value: "acceptance-sandbox" },
  });
  fireEvent.change(dialog.getByLabelText("owner repository (optional)"), {
    target: { value: "https://github.com/example/acceptance" },
  });
  const create = dialog.getByRole("button", { name: "Create task" });
  expect(dialog.getByLabelText("Task name")).toHaveValue("");
  expect(create).toBeDisabled();
  expect(dialog.getByRole("checkbox", { name: "Investigate" })).toBeChecked();
  fireEvent.click(dialog.getByText("Task options"));
  expect(dialog.getByRole("checkbox", { name: "Allow follow-ups" })).toBeChecked();
  expect(dialog.getByLabelText("Retries")).toHaveValue(2);
  expect(screen.getByText("4 tasks · 4 dependencies")).toBeInTheDocument();
  fireEvent.change(dialog.getByLabelText("Task name"), { target: { value: "   " } });
  fireEvent.change(dialog.getByLabelText("Task goal"), { target: { value: "Check the evidence" } });
  expect(create).toBeDisabled();
  fireEvent.change(dialog.getByLabelText("Task name"), { target: { value: " Acceptance check " } });
  fireEvent.click(dialog.getByRole("checkbox", { name: "Investigate" }));
  fireEvent.click(dialog.getByRole("checkbox", { name: "Review constraints" }));
  fireEvent.click(dialog.getByRole("checkbox", { name: "Require review before continuing" }));
  expect(screen.getByText("4 tasks · 4 dependencies")).toBeInTheDocument();
  fireEvent.click(create);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Task name")).toHaveValue("Acceptance check");
  expect(screen.getByRole("region", { name: "Task settings" })).not.toHaveTextContent(/\bteam\b/i);
  expect(screen.getByText("5 tasks · 5 dependencies")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Export YAML" }));
  await screen.findByLabelText("Generated YAML");
  const definition = posts[0].body.workflow as WorkflowDefinition;
  expect(definition.nodes.at(-1)).toMatchObject({
    name: "Acceptance check",
    goal: "Check the evidence",
    team: "task_1",
    depends_on: ["constraints"],
    allow_expansion: true,
    max_rounds: 3,
    gates: [{ id: "review", kind: "external", artifacts: ["result"] }],
  });
  expect(definition.teams).toHaveLength(5);
  expect(definition.teams[0].members[0].agent_id).toBe("agent-own");
  expect(definition.teams[0].members[0].placement.provider).toBe("analysis-sandbox");
  expect(definition.teams.at(-1)?.members[0]).toMatchObject({
    agent_id: "acceptance-agent",
    placement: {
      kind: "sandbox",
      provider: "acceptance-sandbox",
      workspace: "https://github.com/example/acceptance",
    },
  });
  expect(definition.nodes.every((task) => task.team === task.id)).toBe(true);
  expect(posts[0].body).not.toHaveProperty("backend");
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete task" }));
  expect(screen.queryByRole("button", { name: "Acceptance check" })).not.toBeInTheDocument();
  expect(screen.getByText("4 tasks · 4 dependencies")).toBeInTheDocument();
});

it.each(["Cancel", "Close", "Escape"])("discards task changes on %s", async (action) => {
  renderPage();
  fireEvent.click(screen.getByRole("button", { name: "Add task" }));
  const dialog = within(screen.getByRole("dialog", { name: "Add task" }));
  fireEvent.change(dialog.getByLabelText("Task name"), { target: { value: "Discard this" } });
  fireEvent.click(dialog.getByText("Execution settings"));
  fireEvent.change(dialog.getByLabelText("owner agent ID"), {
    target: { value: "discarded-agent" },
  });
  fireEvent.change(dialog.getByLabelText("owner sandbox provider"), {
    target: { value: "discarded-sandbox" },
  });
  fireEvent.click(dialog.getByRole("checkbox", { name: "Require review before continuing" }));
  if (action === "Escape") fireEvent.keyDown(document, { key: "Escape" });
  else fireEvent.click(dialog.getByRole("button", { name: action }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByLabelText("Task name")).toHaveValue("Investigate");
  expect(screen.getByText("4 tasks · 4 dependencies")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Add task" }));
  const reopened = within(screen.getByRole("dialog", { name: "Add task" }));
  fireEvent.click(reopened.getByText("Execution settings"));
  expect(reopened.getByLabelText("owner agent ID")).toHaveValue("replace-with-registered-agent-id");
  expect(reopened.getByLabelText("owner sandbox provider")).toHaveValue(
    "replace-with-sandbox-provider",
  );
  expect(reopened.getByLabelText("Task name")).toHaveValue("");
  expect(
    reopened.getByRole("checkbox", { name: "Require review before continuing" }),
  ).not.toBeChecked();
});

it("edits one task's execution and exports agent settings without live credentials", async () => {
  renderPage();
  fireEvent.click(screen.getByText("Execution settings"));
  fireEvent.change(screen.getByLabelText("owner agent ID"), { target: { value: "agent-own" } });
  fireEvent.click(screen.getByRole("button", { name: "Add agent" }));
  fireEvent.change(screen.getByLabelText("specialist_1 agent ID"), {
    target: { value: "agent-review" },
  });
  fireEvent.change(screen.getByLabelText("owner sandbox provider"), {
    target: { value: "analysis-sandbox" },
  });
  fireEvent.change(screen.getByLabelText("specialist_1 sandbox provider"), {
    target: { value: "review-sandbox" },
  });
  fireEvent.change(screen.getByLabelText("owner repository (optional)"), {
    target: { value: "https://github.com/example/work" },
  });
  fireEvent.change(screen.getByLabelText("owner repository (optional)"), {
    target: { value: "" },
  });
  fireEvent.change(screen.getByLabelText("Execution mode"), { target: { value: "parallel" } });
  fireEvent.change(screen.getByLabelText("Lead agent"), { target: { value: "specialist_1" } });
  expect(screen.getByRole("button", { name: "Remove specialist_1" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Test using"), { target: { value: "live" } });
  expect(await screen.findByText("Configure workflow server")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Test workflow" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Export YAML" }));
  await screen.findByLabelText("Generated YAML");
  const team = (posts[0].body.workflow as WorkflowDefinition).teams[0];
  expect(team.members.map((member) => member.agent_id)).toEqual(["agent-own", "agent-review"]);
  expect(team.members.every((member) => member.placement.kind === "sandbox")).toBe(true);
  expect(team.members.map((member) => member.placement.provider)).toEqual([
    "analysis-sandbox",
    "review-sandbox",
  ]);
  expect(team.members[0].placement).not.toHaveProperty("workspace");
  expect(team.strategy).toBe("parallel");
  expect(team.lead).toBe("specialist_1");
  expect(posts[0].body).not.toHaveProperty("backend");
  const otherTeams = (posts[0].body.workflow as WorkflowDefinition).teams.slice(1);
  expect(
    otherTeams.every(
      (other) => other.members.length === 1 && other.members[0].placement.kind === "sandbox",
    ),
  ).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  fireEvent.click(
    within(screen.getByRole("region", { name: "Workflow DAG" })).getByRole("button", {
      name: "Propose a change",
    }),
  );
  fireEvent.click(screen.getByText("Execution settings"));
  expect(screen.getByLabelText("owner agent ID")).toHaveValue("replace-with-registered-agent-id");
});

it.each([false, true])(
  "requires a configured workflow server connection for a live test (enabled: %s)",
  async (serverEnabled) => {
    enabledBackends = serverEnabled ? ["simulation", "server"] : ["simulation"];
    renderPage();
    fireEvent.click(screen.getByText("Execution settings"));
    fireEvent.change(screen.getByLabelText("Test using"), { target: { value: "live" } });
    const test = screen.getByRole("button", { name: "Test workflow" });
    if (!serverEnabled) {
      expect(await screen.findByText("Configure workflow server")).toBeInTheDocument();
      expect(test).toBeDisabled();
      return;
    }
    await waitFor(() => expect(test).toBeEnabled());
    fireEvent.click(test);
    await screen.findByRole("heading", { name: "Test workflow" });
    expect(posts[0].body).not.toHaveProperty("backend");
    expect(posts.find((post) => post.path === "/v1/workflows")?.body.backend).toBe("server");
  },
);

it.each(["Pass review", "Fail review", "Send feedback"])(
  "tests a compiled snapshot and submits %s",
  async (action) => {
    renderPage();
    await openTest();
    expect(posts[2]).toEqual({
      path: "/v1/workflows",
      body: {
        backend: "simulation",
        definition: "name: generated\nnodes: []\n",
        workflow_id: expect.any(String),
      },
    });
    fireEvent.change(screen.getByLabelText("Feedback or validation evidence"), {
      target: { value: "Reviewed revision one" },
    });
    fireEvent.click(screen.getByRole("button", { name: action }));
    await waitFor(() => expect(posts).toHaveLength(5));
    expect(posts[3].body).toMatchObject(
      action === "Send feedback"
        ? { kind: "feedback", node_id: "propose" }
        : {
            kind: "gate_result",
            node_id: "propose",
            candidate_id: "propose-1",
            outcome: { passed: action === "Pass review" },
          },
    );
    fireEvent.click(screen.getByRole("button", { name: "Back to designer" }));
    fireEvent.change(screen.getByLabelText("Workflow name"), {
      target: { value: "Edited after test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "View test" }));
    expect(screen.queryByText(/Edited after test/)).not.toBeInTheDocument();
  },
);

it("keeps invalid reconciliation input and surfaces the error", async () => {
  run.nodes.propose.status = "interrupted";
  run.nodes.propose.members.owner.turns = {
    turn: { status: "uncertain", remote_id: "invocation-one" },
  };
  renderPage();
  await waitFor(() => expect(screen.getByRole("button", { name: "Test workflow" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Test workflow" }));
  fireEvent.click(await screen.findByRole("button", { name: "Propose a change interrupted" }));
  fireEvent.click(screen.getByText("Reconcile an uncertain turn"));
  fireEvent.change(screen.getByLabelText("Reconciliation event JSON"), {
    target: { value: "{broken" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Submit reconciliation" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("valid JSON object");
  expect(screen.getByLabelText("Reconciliation event JSON")).toHaveValue("{broken");
});

it("shows field validation errors and preserves the design", async () => {
  renderPage();
  fireEvent.change(screen.getByLabelText("Workflow name"), {
    target: { value: "Keep this draft" },
  });
  vi.mocked(authenticatedFetch).mockResolvedValue(
    Response.json(
      {
        detail: [
          { loc: ["body", "workflow", "goal"], msg: "String should have at least 1 character" },
        ],
      },
      { status: 422 },
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "Export YAML" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("workflow → goal");
  expect(screen.getByLabelText("Workflow name")).toHaveValue("Keep this draft");
});

it("saves a design before execution and reuses its root for subsequent tests", async () => {
  renderPage();
  fireEvent.change(screen.getByLabelText("Workflow name"), { target: { value: "Rooted design" } });
  fireEvent.click(screen.getByRole("button", { name: "Save workflow" }));
  expect(await screen.findByRole("link", { name: "Open root session" })).toHaveAttribute(
    "href",
    "/c/root-one",
  );
  expect(screen.getByText(/Workflow saved/)).toBeInTheDocument();
  expect(posts.some((post) => post.path === "/v1/workflows")).toBe(false);
  const firstSave = posts.find((post) => post.path.includes("/designs/"))!;
  fireEvent.change(screen.getByLabelText("Workflow goal"), { target: { value: "New goal" } });
  expect(screen.getByText(/Unsaved changes/)).toBeInTheDocument();
  await openTest();
  expect(posts.filter((post) => post.path.includes("/designs/"))).toHaveLength(2);
  expect(posts.filter((post) => post.path.includes("/designs/"))[1].path).toBe(firstSave.path);
  expect(posts.find((post) => post.path === "/v1/workflows")?.body.workflow_id).toBe(
    firstSave.path.split("/").pop(),
  );
});

it("restores the saved DAG through its workflow URL", async () => {
  const workflowId = "a".repeat(32);
  const definition = executionDefinition(starterWorkflow());
  definition.name = "Saved DAG";
  const fetch = vi.mocked(authenticatedFetch).getMockImplementation()!;
  vi.mocked(authenticatedFetch).mockImplementation(async (url, init) =>
    String(url).endsWith("/designs/" + workflowId) && !init
      ? Response.json({
          workflow_id: workflowId,
          root_session_id: "saved-root",
          workflow: definition,
        })
      : fetch(url, init),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/workflows?workflow=" + workflowId]}>
        <WorkflowsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(await screen.findByLabelText("Workflow name")).toHaveValue("Saved DAG");
  expect(screen.getByRole("link", { name: "Open root session" })).toHaveAttribute(
    "href",
    "/c/saved-root",
  );
  expect(posts).toHaveLength(0);
});
