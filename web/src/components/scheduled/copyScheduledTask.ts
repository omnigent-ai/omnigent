import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import type { ProjectSummary } from "@/hooks/useConversations";
import type { ScheduledTask } from "@/lib/scheduledTasksApi";
import { harnessUnconfiguredOnHost } from "@/lib/harnessSetup";
import { nativeCodingAgentForAvailableAgent } from "@/lib/nativeCodingAgents";
import { readTriggerWorkerSettings, updateTriggerWorkerSettings } from "./triggerTemplates";

/** Keep the shared custom agent selectable until its recipient-owned copy is saved. */
export function scheduledTaskCopyAgents(
  source: ScheduledTask | null,
  agents: AvailableAgent[],
): AvailableAgent[] {
  const shared = source?.sharedAgent;
  return shared && !agents.some((agent) => agent.id === shared.id) ? [...agents, shared] : agents;
}

/** Keep portable configuration and identify choices belonging to the original owner. */
export function prepareScheduledTaskCopy(
  source: ScheduledTask,
  agents: AvailableAgent[],
  hosts: Host[],
  projects: ProjectSummary[],
  managedSandboxesEnabled: boolean,
): { draft: ScheduledTask; issues: string[] } {
  const draft = {
    ...source,
    sessionId: null,
    lastRunAt: null,
    lastRunStatus: null,
    lastRunConversationId: null,
    nextRunAt: null,
  };
  const issues: string[] = [];
  const agent = agents.find((item) => item.id === source.agentId);
  if (!agent) {
    issues.push("Harness: choose one available to you.");
    draft.agentId = "";
    draft.modelOverride = null;
    draft.reasoningEffort = null;
    draft.permissionMode = null;
  }
  const host = hosts.find((item) => item.host_id === source.hostId && !item.sandbox_provider);
  if (source.executionTarget === "connected_host" && source.hostId && !host) {
    issues.push("Run on: choose your host or a sandbox. Select its workspace if needed.");
    draft.hostId = null;
    draft.workspace = null;
    draft.baseBranch = null;
  }
  if (
    agent &&
    host &&
    harnessUnconfiguredOnHost(
      nativeCodingAgentForAvailableAgent(agent)?.harness ?? agent.harness,
      host,
    )
  ) {
    issues.push("Harness: configure it on your host or choose another harness or host.");
  }
  if (source.executionTarget === "managed_sandbox" && !managedSandboxesEnabled) {
    issues.push("Run on: this deployment does not offer sandboxes; choose a host.");
    draft.executionTarget = "connected_host";
    draft.hostId = null;
    draft.workspace = null;
    draft.baseBranch = null;
  }
  if (source.projectId && !projects.some((item) => item.id === source.projectId)) {
    issues.push("Project: choose one of your projects, or leave it unfiled.");
    draft.projectId = null;
  }
  const worker = readTriggerWorkerSettings(source.prompt);
  if (worker?.agentId && worker.agentId === source.sharedAgent?.id) {
    draft.prompt =
      updateTriggerWorkerSettings(source.prompt, { ...worker, agentId: null }) ?? source.prompt;
  } else if (worker?.agentId && !agents.some((item) => item.id === worker.agentId)) {
    issues.push(
      "Worker harness: the original is unavailable. Matching sessions will use your selected harness; review the instructions.",
    );
    draft.prompt =
      updateTriggerWorkerSettings(source.prompt, { agentId: null, model: "", effort: "" }) ??
      source.prompt;
  }
  return { draft, issues };
}
