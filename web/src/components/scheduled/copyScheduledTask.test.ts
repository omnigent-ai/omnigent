import { describe, expect, it } from "vitest";
import { testAgent } from "@/test/agentFixtures";
import { scheduledTaskCopyInput, type ScheduledTask } from "@/lib/scheduledTasksApi";
import { prepareScheduledTaskCopy, scheduledTaskCopyAgents } from "./copyScheduledTask";
import {
  emptyTriggerFields,
  readTriggerWorkerSettings,
  renderTriggerPrompt,
  updateTriggerWorkerSettings,
} from "./triggerTemplates";

const source: ScheduledTask = {
  id: "original",
  name: "Triage",
  prompt: "Triage tickets",
  rrule: "FREQ=DAILY",
  timezone: "UTC",
  ownerUserId: "alice",
  agentId: "agent",
  createdAt: 1,
  updatedAt: 2,
  modelOverride: "sonnet",
  reasoningEffort: "high",
  permissionMode: "acceptEdits",
  maxCostUsd: 4,
  sessionMode: "continue",
  sessionId: "original-session",
  workspace: "/repo",
  baseBranch: "HEAD",
  hostId: "host",
  executionTarget: "connected_host",
  projectId: "project",
  state: "active",
  lastRunAt: 3,
  lastRunStatus: "succeeded",
  lastRunConversationId: "run-session",
  nextRunAt: "tomorrow",
};
const agents = [testAgent("agent", "claude-native-ui", { harness: "claude-native" })];
const hosts = [{ host_id: "host", name: "My host", owner: "bob", status: "online" as const }];
const projects = [{ id: "project", name: "My project" }];

describe("automation copies", () => {
  it("preserves compatible configuration and excludes ownership and runtime state from creation", () => {
    const { draft, issues } = prepareScheduledTaskCopy(source, agents, hosts, projects, false);
    expect(issues).toEqual([]);
    expect(scheduledTaskCopyInput(draft)).toEqual({
      sharedTaskId: "original",
      name: "Triage",
      prompt: "Triage tickets",
      rrule: "FREQ=DAILY",
      timezone: "UTC",
      agentId: "agent",
      modelOverride: "sonnet",
      reasoningEffort: "high",
      permissionMode: "acceptEdits",
      maxCostUsd: 4,
      sessionMode: "continue",
      executionTarget: "connected_host",
      hostId: "host",
      workspace: "/repo",
      baseBranch: "HEAD",
      projectId: "project",
    });
    expect(source.sessionId).toBe("original-session");
  });
  it("asks for replacements without carrying the original user's host, project, or unavailable agent", () => {
    const { draft, issues } = prepareScheduledTaskCopy(source, [], [], [], false);
    expect(issues).toHaveLength(3);
    expect(draft).toMatchObject({
      agentId: "",
      hostId: null,
      projectId: null,
      workspace: null,
      baseBranch: null,
      modelOverride: null,
      reasoningEffort: null,
      permissionMode: null,
      prompt: source.prompt,
      rrule: source.rrule,
      timezone: source.timezone,
      sessionMode: "continue",
      maxCostUsd: 4,
    });
  });
  it("preserves sandbox repositories when supported and asks for a host otherwise", () => {
    const sandbox = {
      ...source,
      executionTarget: "managed_sandbox" as const,
      hostId: null,
      workspace: "https://github.com/org/repo",
      baseBranch: null,
      projectId: null,
    };
    expect(prepareScheduledTaskCopy(sandbox, agents, [], [], true).issues).toEqual([]);
    const unsupported = prepareScheduledTaskCopy(sandbox, agents, [], [], false);
    expect(unsupported.issues).toHaveLength(1);
    expect(unsupported.draft).toMatchObject({
      executionTarget: "connected_host",
      hostId: null,
      workspace: null,
    });
  });
});

it("keeps a shared custom agent available without adding it to the caller's catalog", () => {
  const sharedAgent = testAgent("private", "Research brief", {
    harness: "claude-native",
    shared: true,
  });
  const task = { ...source, agentId: sharedAgent.id, sharedAgent };
  const choices = scheduledTaskCopyAgents(task, agents);
  expect(choices).toContain(sharedAgent);
  expect(agents).not.toContain(sharedAgent);
  expect(scheduledTaskCopyAgents(task, choices)).toBe(choices);
  expect(prepareScheduledTaskCopy(task, choices, hosts, projects, false).issues).toEqual([]);
});

it("lets a trigger's workers use the copied custom agent instead of the owner's agent ID", () => {
  const sharedAgent = testAgent("private", "Research brief", {
    harness: "claude-native",
    shared: true,
  });
  const prompt = updateTriggerWorkerSettings(
    renderTriggerPrompt(
      "jira",
      { ...emptyTriggerFields("jira"), scope: "DEMO", instructions: "Summarize the ticket." },
      { placement: "same_host", sessionLink: "https://example.test/c/SESSION" },
    ),
    { agentId: sharedAgent.id, model: "", effort: "" },
  )!;
  const task = { ...source, agentId: sharedAgent.id, sharedAgent, prompt };
  const { draft, issues } = prepareScheduledTaskCopy(
    task,
    scheduledTaskCopyAgents(task, agents),
    hosts,
    projects,
    false,
  );
  expect(issues).toEqual([]);
  expect(readTriggerWorkerSettings(draft.prompt)?.agentId).toBeNull();
  expect(draft.prompt).toContain("Summarize the ticket.");
  expect(readTriggerWorkerSettings(prompt)?.agentId).toBe("private");
});
