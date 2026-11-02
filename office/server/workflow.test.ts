import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { interviewProgress } from "../shared/interviewProgress.ts";
import type { AgentRoleId } from "../shared/sdlcTheme.ts";
import type { InterviewQuestion } from "../shared/workflowTypes.ts";
import { FakeOmnigent } from "./fakeOmnigent.ts";
import { StateStore } from "./stateStore.ts";
import { addRole, addTaskType, assign as assignRaci, nativeTeam, removeRole, removeTaskType, sdlcTeam, teamOf, updateRole } from "../shared/team.ts";
import { NATIVE_TEAM_TEMPLATE, effectiveRoleHarness } from "../shared/teamTemplates.ts";
import { templateFileYaml } from "../shared/teamFile.ts";
import { roleInstructions } from "./bundle.ts";
import { type McpCaller, WorkflowError, WorkflowService, normalizeRuntime } from "./workflow.ts";

const NOW = 1_800_000_000;

const RUNTIME = { harness: "openai-agents", runnerId: "runner_1" };

describe("WorkflowService", () => {
  let directory: string;
  let store: StateStore;
  let omnigent: FakeOmnigent;
  let workflow: WorkflowService;
  let clock: number;
  const notifications: string[] = [];

  const build = () =>
    new WorkflowService({
      omnigent,
      store,
      notifier: { notify: (title) => notifications.push(title) },
      mcpUrl: "http://127.0.0.1:6780/office/mcp",
      clock: () => clock,
    });

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "office-workflow-"));
    store = new StateStore(join(directory, "office-state.json"));
    omnigent = new FakeOmnigent();
    clock = NOW;
    notifications.length = 0;
    workflow = build();
  });

  afterEach(() => rmSync(directory, { recursive: true, force: true }));

  async function assign() {
    const view = await workflow.assignTheme("proj", { runtime: RUNTIME });
    return view.branch.id;
  }

  function caller(branchId: string, role: AgentRoleId): McpCaller {
    const link = store.state.workflow.roleSessions.find((entry) => entry.branchId === branchId && entry.role === role && entry.retiredAt === null);
    if (!link) throw new Error(`no ${role} session`);
    const resolved = workflow.callerForToken((link as unknown as { mcpToken: string }).mcpToken);
    if (!resolved) throw new Error("token did not resolve");
    return resolved;
  }

  async function task(branchId: string, taskType: string, extra: Record<string, unknown> = {}) {
    const result = await workflow.createTask(caller(branchId, "pm"), {
      taskType,
      title: `${taskType} task`,
      deliverable: "A deliverable",
      acceptanceCriteria: ["It works"],
      evidenceRequired: ["Test report"],
      ...extra,
    });
    return (result.task as { id: string }).id;
  }

  it("assigns SDLC with a labeled PM session, office MCP access, and runtime gate policies", async () => {
    const branchId = await assign();
    expect(omnigent.sessions).toHaveLength(1);
    const pm = omnigent.sessions[0];
    expect(pm.metadata).toMatchObject({
      project_id: "proj",
      labels: { "office.branch": branchId, "office.role": "pm", "office.theme": "sdlc", "office.temporary": "false" },
    });
    expect(pm.spec.prompt).toContain("You are the PM");
    expect(pm.spec.tools).toMatchObject({ office: { type: "mcp", url: "http://127.0.0.1:6780/office/mcp" } });
    expect(omnigent.operations).toContain(`bind:${pm.id}`);
    expect(omnigent.sessionPolicies(pm.id).map((policy) => policy.name)).toEqual(["office_merge_gate_v1", "office_deployment_gate_v1"]);
    await expect(workflow.assignTheme("proj", { runtime: RUNTIME })).rejects.toMatchObject({ code: "theme_already_assigned" });
  });

  it("requires a host (with workspace) or runner for role sessions", async () => {
    await expect(workflow.assignTheme("proj", { runtime: { harness: "claude-native" } })).rejects.toMatchObject({ code: "invalid_runtime" });
    await expect(workflow.assignTheme("proj", { runtime: { harness: "claude-native", hostId: "host_1" } })).rejects.toMatchObject({
      code: "invalid_runtime",
    });
  });

  it("recovers a missing PM coordinator using the project's configured workspace", async () => {
    let failFirst = true;
    const origCreate = omnigent.createBundledSession.bind(omnigent);
    omnigent.createBundledSession = async (bundle, metadata) => {
      if (failFirst) {
        failFirst = false;
        throw new Error("workspace required when host_id is set");
      }
      return origCreate(bundle, metadata);
    };
    omnigent.listProjects = async () => [{ id: "proj", name: "Shop", config: { host_id: "host_1", workspace: "/repo/shop" } }];
    await expect(workflow.assignTheme("proj", { runtime: { hostId: "host_1", workspace: "/repo/shop" } })).rejects.toMatchObject({
      code: "coordinator_start_failed",
    });
    const branchId = workflow.activeBranch("proj")!.id;
    delete store.state.workflow.branches[branchId].runtime.workspace;
    expect(workflow.coordinatorSession(branchId)).toBeNull();

    const sessionId = await workflow.ensureCoordinator(branchId);
    expect(sessionId).toBe("conv_1");
    expect(store.state.workflow.branches[branchId].runtime.workspace).toBe("/repo/shop");
    expect(omnigent.sessions[0].metadata).toMatchObject({ host_id: "host_1", workspace: "/repo/shop" });
  });

  it("creates tasks from the matrix and enforces the task contract", async () => {
    const branchId = await assign();
    const reviewId = await task(branchId, "code_review");
    const review = store.state.workflow.tasks[reviewId];
    expect(review.responsible).toEqual(["architect", "reviewer"]);
    expect(review.accountable).toBe("reviewer");
    expect(review.gate).toBe("merge");

    const requirementsId = await task(branchId, "requirements");
    expect(store.state.workflow.tasks[requirementsId].status).toBe("blocked");
    expect(workflow.pendingHumanRequests("proj")).toBe(1);

    await expect(task(branchId, "unknown")).rejects.toMatchObject({ code: "unknown_task_type" });
    await expect(task(branchId, "implementation", { evidenceRequired: [] })).rejects.toMatchObject({ code: "invalid_input" });
    await task(branchId, "implementation", { resources: ["src/cart.ts"] });
    await expect(task(branchId, "bug_fix", { resources: ["src/cart.ts"] })).rejects.toMatchObject({ code: "resource_owned" });
  });

  it("lets only the coordinator create and dispatch tasks, and starts role sessions on first dispatch", async () => {
    const branchId = await assign();
    const designId = await task(branchId, "architecture");
    const codeId = await task(branchId, "implementation", { dependsOn: [designId] });
    await expect(workflow.dispatchTask(caller(branchId, "pm"), { taskId: codeId })).rejects.toMatchObject({ code: "dependencies_unmet" });

    await workflow.dispatchTask(caller(branchId, "pm"), { taskId: designId, brief: "Keep it simple" });
    const architect = omnigent.sessions.find((entry) => (entry.metadata.labels as Record<string, string>)["office.role"] === "architect");
    expect(architect).toBeDefined();
    const brief = omnigent.messages.find((message) => message.sessionId === architect!.id)!;
    expect(brief.text).toContain("Deliverable: A deliverable");
    expect(brief.text).toContain("PM notes: Keep it simple");
    // Office relays are tagged by stable id so they never count as human interactions.
    expect(brief.stableId).toMatch(/^[0-9a-f]{32}$/);
    expect(workflow.isRelayedItem(brief.stableId!)).toBe(true);
    expect(store.state.workflow.tasks[designId].status).toBe("in_progress");

    await expect(workflow.createTask(caller(branchId, "architect"), { taskType: "implementation" })).rejects.toMatchObject({
      code: "coordinator_only",
    });
  });

  it("routes specialist messages through the coordinator", async () => {
    const branchId = await assign();
    await workflow.dispatchTask(caller(branchId, "pm"), { taskId: await task(branchId, "architecture") });
    await workflow.dispatchTask(caller(branchId, "pm"), { taskId: await task(branchId, "implementation") });
    await expect(workflow.messageRole(caller(branchId, "architect"), { role: "developer", text: "hi" })).rejects.toMatchObject({
      code: "route_through_coordinator",
    });
    await workflow.messageRole(caller(branchId, "architect"), { role: "pm", text: "Need a decision on the DB" });
    expect(omnigent.messages.at(-1)!.text).toContain("[From Architect via the office] Need a decision on the DB");
  });

  it("binds gate approvals to the artifact digest and enforces merge operations at runtime", async () => {
    const branchId = await assign();
    const reviewId = await task(branchId, "code_review");
    await workflow.dispatchTask(caller(branchId, "pm"), { taskId: reviewId });
    const reviewer = caller(branchId, "reviewer");

    await expect(workflow.requestGate(reviewer, { gate: "merge", taskId: reviewId, summary: "Ready" })).rejects.toMatchObject({
      code: "operation_required",
    });
    await expect(
      workflow.requestGate(reviewer, { gate: "merge", taskId: reviewId, summary: "Ready", operation: "npm test" }),
    ).rejects.toMatchObject({ code: "operation_not_gated" });
    const { gateId } = (await workflow.requestGate(reviewer, {
      gate: "merge",
      taskId: reviewId,
      summary: "PR 42 reviewed",
      artifactKind: "commit",
      artifactRef: "abc123",
      artifactDigest: "abc123",
      operation: "gh pr merge 42 --squash",
    })) as { gateId: string };
    expect(store.state.workflow.tasks[reviewId].status).toBe("awaiting_gate");
    expect(workflow.pendingHumanRequests("proj")).toBe(1);
    await new Promise((resolve) => setImmediate(resolve));
    expect(notifications).toContain("Shop: Merge approval needed");

    await expect(
      workflow.decideGate(branchId, gateId as string, { action: "approve", digest: "different", feedback: "" }),
    ).rejects.toMatchObject({ code: "stale_request" });

    clock = NOW + 60;
    const before = omnigent.sessionPolicies(reviewer.link.sessionId).find((policy) => policy.name.startsWith("office_merge_gate"))!;
    expect(before.expression).not.toContain("gh pr merge 42");
    await workflow.decideGate(branchId, gateId as string, { action: "approve", digest: "abc123", feedback: "" });
    const merge = omnigent.sessionPolicies(reviewer.link.sessionId).filter((policy) => policy.name.startsWith("office_merge_gate"));
    expect(merge).toHaveLength(1);
    expect(merge[0].name).toBe("office_merge_gate_v2");
    expect(merge[0].expression).toContain('"gh pr merge 42 --squash"');
    // The replacement policy is added before the old one is removed.
    const addIndex = omnigent.operations.indexOf("policy+:office_merge_gate_v2");
    expect(addIndex).toBeLessThan(omnigent.operations.indexOf(`policy-:${before.id}`));
    expect(workflow.latestDecisionAt("proj")).toBe(NOW + 60);

    const replay = await workflow.decideGate(branchId, gateId as string, { action: "approve", digest: "abc123", feedback: "" });
    expect(replay.replayed).toBe(true);
    await expect(
      workflow.decideGate(branchId, gateId as string, { action: "request_changes", digest: "abc123", feedback: "no" }),
    ).rejects.toMatchObject({ code: "already_decided" });

    // Completing the task ends the operation's approval.
    await workflow.updateTask(reviewer, { taskId: reviewId, status: "submitted", summary: "Merged", references: ["abc123"] });
    await workflow.completeTask(reviewer, { taskId: reviewId, summary: "Merged" });
    const after = omnigent.sessionPolicies(reviewer.link.sessionId).find((policy) => policy.name.startsWith("office_merge_gate"))!;
    expect(after.expression).not.toContain("gh pr merge 42");
  });

  it("requires the accountable role, evidence, and an approved gate to complete a task", async () => {
    const branchId = await assign();
    const designId = await task(branchId, "architecture");
    await workflow.dispatchTask(caller(branchId, "pm"), { taskId: designId });
    await expect(workflow.completeTask(caller(branchId, "pm"), { taskId: designId, summary: "done" })).rejects.toMatchObject({
      code: "not_accountable",
    });
    await expect(workflow.completeTask(caller(branchId, "architect"), { taskId: designId, summary: "done" })).rejects.toMatchObject({
      code: "gate_required",
    });
  });

  it("requires feedback for changes and returns the task to the worker", async () => {
    const branchId = await assign();
    const designId = await task(branchId, "architecture");
    await workflow.dispatchTask(caller(branchId, "pm"), { taskId: designId });
    const architect = caller(branchId, "architect");
    const { gateId } = (await workflow.requestGate(architect, {
      gate: "architecture",
      taskId: designId,
      summary: "Design doc",
      artifactKind: "file",
      artifactRef: "docs/design.md",
      artifactDigest: "sha-1",
    })) as { gateId: string };
    await expect(workflow.decideGate(branchId, gateId, { action: "request_changes", digest: "sha-1", feedback: "" })).rejects.toMatchObject({
      code: "feedback_required",
    });
    await workflow.decideGate(branchId, gateId, { action: "request_changes", digest: "sha-1", feedback: "Add caching" });
    expect(store.state.workflow.tasks[designId].status).toBe("in_progress");
    expect(omnigent.messages.some((message) => message.sessionId === architect.link.sessionId && message.text.includes("Add caching"))).toBe(true);
  });

  it("marks approvals stale when requirements change, cascading to dependent gates", async () => {
    const branchId = await assign();
    const pm = caller(branchId, "pm");
    await workflow.updateBoard(branchId, 0, [{ title: "Checkout", description: "Pay by card", acceptanceCriteria: ["Card accepted"] }]);
    const requirementsGate = (await workflow.requestGate(pm, { gate: "requirements", summary: "Board v1" })) as { gateId: string };
    const board = store.state.workflow.boards[branchId];
    await workflow.decideGate(branchId, requirementsGate.gateId, { action: "approve", digest: board.digest, feedback: "" });

    const designId = await task(branchId, "architecture");
    await workflow.dispatchTask(pm, { taskId: designId });
    const architectureGate = (await workflow.requestGate(caller(branchId, "architect"), {
      gate: "architecture",
      taskId: designId,
      summary: "Design",
      artifactKind: "file",
      artifactRef: "docs/design.md",
      artifactDigest: "sha-2",
    })) as { gateId: string };

    await expect(workflow.updateBoard(branchId, 0, [])).rejects.toMatchObject({ code: "board_conflict" });
    await workflow.updateBoard(branchId, 1, [{ title: "Checkout", description: "Pay by card or wallet", acceptanceCriteria: [] }]);
    expect(store.state.workflow.gates[requirementsGate.gateId].status).toBe("stale");
    expect(store.state.workflow.gates[architectureGate.gateId].status).toBe("stale");
    await expect(
      workflow.decideGate(branchId, architectureGate.gateId, { action: "approve", digest: "sha-2", feedback: "" }),
    ).rejects.toMatchObject({ code: "stale_request" });
    // A decision that later went stale still counts as a meaningful interaction.
    expect(workflow.latestDecisionAt("proj")).toBe(NOW);
  });

  it("accepts PM proposals only against the current board revision", async () => {
    const branchId = await assign();
    const pm = caller(branchId, "pm");
    const first = (await workflow.proposeBoard(pm, { requirements: [{ title: "Search" }], rationale: "From interview" })) as { proposalId: string };
    await workflow.updateBoard(branchId, 0, [{ title: "Login", description: "", acceptanceCriteria: [] }]);
    await expect(workflow.decideProposal(branchId, first.proposalId, "accept", "")).rejects.toMatchObject({ code: "stale_request" });
    const second = (await workflow.proposeBoard(pm, { requirements: [{ title: "Login" }, { title: "Search" }] })) as { proposalId: string };
    await workflow.decideProposal(branchId, second.proposalId, "accept", "Looks right");
    expect(store.state.workflow.boards[branchId].requirements.map((entry) => entry.title)).toEqual(["Login", "Search"]);
  });

  it("asks interview questions one at a time", async () => {
    const branchId = await assign();
    const pm = caller(branchId, "pm");
    const { questionId } = (await workflow.askInterview(pm, { question: "Who are the users?" })) as { questionId: string };
    await expect(workflow.askInterview(pm, { question: "What else?" })).rejects.toMatchObject({ code: "question_pending" });
    const answer = await workflow.answerInterview(branchId, questionId, "Shoppers");
    expect(answer.text).toContain("Shoppers");
    expect(answer.sessionId).toBe(pm.link.sessionId);
    await expect(workflow.answerInterview(branchId, questionId, "Admins")).rejects.toMatchObject({ code: "already_decided" });
    await workflow.askInterview(pm, { question: "What else?" });
  });

  it("tracks an interview agenda: planned, asked, answered, added, and skipped questions", async () => {
    const branchId = await assign();
    const pm = caller(branchId, "pm");
    workflow.interviewKickoff(branchId, "checkout");
    const plan = workflow.planInterview(pm, { questions: ["Who pays?", "Which cards?", "Refunds?", "Who pays?"] }) as {
      added: { id: string; origin: string }[];
    };
    expect(plan.added.map((entry) => entry.origin)).toEqual(["plan", "plan", "plan"]);
    await expect(workflow.answerInterview(branchId, plan.added[0].id, "Early")).rejects.toMatchObject({ code: "not_asked" });
    await workflow.askInterview(pm, { questionId: plan.added[0].id });
    await workflow.answerInterview(branchId, plan.added[0].id, "Shoppers");
    workflow.planInterview(pm, { questions: ["Guest checkout?"] });
    workflow.skipInterview(pm, { questionId: plan.added[2].id, reason: "Out of scope" });
    await workflow.askInterview(pm, { question: "Which currency?" });
    const view = workflow.view(branchId);
    const agenda = view.interviews.filter((question) => question.interviewId === view.currentInterview?.id);
    expect(interviewProgress(agenda).counts).toEqual({ answered: 1, awaiting: 1, upcoming: 1, added: 1, skipped: 1 });
    expect(workflow.pendingHumanRequests("proj")).toBe(1);

    // A new interview skips the previous interview's unasked questions.
    await workflow.answerInterview(branchId, agenda.find((question) => question.status === "asked")!.id, "EUR");
    workflow.interviewKickoff(branchId, "shipping");
    const previous = store.state.workflow.interviews;
    expect(Object.values(previous).filter((question) => question.status === "planned")).toHaveLength(0);
    expect(workflow.view(branchId).currentInterview?.topic).toBe("shipping");
  });

  it("upgrades interview questions saved before agendas existed", async () => {
    const branchId = await assign();
    const legacy = (id: string, answer: string | null) =>
      ({ id, branchId, question: id, askedAt: NOW, answer, answeredAt: answer ? NOW : null }) as unknown as InterviewQuestion;
    store.state.workflow.interviews = { old_answered: legacy("old_answered", "Yes"), old_open: legacy("old_open", null) };
    workflow = build();
    const view = workflow.view(branchId);
    expect(view.interviews.map((question) => question.status)).toEqual(["answered", "asked"]);
    expect(view.interviews.every((question) => question.interviewId === view.currentInterview?.id)).toBe(true);
    await workflow.answerInterview(branchId, "old_open", "Now answered");
  });

  it("records why a role could not start and clears it once the role starts", async () => {
    const branchId = await assign();
    const create = omnigent.createBundledSession.bind(omnigent);
    omnigent.createBundledSession = async () => {
      throw Object.assign(new Error("Harness 'copilot' is not configured on the target host"), { code: "harness_not_configured" });
    };
    await expect(workflow.rolloverRole(branchId, "reviewer", "")).rejects.toThrow("not configured");
    expect(workflow.roleStartFailure(branchId, "reviewer")).toMatchObject({ code: "harness_not_configured", harness: "openai-agents" });
    omnigent.createBundledSession = create;
    await workflow.rolloverRole(branchId, "reviewer", "");
    expect(workflow.roleStartFailure(branchId, "reviewer")).toBeNull();
  });

  it("closes temporary workers by archiving their sessions, leaving their tasks for the PM", async () => {
    const branchId = await assign();
    const taskId = await task(branchId, "architecture");
    await workflow.dispatchTask(caller(branchId, "pm"), { taskId, temporary: true });
    const temporary = workflow.view(branchId).roleSessions.find((link) => link.temporary && link.retiredAt === null);
    expect(temporary).toBeDefined();
    const pmSession = caller(branchId, "pm").link.sessionId;
    // Only live temporary workers of the room can be closed.
    await expect(workflow.closeTemporaryWorkers(branchId, [pmSession])).rejects.toMatchObject({ code: "not_temporary" });
    await expect(workflow.closeTemporaryWorkers(branchId, [])).rejects.toMatchObject({ code: "no_sessions" });

    const result = await workflow.closeTemporaryWorkers(branchId, [temporary!.sessionId]);
    expect(result).toEqual({ closed: [temporary!.sessionId], failed: [] });
    expect(omnigent.interrupted).toContain(temporary!.sessionId);
    expect(omnigent.archived).toEqual([temporary!.sessionId]);
    const view = workflow.view(branchId);
    expect(view.roleSessions.find((link) => link.sessionId === temporary!.sessionId)?.retiredAt).not.toBeNull();
    expect(view.tasks.find((entry) => entry.id === taskId)?.status).toBe("in_progress");
    expect(view.events.map((event) => event.summary)).toContainEqual(expect.stringMatching(/PO closed the temporary .+; the task is still in progress/));
    await expect(workflow.closeTemporaryWorkers(branchId, [temporary!.sessionId])).rejects.toMatchObject({ code: "not_temporary" });
  });

  it("closes replaced sessions, archiving them, but never a role's current session", async () => {
    const branchId = await assign();
    const oldPm = caller(branchId, "pm").link.sessionId;
    await workflow.rolloverRole(branchId, "pm", "Context is full.");
    const newPm = caller(branchId, "pm").link.sessionId;
    await expect(workflow.closeReplacedSessions(branchId, [newPm])).rejects.toMatchObject({ code: "not_replaced" });
    await expect(workflow.closeReplacedSessions(branchId, ["s_elsewhere"])).rejects.toMatchObject({ code: "not_replaced" });
    await expect(workflow.closeReplacedSessions(branchId, [])).rejects.toMatchObject({ code: "no_sessions" });

    expect(await workflow.closeReplacedSessions(branchId, [oldPm])).toEqual({ closed: [oldPm], failed: [] });
    expect(omnigent.archived).toEqual([oldPm]);
    expect(omnigent.archived).not.toContain(newPm);
    expect(workflow.view(branchId).events.map((event) => event.summary)).toContainEqual("PO closed 1 replaced session (PM).");
  });

  it("lets the PO change the room's RACI, which new tasks follow", async () => {
    const branchId = await assign();
    const before = await task(branchId, "implementation");
    await workflow.setTeam(branchId, assignRaci(sdlcTeam(), "implementation", "architect", "A"));
    const after = await task(branchId, "implementation");
    const tasks = workflow.view(branchId).tasks;
    // Existing tasks keep their roles; new ones follow the room's matrix.
    expect(tasks.find((entry) => entry.id === before)).toMatchObject({ responsible: ["developer"], accountable: "developer" });
    expect(tasks.find((entry) => entry.id === after)).toMatchObject({ responsible: ["developer"], accountable: "architect" });
    const snapshot = workflow.boardSnapshot(caller(branchId, "pm")) as { matrix: { taskType: string; assignments: Record<string, string> }[] };
    expect(snapshot.matrix.find((row) => row.taskType === "implementation")?.assignments).toMatchObject({ architect: "A", developer: "R" });
    expect(workflow.view(branchId).events.map((event) => event.summary)).toContainEqual(expect.stringContaining("Architect accountable for Write code and unit tests"));
    // A task type cannot be left without a responsible role.
    await expect(workflow.setTeam(branchId, assignRaci(sdlcTeam(), "test_plan", "qa", "A"))).rejects.toThrow(/needs at least one responsible role/);
    // Templates carry the team, and switching to the default brings the SDLC team back.
    const template = workflow.saveTeamTemplate(branchId, "Architect owns code", "");
    expect(template.team?.taskTypes.find((entry) => entry.id === "implementation")?.assignments).toMatchObject({ architect: "A", developer: "R" });
    const reset = await workflow.applyTeamTemplate(branchId, "builtin_default", false);
    expect(reset.changed).toEqual(expect.arrayContaining(["architect", "developer"]));
    expect(store.state.workflow.branches[branchId].runtime.team).toBeUndefined();
  });

  it("runs custom roles and task types, and retires roles taken off the team", async () => {
    const branchId = await assign();
    const withDesigner = addRole(sdlcTeam(), "UX Designer");
    const designer = withDesigner.roleId;
    const team = updateRole(withDesigner.team, designer, { brief: "You design screens and flows." });
    const withType = addTaskType(team, "Design screens");
    // A new task type starts with the PM as R+A; handing it to the designer leaves the PM responsible until changed.
    const handedOver = assignRaci(assignRaci(withType.team, withType.taskTypeId, designer, "RA"), withType.taskTypeId, "pm", "I");
    await workflow.setTeam(branchId, handedOver);
    expect(workflow.view(branchId).events.map((event) => event.summary)).toContainEqual(expect.stringContaining("added UX Designer"));

    // The PM sees the room's task types and roles, and the designer starts with its own brief.
    const pm = caller(branchId, "pm");
    const created = await workflow.createTask(pm, {
      taskType: withType.taskTypeId,
      title: "Checkout screens",
      deliverable: "Mockups",
      acceptanceCriteria: ["Covers checkout"],
      evidenceRequired: ["Figma link"],
    });
    const taskId = (created.task as { id: string }).id;
    expect(workflow.view(branchId).tasks.find((entry) => entry.id === taskId)).toMatchObject({ responsible: [designer], accountable: designer });
    await workflow.dispatchTask(pm, { taskId });
    const spec = omnigent.sessions.at(-1)?.spec as { name: string; prompt: string };
    expect(spec.name).toBe(`sdlc_${designer}`);
    expect(spec.prompt).toContain("Your role: UX Designer.");
    expect(spec.prompt).toContain("You design screens and flows.");
    const designerSession = workflow.view(branchId).roleSessions.find((link) => link.role === designer)!.sessionId;
    await workflow.messageRole(pm, { role: designer, text: "Please start." });

    // Taking the role off the team stops and retires its session; the task stays.
    const without = removeRole(teamOf(store.state.workflow.branches[branchId].runtime), designer);
    await workflow.setTeam(branchId, without.taskTypes.reduce((next, entry) => (entry.id === withType.taskTypeId ? removeTaskType(next, entry.id) : next), without));
    expect(omnigent.interrupted).toContain(designerSession);
    expect(workflow.view(branchId).roleSessions.find((link) => link.sessionId === designerSession)?.retiredAt).not.toBeNull();
    await expect(workflow.messageRole(pm, { role: designer, text: "Still there?" })).rejects.toMatchObject({ code: "invalid_role" });
    expect(workflow.view(branchId).tasks.find((entry) => entry.id === taskId)).toBeDefined();
  });

  it("saves the team as a template and switches rooms between templates", async () => {
    const branchId = await assign();
    const runtime = () => store.state.workflow.branches[branchId].runtime;
    await workflow.switchRoleHarness(branchId, ["pm"], "claude-sdk", "opus", "Try Claude");
    await workflow.setRoleInstructions(branchId, "qa", "Test in Safari too.", false);
    const saved = workflow.saveTeamTemplate(branchId, "Claude PM", "PM on Claude, QA in Safari");
    // The room runs every role on openai-agents, so the template keeps that too.
    expect(saved.roles).toMatchObject({
      pm: { harness: "claude-sdk", model: "opus" },
      qa: { harness: "openai-agents", instructions: "Test in Safari too." },
      developer: { harness: "openai-agents" },
    });
    expect(runtime().teamTemplateId).toBe(saved.id);
    // Saving under the same name replaces it; the built-in name is reserved.
    expect(workflow.saveTeamTemplate(branchId, "claude pm", "").id).toBe(saved.id);
    expect(() => workflow.saveTeamTemplate(branchId, "Default team", "")).toThrow(WorkflowError);
    expect(workflow.teamTemplates().map((template) => template.name)).toEqual(["Default team", "Native (single agent)", "claude pm"]);

    // Switching to the default resets every role; the running PM restarts on request.
    const oldPm = caller(branchId, "pm").link.sessionId;
    const result = await workflow.applyTeamTemplate(branchId, "builtin_default", true);
    expect(result.changed).toEqual(["pm", "architect", "developer", "reviewer", "qa", "devops"]);
    expect(runtime()).toMatchObject({ teamTemplateId: "builtin_default", roleHarnesses: {}, roleCustomInstructions: {} });
    expect(caller(branchId, "pm").link.sessionId).not.toBe(oldPm);
    expect(workflow.view(branchId).events.map((event) => event.summary)).toContainEqual(expect.stringContaining('switched the team to "Default team": PM (harness, model)'));

    // And back again, without restarting.
    await workflow.applyTeamTemplate(branchId, saved.id, false);
    expect(runtime().roleHarnesses).toMatchObject({ pm: "claude-sdk", qa: "openai-agents" });
    expect(runtime().roleCustomInstructions).toEqual({ qa: "Test in Safari too." });
    workflow.deleteTeamTemplate(saved.id);
    await expect(workflow.applyTeamTemplate(branchId, saved.id, false)).rejects.toMatchObject({ code: "template_not_found" });
    expect(() => workflow.deleteTeamTemplate("builtin_default")).toThrow(WorkflowError);
  });

  it("imports a team file as a template, keeping built-in names free", async () => {
    const branchId = await assign();
    const yaml = templateFileYaml(NATIVE_TEAM_TEMPLATE);
    const first = workflow.importTeamTemplate(yaml);
    expect(first).toMatchObject({ replaced: false, template: { name: "Native (single agent) (imported)", team: nativeTeam() } });
    // Importing the same name again replaces it.
    expect(workflow.importTeamTemplate(yaml)).toMatchObject({ replaced: true, template: { id: first.template.id } });
    expect(() => workflow.importTeamTemplate("kind: nope")).toThrow(expect.objectContaining({ code: "invalid_team_file" }));
    await workflow.applyTeamTemplate(branchId, first.template.id, false);
    expect(workflow.roleName(branchId, "pm")).toBe("Full Stack Developer (FSD)");
  });

  it("switches a room to the native theme: one PM on a native harness doing every task type", async () => {
    const branchId = await assign();
    const result = await workflow.applyTeamTemplate(branchId, "builtin_native", false);
    expect(result.changed).toEqual(["pm"]);
    const view = workflow.view(branchId);
    const runtime = store.state.workflow.branches[branchId].runtime;
    expect(runtime.team?.roles.map((role) => role.id)).toEqual(["pm"]);
    expect(effectiveRoleHarness(runtime, "pm")).toBe("claude-native");
    // The one agent is the Full Stack Developer in this room's prompt and events.
    expect(workflow.roleName(branchId, "pm")).toBe("Full Stack Developer (FSD)");
    expect(roleInstructions({ role: "pm", projectName: "Demo", runtime })).toContain("Your role: Full Stack Developer (FSD).");
    expect(view.events.map((event) => event.summary)).toContainEqual(expect.stringContaining("Developer (leaves the team)"));
    expect(() => workflow.deleteTeamTemplate("builtin_native")).toThrow(WorkflowError);
    expect(() => workflow.saveTeamTemplate(branchId, "native (single agent)", "")).toThrow(WorkflowError);
  });

  it("saves a role's tool and policy choices, marking only session changes as needing a restart", async () => {
    const branchId = await assign();
    const available = ["office_board_get", "office_task_update", "office_message_role", "office_link_publish"];
    await workflow.setRoleToolSettings(branchId, "developer", { disabledTools: ["office_link_publish"] }, available, false);
    const runtime = store.state.workflow.branches[branchId].runtime;
    expect(runtime.roleToolSettings?.developer).toEqual({ disabledTools: ["office_link_publish"], updatedAt: undefined });
    await workflow.setRoleToolSettings(branchId, "developer", { riskPolicy: "deny" }, available, false);
    expect(runtime.roleToolSettings?.developer?.updatedAt).toEqual(expect.any(Number));
    await expect(workflow.setRoleToolSettings(branchId, "developer", { disabledTools: ["office_task_update"] }, available, false)).rejects.toMatchObject({
      code: "invalid_tool_settings",
    });
    expect(workflow.view(branchId).events.map((event) => event.summary)).toContainEqual(expect.stringContaining("risk policy: deny risky commands"));
  });

  it("shows each role's prompt and applies the PO's instructions, restarting a running role on request", async () => {
    const branchId = await assign();
    const oldPm = caller(branchId, "pm").link.sessionId;
    await workflow.setRoleInstructions(branchId, "qa", "  Test in Safari too.  ", false);
    await workflow.setRoleInstructions(branchId, "pm", "Keep standups short.", true);
    const config = await workflow.teamConfig(branchId);
    expect(config.find((entry) => entry.role === "qa")).toMatchObject({ customInstructions: "Test in Safari too." });
    expect(config.find((entry) => entry.role === "qa")?.prompt).toContain("PO instructions for this role:\nTest in Safari too.");
    // The PM restarted onto the new prompt; QA has not started, so it simply starts with it later.
    expect(caller(branchId, "pm").link.sessionId).not.toBe(oldPm);
    expect(omnigent.sessions.at(-1)?.spec.prompt).toContain("Keep standups short.");
    await workflow.setRoleInstructions(branchId, "qa", "", false);
    expect((await workflow.teamConfig(branchId)).find((entry) => entry.role === "qa")?.customInstructions).toBe("");
    await expect(workflow.setRoleInstructions(branchId, "po", "x", false)).rejects.toMatchObject({ code: "invalid_role" });
  });

  it("switches roles to another harness and restarts the running ones with a handoff", async () => {
    const branchId = await assign();
    const oldPm = caller(branchId, "pm").link.sessionId;
    store.state.workflow.branches[branchId].runtime.roleModels = { pm: "claude-opus", qa: "claude-haiku" };
    await workflow.switchRoleHarness(branchId, ["pm", "qa"], "codex", "", "Claude quota is spent.");
    const runtime = store.state.workflow.branches[branchId].runtime;
    expect(runtime.roleHarnesses).toMatchObject({ pm: "codex", qa: "codex" });
    expect(runtime.roleModels).toEqual({});
    const newPm = caller(branchId, "pm").link.sessionId;
    expect(newPm).not.toBe(oldPm);
    expect((omnigent.sessions.at(-1)?.spec.executor as { config: { harness: string } }).config.harness).toBe("codex");
    // QA never started, so it simply starts on the new harness later.
    expect(omnigent.sessions).toHaveLength(2);
    await expect(workflow.switchRoleHarness(branchId, [], "codex", "", "")).rejects.toMatchObject({ code: "invalid_role" });

    // Changing only the model keeps the harness and restarts the role on that model.
    await workflow.switchRoleHarness(branchId, ["pm"], "codex", "gpt-6.1-sol", "Use a stronger model.", "high");
    expect(runtime.roleModels).toEqual({ pm: "gpt-6.1-sol" });
    expect(runtime.roleEfforts).toEqual({ pm: "high" });
    expect(omnigent.sessions.at(-1)?.spec.executor).toMatchObject({ config: { harness: "codex" }, model: "gpt-6.1-sol", reasoning_effort: "high" });
    expect(workflow.view(branchId).events.map((event) => event.summary)).toContainEqual(
      expect.stringContaining("PM now uses codex (gpt-6.1-sol, high effort)"),
    );
    await expect(workflow.switchRoleHarness(branchId, ["pm"], "codex", "", "", "extreme")).rejects.toMatchObject({ code: "invalid_effort" });
  });

  it("keeps result links from the PO and roles, updating by URL and refusing unsafe ones", async () => {
    const branchId = await assign();
    const pm = caller(branchId, "pm");
    const shared = workflow.publishLink(pm, { label: "Staging", url: "https://staging.example.com/app" });
    expect(shared).toMatchObject({ addedBy: "pm", label: "Staging", url: "https://staging.example.com/app" });
    workflow.publishLink(pm, { label: "Staging v2", url: "https://staging.example.com/app" });
    workflow.addProjectLink("proj", { label: "PR #42", url: "https://github.com/acme/shop/pull/42", description: "Checkout" }, "po");
    expect(workflow.projectLinks("proj").map((link) => [link.label, link.addedBy])).toEqual([
      ["Staging v2", "pm"],
      ["PR #42", "po"],
    ]);
    expect(workflow.view(branchId).events.map((event) => event.summary)).toContainEqual(expect.stringContaining("PM shared a link: Staging"));
    expect(() => workflow.addProjectLink("proj", { label: "x", url: "javascript:alert(1)" }, "po")).toThrow(/http/);
    expect(() => workflow.addProjectLink("proj", { label: "", url: "https://example.com" }, "po")).toThrow(/label/);
    workflow.removeProjectLink("proj", shared.id);
    expect(workflow.projectLinks("proj")).toHaveLength(1);
    expect(() => workflow.removeProjectLink("proj", shared.id)).toThrow(WorkflowError);
  });

  it("tells a restarted PM about the interview it was running", async () => {
    const branchId = await assign();
    const pm = caller(branchId, "pm");
    workflow.interviewKickoff(branchId, "checkout");
    expect(workflow.interviewAwaitingCoordinator(branchId)?.reason).toContain("not planned or asked");
    const plan = workflow.planInterview(pm, { questions: ["Who pays?", "Which cards?"] }) as { added: { id: string }[] };
    await workflow.askInterview(pm, { questionId: plan.added[0].id });
    expect(workflow.interviewAwaitingCoordinator(branchId)).toBeNull();
    await workflow.rolloverRole(branchId, "pm", "The harness crashed.");
    const brief = omnigent.messages.at(-1)?.text ?? "";
    expect(brief).toContain('An interview about "checkout" is in progress.');
    expect(brief).toContain("The PO has not answered yet: Who pays?");
    expect(brief).toContain(`${plan.added[1].id}: Which cards?`);
    await workflow.answerInterview(branchId, plan.added[0].id, "Shoppers");
    expect(workflow.interviewAwaitingCoordinator(branchId)?.reason).toContain("has not asked the next one");
  });

  it("stops every role immediately and rolls a role over with a recorded handoff", async () => {
    const branchId = await assign();
    const oldPm = caller(branchId, "pm").link.sessionId;
    const result = await workflow.stopBranch(branchId);
    expect(result.stopped).toBe(1);
    expect(omnigent.interrupted).toEqual([oldPm]);

    await workflow.rolloverRole(branchId, "pm", "Coordinator failed");
    const newPm = caller(branchId, "pm").link;
    expect(newPm.sessionId).not.toBe(oldPm);
    expect(newPm.handoffFromSessionId).toBe(oldPm);
    expect(omnigent.messages.at(-1)!.text).toContain("[Office handoff]");
    expect(store.state.workflow.roleSessions.find((link) => link.sessionId === oldPm)!.retiredAt).not.toBeNull();
  });

  it("keeps workflow state and role identity across a service restart", async () => {
    const branchId = await assign();
    const token = (store.state.workflow.roleSessions[0] as unknown as { mcpToken: string }).mcpToken;
    const reloaded = new WorkflowService({
      omnigent,
      store: new StateStore(join(directory, "office-state.json")),
      notifier: { notify: () => undefined },
      mcpUrl: "http://127.0.0.1:6780/office/mcp",
      clock: () => clock,
    });
    expect(reloaded.activeBranch("proj")?.id).toBe(branchId);
    expect(reloaded.callerForToken(token)?.link.role).toBe("pm");
    expect(reloaded.callerForToken("not-a-token")).toBeNull();
    expect(() => reloaded.view("branch_missing")).toThrow(WorkflowError);
  });
});

describe("normalizeRuntime", () => {
  it("keeps per-role efforts Omnigent knows and drops other values and roles", () => {
    const runtime = normalizeRuntime({
      hostId: "host",
      workspace: "/repo",
      roleEfforts: { pm: "high", qa: "extreme", po: "low", developer: 3 },
    });
    expect(runtime.roleEfforts).toEqual({ pm: "high" });
    expect(normalizeRuntime({ hostId: "host", workspace: "/repo", roleEfforts: { qa: "" } }).roleEfforts).toBeUndefined();
  });
});
