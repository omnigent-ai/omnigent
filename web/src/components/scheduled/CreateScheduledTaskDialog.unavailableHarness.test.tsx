import { testAgent } from "@/test/agentFixtures";
// The automation dialog with the real agent picker (the main dialog suite stubs it). A run
// starts later, so a saved automation's model and effort stay editable while its harness is
// unavailable on the host, and the picker keeps showing the warning.

import type * as ScheduledTasksApiModule from "@/lib/scheduledTasksApi";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as SandboxModelsModule from "@/hooks/useSandboxModelOptions";

import userEvent from "@testing-library/user-event";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CreateScheduledTaskDialog } from "./CreateScheduledTaskDialog";
import * as agentsHook from "@/hooks/useAvailableAgents";
import * as hostsHook from "@/hooks/useHosts";
import * as scheduledHooks from "@/hooks/useScheduledTasks";
import type { Host } from "@/hooks/useHosts";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { TooltipProvider } from "@/components/ui/tooltip";

vi.mock("@/hooks/useHostWorktrees", () => ({
  useHostWorktrees: () => ({ data: undefined, isFetching: false }),
}));
vi.mock("@/hooks/useAvailableAgents", async (original) => ({
  ...(await original<typeof agentsHook>()),
  useAvailableAgents: vi.fn(),
}));
// No host catalog: Claude falls back to its built-in aliases (Opus, Sonnet, ...).
vi.mock("@/hooks/useHosts", () => ({
  useHosts: vi.fn(),
  useHostModelOptions: vi.fn(() => ({ data: undefined })),
}));
vi.mock("@/hooks/useSandboxModelOptions", async (original) => ({
  ...(await original<typeof SandboxModelsModule>()),
  useSandboxModelOptions: vi.fn(() => ({ data: undefined })),
}));
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: null, isLoading: false, error: null }),
}));
vi.mock("@/hooks/useScheduledTasks", () => ({
  useCreateScheduledTask: vi.fn(),
  useUpdateScheduledTask: vi.fn(),
}));
vi.mock("@/lib/agentLabels", async (original) => ({
  ...(await original<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({}),
}));
vi.mock("@/hooks/useConversations", () => ({
  useProjects: () => ({ data: [], isPending: false, isSuccess: true, isError: false }),
  useResolveOrCreateProjectId: () => vi.fn(),
}));

const CLAUDE = testAgent("ag_claude_native", "claude-native-ui", {
  display_name: "Claude Code",
  harness: "claude-native",
});
// Online, but Claude Code's CLI is missing on it.
const HOST: Host = {
  host_id: "host_1",
  name: "laptop",
  owner: "me",
  status: "online",
  configured_harnesses: { "claude-native": "binary-missing" },
};

const updateMutateAsync = vi.fn();

beforeEach(() => {
  vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
    data: [CLAUDE],
  } as unknown as ReturnType<typeof agentsHook.useAvailableAgents>);
  vi.mocked(hostsHook.useHosts).mockReturnValue({
    data: [HOST],
  } as unknown as ReturnType<typeof hostsHook.useHosts>);
  vi.mocked(scheduledHooks.useCreateScheduledTask).mockReturnValue({
    mutateAsync: vi.fn(),
    isPending: false,
  } as unknown as ReturnType<typeof scheduledHooks.useCreateScheduledTask>);
  updateMutateAsync.mockReset().mockResolvedValue({ id: "st_1" });
  vi.mocked(scheduledHooks.useUpdateScheduledTask).mockReturnValue({
    mutateAsync: updateMutateAsync,
    isPending: false,
  } as unknown as ReturnType<typeof scheduledHooks.useUpdateScheduledTask>);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
});

function savedTask(
  overrides: Partial<ScheduledTasksApiModule.ScheduledTask>,
): ScheduledTasksApiModule.ScheduledTask {
  return {
    id: "st_1",
    name: "Morning brief",
    prompt: "Summarize overnight activity",
    rrule: "FREQ=DAILY;BYHOUR=8;BYMINUTE=30",
    ownerUserId: null,
    agentId: CLAUDE.id,
    timezone: "America/Los_Angeles",
    createdAt: 1,
    updatedAt: 2,
    modelOverride: "opus",
    reasoningEffort: "high",
    permissionMode: null,
    sessionMode: "new",
    sessionId: null,
    workspace: null,
    hostId: null,
    executionTarget: "connected_host",
    projectId: null,
    state: "active",
    lastRunAt: null,
    lastRunStatus: null,
    lastRunConversationId: null,
    nextRunAt: null,
    ...overrides,
  } as ScheduledTasksApiModule.ScheduledTask;
}

function renderDialog(task: ScheduledTasksApiModule.ScheduledTask) {
  return render(
    <CapabilitiesProvider
      info={{ ...SERVER_INFO_OFFLINE_FALLBACK, scheduled_task_harness_settings: true }}
    >
      <StoryQueryRouter>
        <TooltipProvider>
          <CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={task} />
        </TooltipProvider>
      </StoryQueryRouter>
    </CapabilitiesProvider>,
  );
}

describe("CreateScheduledTaskDialog with an unavailable harness", () => {
  // Pinned to the host, or unpinned, where the picker checks the first online host.
  it.each([
    ["pinned", "host_1"],
    ["unpinned", null],
  ])("keeps a %s automation's saved model and effort editable", async (_label, hostId) => {
    const user = userEvent.setup();
    renderDialog(savedTask({ hostId }));
    const trigger = within(screen.getByTestId("task-agent-picker")).getByTestId(
      "new-chat-landing-agent-select",
    );
    // The picker hides an unavailable harness's model, so the trigger names the agent.
    expect(trigger).toHaveTextContent("Claude Code");
    expect(trigger).toHaveTextContent("High");
    expect(within(trigger).getByTestId("new-chat-landing-agent-warning")).toBeInTheDocument();

    await user.click(trigger);
    const row = screen.getByTestId(`new-chat-landing-agent-${CLAUDE.id}`);
    expect(row).toHaveAttribute("aria-disabled", "true");
    await user.click(screen.getByTestId(`new-chat-landing-agent-config-${CLAUDE.id}`));
    const models = await screen.findByTestId("task-agent-models");
    expect(within(models).getByRole("menuitemcheckbox", { name: "Opus" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    // Clear both overrides.
    await user.click(within(models).getByRole("menuitemcheckbox", { name: "Default" }));
    const efforts = screen.getByTestId("task-agent-efforts");
    await user.click(within(efforts).getByRole("menuitemcheckbox", { name: "Default" }));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("task-agent-models")).not.toBeInTheDocument());
    expect(within(trigger).getByTestId("new-chat-landing-agent-warning")).toBeInTheDocument();

    await user.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
      modelOverride: null,
      reasoningEffort: null,
    });
  });
});
