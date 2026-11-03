import { testAgent } from "@/test/agentFixtures";
// Tests for the manual create dialog: submit stays disabled until the required
// fields are filled, the workspace-without-host pairing rule surfaces inline,
// and a valid submit calls the create mutation with the RRULE built from the
// schedule fields (host/workspace omitted when unset).
//
// The agent/host hooks and the create mutation are mocked; WorkspacePicker is
// stubbed (its filesystem browsing is out of scope here).

import type * as ScheduledTasksApiModule from "@/lib/scheduledTasksApi";

import type React from "react";
import userEvent from "@testing-library/user-event";
import {
  cleanup,
  fireEvent,
  render as renderBase,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CreateScheduledTaskDialog,
  isInsidePopper,
  shouldGuardDialogDismiss,
} from "./CreateScheduledTaskDialog";
import * as agentsHook from "@/hooks/useAvailableAgents";
import * as identity from "@/lib/identity";
import * as sandboxModelsHook from "@/hooks/useSandboxModelOptions";
import * as hostsHook from "@/hooks/useHosts";
import * as scheduledHooks from "@/hooks/useScheduledTasks";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import { emptyTriggerFields, renderTriggerPrompt, splitTriggerPrompt } from "./triggerTemplates";

const gitWorkspace = vi.hoisted(() => ({
  available: true as boolean | undefined,
  checking: false,
  path: "/home/me/repo",
  query: vi.fn(),
}));
vi.mock("@/hooks/useHostWorktrees", () => ({
  useHostWorktrees: (hostId: string | null, path: string | null) => {
    gitWorkspace.query(hostId, path);
    let data: { path: string; branch: string; is_main: boolean }[] | undefined;
    if (hostId && path && gitWorkspace.available !== undefined) {
      data = gitWorkspace.available ? [{ path, branch: "main", is_main: true }] : [];
    }
    return { data, isFetching: gitWorkspace.checking };
  },
}));
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
// useHostModelOptions is consumed by the ModelEffortFields sub-form (model
// dropdown source). Returning no data makes it fall back to the static Claude
// alias list, which is what a no-host-pinned scheduled task uses anyway.
vi.mock("@/hooks/useHosts", () => ({
  useHosts: vi.fn(),
  useHostModelOptions: vi.fn(() => ({ data: undefined })),
}));
vi.mock("@/hooks/useSandboxModelOptions", async (original) => ({
  ...(await original<typeof sandboxModelsHook>()),
  useSandboxModelOptions: vi.fn(() => ({ data: undefined })),
}));
vi.mock("@/hooks/useScheduledTasks", () => ({
  useCreateScheduledTask: vi.fn(),
  useUpdateScheduledTask: vi.fn(),
}));
vi.mock("@/lib/agentLabels", () => ({ useBrainHarnessLabels: () => ({}) }));
// The project field (and the shared ProjectPicker inside it) lists projects
// from this hook.
const PROJECTS = [
  { id: "proj_1", name: "Triage", icon: null },
  { id: "proj_2", name: "Reports", icon: null },
];
const projectsQuery = vi.hoisted(() => ({
  current: { data: undefined as unknown, isPending: false, isSuccess: false, isError: false },
}));
const resolveOrCreateProjectId = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useConversations", () => ({
  useProjects: () => projectsQuery.current,
  useResolveOrCreateProjectId: () => resolveOrCreateProjectId,
}));
vi.mock("@/shell/WorkspacePicker", () => ({
  isNavigablePath: (path: string) => path.startsWith("/"),
  WorkspacePicker: ({ onSelect }: { onSelect?: (p: string) => void }) => (
    <button type="button" onClick={() => onSelect?.(gitWorkspace.path)}>
      pick-workspace
    </button>
  ),
}));

// Stub the heavy shared picker: expose buttons that drive the exact callbacks
// the dialog wires (select an entry, set model/effort knobs, toggle open state)
// so we can test the dialog's selection→payload mapping + dismiss-guard without
// the real Radix menu / QueryClient. Two entries mirror the two mapping cases:
// a bare harness (claude-native-ui) and a plain agent (polly).
vi.mock("@/shell/NewChatDialog", () => ({
  AgentHarnessPicker: ({
    onSelectAgent,
    onOpenChange,
    effectiveAgentId,
    agentLabel,
    host,
    dropdownModal,
    contentClassName,
    harnessEntries,
    agentEntries,
  }: {
    onSelectAgent: (a: AvailableAgent) => void;
    onOpenChange?: (open: boolean) => void;
    effectiveAgentId: string | null;
    agentLabel: string;
    host?: { host_id: string } | null;
    dropdownModal?: boolean;
    contentClassName?: string;
    harnessEntries: AvailableAgent[];
    agentEntries: AvailableAgent[];
  }) => (
    <div
      data-testid="agent-picker-stub"
      data-effective={effectiveAgentId ?? ""}
      // Surface the host the dialog feeds the picker for badge computation, so
      // a test can assert it's populated even when no host is pinned.
      data-badge-host={host?.host_id ?? ""}
      data-dropdown-modal={dropdownModal === false ? "false" : "true"}
      data-content-class={contentClassName}
      data-harness-entries={harnessEntries.map((agent) => agent.name).join(",")}
      data-agent-entries={agentEntries.map((agent) => agent.name).join(",")}
    >
      <span>{agentLabel}</span>
      {harnessEntries
        .filter((agent) => agent.name !== "claude-native-ui")
        .map((agent) => (
          <button key={agent.id} type="button" onClick={() => onSelectAgent(agent)}>
            pick {agent.harness}
          </button>
        ))}

      <button
        type="button"
        data-testid="pick-harness-claude"
        onClick={() =>
          onSelectAgent(
            testAgent("ag_claude_native", "claude-native-ui", {
              display_name: "Claude Code",
              harness: "claude-native",
            }),
          )
        }
      >
        pick claude harness
      </button>
      <button
        type="button"
        data-testid="pick-agent-polly"
        onClick={() =>
          onSelectAgent(
            testAgent("ag_1", "polly", { display_name: "Polly", harness: "claude-sdk" }),
          )
        }
      >
        pick polly
      </button>
      <button type="button" data-testid="picker-open" onClick={() => onOpenChange?.(true)}>
        open
      </button>
      <button type="button" data-testid="picker-close" onClick={() => onOpenChange?.(false)}>
        close
      </button>
    </div>
  ),
}));

const AGENTS: AvailableAgent[] = [
  testAgent("ag_1", "polly", { display_name: "Polly", harness: "claude-sdk" }),
  testAgent("ag_claude_native", "claude-native-ui", {
    display_name: "Claude Code",
    harness: "claude-native",
  }),
];

const mutateAsync = vi.fn();
const updateMutateAsync = vi.fn();

beforeEach(() => {
  gitWorkspace.available = true;
  gitWorkspace.checking = false;
  gitWorkspace.path = "/home/me/repo";
  gitWorkspace.query.mockClear();
  vi.mocked(hostsHook.useHostModelOptions).mockReturnValue({ data: undefined } as ReturnType<
    typeof hostsHook.useHostModelOptions
  >);
  vi.mocked(sandboxModelsHook.useSandboxModelOptions).mockReturnValue({
    data: undefined,
  } as ReturnType<typeof sandboxModelsHook.useSandboxModelOptions>);
  projectsQuery.current = { data: PROJECTS, isPending: false, isSuccess: true, isError: false };
  mutateAsync.mockReset().mockResolvedValue({ id: "st_new" });
  updateMutateAsync.mockReset().mockResolvedValue({ id: "st_1" });
  resolveOrCreateProjectId.mockReset();
  vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
    data: AGENTS,
  } as unknown as ReturnType<typeof agentsHook.useAvailableAgents>);
  vi.mocked(hostsHook.useHosts).mockReturnValue({
    data: [{ host_id: "host_1", name: "laptop", owner: "me", status: "online" }],
  } as unknown as ReturnType<typeof hostsHook.useHosts>);
  vi.mocked(scheduledHooks.useCreateScheduledTask).mockReturnValue({
    mutateAsync,
    isPending: false,
  } as unknown as ReturnType<typeof scheduledHooks.useCreateScheduledTask>);
  vi.mocked(scheduledHooks.useUpdateScheduledTask).mockReturnValue({
    mutateAsync: updateMutateAsync,
    isPending: false,
  } as unknown as ReturnType<typeof scheduledHooks.useUpdateScheduledTask>);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  vi.useRealTimers();
});

function render(ui: Parameters<typeof renderBase>[0]) {
  return renderBase(ui, {
    wrapper: ({ children }) => (
      <CapabilitiesProvider
        info={{
          ...SERVER_INFO_OFFLINE_FALLBACK,
          features: { automations_v2: true },
          scheduled_task_harness_settings: true,
          scheduled_task_worktrees: true,
        }}
      >
        {children}
      </CapabilitiesProvider>
    ),
  });
}

function renderDialog(onOpenChange: (open: boolean) => void = vi.fn()) {
  return render(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} />);
}

/** Render inside a CapabilitiesProvider that advertises managed sandboxes. */
function renderWithSandboxes(onOpenChange: (open: boolean) => void = vi.fn()) {
  return render(
    <CapabilitiesProvider
      info={{ ...SERVER_INFO_OFFLINE_FALLBACK, managed_sandboxes_enabled: true }}
    >
      <CreateScheduledTaskDialog open onOpenChange={onOpenChange} />
    </CapabilitiesProvider>,
  );
}

function scheduledTask(overrides: Partial<ScheduledTasksApiModule.ScheduledTask> = {}) {
  return {
    id: "st_1",
    name: "Morning brief",
    prompt: "Summarize overnight activity",
    rrule: "FREQ=DAILY;BYHOUR=8;BYMINUTE=30",
    ownerUserId: null,
    agentId: "ag_1",
    timezone: "America/Los_Angeles",
    createdAt: 1,
    updatedAt: 2,
    modelOverride: null,
    reasoningEffort: null,
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
  } satisfies ScheduledTasksApiModule.ScheduledTask;
}

describe("agent picker readiness (needs-setup badges)", () => {
  it("shows the model + effort controls for the default (Claude-native) agent", () => {
    renderDialog();
    expect(screen.getByRole("button", { name: "Advanced" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    // The default effective agent is claude-native-ui (a capable native coding
    // agent), so the model/effort pickers render and the "uses defaults" hint —
    // shown only when the controls are hidden — is absent.
    expect(screen.getByTestId("task-model-effort-field")).toBeInTheDocument();
    expect(screen.getByTestId("task-model-trigger")).toBeInTheDocument();
    expect(screen.getByTestId("task-effort-trigger")).toBeInTheDocument();
    expect(
      screen.queryByText("Uses this agent's default model, effort, and permission settings"),
    ).not.toBeInTheDocument();
  });

  it("feeds the picker a fallback online host so 'needs setup' badges show with no host pinned", () => {
    renderDialog();
    // Fresh state: no host pinned, but the dialog still passes the first online
    // host to the picker for badge computation (badgeHost fallback) so the
    // "needs setup" affordance isn't invisible until the user picks a host.
    const picker = screen.getByTestId("agent-picker-stub");
    expect(picker.getAttribute("data-badge-host")).toBe("host_1");
  });

  it("embeds the agent dropdown in non-modal mode so inside-dialog clicks only close the menu", () => {
    renderDialog();
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute("data-dropdown-modal", "false");
  });

  it("uses the shared viewport-aware menu height instead of a fixed scroll cap", () => {
    renderDialog();
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute("data-content-class", "w-80");
  });

  it("groups generic ACP choices with harnesses like the main composer", () => {
    vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
      data: [
        ...AGENTS,
        testAgent("ag_jcode", "jcode", {
          display_name: "Jcode",
          harness: "jcode",
          acpHarness: true,
        }),
        testAgent("ag_grok", "grok", {
          display_name: "Grok Build",
          harness: "grok",
          acpHarness: true,
        }),
      ],
    } as unknown as ReturnType<typeof agentsHook.useAvailableAgents>);

    renderDialog();

    const picker = screen.getByTestId("agent-picker-stub");
    expect(picker).toHaveAttribute("data-harness-entries", "claude-native-ui,jcode,grok");
    expect(picker).toHaveAttribute("data-agent-entries", "polly");
  });
});

describe("CreateScheduledTaskDialog validation", () => {
  it("keeps submit disabled until name + prompt are set (agent defaults to the first)", () => {
    renderDialog();
    const submit = screen.getByTestId("create-scheduled-task-submit");
    expect(submit).toHaveTextContent("Create automation");
    // Agent is never blank — the picker resolves a default (first agent) — so
    // only name + prompt gate submit.
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
    expect(submit).toBeEnabled();
  });

  it("pins a workspace on confirmation and waits for reconnection if its host disconnects", () => {
    const onOpenChange = vi.fn();
    const { rerender } = renderDialog(onOpenChange);
    const hostTrigger = screen.getByTestId("task-host-trigger");
    fireEvent.pointerDown(hostTrigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    fireEvent.click(hostTrigger);
    fireEvent.click(screen.getByRole("option", { name: /laptop/ }));

    fireEvent.click(screen.getByTestId("task-workspace-browse"));
    expect(screen.getByTestId("workspace-picker-dialog")).toBeInTheDocument();
    expect(screen.queryByText("/home/me/repo")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "pick-workspace" }));

    expect(screen.queryByTestId("workspace-picker-dialog")).toBeNull();
    expect(screen.getAllByText("/home/me/repo")).toHaveLength(2);

    expect(gitWorkspace.query).toHaveBeenLastCalledWith("host_1", "/home/me/repo");
    fireEvent.click(screen.getByRole("checkbox", { name: "Random worktree" }));
    fireEvent.click(screen.getByTestId("task-workspace-browse"));
    expect(screen.getByTestId("workspace-picker-dialog")).toBeInTheDocument();
    vi.mocked(hostsHook.useHosts).mockReturnValue({
      data: [{ host_id: "host_1", name: "laptop", owner: "me", status: "offline" }],
    } as unknown as ReturnType<typeof hostsHook.useHosts>);
    rerender(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} />);
    expect(screen.queryByTestId("workspace-picker-dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("task-workspace-browse")).toBeDisabled();
    expect(gitWorkspace.query).toHaveBeenLastCalledWith(null, "/home/me/repo");
    expect(screen.queryByRole("checkbox", { name: "Random worktree" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Turn off random worktree" })).toBeEnabled();
    expect(screen.getAllByText("/home/me/repo")).toHaveLength(2);

    vi.mocked(hostsHook.useHosts).mockReturnValue({
      data: [{ host_id: "host_1", name: "laptop", owner: "me", status: "online" }],
    } as unknown as ReturnType<typeof hostsHook.useHosts>);
    rerender(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} />);
    expect(screen.getByTestId("task-workspace-browse")).toBeEnabled();
    expect(gitWorkspace.query).toHaveBeenLastCalledWith("host_1", "/home/me/repo");
    expect(screen.getByRole("checkbox", { name: "Random worktree" })).toBeChecked();
    expect(screen.queryByTestId("workspace-picker-dialog")).not.toBeInTheDocument();
    expect(screen.queryByText("Connect your host to select a workspace.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("task-workspace-browse"));
    expect(screen.getByTestId("workspace-picker-dialog")).toBeInTheDocument();
  });
});

describe("CreateScheduledTaskDialog prefill (seed-on-open + reset)", () => {
  it("seeds Name + Prompt from initialName/initialPrompt when opened", () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        initialName="Daily morning brief"
        initialPrompt="Summarize overnight activity."
      />,
    );
    expect((screen.getByTestId("task-name-input") as HTMLInputElement).value).toBe(
      "Daily morning brief",
    );
    expect((screen.getByTestId("task-prompt-input") as HTMLTextAreaElement).value).toBe(
      "Summarize overnight activity.",
    );
  });

  it("starts EMPTY when opened with no initial values (manual path)", () => {
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} />);
    expect((screen.getByTestId("task-name-input") as HTMLInputElement).value).toBe("");
    expect((screen.getByTestId("task-prompt-input") as HTMLTextAreaElement).value).toBe("");
  });

  it("does not clobber user edits while the dialog stays open", () => {
    const { rerender } = render(
      <CreateScheduledTaskDialog open onOpenChange={vi.fn()} initialName="Seed" />,
    );
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Edited" } });
    // A re-render with the SAME open+props must not re-seed over the edit.
    rerender(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} initialName="Seed" />);
    expect((screen.getByTestId("task-name-input") as HTMLInputElement).value).toBe("Edited");
  });

  it("reseeds on a fresh open, and a no-prefill reopen starts empty (no stale leak)", () => {
    const { rerender } = render(
      <CreateScheduledTaskDialog open={false} onOpenChange={vi.fn()} initialName="First" />,
    );
    // Open with "First".
    rerender(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} initialName="First" />);
    expect((screen.getByTestId("task-name-input") as HTMLInputElement).value).toBe("First");

    // Close (resetForm clears), then reopen with NO prefill → empty.
    rerender(<CreateScheduledTaskDialog open={false} onOpenChange={vi.fn()} />);
    rerender(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} />);
    expect((screen.getByTestId("task-name-input") as HTMLInputElement).value).toBe("");
  });
});

describe("CreateScheduledTaskDialog edit mode", () => {
  it("seeds fields from the scheduled task and uses edit copy", () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ agentId: "ag_1" })}
      />,
    );
    expect(screen.getByText("Edit automation")).toBeInTheDocument();
    expect(screen.getByText(/Update this recurring agent session/i)).toBeInTheDocument();
    expect(screen.getByTestId("create-scheduled-task-submit")).toHaveTextContent("Save changes");
    expect((screen.getByTestId("task-name-input") as HTMLInputElement).value).toBe("Morning brief");
    expect((screen.getByTestId("task-prompt-input") as HTMLTextAreaElement).value).toBe(
      "Summarize overnight activity",
    );
    // Edit mode offers the same picker create does, seeded with the task's own
    // agent — the harness of an existing automation is changeable.
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute("data-effective", "ag_1");
    expect(screen.getByTestId("agent-picker-stub")).toHaveTextContent("Polly");
    expect(screen.getByTestId("schedule-time")).toHaveValue("08:30 AM");
  });

  it("keeps a task bound to an agent the picker hides instead of retargeting it", () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ agentId: "ag_gone" })}
      />,
    );
    // Without the edit-mode fallback this would silently resolve to the first
    // listed agent and switch the harness on the next save.
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute("data-effective", "ag_gone");
  });

  it("sends agentId when the harness is switched, clearing the old agent's settings", async () => {
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={scheduledTask()} />);
    fireEvent.click(screen.getByTestId("pick-harness-claude"));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const { input } = updateMutateAsync.mock.calls[0][0];
    expect(input.agentId).toBe("ag_claude_native");
    // The target harness carries model/effort/permission controls, so the PATCH
    // states them explicitly — at Default, i.e. cleared.
    expect(input.modelOverride).toBeNull();
    expect(input.reasoningEffort).toBeNull();
    expect(input.permissionMode).toBeNull();
  });

  it("round-trips non-quarter-hour edit times through the update payload", async () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ rrule: "FREQ=DAILY;BYHOUR=17;BYMINUTE=7" })}
      />,
    );
    expect(screen.getByTestId("schedule-time")).toHaveValue("05:07 PM");
    fireEvent.click(screen.getByTestId("schedule-time-picker-trigger"));
    expect(screen.getByTestId("schedule-hour-05")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("schedule-minute-07")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("schedule-period-PM")).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input.rrule).toBe("FREQ=DAILY;BYHOUR=17;BYMINUTE=7");
  });

  it("submits supported edits through the update mutation without changing agent defaults", async () => {
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={scheduledTask()} />);
    fireEvent.change(screen.getByTestId("task-name-input"), {
      target: { value: "Updated brief" },
    });
    fireEvent.change(screen.getByTestId("task-prompt-input"), {
      target: { value: "Updated prompt" },
    });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync).not.toHaveBeenCalled();
    expect(updateMutateAsync.mock.calls[0][0]).toEqual({
      id: "st_1",
      input: {
        name: "Updated brief",
        prompt: "Updated prompt",
        rrule: "FREQ=DAILY;BYHOUR=8;BYMINUTE=30",
        timezone: "America/Los_Angeles",
      },
    });
  });

  it("keeps the task's settings when the pick lands back on its own agent", async () => {
    // Switching away and back is not a rebind, so the stored model/effort/
    // permission must survive it — the clear is only justified by a real switch.
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: "ag_claude_native",
          modelOverride: "opus",
          reasoningEffort: "high",
          permissionMode: "acceptEdits",
        })}
      />,
    );
    fireEvent.click(screen.getByTestId("pick-agent-polly"));
    fireEvent.click(screen.getByTestId("pick-harness-claude"));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const { input } = updateMutateAsync.mock.calls[0][0];
    expect(input).not.toHaveProperty("agentId");
    expect(input.modelOverride).toBe("opus");
    expect(input.reasoningEffort).toBe("high");
    expect(input.permissionMode).toBe("acceptEdits");
  });

  it("keeps the task's settings when the current agent is re-picked", async () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: "ag_claude_native",
          permissionMode: "bypassPermissions",
        })}
      />,
    );
    fireEvent.click(screen.getByTestId("pick-harness-claude"));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const { input } = updateMutateAsync.mock.calls[0][0];
    expect(input).not.toHaveProperty("agentId");
    expect(input.permissionMode).toBe("bypassPermissions");
  });

  it.each(["Every day", "Every hour", "Custom schedule"])(
    "blocks unsupported rules until explicitly replaced with %s",
    async (frequency) => {
      render(
        <CreateScheduledTaskDialog
          open
          onOpenChange={vi.fn()}
          editingTask={scheduledTask({ rrule: "FREQ=DAILY;BYHOUR=9,17;BYMINUTE=0" })}
        />,
      );
      expect(screen.getByRole("alert")).toHaveTextContent("This schedule can't be edited");
      expect(screen.getByLabelText("Frequency")).toHaveTextContent("Choose frequency");
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      fireEvent.keyDown(screen.getByLabelText("Schedule ends"), { key: "Enter" });
      fireEvent.click(await screen.findByRole("option", { name: "After" }));
      fireEvent.change(screen.getByLabelText("Occurrences"), { target: { value: "3" } });
      expect(screen.getByRole("alert")).toHaveTextContent("This schedule can't be edited");
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      expect(updateMutateAsync).not.toHaveBeenCalled();
      fireEvent.keyDown(screen.getByTestId("schedule-preset-trigger"), { key: "Enter" });
      fireEvent.click(await screen.findByRole("option", { name: frequency }));
      if (frequency === "Custom schedule") {
        expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
        fireEvent.keyDown(screen.getByLabelText("Repeat every"), { key: "Escape" });
        expect(screen.getByLabelText("Frequency")).toHaveTextContent("Choose frequency");
        expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
        fireEvent.keyDown(screen.getByLabelText("Frequency"), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name: frequency }));
        fireEvent.change(screen.getByLabelText("Repeat every"), { target: { value: "2" } });
        fireEvent.click(screen.getByRole("button", { name: "Apply" }));
        expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
      }
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const expectedRules: Record<string, string> = {
        "Every day": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;COUNT=3",
        "Every hour": "FREQ=HOURLY;BYMINUTE=0;COUNT=3",
        "Custom schedule": "FREQ=DAILY;INTERVAL=2;BYHOUR=9;BYMINUTE=0;COUNT=3",
      };
      expect(updateMutateAsync.mock.calls[0][0].input.rrule).toBe(expectedRules[frequency]);
    },
  );
});

describe("CreateScheduledTaskDialog sandbox mode", () => {
  // Open the Host <Select> the Radix-in-jsdom way (see ForkSessionDialog.test).
  function openHostSelect() {
    const trigger = screen.getByTestId("task-host-trigger");
    fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    fireEvent.click(trigger);
  }

  it("offers no sandbox host option when the server does not advertise managed sandboxes", () => {
    renderDialog();
    openHostSelect();
    expect(screen.queryByTestId("task-host-sandbox-option")).not.toBeInTheDocument();
  });

  it("chooses no target until the host list loads, then starts on the sandbox", () => {
    const hosts = vi.mocked(hostsHook.useHosts);
    hosts.mockReturnValue({ data: undefined, isPending: true } as unknown as ReturnType<
      typeof hostsHook.useHosts
    >);
    const view = renderWithSandboxes();
    expect(screen.getByTestId("task-host-trigger")).toHaveTextContent("Pick a host");
    hosts.mockReturnValue({
      data: [{ host_id: "host_1", name: "laptop", owner: "me", status: "online" }],
      isPending: false,
    } as unknown as ReturnType<typeof hostsHook.useHosts>);
    view.rerender(
      <CapabilitiesProvider
        info={{ ...SERVER_INFO_OFFLINE_FALLBACK, managed_sandboxes_enabled: true }}
      >
        <CreateScheduledTaskDialog open onOpenChange={vi.fn()} />
      </CapabilitiesProvider>,
    );
    expect(screen.getByTestId("task-host-trigger")).not.toHaveTextContent("laptop");
    expect(
      screen.getByText(
        "Provisions a fresh sandbox for each run. Shutdown follows the server’s sandbox configuration.",
      ),
    ).toBeInTheDocument();
  });

  it("starts a new task in sandbox mode when sandboxes are offered, and a host pick sticks", () => {
    renderWithSandboxes();
    // The sandbox default wins over the online host and shows the fresh-sandbox
    // copy; the connected-host workspace picker never appears.
    expect(
      screen.getByText(
        "Provisions a fresh sandbox for each run. Shutdown follows the server’s sandbox configuration.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("pick-workspace")).not.toBeInTheDocument();
    openHostSelect();
    fireEvent.click(screen.getByRole("option", { name: /laptop/ }));
    expect(
      screen.getByText("Runs on this host. A run fails while the host is offline."),
    ).toBeInTheDocument();
  });

  it.each([
    {
      mode: "create",
      saved: null,
      label: "Provider default",
      pick: "runtime",
      expected: "https://github.com/example/runtime",
    },
    {
      mode: "create-default",
      saved: null,
      label: "Provider default",
      pick: null,
      expected: undefined,
    },
    {
      mode: "edit",
      saved: "https://github.com/example/runtime",
      label: "runtime",
      pick: "universe",
      expected: "https://github.com/example/universe",
    },
    {
      mode: "edit-default",
      saved: null,
      label: "Provider default",
      pick: null,
      expected: undefined,
    },
    {
      mode: "edit-unchanged",
      saved: "https://github.com/example/runtime",
      label: "runtime",
      pick: null,
      expected: undefined,
    },
    {
      mode: "edit-branch",
      saved: "https://github.com/example/runtime#main",
      label: "https://github.com/example/runtime#main",
      pick: null,
      expected: undefined,
    },
    {
      mode: "edit-legacy",
      saved: "/home/me",
      label: "Provider default",
      pick: null,
      expected: undefined,
    },
    {
      mode: "edit-clear",
      saved: "https://github.com/example/runtime",
      label: "runtime",
      pick: "Provider default",
      expected: null,
    },
  ])(
    "saves only changed repository selections ($mode)",
    async ({ mode, saved, label, pick, expected }) => {
      const editing = mode.startsWith("edit");
      const exercisesRestoration = mode === "edit-unchanged";
      const runtime = "https://github.com/example/runtime";
      const universe = "https://github.com/example/universe";
      render(
        <CapabilitiesProvider
          info={{
            ...SERVER_INFO_OFFLINE_FALLBACK,
            managed_sandboxes_enabled: true,
            scheduled_task_harness_settings: true,
            scheduled_task_sandbox_workspaces: true,
            sandbox_provider: "arclet",
            sandbox_provider_capabilities: {
              arclet: {
                repo_catalog: [
                  { name: "universe", url: universe },
                  { name: "runtime", url: runtime },
                ],
              },
            },
          }}
        >
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            initialName="Nightly"
            initialPrompt="Do it"
            editingTask={
              editing
                ? scheduledTask({
                    agentId: "ag_claude_native",
                    executionTarget: "managed_sandbox",
                    workspace: saved,
                    sessionMode: "continue",
                    sessionId: "existing-session",
                    modelOverride: "sonnet",
                    reasoningEffort: "medium",
                    permissionMode: "acceptEdits",
                  })
                : null
            }
          />
        </CapabilitiesProvider>,
      );
      if (!editing) {
        openHostSelect();
        fireEvent.click(screen.getByTestId("task-host-sandbox-option"));
      } else if (exercisesRestoration) {
        openHostSelect();
        fireEvent.click(await screen.findByRole("option", { name: "laptop" }));
        openHostSelect();
        fireEvent.click(screen.getByTestId("task-host-sandbox-option"));
      }
      expect(screen.queryByTestId("task-workspace-browse")).not.toBeInTheDocument();
      expect(screen.getByLabelText("Repository")).toHaveTextContent(label);
      const sessionLock = /To change model, effort, or permission mode/;
      if (editing) {
        expect(screen.getByText(sessionLock)).toBeInTheDocument();
        expect(screen.getByTestId("task-model-trigger")).toBeDisabled();
        expect(screen.getByTestId("task-permission-trigger")).toBeDisabled();
      }
      if (exercisesRestoration) {
        fireEvent.keyDown(screen.getByLabelText("Repository"), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name: "universe" }));
        expect(screen.queryByText(sessionLock)).not.toBeInTheDocument();
        expect(screen.getByTestId("task-model-trigger")).toBeEnabled();
        expect(screen.getByTestId("task-permission-trigger")).toBeEnabled();
        fireEvent.keyDown(screen.getByLabelText("Repository"), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name: label }));
        expect(screen.getByText(sessionLock)).toBeInTheDocument();
        expect(screen.getByTestId("task-model-trigger")).toBeDisabled();
        expect(screen.getByTestId("task-permission-trigger")).toBeDisabled();
        expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("Sonnet");
        expect(screen.getByTestId("task-effort-trigger")).toHaveTextContent("Medium");
        expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Accept edits");
      }
      if (pick !== null) {
        fireEvent.keyDown(screen.getByLabelText("Repository"), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name: pick }));
      }
      expect(screen.getByRole("dialog")).toBeInTheDocument();
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      const mutation = editing ? updateMutateAsync : mutateAsync;
      await waitFor(() => expect(mutation).toHaveBeenCalledTimes(1));
      const input = editing ? mutation.mock.calls[0][0].input : mutation.mock.calls[0][0];
      if (expected === undefined) {
        expect(input).not.toHaveProperty("workspace");
        if (editing) {
          expect(input).not.toHaveProperty("sessionId");
          expect(input).not.toHaveProperty("modelOverride");
        }
      } else {
        expect(input.workspace).toBe(expected);
      }
      expect(input).not.toHaveProperty("hostId");
    },
  );

  it.each([false, true])(
    "gates catalog selection and clears it on a host switch (supported: %s)",
    async (supported) => {
      render(
        <CapabilitiesProvider
          info={{
            ...SERVER_INFO_OFFLINE_FALLBACK,
            managed_sandboxes_enabled: true,
            scheduled_task_sandbox_workspaces: supported,
            sandbox_provider: "arclet",
            sandbox_provider_capabilities: {
              arclet: {
                repo_catalog: [{ name: "runtime", url: "https://github.com/example/runtime" }],
              },
            },
          }}
        >
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            initialName="Nightly"
            initialPrompt="Do it"
          />
        </CapabilitiesProvider>,
      );
      openHostSelect();
      fireEvent.click(screen.getByTestId("task-host-sandbox-option"));
      if (supported) {
        fireEvent.keyDown(screen.getByLabelText("Repository"), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name: "runtime" }));
      } else {
        expect(screen.queryByLabelText("Repository")).not.toBeInTheDocument();
      }
      openHostSelect();
      fireEvent.click(await screen.findByRole("option", { name: "laptop" }));
      expect(screen.queryByLabelText("Repository")).not.toBeInTheDocument();
      expect(screen.getByTestId("task-workspace-browse")).toHaveTextContent("Browse folders");
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
      expect(mutateAsync.mock.calls[0][0]).toMatchObject({ hostId: "host_1" });
      expect(mutateAsync.mock.calls[0][0]).not.toHaveProperty("workspace");
    },
  );

  it("submits execution_target=managed_sandbox with no host/workspace when the sandbox option is chosen", async () => {
    vi.mocked(hostsHook.useHosts).mockReturnValue({
      data: [{ host_id: "host_1", name: "laptop", owner: "me", status: "offline" }],
    } as unknown as ReturnType<typeof hostsHook.useHosts>);
    renderWithSandboxes();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
    openHostSelect();
    fireEvent.click(screen.getByTestId("task-host-sandbox-option"));

    expect(screen.queryByTestId("task-workspace-browse")).not.toBeInTheDocument();
    expect(screen.queryByText("Connect your host to select a workspace.")).not.toBeInTheDocument();

    const submit = screen.getByTestId("create-scheduled-task-submit");
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg).toMatchObject({ executionTarget: "managed_sandbox" });
    expect(arg).not.toHaveProperty("hostId");
    expect(arg).not.toHaveProperty("workspace");
  });
});

describe("CreateScheduledTaskDialog submit", () => {
  it("submits required fields with the built RRULE on the first online host", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });

    const submit = screen.getByTestId("create-scheduled-task-submit");
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg).toMatchObject({
      name: "Nightly",
      prompt: "Do it",
      // Default agent = first after sortAgentsForDisplay (harness rows rank
      // first, so the Claude Code harness is the default) — matches NewChatDialog.
      agentId: "ag_claude_native",
      // Default schedule model is daily at 09:00.
      rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
      hostId: "host_1",
    });
    expect(arg).not.toHaveProperty("workspace");
    // Timezone has no visible control but is still inferred + sent: a non-empty
    // IANA-ish string (whatever the test env's local zone resolves to).
    expect(typeof arg.timezone).toBe("string");
    expect(arg.timezone.length).toBeGreaterThan(0);
  });

  // With the model/effort controls left at their Default (unselected) state, the
  // create body carries agent_id and NO model_override / reasoning_effort,
  // whether the pick is a bare harness (claude-native) or a plain agent (polly).
  it("maps a bare-harness pick to its agent_id, never sends model/effort", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "N" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("pick-harness-claude"));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg.agentId).toBe("ag_claude_native");
    expect(arg).not.toHaveProperty("modelOverride");
    expect(arg).not.toHaveProperty("reasoningEffort");
  });

  it("maps an agent pick to its agent_id, never sends model/effort", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "N" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("pick-agent-polly"));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg.agentId).toBe("ag_1");
    expect(arg).not.toHaveProperty("modelOverride");
    expect(arg).not.toHaveProperty("reasoningEffort");
  });

  it("renders the Time field as an input with a compact picker trigger", () => {
    renderDialog();
    const timeField = screen.getByTestId("schedule-time");
    expect(timeField.tagName).toBe("INPUT");
    expect(timeField).toHaveAttribute("placeholder", "5:00 PM");
    expect(timeField).toHaveClass("text-ui");
    expect(screen.getByTestId("schedule-time-picker-trigger")).toBeInTheDocument();
  });

  it("reads as a schedule sentence", () => {
    renderDialog();
    const row = screen.getByTestId("schedule-frequency-time-row");
    expect(row).toHaveTextContent("Every day");
    expect(within(row).getByText("at", { exact: true })).toBeVisible();
    expect(row).toContainElement(screen.getByLabelText("Time"));
  });

  it("waits for a host pick when no host is online", async () => {
    vi.mocked(hostsHook.useHosts).mockReturnValue({
      data: [{ host_id: "host_1", name: "laptop", owner: "me", status: "offline" }],
    } as unknown as ReturnType<typeof hostsHook.useHosts>);
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });

    // There is no unpinned choice, so a new task needs a host or a sandbox.
    expect(screen.getByTestId("task-host-trigger")).toHaveTextContent("Pick a host");
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    const hostTrigger = screen.getByTestId("task-host-trigger");
    fireEvent.pointerDown(hostTrigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    fireEvent.click(hostTrigger);
    expect(screen.queryByRole("option", { name: /fire time/i })).toBeNull();
    fireEvent.click(screen.getByRole("option", { name: /laptop/ }));

    await waitFor(() => expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled());
    const browse = screen.getByTestId("task-workspace-browse");
    expect(browse).toBeDisabled();
    expect(browse).toHaveAccessibleDescription("Connect your host to select a workspace.");
    fireEvent.click(browse);
    expect(screen.queryByTestId("workspace-picker-dialog")).not.toBeInTheDocument();
  });

  it("preserves a saved workspace when switching to an offline host, but disables browsing", async () => {
    vi.mocked(hostsHook.useHosts).mockReturnValue({
      data: [
        { host_id: "host_1", name: "laptop", owner: "me", status: "online" },
        { host_id: "host_2", name: "desktop", owner: "me", status: "offline" },
      ],
    } as unknown as ReturnType<typeof hostsHook.useHosts>);
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ hostId: "host_1", workspace: "/home/me/repo" })}
      />,
    );
    const hostTrigger = screen.getByTestId("task-host-trigger");
    fireEvent.pointerDown(hostTrigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    fireEvent.click(hostTrigger);
    fireEvent.click(screen.getByRole("option", { name: /desktop/ }));

    // Omitting the workspace would keep the saved one, so the form keeps showing it.
    expect(screen.getByTestId("task-workspace-browse")).toHaveTextContent("/home/me/repo");
    expect(screen.getByTestId("task-workspace-browse")).toBeDisabled();
    expect(screen.getByText("Connect your host to select a workspace.")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("task-workspace-browse"));
    expect(screen.queryByTestId("workspace-picker-dialog")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
      hostId: "host_2",
      workspace: "/home/me/repo",
    });
  });

  it("keeps an older unpinned task unpinned until a host is picked", async () => {
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={scheduledTask()} />);

    expect(screen.getByTestId("task-host-trigger")).toHaveTextContent("Most recently active host");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).not.toHaveProperty("hostId");
  });

  it("lays out Host full-width like the other top-level fields", () => {
    renderDialog();
    const hostField = screen.getByTestId("task-host-field");
    const hostTrigger = screen.getByTestId("task-host-trigger");
    expect(hostField).not.toHaveClass("sm:w-64");
    expect(hostField).toContainElement(hostTrigger);
    expect(hostTrigger).toHaveClass("w-full");
  });

  it("keeps the footer visible by letting only the dialog body scroll", () => {
    renderDialog();
    expect(screen.getByTestId("create-scheduled-task-dialog")).toHaveClass("flex", "flex-col");
    expect(screen.getByTestId("scheduled-task-dialog-body")).toHaveClass(
      "min-h-0",
      "flex-1",
      "overflow-y-auto",
    );
    expect(document.querySelector('[data-slot="dialog-footer"]')).toHaveClass("shrink-0");
  });

  it("chooses a non-quarter-hour time from the compact picker", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("schedule-time-picker-trigger"));
    fireEvent.click(await screen.findByTestId("schedule-hour-05"));
    fireEvent.click(screen.getByTestId("schedule-minute-07"));
    fireEvent.click(screen.getByTestId("schedule-period-PM"));
    expect(screen.getByTestId("schedule-time")).toHaveValue("05:07 PM");

    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].rrule).toBe("FREQ=DAILY;BYHOUR=17;BYMINUTE=7");
  });

  it("shows all minute choices in the compact picker", async () => {
    renderDialog();
    fireEvent.click(screen.getByTestId("schedule-time-picker-trigger"));
    const minuteColumn = await screen.findByTestId("schedule-minute-column");
    expect(minuteColumn.querySelectorAll('[data-testid^="schedule-minute-"]')).toHaveLength(60);
    expect(screen.getByTestId("schedule-minute-00")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-01")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-15")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-30")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-45")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-59")).toBeInTheDocument();
  });

  it("makes overflowing picker columns scrollable without closing the picker", async () => {
    renderDialog();
    fireEvent.click(screen.getByTestId("schedule-time-picker-trigger"));
    const hourColumn = await screen.findByTestId("schedule-hour-column");
    expect(hourColumn).toHaveClass("overflow-y-auto", "overscroll-contain");
    expect(screen.getByTestId("schedule-minute-column")).toHaveClass("overflow-y-auto");

    fireEvent.wheel(hourColumn, { deltaY: 120 });
    expect(screen.getByTestId("schedule-time-picker")).toBeInTheDocument();
  });

  it("renders all minute values and selects a non-quarter-hour minute", async () => {
    renderDialog();
    fireEvent.click(screen.getByTestId("schedule-time-picker-trigger"));
    const minuteColumn = screen.getByTestId("schedule-minute-column");
    expect(minuteColumn.querySelectorAll('[data-testid^="schedule-minute-"]')).toHaveLength(60);
    expect(screen.getByTestId("schedule-minute-00")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-37")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-59")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("schedule-minute-37"));
    expect(screen.getByTestId("schedule-time")).toHaveValue("09:37 AM");

    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    const submit = screen.getByTestId("create-scheduled-task-submit");
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].rrule).toBe("FREQ=DAILY;BYHOUR=9;BYMINUTE=37");
  });

  it("round-trips the current non-quarter-hour minute as the selected picker value", () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=7" })}
      />,
    );
    fireEvent.click(screen.getByTestId("schedule-time-picker-trigger"));
    expect(screen.getByTestId("schedule-minute-00")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-07")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("schedule-minute-15")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-30")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-45")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-08")).toBeInTheDocument();
  });

  it("typing a non-quarter-hour time flows into the submitted RRULE", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.change(screen.getByTestId("schedule-time"), { target: { value: "5:07 PM" } });

    const submit = screen.getByTestId("create-scheduled-task-submit");
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    // Default preset is Daily -> 17:07.
    expect(mutateAsync.mock.calls[0][0].rrule).toBe("FREQ=DAILY;BYHOUR=17;BYMINUTE=7");
  });

  it("does not canonicalize partial time input while the field is focused", () => {
    renderDialog();
    const timeField = screen.getByTestId("schedule-time");

    timeField.focus();
    fireEvent.change(timeField, { target: { value: "" } });
    fireEvent.change(timeField, { target: { value: "1" } });
    expect(timeField).toHaveValue("1");
    fireEvent.change(timeField, { target: { value: "1:" } });
    expect(timeField).toHaveValue("1:");
    fireEvent.change(timeField, { target: { value: "1:15" } });
    expect(timeField).toHaveValue("1:15");
    expect(screen.queryByTestId("schedule-error")).toBeNull();

    fireEvent.blur(timeField);
    expect(timeField).toHaveValue("01:15 AM");
  });

  it("canonicalizes typed time on blur", () => {
    renderDialog();
    const timeField = screen.getByTestId("schedule-time");
    fireEvent.change(timeField, { target: { value: "17:07" } });
    fireEvent.blur(timeField);
    expect(timeField).toHaveValue("05:07 PM");
  });

  it("uses text-ui for dialog text fields that wrap shared primitives", () => {
    renderDialog();
    expect(screen.getByTestId("task-name-input")).toHaveClass("text-ui");
    expect(screen.getByTestId("task-prompt-input")).toHaveClass("text-ui");
  });

  it("blocks submit while the typed time is invalid", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.change(screen.getByTestId("schedule-time"), { target: { value: "25:99" } });
    expect(screen.getByTestId("schedule-error")).toHaveTextContent("Enter a valid time");
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
  });

  it("explains the hourly minute and saves any minute from the dropdown", async () => {
    renderDialog();
    fireEvent.keyDown(screen.getByTestId("schedule-preset-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Every hour" }));
    expect(screen.getByText("Runs at the start of every hour.")).toBeInTheDocument();
    expect(screen.queryByTestId("schedule-time-picker-trigger")).toBeNull();
    fireEvent.keyDown(screen.getByLabelText("Minute of the hour"), { key: "Enter" });
    const options = await screen.findAllByRole("option");
    expect(options).toHaveLength(60);
    expect(options[0]).toHaveTextContent("00");
    expect(options[59]).toHaveTextContent("59");
    fireEvent.click(screen.getByRole("option", { name: "07" }));
    expect(screen.getByText("Runs at 7 minutes past every hour.")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].rrule).toBe("FREQ=HOURLY;BYMINUTE=7");
  });

  it.each(["Every hour", "Custom schedule"])(
    "recovers an invalid typed time when changing to hourly through %s",
    async (preset) => {
      renderDialog();
      fireEvent.change(screen.getByTestId("schedule-time"), { target: { value: "invalid" } });
      fireEvent.keyDown(screen.getByLabelText("Frequency"), { key: "Enter" });
      fireEvent.click(await screen.findByRole("option", { name: preset }));
      if (preset === "Custom schedule") {
        fireEvent.keyDown(screen.getByLabelText("Repeat unit"), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name: "Hours" }));
      }
      expect(screen.getByLabelText("Minute of the hour")).toHaveTextContent("00");
      expect(screen.queryByText(/NaN/)).not.toBeInTheDocument();
      if (preset === "Custom schedule") {
        expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled();
      } else {
        expect(screen.getByText("Runs at the start of every hour.")).toBeInTheDocument();
      }
    },
  );

  it("offers recurring presets and a visual custom schedule", async () => {
    renderDialog();
    fireEvent.keyDown(screen.getByTestId("schedule-preset-trigger"), { key: "Enter" });
    const options = (await screen.findAllByRole("option")).map((option) => option.textContent);
    expect(options).toEqual([
      "Every hour",
      "Every day",
      "Every weekday",
      "Every week",
      "Custom schedule",
    ]);
    fireEvent.click(screen.getByRole("option", { name: "Custom schedule" }));
    expect(screen.getByLabelText("Repeat every")).toHaveValue(1);
    fireEvent.keyDown(screen.getByLabelText("Repeat unit"), { key: "Enter" });
    expect((await screen.findAllByRole("option")).map((option) => option.textContent)).toEqual([
      "Hours",
      "Days",
      "Weeks",
      "Months",
      "Years",
    ]);
  });

  it("applies custom weekdays, validates intervals and submits the selected timezone and end count", async () => {
    renderDialog();
    fireEvent.keyDown(screen.getByLabelText("Frequency"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Custom schedule" }));
    fireEvent.change(screen.getByLabelText("Repeat every"), { target: { value: "0" } });
    expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Repeat every"), { target: { value: "2" } });
    fireEvent.keyDown(screen.getByLabelText("Repeat unit"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Weeks" }));
    fireEvent.click(screen.getByRole("button", { name: "Wednesday" }));
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.queryByLabelText("Repeat every")).not.toBeInTheDocument();
    const recurrenceSummary = screen.getByTestId("custom-schedule-trigger").textContent;
    expect(recurrenceSummary).not.toContain("Select schedule");
    await userEvent.click(screen.getByLabelText("Timezone"));
    fireEvent.change(screen.getByLabelText("Search timezones"), { target: { value: "Singapore" } });
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(1));
    await userEvent.click(screen.getByRole("option", { name: "Asia/Singapore" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Timezone")).toHaveTextContent("Asia/Singapore"),
    );
    fireEvent.keyDown(screen.getByLabelText("Schedule ends"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "After" }));
    fireEvent.change(screen.getByLabelText("Occurrences"), { target: { value: "1" } });
    expect(screen.getByTestId("schedule-error")).toHaveTextContent("at least 2");
    expect(screen.getByTestId("custom-schedule-trigger")).toHaveTextContent(recurrenceSummary!);
    fireEvent.keyDown(screen.getByLabelText("Schedule ends"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "On date" }));
    expect(screen.getByTestId("custom-schedule-trigger")).toHaveTextContent(recurrenceSummary!);
    fireEvent.keyDown(screen.getByLabelText("Schedule ends"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "After" }));
    fireEvent.change(screen.getByLabelText("Occurrences"), { target: { value: "6" } });
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({
      rrule: "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;BYHOUR=9;BYMINUTE=0;COUNT=6",
      timezone: "Asia/Singapore",
    });
  });

  it("keeps the saved preset when the first Custom draft is cancelled", async () => {
    const rule = "FREQ=DAILY;BYHOUR=9;BYMINUTE=0";
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ rrule: rule })}
      />,
    );
    fireEvent.keyDown(screen.getByLabelText("Frequency"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Custom schedule" }));
    fireEvent.change(screen.getByLabelText("Repeat every"), { target: { value: "3" } });
    fireEvent.keyDown(screen.getByLabelText("Repeat every"), { key: "Escape" });
    expect(screen.getByLabelText("Frequency")).toHaveTextContent("Every day");
    expect(screen.queryByTestId("custom-schedule-trigger")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input.rrule).toBe(rule);
  });

  it("allows metadata edits on an expired schedule but validates a replacement", async () => {
    const rule = "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;UNTIL=20200101T235959Z";
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ rrule: rule, timezone: "UTC" })}
      />,
    );
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Renamed" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), {
      target: { value: "Updated prompt" },
    });
    expect(screen.queryByTestId("schedule-error")).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByLabelText("Frequency"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Every hour" }));
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    fireEvent.keyDown(screen.getByLabelText("Frequency"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Every day" }));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
      name: "Renamed",
      prompt: "Updated prompt",
      rrule: rule,
    });
  });

  it("reopens a yearly schedule and discards unapplied custom changes", async () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          rrule: "FREQ=YEARLY;INTERVAL=2;BYMONTH=6;BYMONTHDAY=1,15;BYHOUR=9;BYMINUTE=0;COUNT=4",
          timezone: "Asia/Singapore",
        })}
      />,
    );
    expect(screen.getByLabelText("Frequency")).toHaveTextContent("Custom schedule");
    expect(screen.getByLabelText("Timezone")).toHaveTextContent("Asia/Singapore");
    expect(screen.getByLabelText("Occurrences")).toHaveValue(4);
    fireEvent.click(screen.getByTestId("custom-schedule-trigger"));
    expect(screen.getByLabelText("Repeat every")).toHaveValue(2);
    expect(screen.getByLabelText("Month")).toHaveTextContent("June");
    const days = within(screen.getByRole("group", { name: "Days of the month" }));
    expect(days.getByRole("button", { name: "15" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.change(screen.getByLabelText("Repeat every"), { target: { value: "3" } });
    fireEvent.keyDown(screen.getByLabelText("Month"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "July" }));
    fireEvent.click(days.getByRole("button", { name: "20" }));
    for (const label of ["Month", "Repeat unit"]) {
      fireEvent.keyDown(screen.getByLabelText(label), { key: "Enter" });
      fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });
      expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
      expect(screen.getByLabelText("Repeat every")).toHaveValue(3);
      expect(screen.getByLabelText("Month")).toHaveTextContent("July");
      expect(days.getByRole("button", { name: "20" })).toHaveAttribute("aria-pressed", "true");
    }

    fireEvent.keyDown(screen.getByLabelText("Repeat every"), { key: "Escape" });
    fireEvent.click(screen.getByTestId("custom-schedule-trigger"));
    expect(screen.getByLabelText("Repeat every")).toHaveValue(2);
    expect(screen.getByLabelText("Month")).toHaveTextContent("June");
    expect(screen.getByRole("button", { name: "20" })).toHaveAttribute("aria-pressed", "false");

    fireEvent.keyDown(screen.getByLabelText("Repeat every"), { key: "Escape" });
    fireEvent.click(screen.getByTestId("custom-schedule-trigger"));
    fireEvent.keyDown(screen.getByLabelText("Month"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "July" }));
    const updatedDays = within(screen.getByRole("group", { name: "Days of the month" }));
    fireEvent.click(updatedDays.getByRole("button", { name: "15" }));
    fireEvent.click(updatedDays.getByRole("button", { name: "20" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input.rrule).toBe(
      "FREQ=YEARLY;INTERVAL=2;BYMONTH=7;BYMONTHDAY=1,20;BYHOUR=9;BYMINUTE=0;COUNT=4",
    );
  });

  it("saves an inclusive end date in the selected timezone", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-01T00:00:00Z"));
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ timezone: "Asia/Singapore" })}
      />,
    );
    fireEvent.keyDown(screen.getByLabelText("Schedule ends"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "On date" }));
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    expect(screen.getByLabelText("Time")).not.toHaveAttribute("aria-invalid");
    for (const value of ["0002-06-10", "0020-06-10", "0203-06-10"]) {
      fireEvent.change(screen.getByLabelText("End date"), { target: { value } });
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    }
    await userEvent.click(screen.getByLabelText("Timezone"));
    fireEvent.change(screen.getByLabelText("Search timezones"), { target: { value: "New_York" } });
    await userEvent.click(await screen.findByRole("option", { name: "America/New York" }));
    fireEvent.change(screen.getByLabelText("End date"), { target: { value: "9999-12-31" } });
    expect(screen.getByLabelText("End date")).toHaveValue("9999-12-31");
    expect(screen.getByTestId("schedule-error")).toHaveTextContent(
      "This end date is unavailable in this timezone",
    );
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    await userEvent.click(screen.getByLabelText("Timezone"));
    fireEvent.change(screen.getByLabelText("Search timezones"), { target: { value: "Singapore" } });
    await userEvent.click(await screen.findByRole("option", { name: "Asia/Singapore" }));
    expect(screen.getByLabelText("End date")).toHaveValue("9999-12-31");
    expect(screen.queryByTestId("schedule-error")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("End date"), { target: { value: "2027-06-10" } });
    await userEvent.click(screen.getByLabelText("Timezone"));
    fireEvent.change(screen.getByLabelText("Search timezones"), { target: { value: "New_York" } });
    await userEvent.click(await screen.findByRole("option", { name: "America/New York" }));
    expect(screen.getByLabelText("End date")).toHaveValue("2027-06-10");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input.rrule).toContain(";UNTIL=20270611T035959Z");
  });

  it("does not render the 'Reads as' schedule preview", () => {
    renderDialog();
    expect(screen.queryByTestId("schedule-preview")).toBeNull();
    expect(screen.queryByText(/Reads as:/i)).toBeNull();
  });
});

describe("CreateScheduledTaskDialog model + effort controls", () => {
  it("hides Advanced when no harness is available", () => {
    vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
      data: [],
    } as unknown as ReturnType<typeof agentsHook.useAvailableAgents>);
    renderDialog();
    expect(screen.queryByRole("button", { name: "Advanced" })).not.toBeInTheDocument();
  });

  it("expands Advanced again when the dialog reopens", () => {
    const onOpenChange = vi.fn();
    const { rerender } = renderDialog(onOpenChange);
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    rerender(<CreateScheduledTaskDialog open={false} onOpenChange={onOpenChange} />);
    rerender(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} />);
    expect(screen.getByRole("button", { name: "Advanced" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByTestId("task-model-trigger")).toBeVisible();
  });

  it("renders model + effort for a capable (Claude-native) agent, hides them for a plain agent", () => {
    renderDialog();
    // Default effective agent is the capable claude-native harness → controls show.
    expect(screen.getByTestId("task-model-effort-field")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    expect(screen.getByRole("button", { name: "Advanced" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByTestId("task-model-trigger")).not.toBeInTheDocument();

    // Switch to Polly (claude-sdk, no permissionMode capability) → controls hide.
    fireEvent.click(screen.getByTestId("pick-agent-polly"));
    expect(screen.queryByRole("button", { name: "Advanced" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("task-model-effort-field")).not.toBeInTheDocument();
    expect(screen.queryByTestId("task-model-trigger")).not.toBeInTheDocument();
    expect(screen.queryByTestId("task-effort-trigger")).not.toBeInTheDocument();
    // The "uses defaults" hint returns when the controls are hidden.
    expect(
      screen.getByText("Uses this agent's default model, effort, and permission settings"),
    ).toBeInTheDocument();
  });

  it("sends the selected model + effort on create for a capable agent", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "N" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });

    // Pick a model (keyboard is the reliable jsdom path for Radix Select).
    fireEvent.keyDown(screen.getByTestId("task-model-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Opus" }));
    // Pick an effort.
    fireEvent.keyDown(screen.getByTestId("task-effort-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "High" }));
    // Pick a permission mode.
    fireEvent.keyDown(screen.getByTestId("task-permission-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Accept edits" }));

    // Collapsing and reopening keeps the chosen settings; collapsed settings still save.
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    expect(screen.queryByTestId("task-model-trigger")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("Opus");
    expect(screen.getByTestId("task-effort-trigger")).toHaveTextContent("High");
    expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Accept edits");
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));

    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg.modelOverride).toBe("opus");
    expect(arg.reasoningEffort).toBe("high");
    expect(arg.permissionMode).toBe("acceptEdits");
  });

  it("omits model + effort + permission on create when left at Default", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "N" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg).not.toHaveProperty("modelOverride");
    expect(arg).not.toHaveProperty("reasoningEffort");
    expect(arg).not.toHaveProperty("permissionMode");
  });

  it("prefills model + effort + permission in edit mode from the loaded task", async () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: "ag_claude_native",
          modelOverride: "sonnet",
          reasoningEffort: "medium",
          permissionMode: "acceptEdits",
        })}
      />,
    );
    // Prefilled selections surface as the trigger's shown value.
    expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("Sonnet");
    expect(screen.getByTestId("task-effort-trigger")).toHaveTextContent("Medium");
    expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Accept edits");
    for (const field of ["model", "effort", "permission"]) {
      expect(screen.getByTestId(`task-${field}-trigger`)).toBeEnabled();
    }
    expect(screen.queryByTestId("task-session-settings-note")).not.toBeInTheDocument();
  });

  it.each([false, true])(
    "locks existing session settings with session controls supported=%s",
    async (sessionModesSupported) => {
      render(
        <CapabilitiesProvider
          info={{
            ...SERVER_INFO_OFFLINE_FALLBACK,
            scheduled_task_session_modes: sessionModesSupported,
          }}
        >
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            editingTask={scheduledTask({
              agentId: "ag_claude_native",
              sessionMode: "continue",
              sessionId: "conv_1",
            })}
          />
        </CapabilitiesProvider>,
      );
      for (const field of ["model", "effort", "permission"]) {
        expect(screen.getByTestId(`task-${field}-trigger`)).toBeDisabled();
      }
      expect(screen.getByTestId("task-session-settings-note")).toHaveTextContent(
        "To change model, effort, or permission mode, edit the existing session directly.",
      );
      fireEvent.change(screen.getByTestId("task-prompt-input"), {
        target: { value: "Updated prompt" },
      });
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const { input } = updateMutateAsync.mock.calls[0][0];
      expect(input.prompt).toBe("Updated prompt");
      for (const field of ["modelOverride", "reasoningEffort", "permissionMode", "sessionId"]) {
        expect(input).not.toHaveProperty(field);
      }
    },
  );

  it.each([false, true])(
    "only saves edited session settings when reset stays selected=%s",
    async (reset) => {
      render(
        <CapabilitiesProvider
          info={{ ...SERVER_INFO_OFFLINE_FALLBACK, scheduled_task_session_modes: true }}
        >
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            editingTask={scheduledTask({
              agentId: "ag_claude_native",
              modelOverride: "sonnet",
              reasoningEffort: "medium",
              permissionMode: "plan",
              sessionMode: "continue",
              sessionId: "conv_1",
              hostId: "host_1",
              workspace: "/home/me/canonical-repo",
            })}
          />
        </CapabilitiesProvider>,
      );

      fireEvent.click(screen.getByTestId("task-workspace-browse"));
      fireEvent.click(screen.getByRole("button", { name: "pick-workspace" }));
      expect(screen.getByTestId("task-model-trigger")).toBeDisabled();
      fireEvent.click(screen.getByTestId("task-start-new-session"));
      expect(screen.queryByTestId("task-session-settings-note")).not.toBeInTheDocument();
      for (const [field, option] of [
        ["model", "Opus"],
        ["effort", "High"],
        ["permission", "Auto"],
      ]) {
        const control = screen.getByTestId(`task-${field}-trigger`);
        expect(control).toBeEnabled();
        fireEvent.keyDown(control, { key: "Enter" });
        fireEvent.click(screen.getByRole("option", { name: option }));
      }
      if (!reset) {
        fireEvent.click(screen.getByTestId("task-start-new-session"));
        expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("Sonnet");
        expect(screen.getByTestId("task-effort-trigger")).toHaveTextContent("Medium");
        expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Plan");
        for (const field of ["model", "effort", "permission"]) {
          expect(screen.getByTestId(`task-${field}-trigger`)).toBeDisabled();
        }
      }
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const { input } = updateMutateAsync.mock.calls[0][0];
      if (reset) {
        expect(input).toMatchObject({
          modelOverride: "opus",
          reasoningEffort: "high",
          permissionMode: "auto",
          sessionId: null,
        });
      } else {
        for (const field of ["modelOverride", "reasoningEffort", "permissionMode", "sessionId"]) {
          expect(input).not.toHaveProperty(field);
        }
      }
    },
  );

  it("allows configuring a continuing task before its first session exists", () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ agentId: "ag_claude_native", sessionMode: "continue" })}
      />,
    );
    for (const field of ["model", "effort", "permission"]) {
      expect(screen.getByTestId(`task-${field}-trigger`)).toBeEnabled();
    }
    expect(screen.queryByTestId("task-session-settings-note")).not.toBeInTheDocument();
  });

  it.each(["session mode", "host"])(
    "unlocks settings when changing the %s starts a new session",
    async (change) => {
      render(
        <CapabilitiesProvider
          info={{ ...SERVER_INFO_OFFLINE_FALLBACK, scheduled_task_session_modes: true }}
        >
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            editingTask={scheduledTask({
              agentId: "ag_claude_native",
              sessionMode: "continue",
              sessionId: "conv_1",
              modelOverride: "opus",
              reasoningEffort: "high",
              permissionMode: "auto",
            })}
          />
        </CapabilitiesProvider>,
      );
      expect(screen.getByTestId("task-model-trigger")).toBeDisabled();
      fireEvent.keyDown(
        screen.getByTestId(change === "host" ? "task-host-trigger" : "task-session-mode-trigger"),
        { key: "Enter" },
      );
      fireEvent.click(
        await screen.findByRole("option", {
          name: change === "host" ? "laptop" : "New session each run",
        }),
      );
      for (const field of ["model", "effort", "permission"]) {
        expect(screen.getByTestId(`task-${field}-trigger`)).toBeEnabled();
      }
      expect(screen.queryByTestId("task-session-settings-note")).not.toBeInTheDocument();
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const { input } = updateMutateAsync.mock.calls[0][0];
      expect(input).toMatchObject({
        modelOverride: change === "host" ? null : "opus",
        reasoningEffort: change === "host" ? null : "high",
        permissionMode: "auto",
      });
      expect(input).not.toHaveProperty("sessionId");
      if (change === "host") {
        expect(input.hostId).toBe("host_1");
        expect(input).not.toHaveProperty("sessionMode");
      } else {
        expect(input.sessionMode).toBe("new");
      }
    },
  );

  it.each([false, true])(
    "keeps workspace aliases locked without reset with session controls supported=%s",
    async (sessionModesSupported) => {
      render(
        <CapabilitiesProvider
          info={{
            ...SERVER_INFO_OFFLINE_FALLBACK,
            scheduled_task_session_modes: sessionModesSupported,
          }}
        >
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            editingTask={scheduledTask({
              agentId: "ag_claude_native",
              sessionMode: "continue",
              sessionId: "conv_1",
              hostId: "host_1",
              workspace: "/home/me/canonical-repo",
            })}
          />
        </CapabilitiesProvider>,
      );
      // The picker returns an alias; only the server can resolve it to the saved directory.
      fireEvent.click(screen.getByTestId("task-workspace-browse"));
      fireEvent.click(screen.getByRole("button", { name: "pick-workspace" }));
      expect(screen.getByTestId("task-workspace-browse")).toHaveTextContent("/home/me/repo");
      for (const field of ["model", "effort", "permission"]) {
        expect(screen.getByTestId(`task-${field}-trigger`)).toBeDisabled();
      }
      expect(screen.getByTestId("task-session-settings-note")).toBeVisible();
      if (!sessionModesSupported) {
        expect(screen.queryByTestId("task-start-new-session")).not.toBeInTheDocument();
      }
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const { input } = updateMutateAsync.mock.calls[0][0];
      expect(input.workspace).toBe("/home/me/repo");
      expect(input.hostId).toBe("host_1");
      for (const field of ["modelOverride", "reasoningEffort", "permissionMode", "sessionId"]) {
        expect(input).not.toHaveProperty(field);
      }
    },
  );

  it("threads model + effort + permission through update on edit, nulling a cleared override", async () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: "ag_claude_native",
          modelOverride: "opus",
          reasoningEffort: "high",
          permissionMode: "plan",
        })}
      />,
    );
    // Reset the model back to Default; leave effort + permission untouched.
    fireEvent.keyDown(screen.getByTestId("task-model-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Default" }));

    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const { input } = updateMutateAsync.mock.calls[0][0];
    // Cleared model → null; untouched effort + permission → the prefilled values.
    expect(input.modelOverride).toBeNull();
    expect(input.reasoningEffort).toBe("high");
    expect(input.permissionMode).toBe("plan");
  });
});

describe("CreateScheduledTaskDialog project", () => {
  function renderWithProjects(
    ui: React.ReactElement = <CreateScheduledTaskDialog open onOpenChange={vi.fn()} />,
  ) {
    return render(
      <CapabilitiesProvider
        info={{ ...SERVER_INFO_OFFLINE_FALLBACK, scheduled_task_projects: true }}
      >
        {ui}
      </CapabilitiesProvider>,
    );
  }

  it("hides the project field when the server does not accept task projects", () => {
    renderDialog();
    expect(screen.queryByTestId("task-project-field")).not.toBeInTheDocument();
  });

  function fillRequired() {
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
  }

  function openProjectMenu() {
    fireEvent.keyDown(screen.getByTestId("task-project-trigger"), { key: "Enter" });
  }

  async function submitCreate() {
    const submit = screen.getByTestId("create-scheduled-task-submit");
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    return mutateAsync.mock.calls[0][0];
  }

  it("sends no project when none is picked", async () => {
    renderWithProjects();
    fillRequired();
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("No project");
    const arg = await submitCreate();
    expect(arg).not.toHaveProperty("projectId");
    expect(resolveOrCreateProjectId).not.toHaveBeenCalled();
  });

  it("files runs into an existing project picked from the menu", async () => {
    renderWithProjects();
    fillRequired();
    openProjectMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Triage" }));
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("Triage");
    expect(await submitCreate()).toMatchObject({ projectId: "proj_1" });
  });

  it("blocks a second save while a typed project is being created", async () => {
    renderWithProjects();
    fillRequired();
    openProjectMenu();
    fireEvent.change(await screen.findByLabelText("Search or create project"), {
      target: { value: "Nightly reports" },
    });
    fireEvent.click(await screen.findByRole("menuitem", { name: /Create\s+Nightly reports/ }));
    let finishCreate: (id: string) => void = () => undefined;
    resolveOrCreateProjectId.mockReturnValueOnce(
      new Promise<string>((resolve) => {
        finishCreate = resolve;
      }),
    );
    const submit = screen.getByTestId("create-scheduled-task-submit");
    fireEvent.click(submit);
    await waitFor(() => expect(submit).toBeDisabled());
    fireEvent.click(submit);
    finishCreate("proj_new");
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(resolveOrCreateProjectId).toHaveBeenCalledTimes(1);
  });

  it("keeps the picked project when it is renamed before saving", async () => {
    const view = renderWithProjects();
    fillRequired();
    openProjectMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Triage" }));
    // Another tab renames the project; the list refresh drops the old name.
    projectsQuery.current = {
      data: [{ id: "proj_1", name: "Bugs", icon: null }, PROJECTS[1]],
      isPending: false,
      isSuccess: true,
      isError: false,
    };
    view.rerender(
      <CapabilitiesProvider
        info={{ ...SERVER_INFO_OFFLINE_FALLBACK, scheduled_task_projects: true }}
      >
        <CreateScheduledTaskDialog open onOpenChange={vi.fn()} />
      </CapabilitiesProvider>,
    );
    // The trigger follows the picked project's new name.
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("Bugs");
    expect(await submitCreate()).toMatchObject({ projectId: "proj_1" });
    expect(resolveOrCreateProjectId).not.toHaveBeenCalled();
  });

  it("keeps the dialog open and retryable when creating its project fails", async () => {
    renderWithProjects();
    fillRequired();
    openProjectMenu();
    fireEvent.change(await screen.findByLabelText("Search or create project"), {
      target: { value: "Nightly reports" },
    });
    fireEvent.click(await screen.findByRole("menuitem", { name: /Create\s+Nightly reports/ }));
    resolveOrCreateProjectId.mockRejectedValueOnce(new Error("500 Internal Server Error"));
    const submit = screen.getByTestId("create-scheduled-task-submit");
    fireEvent.click(submit);
    expect(await screen.findByTestId("create-error")).toHaveTextContent(
      "500 Internal Server Error",
    );
    expect(mutateAsync).not.toHaveBeenCalled();
    resolveOrCreateProjectId.mockResolvedValueOnce("proj_new");
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({ projectId: "proj_new" });
  });

  it("creates a project from a typed name", async () => {
    renderWithProjects();
    fillRequired();
    openProjectMenu();
    fireEvent.change(await screen.findByLabelText("Search or create project"), {
      target: { value: "Nightly reports" },
    });
    fireEvent.click(await screen.findByRole("menuitem", { name: /Create\s+Nightly reports/ }));
    resolveOrCreateProjectId.mockResolvedValueOnce("proj_new");
    expect(await submitCreate()).toMatchObject({ projectId: "proj_new" });
    expect(resolveOrCreateProjectId).toHaveBeenCalledWith("Nightly reports");
  });

  function renderEditingFiledTask() {
    renderWithProjects(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ projectId: "proj_1" })}
      />,
    );
  }

  it("keeps an edited task's project when it is not changed", async () => {
    renderEditingFiledTask();
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("Triage");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const { input } = updateMutateAsync.mock.calls[0][0];
    expect(input).not.toHaveProperty("projectId");
  });

  it("shows a deleted saved project as no project and clears it on save", async () => {
    renderWithProjects(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ projectId: "proj_deleted" })}
      />,
    );
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("No project");
    openProjectMenu();
    expect(await screen.findByLabelText("Search or create project")).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "No project" })).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByLabelText("Search or create project"), { key: "Escape" });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({ projectId: null });
  });

  it("moves an edited task to another existing project", async () => {
    renderEditingFiledTask();
    openProjectMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Reports" }));
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("Reports");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({ projectId: "proj_2" });
  });

  it("shows a loading label, not a deleted one, while projects load", async () => {
    projectsQuery.current = { data: undefined, isPending: true, isSuccess: false, isError: false };
    renderEditingFiledTask();
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("Loading…");
    openProjectMenu();
    expect(await screen.findByLabelText("Search or create project")).toBeInTheDocument();
  });

  it("can unfile a task whose project list fails to load", async () => {
    projectsQuery.current = { data: undefined, isPending: false, isSuccess: false, isError: true };
    renderEditingFiledTask();
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("Project unavailable");
    openProjectMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: "No project" }));
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("No project");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({ projectId: null });
  });

  it("unfiles an edited task's runs when its project is removed", async () => {
    renderEditingFiledTask();
    openProjectMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Remove from Triage" }));
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("No project");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({ projectId: null });
  });
});

describe("nested dropdowns do not dismiss the Dialog (isInsidePopper guard)", () => {
  // The guard's decision is pure DOM: is the interaction target inside a Radix
  // popper / Select portal? Unit-test that directly — jsdom can't faithfully
  // reproduce Radix's pointer-capture portal outside-click, so the full
  // "click an option → dialog stays open" path is covered by the live pane
  // verification (see the task report), not here.
  it("treats a click inside a Select portal as inside-popper", () => {
    const content = document.createElement("div");
    content.setAttribute("data-slot", "select-content");
    const option = document.createElement("div");
    option.setAttribute("role", "option");
    content.appendChild(option);
    document.body.appendChild(content);
    expect(isInsidePopper(option)).toBe(true);

    const wrapper = document.createElement("div");
    wrapper.setAttribute("data-radix-popper-content-wrapper", "");
    const inner = document.createElement("span");
    wrapper.appendChild(inner);
    document.body.appendChild(wrapper);
    expect(isInsidePopper(inner)).toBe(true);

    const listbox = document.createElement("div");
    listbox.setAttribute("role", "listbox");
    document.body.appendChild(listbox);
    expect(isInsidePopper(listbox)).toBe(true);
  });

  it("treats a click inside the agent DropdownMenu portal as inside-popper", () => {
    const content = document.createElement("div");
    content.setAttribute("data-slot", "dropdown-menu-content");
    const item = document.createElement("div");
    content.appendChild(item);
    document.body.appendChild(content);
    expect(isInsidePopper(item)).toBe(true);
  });

  it("treats the real backdrop (outside any popper) as NOT inside-popper", () => {
    const backdrop = document.createElement("div");
    document.body.appendChild(backdrop);
    expect(isInsidePopper(backdrop)).toBe(false);
    expect(isInsidePopper(null)).toBe(false);
    expect(isInsidePopper(document.body)).toBe(false);
  });
});

describe("shouldGuardDialogDismiss (backdrop click closes; dropdown-dismiss guarded)", () => {
  function overlayTarget(): Element {
    const overlay = document.createElement("div");
    overlay.setAttribute("data-slot", "dialog-overlay");
    document.body.appendChild(overlay);
    return overlay;
  }
  function popperTarget(): Element {
    const content = document.createElement("div");
    content.setAttribute("data-slot", "select-content");
    const inner = document.createElement("div");
    content.appendChild(inner);
    document.body.appendChild(content);
    return inner;
  }
  function dialogContentTarget(): Element {
    const content = document.createElement("div");
    content.setAttribute("data-slot", "dialog-content");
    document.body.appendChild(content);
    return content;
  }

  it("does NOT guard a genuine backdrop-overlay click → dialog dismisses", () => {
    const target = overlayTarget();
    // Even while a Select is open / just closed / inside grace, a backdrop click
    // must dismiss (the bug was this being swallowed).
    expect(shouldGuardDialogDismiss(target, { selectOpen: true, msSinceSelectClose: 0 })).toBe(
      false,
    );
    expect(shouldGuardDialogDismiss(target, { selectOpen: false, msSinceSelectClose: 10 })).toBe(
      false,
    );
  });

  it("guards a click INSIDE a popper (option pick) → dialog stays open", () => {
    expect(
      shouldGuardDialogDismiss(popperTarget(), { selectOpen: false, msSinceSelectClose: 9999 }),
    ).toBe(true);
  });

  it("guards while a dropdown is open, including clicks inside dialog content", () => {
    expect(
      shouldGuardDialogDismiss(dialogContentTarget(), {
        selectOpen: true,
        msSinceSelectClose: 9999,
      }),
    ).toBe(true);
  });

  it("guards while a Select is open, and within the grace window after it closes", () => {
    const plain = document.createElement("div");
    document.body.appendChild(plain);
    // Select currently open → guarded.
    expect(shouldGuardDialogDismiss(plain, { selectOpen: true, msSinceSelectClose: 9999 })).toBe(
      true,
    );
    // Trailing focus-outside within grace → guarded.
    expect(shouldGuardDialogDismiss(plain, { selectOpen: false, msSinceSelectClose: 50 })).toBe(
      true,
    );
    // Well after grace, not in a popper, no select → NOT guarded (would dismiss).
    expect(shouldGuardDialogDismiss(plain, { selectOpen: false, msSinceSelectClose: 500 })).toBe(
      false,
    );
  });
});

describe("CreateScheduledTaskDialog session mode", () => {
  function renderWithSessionModes(
    editingTask?: ScheduledTasksApiModule.ScheduledTask,
    features: Record<string, boolean> = {},
  ) {
    return render(
      <CapabilitiesProvider
        info={{
          ...SERVER_INFO_OFFLINE_FALLBACK,
          scheduled_task_session_modes: true,
          features,
        }}
      >
        <CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={editingTask} />
      </CapabilitiesProvider>,
    );
  }

  function chooseContinue() {
    const trigger = screen.getByTestId("task-session-mode-trigger");
    fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    fireEvent.click(trigger);
    fireEvent.click(screen.getByTestId("task-session-mode-continue"));
  }

  it("hides the Session control on a server that does not accept session modes", () => {
    renderDialog();
    expect(screen.queryByTestId("task-session-mode-field")).toBeNull();
  });

  it("sends sessionMode only when continue is chosen on create", async () => {
    renderWithSessionModes();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
    chooseContinue();
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({ sessionMode: "continue" });
  });

  it("leaves sessionMode out of a create that keeps a new session per run", async () => {
    renderWithSessionModes();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).not.toHaveProperty("sessionMode");
  });

  it("says a sandbox automation that continues a session keeps one sandbox", () => {
    render(
      <CapabilitiesProvider
        info={{
          ...SERVER_INFO_OFFLINE_FALLBACK,
          scheduled_task_session_modes: true,
          managed_sandboxes_enabled: true,
        }}
      >
        <CreateScheduledTaskDialog open onOpenChange={vi.fn()} />
      </CapabilitiesProvider>,
    );
    chooseContinue();
    const host = screen.getByTestId("task-host-trigger");
    fireEvent.pointerDown(host, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    fireEvent.click(host);
    fireEvent.click(screen.getByTestId("task-host-sandbox-option"));

    expect(
      screen.getByText(
        "Keeps one sandbox for every run, including a session started after a reset. Shutdown follows the server’s sandbox configuration.",
      ),
    ).toBeInTheDocument();
  });

  it("clears the continued session when asked to start a new one on the next run", async () => {
    renderWithSessionModes(scheduledTask({ sessionMode: "continue", sessionId: "conv_1" }));
    fireEvent.click(screen.getByTestId("task-start-new-session"));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const { input } = updateMutateAsync.mock.calls[0][0];
    expect(input.sessionId).toBeNull();
    expect(input).not.toHaveProperty("sessionMode");
  });

  it("keeps a template automation's one session on edit, offering only a reset", async () => {
    const prompt = renderTriggerPrompt(
      "jira",
      {
        event: "created",
        scope: "ES",
        conditions: [],
        instructions: "Triage it.",
        linkBack: true,
      },
      { placement: "same_host", sessionLink: "https://omni.example.com/c/CONVERSATION_ID" },
    );
    renderWithSessionModes(scheduledTask({ sessionMode: "continue", sessionId: "conv_1", prompt }));
    expect(screen.queryByTestId("task-session-mode-trigger")).toBeNull();
    fireEvent.click(screen.getByTestId("task-start-new-session"));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const { input } = updateMutateAsync.mock.calls[0][0];
    expect(input.sessionId).toBeNull();
    expect(input).not.toHaveProperty("sessionMode");
  });

  it("shows nothing about the session for a template automation that hasn't run", () => {
    const prompt =
      "This automation watches Slack and starts one Omnigent session for each newly created message that matches.";
    renderWithSessionModes(scheduledTask({ sessionMode: "continue", prompt }));
    expect(screen.queryByTestId("task-session-mode-field")).toBeNull();
  });

  it("keeps the session choice when editing an automation written by hand", () => {
    renderWithSessionModes(scheduledTask({ sessionMode: "continue", sessionId: "conv_1" }));
    expect(screen.getByTestId("task-session-mode-trigger")).toHaveTextContent(
      "Continue in the same session",
    );
  });

  it("preserves a minute schedule on edit and blocks switching it to new sessions", async () => {
    renderWithSessionModes(
      scheduledTask({ sessionMode: "continue", rrule: "FREQ=MINUTELY;INTERVAL=10" }),
      { automations_v2: true, automations_subhour: true },
    );
    expect(screen.getByTestId("custom-schedule-trigger")).toHaveTextContent("Every 10 minutes");
    fireEvent.keyDown(screen.getByTestId("task-session-mode-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "New session each run" }));
    expect(screen.getByTestId("custom-schedule-trigger")).toHaveTextContent("Every 10 minutes");
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    expect(screen.getByTestId("schedule-error")).toHaveTextContent(
      "require continuing in the same session",
    );
    chooseContinue();
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input.rrule).toBe("FREQ=MINUTELY;INTERVAL=10");
  });

  it.each([undefined, false, true])(
    "negotiates minute schedules with subhour support %s",
    async (supported) => {
      renderWithSessionModes(
        scheduledTask({ sessionMode: "continue", rrule: "FREQ=MINUTELY;INTERVAL=10" }),
        {
          automations_v2: true,
          ...(supported === undefined ? {} : { automations_subhour: supported }),
        },
      );
      expect(screen.getByTestId("custom-schedule-trigger")).toHaveTextContent("Every 10 minutes");
      const submit = screen.getByTestId("create-scheduled-task-submit");
      if (supported) expect(submit).toBeEnabled();
      else {
        expect(submit).toBeDisabled();
        expect(screen.getByTestId("schedule-error")).toHaveTextContent("at least 60 minutes");
        fireEvent.click(submit);
        expect(updateMutateAsync).not.toHaveBeenCalled();
      }
      fireEvent.click(screen.getByTestId("custom-schedule-trigger"));
      fireEvent.keyDown(screen.getByTestId("custom-freq-trigger"), { key: "Enter" });
      const minutes = await screen.findByRole("option", { name: "Minutes" });
      if (supported) expect(minutes).not.toHaveAttribute("aria-disabled", "true");
      else expect(minutes).toHaveAttribute("aria-disabled", "true");
      fireEvent.click(screen.getByRole("option", { name: "Hours" }));
      expect(screen.getByTestId("custom-interval")).toHaveValue(1);
      fireEvent.click(screen.getByRole("button", { name: "Apply" }));
      fireEvent.click(submit);
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      expect(updateMutateAsync.mock.calls[0][0].input.rrule).toBe(
        "FREQ=HOURLY;INTERVAL=1;BYMINUTE=0",
      );
    },
  );

  it.each([false, true])(
    "offers Minutes on create only with subhour support %s",
    async (supported) => {
      renderWithSessionModes(undefined, { automations_v2: true, automations_subhour: supported });
      chooseContinue();
      fireEvent.keyDown(screen.getByTestId("schedule-preset-trigger"), { key: "Enter" });
      fireEvent.click(await screen.findByRole("option", { name: "Custom schedule" }));
      fireEvent.keyDown(screen.getByTestId("custom-freq-trigger"), { key: "Enter" });
      await screen.findByRole("option", { name: "Hours" });
      expect(screen.queryByRole("option", { name: "Minutes" }) !== null).toBe(supported);
    },
  );

  it("offers no new-session option before the first run creates a session", () => {
    renderWithSessionModes(scheduledTask({ sessionMode: "continue" }));
    expect(screen.queryByTestId("task-start-new-session")).toBeNull();
  });
});

describe("CreateScheduledTaskDialog trigger template", () => {
  function renderTrigger(
    source: "jira" | "slack" | "github" | "linear",
    extraInfo: Partial<typeof SERVER_INFO_OFFLINE_FALLBACK> = {},
    sessionLink = "https://omni.example.com/c/CONVERSATION_ID",
  ) {
    return render(
      <CapabilitiesProvider
        info={{ ...SERVER_INFO_OFFLINE_FALLBACK, scheduled_task_session_modes: true, ...extraInfo }}
      >
        <CreateScheduledTaskDialog
          open
          onOpenChange={vi.fn()}
          initialTrigger={source}
          sessionLink={sessionLink}
        />
      </CapabilitiesProvider>,
    );
  }

  /** Open a builder Select the Radix-in-jsdom way and pick an option. */
  function pick(triggerTestId: string, optionTestId: string) {
    const trigger = screen.getByTestId(triggerTestId);
    fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    fireEvent.click(trigger);
    fireEvent.click(screen.getByTestId(optionTestId));
  }

  function type(testId: string, value: string) {
    fireEvent.change(screen.getByTestId(testId), { target: { value } });
  }

  function fillJira() {
    type("task-trigger-scope", "ES");
    type("task-trigger-instructions", "Triage it.");
  }

  async function submittedPrompt(): Promise<string> {
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    return mutateAsync.mock.calls[0][0].prompt;
  }

  it("replaces the prompt with a trigger builder, runs hourly and keeps one session", () => {
    renderTrigger("jira");
    expect(screen.getByTestId("task-name-input")).toHaveValue("Jira tickets");
    expect(screen.queryByTestId("task-prompt-input")).toBeNull();
    expect(screen.getByTestId("task-trigger-event")).toHaveTextContent("Ticket created");
    expect(screen.getByTestId("task-trigger-builder")).toHaveTextContent("in project");
    expect(screen.getByTestId("schedule-preset-trigger")).toHaveTextContent("Every hour");
    // The trigger always keeps one session, so the form doesn't ask.
    expect(screen.queryByTestId("task-session-mode-field")).toBeNull();
    expect(screen.getByTestId("task-trigger-fields")).toHaveTextContent(
      "Runs your automation on all new tickets that match these conditions. Older tickets are skipped, even if they are edited to match later.",
    );
    expect(screen.getByTestId("task-trigger-fields")).toHaveTextContent(
      "Each run handles up to 10 new tickets, oldest first. The rest are picked up on the next run.",
    );
    // The form warns the source is untrusted and says link-back writes there.
    expect(screen.getByTestId("task-trigger-untrusted-note")).toHaveTextContent(
      "Anyone who can create a ticket in this project can add instructions that the agent may follow. The agent runs with your tools and your permissions. Make sure you trust them.",
    );
    const note = screen.getByTestId("task-trigger-attribution-note");
    expect(note).toHaveTextContent("Link-back posts to Jira with your account.");
    expect(note).toHaveTextContent(
      'Anything posted with your account ends with "Sent with Omnigent".',
    );
    // Runs can only search where the agent has the service's tools.
    expect(screen.getByTestId("task-trigger-tools-note")).toHaveTextContent(
      "This only works if the agent has a Jira MCP server where this task runs. Without one, each run stops and says so in its reply.",
    );
    pick("task-trigger-then", "task-trigger-then-none");
    expect(note).not.toHaveTextContent("Link-back posts");
    expect(note).toHaveTextContent("Sent with Omnigent");
  });

  it.each([
    ["arclet", true],
    ["modal", false],
  ])("on a %s sandbox, hides the tools warning: %s", (provider, hidden) => {
    renderTrigger("jira", {
      managed_sandboxes_enabled: true,
      sandbox_provider: provider,
      sandbox_providers: [provider],
    });
    const host = screen.getByTestId("task-host-trigger");
    fireEvent.pointerDown(host, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    fireEvent.click(host);
    fireEvent.click(screen.getByTestId("task-host-sandbox-option"));

    if (hidden) {
      expect(screen.getByTestId("task-host-trigger")).toHaveTextContent("Arclet Sandbox");
      expect(screen.queryByTestId("task-trigger-tools-note")).toBeNull();
    } else {
      expect(screen.getByTestId("task-trigger-tools-note")).toBeInTheDocument();
    }
    // The untrusted-source warning is about the source, not the tools, so it stays.
    expect(screen.getByTestId("task-trigger-untrusted-note")).toBeInTheDocument();
  });

  it("keeps submit disabled until the scope, every condition and the instructions are filled", () => {
    renderTrigger("jira");
    const submit = screen.getByTestId("create-scheduled-task-submit");
    expect(submit).toBeDisabled();
    expect(screen.queryByTestId("task-trigger-preview")).toBeNull();

    type("task-trigger-scope", ",");
    type("task-trigger-instructions", "Triage it.");
    expect(submit).toBeDisabled();
    fillJira();
    expect(submit).toBeEnabled();
    expect(screen.getByTestId("task-trigger-preview")).toHaveTextContent('project = "ES"');

    // A new row starts empty and blocks saving until it's filled in or removed.
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    expect(submit).toBeDisabled();
    expect(screen.getByTestId("task-trigger-empty-condition")).toHaveTextContent(
      "Fill in or remove each condition.",
    );
    type("task-trigger-condition-0-value", "omnigent");
    expect(submit).toBeEnabled();
    fireEvent.click(screen.getByTestId("task-trigger-condition-0-remove"));
    expect(screen.queryByTestId("task-trigger-condition-0")).toBeNull();
    expect(submit).toBeEnabled();

    // Over-long instructions would crowd the item out of a worker's first message.
    type("task-trigger-instructions", "x".repeat(2001));
    expect(submit).toBeDisabled();
    expect(screen.getByTestId("task-trigger-too-long")).toHaveTextContent(
      "Keep instructions to 2,000 characters or fewer",
    );
  });

  it("compiles the builder rows into the saved hourly continue-mode task", async () => {
    // A host-transformed link isn't a prefix, so it must reach the prompt as given.
    renderTrigger("jira", {}, "https://host.example.com/embed#/c/CONVERSATION_ID?o=42");
    fillJira();
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    type("task-trigger-condition-0-value", "omnigent");
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    pick("task-trigger-condition-1-field", "task-trigger-condition-1-field-issuetype");
    pick("task-trigger-condition-1-operator", "task-trigger-condition-1-operator-is_not");
    type("task-trigger-condition-1-value", "Epic");
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    pick("task-trigger-condition-2-field", "task-trigger-condition-2-field-jql");
    // A raw JQL row has one operator, shown as a fixed word.
    expect(screen.queryByTestId("task-trigger-condition-2-operator")).toBeNull();
    expect(screen.getByTestId("task-trigger-condition-2")).toHaveTextContent("matches");
    type("task-trigger-condition-2-value", 'summary ~ "crash"');

    const prompt = await submittedPrompt();
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg).toMatchObject({
      name: "Jira tickets",
      sessionMode: "continue",
      templateId: "jira",
    });
    expect(arg.rrule).toMatch(/^FREQ=HOURLY/);
    expect(prompt).toContain(
      '(project = "ES" AND labels = "omnigent" AND issuetype != "Epic" AND (summary ~ "crash")) AND created >= <that time> ORDER BY created ASC',
    );
    expect(prompt).toContain('placement "same_host"');
    expect(prompt).toContain(
      'Comment "Omnigent session: https://host.example.com/embed#/c/CONVERSATION_ID?o=42" on each ticket',
    );
    expect(prompt).toContain("Instructions for each session:\nTriage it.\n");
  });

  it("hides the value box for an assignee row that is unassigned", async () => {
    renderTrigger("jira");
    fillJira();
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    pick("task-trigger-condition-0-field", "task-trigger-condition-0-field-assignee");
    pick("task-trigger-condition-0-operator", "task-trigger-condition-0-operator-empty");
    expect(screen.getByTestId("task-trigger-condition-0-operator")).toHaveTextContent(
      "is unassigned",
    );
    // Nothing to fill in, so the row neither shows a value box nor blocks saving.
    expect(screen.queryByTestId("task-trigger-condition-0-value")).toBeNull();
    expect(screen.queryByTestId("task-trigger-empty-condition")).toBeNull();

    expect(await submittedPrompt()).toContain('(project = "ES" AND assignee is EMPTY)');
  });

  it("starts each worker in its own sandbox when the automation runs in a sandbox", async () => {
    renderTrigger("jira", { managed_sandboxes_enabled: true });
    fillJira();
    const host = screen.getByTestId("task-host-trigger");
    fireEvent.pointerDown(host, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    fireEvent.click(host);
    fireEvent.click(screen.getByTestId("task-host-sandbox-option"));

    const prompt = await submittedPrompt();
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({ executionTarget: "managed_sandbox" });
    expect(prompt).toContain('placement "managed"');
  });

  it("leaves the link-back step out when then only starts the session", async () => {
    renderTrigger("slack");
    expect(screen.getByTestId("task-trigger-event")).toHaveTextContent("Message posted");
    expect(screen.getByTestId("task-trigger-untrusted-note")).toHaveTextContent(
      "Anyone who can post in this channel can add instructions",
    );
    type("task-trigger-scope", "#eng-support");
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    type("task-trigger-condition-0-value", "error");
    type("task-trigger-instructions", "Reproduce it.");
    pick("task-trigger-then", "task-trigger-then-none");

    const prompt = await submittedPrompt();
    expect(mutateAsync.mock.calls[0][0].templateId).toBe("slack");
    expect(prompt).toContain(
      'Watch: new Slack messages in #eng-support whose text contains "error"',
    );
    expect(prompt).not.toContain("Omnigent session:");
  });

  it("builds a Linear trigger for a team, with an unassigned row and the issue link-back", async () => {
    renderTrigger("linear");
    expect(screen.getByTestId("task-name-input")).toHaveValue("Linear issues");
    expect(screen.getByTestId("task-trigger-event")).toHaveTextContent("Issue created");
    expect(screen.getByTestId("task-trigger-builder")).toHaveTextContent("in team");
    expect(screen.getByTestId("task-trigger-untrusted-note")).toHaveTextContent(
      "Anyone who can create an issue in this team can add instructions",
    );
    expect(screen.getByTestId("task-trigger-tools-note")).toHaveTextContent(
      "This only works if the agent has a Linear MCP server where this task runs.",
    );
    type("task-trigger-scope", "ENG");
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    pick("task-trigger-condition-0-field", "task-trigger-condition-0-field-assignee");
    pick("task-trigger-condition-0-operator", "task-trigger-condition-0-operator-empty");
    type("task-trigger-instructions", "Triage it.");

    const prompt = await submittedPrompt();
    expect(mutateAsync.mock.calls[0][0].templateId).toBe("linear");
    expect(prompt).toContain("Watch: new Linear issues in team ENG that are unassigned");
    expect(prompt).toContain(
      'Comment "Omnigent session: https://omni.example.com/c/CONVERSATION_ID" on each issue',
    );
  });

  it("separates the rendered system and user prompts on Edit as prompt", async () => {
    renderTrigger("github");
    expect(screen.getByTestId("task-trigger-event")).toHaveTextContent("Issue opened");
    const warning = screen.getByTestId("task-trigger-untrusted-note");
    expect(warning).toHaveTextContent("Anyone who can open an issue in this repository can add");
    pick("task-trigger-event", "task-trigger-event-pr");
    expect(warning).toHaveTextContent("Anyone who can open a pull request in this repository can");
    type("task-trigger-scope", "acme/app");
    type("task-trigger-instructions", "Fix it.");
    fireEvent.click(screen.getByTestId("task-trigger-edit-as-prompt"));

    expect(screen.queryByTestId("task-trigger-fields")).toBeNull();
    const prompt = screen.getByTestId("task-prompt-input") as HTMLTextAreaElement;
    expect(prompt.value).toBe("Fix it.");
    expect(screen.getByRole("button", { name: "System prompt" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    // The kept session stays, but the user may now change it.
    expect(screen.getByTestId("task-session-mode-trigger")).toBeEnabled();
    expect(screen.getByTestId("task-session-mode-trigger")).toHaveTextContent(
      "Continue in the same session",
    );
    const editedPrompt = `${prompt.value}\nOnly act on pull requests labeled ready.`;
    fireEvent.change(prompt, { target: { value: editedPrompt } });
    const submitted = await submittedPrompt();
    expect(submitted).toContain("repo:acme/app is:pr created:>=<that time>");
    expect(splitTriggerPrompt(submitted).userPrompt).toBe(editedPrompt);
    expect(mutateAsync.mock.calls[0][0].templateId).toBe("github");
  });
});

describe("editing template prompts", () => {
  const original = renderTriggerPrompt(
    "jira",
    { ...emptyTriggerFields("jira"), scope: "ENG", instructions: "Triage the ticket." },
    { placement: "same_host", sessionLink: "https://example.test/c/CONVERSATION_ID" },
  );

  it("keeps a hand-written heading editable for unrelated changes", async () => {
    const prompt = "Write a training document.\n\nInstructions for each session:\n";
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ prompt })}
      />,
    );
    expect(screen.getByLabelText("Prompt", { exact: true })).toHaveValue(prompt);
    expect(screen.queryByRole("button", { name: "System prompt" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId("task-name-input"), {
      target: { value: "Updated name" },
    });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
      name: "Updated name",
      prompt: prompt.trim(),
    });
  });

  it.each([
    ["Your prompt", false],
    ["System prompt", false],
    ["System prompt", true],
  ] as const)(
    "saves and reopens a format-changing edit to %s (remove signature: %s)",
    async (field, removeSignature) => {
      const props = { onOpenChange: vi.fn(), editingTask: scheduledTask({ prompt: original }) };
      const { rerender } = render(<CreateScheduledTaskDialog open {...props} />);
      expect(
        screen.queryByText(/These edits change the template structure/),
      ).not.toBeInTheDocument();
      if (field === "System prompt") {
        fireEvent.click(screen.getByRole("button", { name: "System prompt" }));
        fireEvent.click(screen.getByRole("button", { name: "Edit system prompt" }));
      }
      const editor = screen.getByRole("textbox", { name: field }) as HTMLTextAreaElement;
      const savedValue = editor.value;
      const changedValue = removeSignature
        ? savedValue.replace("This automation watches Jira ", "Custom Jira automation ")
        : `${savedValue}\n\nInstructions for each session:\nExtra heading.`;
      fireEvent.change(editor, { target: { value: changedValue } });
      expect(
        screen.getByText(/After saving, the full text will appear as one prompt/),
      ).toBeVisible();
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
      fireEvent.change(editor, { target: { value: savedValue } });
      expect(
        screen.queryByText(/These edits change the template structure/),
      ).not.toBeInTheDocument();
      fireEvent.change(editor, { target: { value: changedValue } });
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const saved = updateMutateAsync.mock.calls[0][0].input.prompt;
      expect(saved).toContain(removeSignature ? "Custom Jira automation " : "Extra heading.");
      expect(saved).toContain("Triage the ticket.");
      rerender(<CreateScheduledTaskDialog open={false} {...props} />);
      rerender(
        <CreateScheduledTaskDialog
          open
          {...props}
          editingTask={scheduledTask({ prompt: saved })}
        />,
      );
      expect(screen.getByRole("textbox", { name: "Prompt" })).toHaveValue(saved);
      expect(screen.queryByRole("button", { name: "System prompt" })).not.toBeInTheDocument();
    },
  );

  it("edits the user prompt while preserving collapsed system instructions", async () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ prompt: original })}
      />,
    );
    expect(screen.getByLabelText("Your prompt")).toHaveValue("Triage the ticket.");
    expect(screen.getByRole("button", { name: "System prompt" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByRole("button", { name: "Edit system prompt" })).not.toBeInTheDocument();
    for (const value of ["", "  \n"]) {
      fireEvent.change(screen.getByLabelText("Your prompt"), { target: { value } });
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      expect(screen.getByText("Your prompt can't be empty.")).toBeVisible();
      expect(screen.queryByText(/After saving, the full text/)).not.toBeInTheDocument();
    }
    fireEvent.change(screen.getByLabelText("Your prompt"), {
      target: { value: "Investigate it." },
    });
    expect(screen.queryByText("Your prompt can't be empty.")).not.toBeInTheDocument();
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input.prompt).toBe(
      original.replace("Triage the ticket.", "Investigate it."),
    );
  });

  it("requires expansion and Edit, saves both prompts, and resets on reopening", async () => {
    const props = { onOpenChange: vi.fn(), editingTask: scheduledTask({ prompt: original }) };
    const { rerender } = render(<CreateScheduledTaskDialog open {...props} />);
    const toggle = screen.getByRole("button", { name: "System prompt" });
    fireEvent.click(toggle);
    expect(screen.queryByRole("textbox", { name: "System prompt" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit system prompt" }));
    const systemPrompt =
      splitTriggerPrompt(original).systemPrompt!.replace('project = "ENG"', 'project = "OPS"') +
      "\n";
    fireEvent.change(screen.getByRole("textbox", { name: "System prompt" }), {
      target: { value: "  \n" },
    });
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Done editing system prompt" }));
    expect(screen.getByText(/System prompt can't be empty/)).toBeVisible();
    fireEvent.click(toggle);
    expect(screen.getByText(/System prompt can't be empty/)).toBeVisible();
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "Edit system prompt" }));
    fireEvent.change(screen.getByRole("textbox", { name: "System prompt" }), {
      target: { value: systemPrompt },
    });
    expect(screen.queryByText(/System prompt can't be empty/)).not.toBeInTheDocument();
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Your prompt"), { target: { value: "Fix it." } });
    fireEvent.click(toggle);
    fireEvent.click(toggle);
    expect(screen.queryByRole("textbox", { name: "System prompt" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const saved = updateMutateAsync.mock.calls[0][0].input.prompt;
    expect(splitTriggerPrompt(saved)).toEqual({ userPrompt: "Fix it.", systemPrompt });

    rerender(<CreateScheduledTaskDialog open={false} {...props} />);
    rerender(
      <CreateScheduledTaskDialog open {...props} editingTask={scheduledTask({ prompt: saved })} />,
    );
    expect(screen.getByLabelText("Your prompt")).toHaveValue("Fix it.");
    expect(screen.getByRole("button", { name: "System prompt" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    fireEvent.click(screen.getByRole("button", { name: "System prompt" }));
    expect(screen.queryByRole("textbox", { name: "System prompt" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit system prompt" }));
    fireEvent.change(screen.getByRole("textbox", { name: "System prompt" }), {
      target: { value: "Unsaved edit" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(props.onOpenChange).toHaveBeenLastCalledWith(false);
    expect(updateMutateAsync).toHaveBeenCalledTimes(1);
    rerender(<CreateScheduledTaskDialog open={false} {...props} />);
    rerender(
      <CreateScheduledTaskDialog open {...props} editingTask={scheduledTask({ prompt: saved })} />,
    );
    expect(screen.getByLabelText("Your prompt")).toHaveValue("Fix it.");
    fireEvent.click(screen.getByRole("button", { name: "System prompt" }));
    expect(screen.queryByText("Unsaved edit")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit system prompt" }));
    expect(screen.getByRole("textbox", { name: "System prompt" })).toHaveValue(systemPrompt);
  });
});

describe("scheduler capabilities", () => {
  it.each([false, undefined, "loading"] as const)(
    "hides anchored recurrence controls when automations_v2 is %s",
    async (enabled) => {
      const info: React.ComponentProps<typeof CapabilitiesProvider>["info"] =
        enabled === "loading"
          ? "loading"
          : {
              ...SERVER_INFO_OFFLINE_FALLBACK,
              features: enabled === undefined ? {} : { automations_v2: enabled },
            };
      render(
        <CapabilitiesProvider info={info}>
          <CreateScheduledTaskDialog open onOpenChange={vi.fn()} />
        </CapabilitiesProvider>,
      );
      fireEvent.keyDown(screen.getByLabelText("Frequency"), { key: "Enter" });
      expect((await screen.findAllByRole("option")).map((option) => option.textContent)).toEqual([
        "Every hour",
        "Every day",
        "Every weekday",
        "Every week",
      ]);
      fireEvent.click(screen.getByRole("option", { name: "Every hour" }));
      fireEvent.keyDown(screen.getByLabelText("Schedule ends"), { key: "Enter" });
      expect((await screen.findAllByRole("option")).map((option) => option.textContent)).toEqual([
        "Never",
        "On date",
      ]);
    },
  );

  it.each(
    ([false, undefined, "loading"] as const).flatMap((enabled) =>
      [
        { rule: "FREQ=DAILY;INTERVAL=2;BYHOUR=9;BYMINUTE=0", replacements: ["custom"] },
        { rule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;COUNT=3", replacements: ["count"] },
        {
          rule: "FREQ=DAILY;INTERVAL=2;BYHOUR=9;BYMINUTE=0;COUNT=3",
          replacements: ["custom", "count"],
        },
        {
          rule: "FREQ=DAILY;INTERVAL=2;BYHOUR=9;BYMINUTE=0;COUNT=3",
          replacements: ["count", "custom"],
        },
      ].map((scenario) => ({ enabled, ...scenario })),
    ),
  )(
    "protects saved rules with $enabled capability: $rule, replacing $replacements",
    async ({ enabled, rule, replacements }) => {
      const info: React.ComponentProps<typeof CapabilitiesProvider>["info"] =
        enabled === "loading"
          ? "loading"
          : {
              ...SERVER_INFO_OFFLINE_FALLBACK,
              features: enabled === undefined ? {} : { automations_v2: enabled },
            };
      render(
        <CapabilitiesProvider info={info}>
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            editingTask={scheduledTask({ rrule: rule })}
          />
        </CapabilitiesProvider>,
      );
      expect(screen.getByRole("alert")).toHaveTextContent(
        "This server does not support custom schedules or occurrence limits",
      );
      fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Renamed" } });
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      expect(updateMutateAsync).not.toHaveBeenCalled();
      for (const [index, kind] of replacements.entries()) {
        if (kind === "custom") {
          expect(screen.getByTestId("custom-schedule-trigger")).toBeDisabled();
          fireEvent.keyDown(screen.getByLabelText("Frequency"), { key: "Enter" });
          fireEvent.click(screen.getByRole("option", { name: "Every day" }));
        } else {
          expect(screen.getByLabelText("Occurrences")).toHaveValue(3);
          expect(screen.getByLabelText("Occurrences")).toBeDisabled();
          fireEvent.keyDown(screen.getByLabelText("Schedule ends"), { key: "Enter" });
          fireEvent.click(screen.getByRole("option", { name: "Never" }));
        }
        if (index < replacements.length - 1) {
          expect(screen.getByRole("alert")).toHaveTextContent(
            "This server does not support custom schedules or occurrence limits",
          );
          expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
          fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
          expect(updateMutateAsync).not.toHaveBeenCalled();
        }
      }
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      expect(updateMutateAsync.mock.calls[0][0].input.rrule).toBe("FREQ=DAILY;BYHOUR=9;BYMINUTE=0");
    },
  );
});

describe("automation harness settings", () => {
  const models = [
    {
      id: "model-a",
      displayName: "Model A",
      isDefault: true,
      supportedReasoningEfforts: [{ reasoningEffort: "high" }, { reasoningEffort: "low" }],
    },
    {
      id: "model-b",
      displayName: "Model B",
      supportedReasoningEfforts: [{ reasoningEffort: "low" }],
    },
  ];
  function mockHarness(harness: string) {
    const agent = testAgent("selected-agent", `${harness}-ui`, { harness });
    vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({ data: [agent] } as ReturnType<
      typeof agentsHook.useAvailableAgents
    >);
    vi.mocked(hostsHook.useHostModelOptions).mockReturnValue({ data: models } as ReturnType<
      typeof hostsHook.useHostModelOptions
    >);
    return agent;
  }
  async function choose(field: string, option: string) {
    fireEvent.keyDown(screen.getByTestId(`task-${field}-trigger`), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: option }));
  }

  it.each([
    ["codex-native", true, true, true],
    ["pi-native", true, true, false],
    ["devin-native", true, true, true],
    ["cursor-native", false, false, true],
    ["antigravity-native", false, false, true],
    ["opencode-native", false, false, false],
  ])("shows independent settings for %s", (harness, model, effort, permission) => {
    mockHarness(harness);
    renderDialog();
    expect(!!screen.queryByTestId("task-model-trigger")).toBe(model);
    expect(!!screen.queryByTestId("task-effort-trigger")).toBe(effort);
    expect(!!screen.queryByTestId("task-permission-trigger")).toBe(permission);
    expect(hostsHook.useHostModelOptions).toHaveBeenLastCalledWith("host_1", harness, model);
  });

  it("creates Codex settings and resets an effort the next model cannot support", async () => {
    mockHarness("codex-native");
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        initialName="Review"
        initialPrompt="Review changes"
      />,
    );
    await choose("model", "Model A");
    await choose("effort", "High");
    await choose("model", "Model B");
    expect(screen.getByTestId("task-effort-trigger")).toHaveTextContent("Default");
    await choose("effort", "Low");
    await choose("permission", "Read only");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({
      modelOverride: "model-b",
      reasoningEffort: "low",
      permissionMode: "read-only",
    });
  });

  it("preserves saved Codex settings while offline, and can clear them", async () => {
    const agent = mockHarness("codex-native");
    vi.mocked(hostsHook.useHostModelOptions).mockReturnValue({ data: undefined } as ReturnType<
      typeof hostsHook.useHostModelOptions
    >);
    vi.mocked(hostsHook.useHosts).mockReturnValue({
      data: [{ host_id: "host_1", name: "offline host", status: "offline" }],
    } as ReturnType<typeof hostsHook.useHosts>);
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: agent.id,
          modelOverride: "saved-model",
          reasoningEffort: "high",
          permissionMode: "read-only",
          hostId: "host_1",
        })}
      />,
    );
    expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("saved-model");
    expect(screen.getByTestId("task-effort-trigger")).toHaveTextContent("high");
    expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Read only");
    expect(hostsHook.useHostModelOptions).toHaveBeenLastCalledWith("host_1", "codex-native", false);
    await choose("model", "Default");
    await choose("permission", "Default");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
      modelOverride: null,
      reasoningEffort: null,
      permissionMode: null,
    });
  });

  it("clears Codex overrides when switching to a permission-only harness", async () => {
    const codex = mockHarness("codex-native");
    const cursor = testAgent("cursor-agent", "cursor-native-ui", { harness: "cursor-native" });
    vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
      data: [codex, cursor],
    } as ReturnType<typeof agentsHook.useAvailableAgents>);
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: codex.id,
          modelOverride: "model-a",
          reasoningEffort: "high",
          permissionMode: "read-only",
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "pick cursor-native" }));
    expect(screen.queryByTestId("task-model-trigger")).not.toBeInTheDocument();
    expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Default");
    await choose("permission", "Auto-review");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
      agentId: cursor.id,
      permissionMode: "auto-review",
    });
    expect(updateMutateAsync.mock.calls[0][0].input).not.toHaveProperty("modelOverride");
  });

  it.each(["codex-native", "cursor-native", "claude-native", "pi-native"])(
    "uses the sandbox catalog for %s instead of the connected host's",
    async (harness) => {
      const agent = mockHarness(harness);
      vi.mocked(sandboxModelsHook.useSandboxModelOptions).mockReturnValue({
        data: {
          models: [
            {
              ...models[0],
              id: "sandbox-model",
              displayName: "Sandbox model",
              supportedReasoningEfforts: [{ reasoningEffort: "low" }],
            },
          ],
          configured: false,
          status: "ready",
        },
      } as ReturnType<typeof sandboxModelsHook.useSandboxModelOptions>);
      render(sandboxDialog(agent.id, true));
      expect(hostsHook.useHostModelOptions).toHaveBeenLastCalledWith(null, harness, false);
      await choose("model", "Sandbox model");
      expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("Sandbox model");
      fireEvent.keyDown(screen.getByTestId("task-effort-trigger"), { key: "Enter" });
      expect(screen.queryByRole("option", { name: "High" })).not.toBeInTheDocument();
      fireEvent.click(await screen.findByRole("option", { name: "Low" }));
      expect(sandboxModelsHook.useSandboxModelOptions).toHaveBeenLastCalledWith(
        "company",
        harness,
        agent.id,
        null,
        true,
      );
    },
  );

  function sandboxDialog(
    agentId: string,
    editing = false,
    overrides: Partial<ScheduledTasksApiModule.ScheduledTask> = {},
  ) {
    return (
      <CapabilitiesProvider
        info={{
          ...SERVER_INFO_OFFLINE_FALLBACK,
          managed_sandboxes_enabled: true,
          sandbox_provider: "company",
          sandbox_provider_capabilities: { company: { default_models: true } },
          scheduled_task_harness_settings: true,
        }}
      >
        <CreateScheduledTaskDialog
          open
          onOpenChange={vi.fn()}
          initialName="Sandbox review"
          initialPrompt="Review work"
          editingTask={
            editing
              ? scheduledTask({
                  agentId,
                  executionTarget: "managed_sandbox",
                  hostId: null,
                  ...overrides,
                })
              : null
          }
        />
      </CapabilitiesProvider>
    );
  }

  it.each(["claude-native", "pi-native"])(
    "uses provider effort support for %s sandbox models",
    (harness) => {
      const agent = mockHarness(harness);
      vi.mocked(sandboxModelsHook.useSandboxModelOptions).mockReturnValue({
        data: {
          models: [{ id: "provider-model", isDefault: true }],
          configured: false,
          status: "ready",
        },
      } as ReturnType<typeof sandboxModelsHook.useSandboxModelOptions>);
      render(sandboxDialog(agent.id, true));
      expect(screen.getByTestId("task-model-trigger")).toBeVisible();
      expect(screen.queryByTestId("task-effort-trigger")).not.toBeInTheDocument();
    },
  );

  it.each(["host", "sandbox"])(
    "clears target-specific overrides when moving to another %s",
    async (target) => {
      const agent = mockHarness("codex-native");
      vi.mocked(hostsHook.useHosts).mockReturnValue({
        data: [
          { host_id: "host_1", name: "laptop", status: "online" },
          { host_id: "host_2", name: "desktop", status: "online" },
        ],
      } as ReturnType<typeof hostsHook.useHosts>);
      render(
        <CapabilitiesProvider
          info={{ ...SERVER_INFO_OFFLINE_FALLBACK, managed_sandboxes_enabled: true }}
        >
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            editingTask={scheduledTask({
              agentId: agent.id,
              hostId: "host_1",
              modelOverride: "saved-model",
              reasoningEffort: "high",
            })}
          />
        </CapabilitiesProvider>,
      );
      const changeTarget = async () => {
        if (target === "host") await choose("host", "desktop");
        else {
          fireEvent.keyDown(screen.getByTestId("task-host-trigger"), { key: "Enter" });
          fireEvent.click(await screen.findByTestId("task-host-sandbox-option"));
        }
      };
      await changeTarget();
      fireEvent.click(screen.getByRole("button", { name: "pick codex-native" }));
      expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("Default");
      fireEvent.keyDown(screen.getByTestId("task-model-trigger"), { key: "Enter" });
      expect(screen.queryByRole("option", { name: "saved-model" })).not.toBeInTheDocument();
      fireEvent.click(await screen.findByRole("option", { name: "Default" }));
      await choose("host", "laptop");
      expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("saved-model");
      await changeTarget();
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
        modelOverride: null,
        reasoningEffort: null,
      });
    },
  );

  it("clears sandbox model settings when the connected-host harness has no model controls", async () => {
    const agent = mockHarness("cursor-native");
    vi.mocked(sandboxModelsHook.useSandboxModelOptions).mockReturnValue({
      data: { models, configured: false, status: "ready" },
    } as ReturnType<typeof sandboxModelsHook.useSandboxModelOptions>);
    render(sandboxDialog(agent.id, true, { modelOverride: "model-a", reasoningEffort: "high" }));
    expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("Model A");
    expect(screen.getByTestId("task-effort-trigger")).toHaveTextContent("High");
    await choose("host", "laptop");
    expect(screen.queryByTestId("task-model-trigger")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
      modelOverride: null,
      reasoningEffort: null,
    });
  });

  it.each([
    ["codex-native", null],
    ["claude-native", "default"],
  ])("normalizes a saved default permission for %s", async (harness, expected) => {
    const agent = mockHarness(harness!);
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ agentId: agent.id, permissionMode: "default" })}
      />,
    );
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input.permissionMode).toBe(expected);
  });

  it.each([false, true])(
    "blocks an explicitly unsupported sandbox in edit=%s, but recovers after a failed preview",
    async (editing) => {
      const agent = mockHarness("codex-native");
      const data = {
        models: [],
        configured: false,
        status: "unsupported" as const,
        error: "This sandbox cannot run Codex.",
      };
      vi.mocked(sandboxModelsHook.useSandboxModelOptions).mockReturnValue({
        data,
        isError: false,
      } as unknown as ReturnType<typeof sandboxModelsHook.useSandboxModelOptions>);
      const { rerender } = render(sandboxDialog(agent.id, editing));
      if (!editing) {
        fireEvent.keyDown(screen.getByTestId("task-host-trigger"), { key: "Enter" });
        fireEvent.click(await screen.findByTestId("task-host-sandbox-option"));
      }
      expect(screen.getByTestId("task-harness-unsupported")).toHaveTextContent(data.error);
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      expect(mutateAsync).not.toHaveBeenCalled();
      expect(updateMutateAsync).not.toHaveBeenCalled();
      // React Query retains data after a refetch error; it must no longer block.
      vi.mocked(sandboxModelsHook.useSandboxModelOptions).mockReturnValue({
        data,
        isError: true,
        error: new Error("Network unavailable"),
      } as unknown as ReturnType<typeof sandboxModelsHook.useSandboxModelOptions>);
      rerender(sandboxDialog(agent.id, editing));
      expect(screen.queryByTestId("task-harness-unsupported")).not.toBeInTheDocument();
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
    },
  );

  it("restores host models and efforts across reopening while offline, and respects a fresh empty catalog", async () => {
    vi.spyOn(identity, "getCurrentUserId").mockReturnValue("alice");
    mockHarness("codex-native");
    const first = renderDialog();
    expect(screen.getByTestId("task-effort-trigger")).toBeVisible();
    first.unmount();
    vi.mocked(hostsHook.useHostModelOptions).mockReturnValue({ data: undefined } as ReturnType<
      typeof hostsHook.useHostModelOptions
    >);
    vi.mocked(hostsHook.useHosts).mockReturnValue({
      data: [{ host_id: "host_1", name: "Offline host", status: "offline" }],
    } as ReturnType<typeof hostsHook.useHosts>);
    const { rerender } = renderDialog();
    await choose("host", "Offline host (offline)");
    expect(screen.getByTestId("task-effort-trigger")).toBeVisible();
    await choose("model", "Model A");
    expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("Model A");
    vi.mocked(hostsHook.useHostModelOptions).mockReturnValue({ data: [] } as unknown as ReturnType<
      typeof hostsHook.useHostModelOptions
    >);
    rerender(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} />);
    expect(screen.queryByTestId("task-effort-trigger")).not.toBeInTheDocument();
    // Clear the explicit selection; cached choices must not resurrect the old list.
    await choose("model", "Default");
    vi.mocked(hostsHook.useHostModelOptions).mockReturnValue({ data: undefined } as ReturnType<
      typeof hostsHook.useHostModelOptions
    >);
    rerender(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} />);
    fireEvent.keyDown(screen.getByTestId("task-model-trigger"), { key: "Enter" });
    expect(screen.queryByRole("option", { name: "Model A" })).not.toBeInTheDocument();
  });

  it("does not renew expired host choices just by reopening the dialog", async () => {
    vi.spyOn(identity, "getCurrentUserId").mockReturnValue("alice");
    mockHarness("codex-native");
    const fetchedAt = Date.now() - 23 * 60 * 60 * 1000;
    vi.mocked(hostsHook.useHostModelOptions).mockReturnValue({
      data: models,
      dataUpdatedAt: fetchedAt,
    } as ReturnType<typeof hostsHook.useHostModelOptions>);
    const first = renderDialog();
    expect(screen.getByTestId("task-effort-trigger")).toBeVisible();
    first.unmount();
    vi.spyOn(Date, "now").mockReturnValue(fetchedAt + 25 * 60 * 60 * 1000);
    vi.mocked(hostsHook.useHostModelOptions).mockReturnValue({ data: undefined } as ReturnType<
      typeof hostsHook.useHostModelOptions
    >);
    renderDialog();
    fireEvent.keyDown(screen.getByTestId("task-model-trigger"), { key: "Enter" });
    expect(await screen.findByRole("option", { name: "Default" })).toBeVisible();
    expect(screen.queryByRole("option", { name: "Model A" })).not.toBeInTheDocument();
  });

  it("restores a provider catalog during an outage but discards it when the provider reports unsupported", async () => {
    vi.spyOn(identity, "getCurrentUserId").mockReturnValue("alice");
    const agent = mockHarness("codex-native");
    vi.mocked(sandboxModelsHook.useSandboxModelOptions).mockReturnValue({
      data: { models, configured: false, status: "ready" },
    } as ReturnType<typeof sandboxModelsHook.useSandboxModelOptions>);
    const first = render(sandboxDialog(agent.id, true));
    first.unmount();
    vi.mocked(sandboxModelsHook.useSandboxModelOptions).mockReturnValue({
      data: { models: [], configured: false, status: "unavailable" },
    } as unknown as ReturnType<typeof sandboxModelsHook.useSandboxModelOptions>);
    const { rerender } = render(sandboxDialog(agent.id, true));
    await choose("model", "Model A");
    expect(screen.getByTestId("task-effort-trigger")).toBeVisible();
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
    vi.mocked(sandboxModelsHook.useSandboxModelOptions).mockReturnValue({
      data: { models: [], configured: false, status: "unsupported" },
      isError: false,
    } as unknown as ReturnType<typeof sandboxModelsHook.useSandboxModelOptions>);
    rerender(sandboxDialog(agent.id, true));
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    expect(screen.queryByTestId("task-effort-trigger")).not.toBeInTheDocument();
    vi.mocked(sandboxModelsHook.useSandboxModelOptions).mockReturnValue({
      data: { models, configured: false, status: "ready" },
      isError: false,
    } as ReturnType<typeof sandboxModelsHook.useSandboxModelOptions>);
    rerender(sandboxDialog(agent.id, true));
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
    expect(screen.getByTestId("task-effort-trigger")).toBeVisible();
  });

  it.each([
    ["codex-native", false],
    ["claude-native", true],
  ] as const)(
    "preserves older-server permission support for %s",
    (harness, supportsPermissions) => {
      mockHarness(harness);
      render(
        <CapabilitiesProvider info={SERVER_INFO_OFFLINE_FALLBACK}>
          <CreateScheduledTaskDialog open onOpenChange={vi.fn()} />
        </CapabilitiesProvider>,
      );
      expect(screen.getByTestId("task-model-trigger")).toBeVisible();
      if (supportsPermissions) {
        expect(screen.getByTestId("task-permission-trigger")).toBeVisible();
      } else {
        expect(screen.queryByTestId("task-permission-trigger")).not.toBeInTheDocument();
      }
    },
  );
});

describe("automation random worktree", () => {
  it.each(["host", "host-roundtrip", "directory-roundtrip", "sandbox-roundtrip"])(
    "preserves worktree choices across target and directory transitions (%s)",
    async (transition) => {
      const directoryChanged =
        transition === "host-roundtrip" || transition === "directory-roundtrip";
      vi.mocked(hostsHook.useHosts).mockReturnValue({
        data: [
          { host_id: "host_1", name: "laptop", status: "online" },
          { host_id: "host_2", name: "desktop", status: "online" },
        ],
      } as ReturnType<typeof hostsHook.useHosts>);
      render(
        <CapabilitiesProvider
          info={{
            ...SERVER_INFO_OFFLINE_FALLBACK,
            managed_sandboxes_enabled: true,
            scheduled_task_worktrees: true,
          }}
        >
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            editingTask={scheduledTask({
              hostId: "host_1",
              workspace: "/home/me/repo",
              baseBranch: "HEAD",
            })}
          />
        </CapabilitiesProvider>,
      );
      if (directoryChanged) {
        gitWorkspace.path = "/ordinary-folder";
        fireEvent.click(screen.getByTestId("task-workspace-browse"));
        fireEvent.click(screen.getByRole("button", { name: "pick-workspace" }));
      }
      gitWorkspace.available = false;
      if (transition !== "directory-roundtrip") {
        fireEvent.keyDown(screen.getByTestId("task-host-trigger"), { key: "Enter" });
        fireEvent.click(
          screen.getByRole("option", {
            name: transition === "sandbox-roundtrip" ? /Sandbox/ : "desktop",
          }),
        );
      } else {
        // Reselect the ordinary folder to render the negative discovery result.
        fireEvent.click(screen.getByTestId("task-workspace-browse"));
        fireEvent.click(screen.getByRole("button", { name: "pick-workspace" }));
      }
      expect(screen.queryByRole("checkbox", { name: "Random worktree" })).not.toBeInTheDocument();
      if (transition === "host-roundtrip" || transition === "sandbox-roundtrip") {
        if (transition === "sandbox-roundtrip") gitWorkspace.available = true;
        fireEvent.keyDown(screen.getByTestId("task-host-trigger"), { key: "Enter" });
        fireEvent.click(screen.getByRole("option", { name: "laptop" }));
        if (transition === "sandbox-roundtrip") {
          expect(screen.getByRole("checkbox", { name: "Random worktree" })).toBeChecked();
        } else {
          expect(
            screen.queryByRole("checkbox", { name: "Random worktree" }),
          ).not.toBeInTheDocument();
        }
      }
      if (transition === "directory-roundtrip") {
        gitWorkspace.available = true;
        gitWorkspace.path = "/home/me/repo";
        fireEvent.click(screen.getByTestId("task-workspace-browse"));
        fireEvent.click(screen.getByRole("button", { name: "pick-workspace" }));
        expect(screen.getByRole("checkbox", { name: "Random worktree" })).not.toBeChecked();
      }
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalled());
      expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
        hostId: transition === "host" ? "host_2" : "host_1",
        workspace: transition === "host-roundtrip" ? "/ordinary-folder" : "/home/me/repo",
      });
      const input = updateMutateAsync.mock.calls[0][0].input;
      if (transition === "sandbox-roundtrip") expect(input).not.toHaveProperty("baseBranch");
      else expect(input.baseBranch).toBeNull();
    },
  );

  it.each([null, "HEAD"])(
    "omits worktree controls and changes on unsupported servers (saved %s)",
    async (baseBranch) => {
      render(
        <CapabilitiesProvider
          info={{ ...SERVER_INFO_OFFLINE_FALLBACK, scheduled_task_worktrees: false }}
        >
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            editingTask={scheduledTask({
              hostId: "host_1",
              workspace: "/home/me/repo",
              baseBranch,
            })}
          />
        </CapabilitiesProvider>,
      );
      expect(screen.queryByRole("checkbox", { name: "Random worktree" })).not.toBeInTheDocument();
      expect(gitWorkspace.query).toHaveBeenLastCalledWith(null, "/home/me/repo");
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalled());
      expect(updateMutateAsync.mock.calls[0][0].input).not.toHaveProperty("baseBranch");
    },
  );

  it.each([
    ["loading", "Checking whether this directory is a Git repository."],
    ["error", "Repository verification is unavailable."],
    ["offline", "Connect your host to verify this directory."],
    ["non-git", "The selected directory is not a Git repository."],
  ])("can turn off a saved worktree when Git discovery reports %s", async (state, message) => {
    gitWorkspace.available = state === "non-git" ? false : undefined;
    gitWorkspace.checking = state === "loading";
    if (state === "offline") {
      vi.mocked(hostsHook.useHosts).mockReturnValue({
        data: [{ host_id: "host_1", name: "laptop", status: "offline" }],
      } as ReturnType<typeof hostsHook.useHosts>);
    }
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          hostId: "host_1",
          workspace: "/home/me/repo",
          baseBranch: "HEAD",
        })}
      />,
    );
    expect(screen.getByText(`Random worktree is enabled. ${message}`)).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Random worktree" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Turn off random worktree" }));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalled());
    expect(updateMutateAsync.mock.calls[0][0].input.baseBranch).toBeNull();
  });

  it("defaults off and enables a fresh worktree on create", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Review" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Review code" } });
    fireEvent.keyDown(screen.getByTestId("task-host-trigger"), { key: "Enter" });
    fireEvent.click(screen.getByRole("option", { name: /laptop/ }));
    fireEvent.click(screen.getByTestId("task-workspace-browse"));
    fireEvent.click(screen.getByRole("button", { name: "pick-workspace" }));
    const checkbox = screen.getByRole("checkbox", { name: "Random worktree" });
    expect(checkbox).not.toBeChecked();
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalled());
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({
      baseBranch: "HEAD",
      workspace: "/home/me/repo",
      hostId: "host_1",
    });
  });

  it.each([null, "HEAD"])("only shows the option for a Git repository (saved %s)", (baseBranch) => {
    gitWorkspace.available = false;
    const dialog = () => (
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          hostId: "host_1",
          workspace: "/home/me/repo",
          baseBranch,
        })}
      />
    );
    const { rerender } = render(dialog());
    expect(screen.queryByRole("checkbox", { name: "Random worktree" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Creates a fresh randomly named worktree/)).not.toBeInTheDocument();
    gitWorkspace.available = true;
    rerender(dialog());
    const checkbox = screen.getByRole("checkbox", { name: "Random worktree" });
    expect(checkbox).toBeEnabled();
    expect(checkbox).toHaveAttribute("aria-checked", baseBranch === null ? "false" : "true");
  });

  it.each([
    [null, false],
    ["HEAD", false],
    [null, true],
    ["HEAD", true],
  ] as const)("saves toggling the saved option from %s (bound %s)", async (baseBranch, bound) => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: "ag_claude_native",
          hostId: "host_1",
          workspace: "/home/me/repo",
          baseBranch,
          sessionMode: bound ? "continue" : "new",
          sessionId: bound ? "conv_1" : null,
        })}
      />,
    );
    const checkbox = screen.getByRole("checkbox", { name: "Random worktree" });
    expect(checkbox).toHaveAttribute("aria-checked", baseBranch === null ? "false" : "true");
    if (bound) expect(screen.getByTestId("task-model-trigger")).toBeDisabled();
    fireEvent.click(checkbox);
    if (bound) expect(screen.getByTestId("task-model-trigger")).toBeEnabled();
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalled());
    // Changing baseBranch requests the server-side binding reset.
    expect(updateMutateAsync.mock.calls[0][0].input.baseBranch).toBe(
      baseBranch === null ? "HEAD" : null,
    );
  });
});
