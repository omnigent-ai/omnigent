import { testAgent } from "@/test/agentFixtures";
// Tests for the manual create dialog: submit stays disabled until the required
// fields are filled, the workspace-without-host pairing rule surfaces inline,
// and a valid submit calls the create mutation with the RRULE built from the
// schedule fields (host/workspace omitted when unset).
//
// The agent/host hooks and the create mutation are mocked; WorkspacePicker is
// stubbed (its filesystem browsing is out of scope here).

import type * as ScheduledTasksApiModule from "@/lib/scheduledTasksApi";

import React from "react";
import userEvent from "@testing-library/user-event";
import {
  act,
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
import type { ServerInfo } from "@/lib/capabilities";
import type { AutomationSourceSession } from "@/lib/automationFromSession";
import { MemoryRouter } from "react-router-dom";
import {
  decodeTriggerPrompt,
  emptyTriggerFields,
  encodeTriggerPrompt,
  hasTriggerHeader,
  isTaskSessionTriggerPrompt,
  isTriggerPrompt,
  readTriggerWorkerPlacement,
  readTriggerWorkerSettings,
  renderTriggerPrompt,
  splitTriggerPrompt,
  stripTriggerHeader,
  triggerPromptSource,
  updateTriggerWorkerSettings,
  type TriggerSource,
} from "./triggerTemplates";

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
// The "Starts from" field reads the source session's snapshot for its title
// and launch settings.
const sourceSnapshot = vi.hoisted(() => ({ current: null as unknown }));
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: sourceSnapshot.current, isLoading: false, error: null }),
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
    curatedEntries,
    sandboxSelected,
    hasAgents,
    loading,
    selectedConfigContent,
    triggerDetails,
    triggerIcon,
    open,
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
    curatedEntries?: AvailableAgent[];
    sandboxSelected: boolean;
    hasAgents: boolean;
    loading?: boolean;
    selectedConfigContent?: React.ReactNode;
    triggerDetails?: readonly { label: string; value: string }[];
    triggerIcon?: React.ReactNode;
    open?: boolean;
  }) => {
    const config = React.isValidElement<{
      models?: {
        choices: { label: string; checked?: boolean; testId?: string; onSelect?: () => void }[];
      };
      efforts?: {
        choices: { label: string; checked?: boolean; testId?: string; onSelect?: () => void }[];
      };
    }>(selectedConfigContent)
      ? selectedConfigContent.props
      : null;
    const [openConfig, setOpenConfig] = React.useState<"model" | "effort" | null>(null);
    const modelDetail = triggerDetails?.find((detail) => detail.label === "Model");
    const effortDetail = triggerDetails?.find((detail) => detail.label === "Effort");
    const openChoices = openConfig === "model" ? config?.models?.choices : config?.efforts?.choices;
    React.useEffect(() => {
      if (openConfig === null) return;
      const closeOnEscape = (event: KeyboardEvent) => {
        if (event.key === "Escape") setOpenConfig(null);
      };
      window.addEventListener("keydown", closeOnEscape);
      return () => window.removeEventListener("keydown", closeOnEscape);
    }, [openConfig]);
    return (
      <fieldset
        disabled={loading}
        data-testid="agent-picker-stub"
        data-effective={effectiveAgentId ?? ""}
        // Surface the host the dialog feeds the picker for badge computation, so
        // a test can assert it's populated even when no host is pinned.
        data-badge-host={host?.host_id ?? ""}
        data-dropdown-modal={dropdownModal === false ? "false" : "true"}
        data-content-class={contentClassName}
        data-harness-entries={harnessEntries.map((agent) => agent.name).join(",")}
        data-agent-entries={agentEntries.map((agent) => agent.name).join(",")}
        data-curated-entries={curatedEntries?.map((agent) => agent.name).join(",")}
        data-sandbox-selected={String(sandboxSelected)}
        data-trigger-details={triggerDetails
          ?.map((detail) => `${detail.label}:${detail.value}`)
          .join("|")}
        data-has-trigger-icon={String(triggerIcon != null)}
        data-has-config={String(selectedConfigContent != null)}
        data-picker-open={String(open)}
      >
        <span>{hasAgents ? agentLabel : "No agents"}</span>
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
        {modelDetail && (
          <div data-testid="task-model-effort-field">
            <button
              type="button"
              data-testid="task-model-trigger"
              disabled={!config?.models}
              onKeyDown={(event) => {
                if (event.key === "Enter") setOpenConfig("model");
              }}
            >
              {modelDetail.value}
            </button>
            {effortDetail && (
              <button
                type="button"
                data-testid="task-effort-trigger"
                disabled={!config?.efforts}
                onKeyDown={(event) => {
                  if (event.key === "Enter") setOpenConfig("effort");
                }}
              >
                {effortDetail.value}
              </button>
            )}
            {openChoices && (
              <div data-testid={`task-${openConfig}-options`}>
                {openChoices.map((choice) => (
                  <button
                    key={choice.label}
                    type="button"
                    role="option"
                    aria-selected={choice.checked}
                    onClick={() => {
                      choice.onSelect?.();
                      setOpenConfig(null);
                    }}
                  >
                    {choice.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        <div hidden>
          {[...(config?.models?.choices ?? []), ...(config?.efforts?.choices ?? [])].map(
            (choice) =>
              choice.testId && (
                <button
                  key={choice.testId}
                  type="button"
                  data-testid={`${choice.testId}-mock`}
                  onClick={choice.onSelect}
                >
                  {choice.testId}
                </button>
              ),
          )}
        </div>
      </fieldset>
    );
  },
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

type AutomationSectionName = "Run setup" | "Schedule" | "Context" | "Controls";

function openAutomationSection(name: AutomationSectionName) {
  const advancedTrigger = screen.queryByTestId("task-advanced-settings-trigger");
  if (advancedTrigger?.getAttribute("aria-expanded") === "false") {
    fireEvent.click(advancedTrigger);
  }
  if (name === "Schedule") {
    const trigger = screen.getByTestId("task-schedule-trigger");
    if (trigger.getAttribute("aria-expanded") === "false") fireEvent.click(trigger);
  }
}

/** Pick how a new trigger's matches map to sessions in Advanced settings. */
function chooseTriggerSessionMode(label: "New session for each match" | "One task session") {
  fireEvent.keyDown(screen.getByTestId("task-trigger-session-mode"), { key: "Enter" });
  fireEvent.click(screen.getByRole("menuitemradio", { name: new RegExp(`^${label}`) }));
}

function closeTopLevelPopovers() {
  for (const testId of ["task-schedule-trigger", "task-ending-trigger"]) {
    const trigger = screen.queryByTestId(testId);
    if (trigger?.getAttribute("aria-expanded") === "true") fireEvent.click(trigger);
  }
}

function selectSchedulePreset(label: string) {
  openAutomationSection("Schedule");
  fireEvent.click(screen.getByRole("radio", { name: label }));
}

function selectScheduleEnding(label: string) {
  openTopLevelPopover("task-ending-trigger");
  fireEvent.click(screen.getByRole("menuitemradio", { name: label }));
}

function openTopLevelPopover(testId: string) {
  const trigger = screen.getByTestId(testId);
  if (trigger.getAttribute("aria-expanded") !== "false") return;
  if (testId === "task-ending-trigger") {
    fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
  } else {
    fireEvent.click(trigger);
  }
}

function render(
  ui: Parameters<typeof renderBase>[0],
  {
    expandAdvanced = true,
    collapseAdvanced = false,
    info = {},
  }: {
    expandAdvanced?: boolean;
    collapseAdvanced?: boolean;
    info?: Partial<typeof SERVER_INFO_OFFLINE_FALLBACK>;
  } = {},
) {
  const view = renderBase(ui, {
    wrapper: ({ children }) => (
      <CapabilitiesProvider
        info={{
          ...SERVER_INFO_OFFLINE_FALLBACK,
          features: { automations_v2: true },
          scheduled_task_harness_settings: true,
          scheduled_task_approval_skipping_modes: true,
          scheduled_task_worktrees: true,
          scheduled_task_trigger_headers: true,
          ...info,
        }}
      >
        {children}
      </CapabilitiesProvider>
    ),
  });
  function expand() {
    if (collapseAdvanced) return;
    const advancedTrigger = screen.queryByTestId("task-advanced-settings-trigger");
    if (advancedTrigger?.getAttribute("aria-expanded") === "false") {
      fireEvent.click(advancedTrigger);
    }
    if (!expandAdvanced) return;
    for (const testId of ["task-ending-trigger", "task-schedule-trigger"]) {
      const trigger = screen.queryByTestId(testId);
      if (trigger?.getAttribute("aria-expanded") === "false") fireEvent.click(trigger);
    }
  }
  expand();
  return {
    ...view,
    rerender: (next: Parameters<typeof view.rerender>[0]) => {
      view.rerender(next);
      expand();
    },
  };
}

/** A server with harness settings that predates modes that skip approval. */
const REFUSES_APPROVAL_SKIPPING = {
  ...SERVER_INFO_OFFLINE_FALLBACK,
  features: { automations_v2: true },
  scheduled_task_harness_settings: true,
  scheduled_task_worktrees: true,
};

function serverInfo(acceptsApprovalSkipping: boolean) {
  return acceptsApprovalSkipping
    ? { ...REFUSES_APPROVAL_SKIPPING, scheduled_task_approval_skipping_modes: true }
    : REFUSES_APPROVAL_SKIPPING;
}

function renderDialog(onOpenChange: (open: boolean) => void = vi.fn()) {
  return render(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} />);
}

/** Render inside a CapabilitiesProvider that advertises managed sandboxes. */
function renderWithSandboxes(
  onOpenChange: (open: boolean) => void = vi.fn(),
  info: Partial<ServerInfo> = {},
) {
  return render(
    <CapabilitiesProvider
      info={{ ...SERVER_INFO_OFFLINE_FALLBACK, managed_sandboxes_enabled: true, ...info }}
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

describe("onCreated", () => {
  function fillRequiredFields() {
    fireEvent.change(screen.getByLabelText("Automation title"), {
      target: { value: "Morning review" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Instructions" }), {
      target: { value: "Review pull requests" },
    });
  }

  it("is called once, only after the create completes", async () => {
    const onCreated = vi.fn();
    const onOpenChange = vi.fn();
    let finishCreate: (value: unknown) => void = () => {};
    mutateAsync.mockReturnValueOnce(
      new Promise((resolve) => {
        finishCreate = resolve;
      }),
    );
    render(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} onCreated={onCreated} />, {
      expandAdvanced: false,
    });
    fillRequiredFields();
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(onCreated).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);

    finishCreate({ id: "st_new" });
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("is not called when the create is rejected", async () => {
    const onCreated = vi.fn();
    mutateAsync.mockRejectedValueOnce(new Error("create failed"));
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} onCreated={onCreated} />, {
      expandAdvanced: false,
    });
    fillRequiredFields();
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    expect(await screen.findByText("create failed")).toBeVisible();
    expect(onCreated).not.toHaveBeenCalled();
  });

  it("is not called after an edit", async () => {
    const onCreated = vi.fn();
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        onCreated={onCreated}
        editingTask={scheduledTask()}
      />,
    );
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(onCreated).not.toHaveBeenCalled();
  });
});

describe("prompt-first automation setup", () => {
  it("keeps details visible and required while setup controls stay compact", async () => {
    render(
      <CapabilitiesProvider
        info={{
          ...SERVER_INFO_OFFLINE_FALLBACK,
          features: { automations_v2: true },
          scheduled_task_harness_settings: true,
          scheduled_task_approval_skipping_modes: true,
          scheduled_task_worktrees: true,
          scheduled_task_projects: true,
          scheduled_task_session_modes: true,
        }}
      >
        <CreateScheduledTaskDialog open onOpenChange={vi.fn()} />
      </CapabilitiesProvider>,
      { collapseAdvanced: true },
    );
    const instructions = screen.getByRole("textbox", { name: "Instructions" });
    const title = screen.getByLabelText("Automation title");
    expect(title).toHaveFocus();
    expect(screen.getByTestId("create-scheduled-task-dialog")).toHaveClass("sm:max-w-[720px]");
    expect(screen.getByTestId("task-host-trigger")).toHaveAccessibleName("Run on");
    expect(screen.getByTestId("task-agent-picker")).toBeVisible();
    const advancedTrigger = screen.getByTestId("task-advanced-settings-trigger");
    expect(advancedTrigger).toHaveAccessibleName("Advanced settings (optional)");
    expect(advancedTrigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("task-session-mode-trigger")).not.toBeInTheDocument();
    fireEvent.click(advancedTrigger);
    expect(advancedTrigger).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByTestId("task-permission-trigger").compareDocumentPosition(advancedTrigger),
    ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.getByTestId("task-session-mode-trigger")).toHaveAccessibleName(
      "Session continuity",
    );
    expect(screen.getByTestId("task-schedule-trigger")).toHaveAccessibleName("Schedule");
    expect(screen.getByTestId("task-ending-trigger")).toHaveAccessibleName("Ends");
    expect(screen.getByTestId("task-workspace-trigger")).toHaveAccessibleName("Workspace");
    expect(screen.getByTestId("task-project-trigger")).toHaveAccessibleName("Project");
    expect(screen.getByTestId("task-permission-trigger")).toHaveAccessibleName("Permissions");
    fireEvent.click(advancedTrigger);
    expect(screen.queryByTestId("task-session-mode-trigger")).not.toBeInTheDocument();
    fireEvent.click(advancedTrigger);
    expect(screen.queryByRole("button", { name: "Run setup" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Context" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Controls" })).not.toBeInTheDocument();
    expect(title).toBeVisible();
    expect(title).toHaveAttribute("placeholder", "Name your automation");
    expect(title).toHaveClass("font-heading", "text-[1.25em]");
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Frequency")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Schedule ends")).not.toBeInTheDocument();

    const scheduleTrigger = screen.getByTestId("task-schedule-trigger");
    fireEvent.click(scheduleTrigger);
    expect(screen.getByLabelText("Frequency")).toBeVisible();
    expect(screen.getByLabelText("Time", { exact: true })).toBeVisible();
    expect(screen.getByLabelText("Timezone", { exact: true })).toBeVisible();
    await userEvent.click(instructions);
    await waitFor(() => expect(scheduleTrigger).toHaveAttribute("aria-expanded", "false"));
    openTopLevelPopover("task-ending-trigger");
    expect(screen.getByLabelText("Schedule ends")).toBeVisible();
    fireEvent.pointerDown(screen.getByTestId("task-ending-trigger"), {
      button: 0,
      ctrlKey: false,
    });

    fireEvent.change(instructions, { target: { value: "Review pull requests" } });
    expect(screen.queryByTestId("task-execution-summary")).not.toBeInTheDocument();
    expect(title).toHaveValue("");
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Automation title"), {
      target: { value: "Morning review" },
    });
    fireEvent.change(instructions, {
      target: { value: "  Review pull requests\n and report CI failures.  " },
    });
    expect(title).toHaveValue("Morning review");
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({
      name: "Morning review",
      prompt: "Review pull requests\n and report CI failures.",
      hostId: "host_1",
    });
  });

  it("shows DS tooltips for the modal controls", async () => {
    vi.useFakeTimers();
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} />, { expandAdvanced: false });
    async function expectTooltip(testId: string, label: string) {
      const trigger = screen.getByTestId(testId);
      fireEvent.pointerMove(trigger.parentElement!, { pointerType: "mouse" });
      await act(() => vi.advanceTimersByTimeAsync(1000));
      expect(screen.getByRole("tooltip")).toHaveTextContent(label);
      fireEvent.keyDown(trigger, { key: "Escape" });
      expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    }
    await expectTooltip("task-trigger-type", "Trigger type");
    await expectTooltip("task-schedule-trigger", "Schedule");
    await expectTooltip("task-host-trigger", "Run on");
    await expectTooltip("task-permission-trigger", "Permissions");
    expect(screen.getByTestId("task-advanced-settings-trigger")).not.toHaveAttribute(
      "data-slot",
      "tooltip-trigger",
    );
  });

  it.each([
    [false, null],
    [true, "bypassPermissions"],
  ])(
    "on an instructions-only edit, keeps a saved approval-skipping mode only if the server accepts it (%s)",
    async (accepted, expected) => {
      render(
        <CapabilitiesProvider info={serverInfo(accepted)}>
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            editingTask={scheduledTask({
              agentId: "ag_claude_native",
              permissionMode: "bypassPermissions",
            })}
          />
        </CapabilitiesProvider>,
      );
      fireEvent.change(screen.getByLabelText("Instructions"), {
        target: { value: "Also summarize CI failures" },
      });
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
        prompt: "Also summarize CI failures",
        permissionMode: expected,
      });
    },
  );

  it("keeps saved names and overrides when editing instructions without opening the agent menu", async () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: "ag_claude_native",
          modelOverride: "opus",
          reasoningEffort: "high",
        })}
      />,
      { expandAdvanced: false },
    );
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute(
      "data-trigger-details",
      "Model:Opus|Effort:High",
    );
    fireEvent.change(screen.getByLabelText("Instructions"), {
      target: { value: "Also summarize CI failures" },
    });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
      name: "Morning brief",
      prompt: "Also summarize CI failures",
      modelOverride: "opus",
      reasoningEffort: "high",
    });
  });

  it.each(["COUNT=4", "UNTIL=20991231T235959Z"])(
    "keeps Schedule open while editing time with %s",
    (ending) => {
      render(
        <CreateScheduledTaskDialog
          open
          onOpenChange={vi.fn()}
          editingTask={scheduledTask({ rrule: `FREQ=DAILY;BYHOUR=9;BYMINUTE=0;${ending}` })}
        />,
        { expandAdvanced: false },
      );
      const scheduleSection = screen.getByRole("button", { name: "Schedule" });
      expect(scheduleSection).toHaveAttribute("aria-expanded", "false");
      fireEvent.click(scheduleSection);
      fireEvent.change(screen.getByLabelText("Time"), { target: { value: "9:" } });
      expect(screen.getByTestId("schedule-error")).toHaveTextContent("Enter a valid time");
      expect(scheduleSection).toHaveAttribute("aria-expanded", "true");
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      fireEvent.change(screen.getByLabelText("Time"), { target: { value: "9:00" } });
      expect(scheduleSection).toHaveAttribute("aria-expanded", "true");
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
      fireEvent.change(screen.getByLabelText("Time"), { target: { value: "9:30" } });
      expect(scheduleSection).toHaveAttribute("aria-expanded", "true");
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
    },
  );

  it("reveals an expired ending in Ends when rescheduling", () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;UNTIL=20200101T235959Z",
        })}
      />,
      { expandAdvanced: false },
    );
    const scheduleSection = screen.getByRole("button", { name: "Schedule" });
    const endingControl = screen.getByRole("button", { name: "Ends" });
    expect(scheduleSection).toHaveAttribute("aria-expanded", "false");
    expect(endingControl).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(scheduleSection);
    fireEvent.change(screen.getByLabelText("Time"), { target: { value: "9:" } });
    expect(screen.getByTestId("schedule-error")).toHaveTextContent("Enter a valid time");
    expect(scheduleSection).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Time"), { target: { value: "9:00" } });
    expect(scheduleSection).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Time"), { target: { value: "10:00" } });
    expect(scheduleSection).toHaveAttribute("aria-expanded", "false");
    expect(endingControl).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText("End date")).toBeVisible();
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    fireEvent.pointerDown(endingControl, { button: 0, ctrlKey: false });
    expect(screen.queryByLabelText("End date")).not.toBeInTheDocument();
    expect(scheduleSection).toHaveTextContent("Needs attention");
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    fireEvent.pointerDown(endingControl, { button: 0, ctrlKey: false });
    expect(screen.getByLabelText("End date")).toBeVisible();
  });

  it("offers scrolling hour and minute choices while keeping direct time entry", async () => {
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} />, { expandAdvanced: false });
    openAutomationSection("Schedule");
    const timeField = screen.getByLabelText("Time", { exact: true });
    expect(screen.getByLabelText("Frequency")).toBeVisible();
    expect(timeField).toBeVisible();
    expect(timeField).toHaveValue("09:00 AM");
    expect(screen.getByLabelText("Timezone", { exact: true })).toBeVisible();
    expect(timeField.tagName).toBe("INPUT");
    expect(timeField).toHaveAttribute("placeholder", "5:00 PM");
    expect(timeField).toHaveClass("text-ui");
    expect(screen.queryByTestId("schedule-time-picker-trigger")).not.toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Time", { exact: true }));
    const timePicker = screen.getByTestId("schedule-time-picker");
    const hours = within(timePicker).getByRole("listbox", { name: "Hours" });
    const minutes = within(timePicker).getByRole("listbox", { name: "Minutes" });
    expect(within(hours).getByRole("option", { name: "09" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(within(minutes).getByRole("option", { name: "00" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await userEvent.click(within(hours).getByRole("option", { name: "17" }));
    await userEvent.click(within(minutes).getByRole("option", { name: "37" }));
    expect(timeField).toHaveValue("05:37 PM");
    expect(screen.queryByLabelText("Search timezones")).not.toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Timezone", { exact: true }));
    fireEvent.change(screen.getByLabelText("Search timezones"), { target: { value: "Singapore" } });
    await userEvent.click(await screen.findByRole("menuitemradio", { name: "Asia/Singapore" }));
    expect(screen.getByLabelText("Time", { exact: true })).toHaveValue("05:37 PM");
    expect(screen.getByLabelText("Timezone", { exact: true })).toHaveTextContent("Asia/Singapore");
    expect(screen.queryByTestId("schedule-time-picker")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Done" })).not.toBeInTheDocument();
    expect(screen.getByTestId("create-scheduled-task-dialog")).toBeVisible();
  });
});

describe("agent picker readiness (needs-setup badges)", () => {
  it.each([
    ["pending probe", "loading" as const],
    ["boot fallback", SERVER_INFO_OFFLINE_FALLBACK],
  ])("waits for curation during %s", async (_label, pendingInfo) => {
    vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
      data: [...AGENTS, testAgent("ag_codex", "codex-native-ui", { harness: "codex-native" })],
    } as ReturnType<typeof agentsHook.useAvailableAgents>);
    const dialog = (
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        initialName="Nightly"
        initialPrompt="Do it"
      />
    );
    const view = render(<CapabilitiesProvider info={pendingInfo}>{dialog}</CapabilitiesProvider>);
    // A valid explicit target isolates the curation gate from host loading.
    fireEvent.click(screen.getByTestId("task-host-trigger"));
    fireEvent.click(screen.getByRole("option", { name: /laptop/ }));
    expect(screen.getByTestId("pick-harness-claude")).toBeDisabled();
    expect(screen.getByTestId("task-model-trigger")).toBeDisabled();
    expect(screen.getByTestId("task-effort-trigger")).toBeDisabled();
    const permission = screen.getByTestId("task-permission-trigger");
    expect(permission).toBeDisabled();
    await userEvent.click(permission);
    expect(screen.queryByRole("option", { name: "Accept edits" })).not.toBeInTheDocument();
    const submit = screen.getByTestId("create-scheduled-task-submit");
    expect(submit).toBeDisabled();
    fireEvent.click(submit);
    expect(mutateAsync).not.toHaveBeenCalled();

    view.rerender(
      <CapabilitiesProvider
        info={{
          ...SERVER_INFO_OFFLINE_FALLBACK,
          scheduled_task_harness_settings: true,
          picker: {
            restrictToAgentNames: new Set(["codex-native-ui"]),
            defaultAgentName: "codex-native-ui",
          },
        }}
      >
        {dialog}
      </CapabilitiesProvider>,
    );
    const picker = screen.getByTestId("agent-picker-stub");
    expect(screen.getByRole("button", { name: "pick codex-native" })).toBeEnabled();
    expect(picker).toHaveAttribute("data-curated-entries", "codex-native-ui");
    expect(picker).toHaveAttribute("data-harness-entries", "codex-native-ui");
    expect(picker).toHaveAttribute("data-effective", "ag_codex");
    expect(screen.getByTestId("task-model-trigger")).toBeEnabled();
    expect(screen.getByTestId("task-permission-trigger")).toBeEnabled();
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].agentId).toBe("ag_codex");
    // A new automation starts on Approve for me; model and effort keep the agent's defaults.
    expect(mutateAsync.mock.calls[0][0].permissionMode).toBe("approve-for-me");
    for (const override of ["modelOverride", "reasoningEffort"]) {
      expect(mutateAsync.mock.calls[0][0]).not.toHaveProperty(override);
    }

    // A settled older server has no curation and still offers the full roster.
    view.rerender(
      <CapabilitiesProvider info={{ ...SERVER_INFO_OFFLINE_FALLBACK }}>
        {dialog}
      </CapabilitiesProvider>,
    );
    expect(screen.getByTestId("pick-harness-claude")).toBeEnabled();
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute(
      "data-harness-entries",
      "claude-native-ui,codex-native-ui",
    );
  });

  it.each([
    { editing: false, empty: false },
    { editing: true, empty: false },
    { editing: false, empty: true },
    { editing: true, empty: true },
  ])(
    "honors deployment curation when editing=$editing and empty=$empty",
    async ({ editing, empty }) => {
      vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
        data: [
          ...AGENTS,
          testAgent("ag_cursor", "cursor-native-ui", {
            display_name: "Cursor",
            harness: "cursor-native",
          }),
          testAgent("ag_pi", "pi-native-ui", { harness: "pi-native" }),
        ],
      } as ReturnType<typeof agentsHook.useAvailableAgents>);
      render(
        <CapabilitiesProvider
          info={{
            ...SERVER_INFO_OFFLINE_FALLBACK,
            managed_sandboxes_enabled: true,
            sandbox_provider: "arclet",
            picker: {
              restrictToAgentNames: new Set(
                empty ? ["not-installed"] : ["polly", "claude-native-ui", "pi-native-ui"],
              ),
              defaultAgentName: "pi-native-ui",
            },
          }}
        >
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            editingTask={
              editing
                ? scheduledTask({ agentId: "ag_cursor", executionTarget: "managed_sandbox" })
                : null
            }
          />
        </CapabilitiesProvider>,
        { expandAdvanced: false },
      );
      if (!editing && empty) {
        fireEvent.change(screen.getByLabelText("Automation title"), {
          target: { value: "Nightly" },
        });
        fireEvent.change(screen.getByLabelText("Instructions"), {
          target: { value: "Review changes" },
        });
        expect(screen.getByRole("alert")).toHaveTextContent(
          "No agents are available for this automation.",
        );
        expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      }
      const picker = screen.getByTestId("agent-picker-stub");
      expect(picker).toHaveAttribute(
        "data-harness-entries",
        empty ? "" : "claude-native-ui,pi-native-ui",
      );
      expect(picker).toHaveAttribute("data-agent-entries", empty ? "" : "polly");
      expect(picker).toHaveAttribute(
        "data-curated-entries",
        empty ? "" : "polly,claude-native-ui,pi-native-ui",
      );
      expect(picker).toHaveAttribute("data-sandbox-selected", "true");
      expect(picker).toHaveAttribute("data-badge-host", "");
      if (editing) {
        expect(picker).toHaveAttribute("data-effective", "ag_cursor");
        expect(picker).toHaveTextContent("Cursor");
        fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
        await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
        expect(updateMutateAsync.mock.calls[0][0].input.agentId).toBeUndefined();
      } else if (empty) {
        expect(picker).toHaveAttribute("data-effective", "");
        expect(picker).toHaveTextContent("No agents");
        fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
        fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
        expect(screen.getByTestId("task-host-trigger")).toHaveTextContent("Arclet Sandbox");
        expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      } else {
        expect(picker).toHaveAttribute("data-effective", "ag_pi");
      }
    },
  );

  it("shows the model + effort controls for the default (Claude-native) agent", () => {
    renderDialog();
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
    expect(submit).toHaveTextContent("Create");
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
    const { rerender } = render(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} />, {
      expandAdvanced: false,
    });
    const hostTrigger = screen.getByTestId("task-host-trigger");
    fireEvent.pointerDown(hostTrigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    fireEvent.click(hostTrigger);
    fireEvent.click(screen.getByRole("option", { name: /laptop/ }));
    openAutomationSection("Context");

    fireEvent.click(screen.getByTestId("task-workspace-trigger"));
    expect(screen.getByTestId("workspace-picker-dialog")).toBeInTheDocument();
    expect(screen.queryByText("/home/me/repo")).toBeNull();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("workspace-picker-dialog")).not.toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalled();
    openAutomationSection("Context");
    fireEvent.click(screen.getByTestId("task-workspace-trigger"));

    fireEvent.click(screen.getByRole("button", { name: "pick-workspace" }));
    openAutomationSection("Context");

    expect(screen.queryByTestId("workspace-picker-dialog")).toBeNull();
    expect(screen.getByText("/home/me/repo")).toBeVisible();

    expect(gitWorkspace.query).toHaveBeenLastCalledWith("host_1", "/home/me/repo");
    fireEvent.click(screen.getByRole("checkbox", { name: "Random worktree" }));
    fireEvent.click(screen.getByTestId("task-workspace-trigger"));
    expect(screen.getByTestId("workspace-picker-dialog")).toBeInTheDocument();
    vi.mocked(hostsHook.useHosts).mockReturnValue({
      data: [{ host_id: "host_1", name: "laptop", owner: "me", status: "offline" }],
    } as unknown as ReturnType<typeof hostsHook.useHosts>);
    rerender(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} />);
    expect(screen.queryByTestId("workspace-picker-dialog")).not.toBeInTheDocument();
    openAutomationSection("Context");
    expect(screen.getByTestId("task-workspace-trigger")).toBeDisabled();
    expect(gitWorkspace.query).toHaveBeenLastCalledWith(null, "/home/me/repo");
    expect(screen.queryByRole("checkbox", { name: "Random worktree" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Turn off random worktree" })).toBeEnabled();
    expect(screen.getByText("/home/me/repo")).toBeVisible();

    vi.mocked(hostsHook.useHosts).mockReturnValue({
      data: [{ host_id: "host_1", name: "laptop", owner: "me", status: "online" }],
    } as unknown as ReturnType<typeof hostsHook.useHosts>);
    rerender(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} />);
    openAutomationSection("Context");
    expect(screen.getByTestId("task-workspace-trigger")).toBeEnabled();
    expect(gitWorkspace.query).toHaveBeenLastCalledWith("host_1", "/home/me/repo");
    expect(screen.getByRole("checkbox", { name: "Random worktree" })).toBeChecked();
    expect(screen.queryByTestId("workspace-picker-dialog")).not.toBeInTheDocument();
    expect(screen.queryByText("Connect your host to select a workspace.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("task-workspace-trigger"));
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
    await userEvent.click(screen.getByLabelText("Time", { exact: true }));
    expect(screen.getByTestId("schedule-time-picker")).toBeVisible();
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

  it("expands Advanced to explain an unsupported saved rule", () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ rrule: "FREQ=DAILY;BYHOUR=9,17;BYMINUTE=0" })}
      />,
      { collapseAdvanced: true },
    );
    expect(screen.getByTestId("task-advanced-settings-trigger")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("This schedule can't be edited");
    expect(screen.getByLabelText("Frequency")).toBeVisible();
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
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
      expect(screen.getByLabelText("Frequency")).toBeVisible();
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      selectScheduleEnding("After");
      fireEvent.change(screen.getByLabelText("Occurrences"), { target: { value: "3" } });
      expect(screen.getByRole("alert")).toHaveTextContent("This schedule can't be edited");
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      expect(updateMutateAsync).not.toHaveBeenCalled();
      openAutomationSection("Schedule");
      selectSchedulePreset(frequency);
      if (frequency === "Custom schedule") {
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
        fireEvent.change(screen.getByLabelText("Repeat every"), { target: { value: "2" } });
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
    expect(screen.getByTestId("task-host-trigger")).toHaveTextContent("Sandbox");
  });

  it("starts a new task in sandbox mode when sandboxes are offered, and a host pick sticks", () => {
    renderWithSandboxes(vi.fn(), {
      sandbox_provider: "agent_sandbox",
      sandbox_provider_automation_notices: {
        agent_sandbox: "Keep your laptop's credentials fresh.",
        other_sandbox: "Another provider's notice.",
      },
    });
    // The sandbox default wins over the online host and shows the fresh-sandbox
    // copy and the provider's notice; the connected-host workspace picker never appears.
    expect(screen.getByTestId("task-host-trigger")).toHaveTextContent("Sandbox");
    // Only the run target's provider notice shows.
    expect(screen.getByTestId("task-sandbox-automation-notice")).toHaveTextContent(
      "Keep your laptop's credentials fresh.",
    );
    expect(screen.queryByText("Another provider's notice.")).not.toBeInTheDocument();
    expect(screen.queryByText("pick-workspace")).not.toBeInTheDocument();
    openHostSelect();
    fireEvent.click(screen.getByRole("option", { name: /laptop/ }));
    expect(screen.getByTestId("task-host-trigger")).toHaveTextContent("laptop");
    expect(screen.queryByTestId("task-sandbox-automation-notice")).not.toBeInTheDocument();
    openHostSelect();
    fireEvent.click(screen.getByTestId("task-host-sandbox-option"));
    expect(screen.getByTestId("task-sandbox-automation-notice")).toBeInTheDocument();
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
      expect(screen.queryByTestId("task-workspace-trigger")).not.toBeInTheDocument();
      expect(screen.getByLabelText("Repository")).toHaveTextContent(label);
      const sessionLock = /This automation continues in an existing session/;
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
        expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute(
          "data-trigger-details",
          "Model:Sonnet|Effort:Medium",
        );
      }
      if (pick !== null) {
        fireEvent.keyDown(screen.getByLabelText("Repository"), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name: pick }));
      }
      expect(screen.getByTestId("create-scheduled-task-dialog")).toBeInTheDocument();
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
      expect(screen.getByTestId("task-workspace-trigger")).toHaveTextContent("No workspace");
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

    expect(screen.queryByTestId("task-workspace-trigger")).not.toBeInTheDocument();
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
    const workspaceTrigger = screen.getByTestId("task-workspace-trigger");
    expect(workspaceTrigger).toBeDisabled();
    expect(screen.getByText("This host is offline. Runs fail until it reconnects.")).toBeVisible();
    fireEvent.click(workspaceTrigger);
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
    openAutomationSection("Context");
    expect(screen.getByTestId("task-workspace-trigger")).toHaveTextContent("/home/me/repo");
    expect(screen.getByTestId("task-workspace-trigger")).toBeDisabled();
    expect(screen.getByText("This host is offline. Runs fail until it reconnects.")).toBeVisible();
    fireEvent.click(screen.getByTestId("task-workspace-trigger"));
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
    openAutomationSection("Run setup");

    expect(screen.getByTestId("task-host-trigger")).toHaveTextContent("Most recently active host");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).not.toHaveProperty("hostId");
  });

  it("shows Schedule inline after Trigger type", () => {
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} />, { expandAdvanced: false });
    const scheduleSection = screen.getByRole("button", { name: "Schedule" });
    expect(scheduleSection).toHaveAttribute("aria-expanded", "false");
    const hostTrigger = screen.getByTestId("task-host-trigger");
    expect(hostTrigger).toBeVisible();
    expect(screen.getByTestId("task-agent-picker")).toBeVisible();
    expect(screen.getByTestId("task-trigger-type").compareDocumentPosition(scheduleSection)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(scheduleSection.compareDocumentPosition(hostTrigger)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    fireEvent.click(screen.getByTestId("pick-agent-polly"));
    expect(screen.getByTestId("agent-picker-stub")).toHaveTextContent("Polly");
    expect(scheduleSection).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("task-model-trigger")).not.toBeInTheDocument();
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

  it("typing a non-quarter-hour time flows into the submitted RRULE", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    const time = screen.getByLabelText("Time", { exact: true });
    await userEvent.click(time);
    expect(time).toHaveFocus();
    expect(screen.getByTestId("schedule-time-picker")).toBeVisible();
    fireEvent.change(time, { target: { value: "17:07" } });
    fireEvent.blur(time);
    expect(time).toHaveValue("05:07 PM");

    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].rrule).toBe("FREQ=DAILY;BYHOUR=17;BYMINUTE=7");
  });

  it.each([
    ["Daily", "FREQ=DAILY;INTERVAL=1;BYHOUR=17;BYMINUTE=7"],
    ["Weekly", "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO;BYHOUR=17;BYMINUTE=7"],
    ["Monthly", "FREQ=MONTHLY;INTERVAL=1;BYMONTHDAY=1;BYHOUR=17;BYMINUTE=7"],
    ["Yearly", "FREQ=YEARLY;INTERVAL=1;BYMONTH=1;BYMONTHDAY=1;BYHOUR=17;BYMINUTE=7"],
  ])("edits and validates %s custom start times inline", async (frequency, rule) => {
    renderDialog();
    fireEvent.change(screen.getByLabelText("Automation title"), { target: { value: "T" } });
    fireEvent.change(screen.getByLabelText("Instructions"), { target: { value: "P" } });
    selectSchedulePreset("Custom schedule");
    fireEvent.keyDown(screen.getByLabelText("Repeat unit"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: frequency }));
    const time = screen.getByLabelText("Time", { exact: true });
    await userEvent.click(time);
    expect(screen.queryByRole("button", { name: "Apply" })).not.toBeInTheDocument();
    expect(screen.getByTestId("schedule-time-picker")).toBeVisible();
    if (frequency === "Daily") {
      for (const value of ["", "25:99"]) {
        fireEvent.change(time, { target: { value } });
        expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      }
      for (const value of ["00:00", "23:59"]) {
        fireEvent.change(time, { target: { value } });
        expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
      }
    }
    fireEvent.change(time, { target: { value: "17:07" } });
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
    fireEvent.blur(time);
    expect(time).toHaveValue("05:07 PM");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].rrule).toBe(rule);
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

  it("uses title typography for the automation name and text-ui for instructions", () => {
    renderDialog();
    expect(screen.getByTestId("task-name-input")).toHaveClass("font-heading", "text-[1.25em]");
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

  it("types the hourly minute directly and validates its range before saving", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    selectSchedulePreset("Every hour");
    const minute = screen.getByRole("spinbutton", { name: "Minute of the hour" });
    expect(minute).toHaveValue(0);
    expect(minute).toHaveAttribute("min", "0");
    expect(minute).toHaveAttribute("max", "59");
    expect(screen.queryByTestId("schedule-time-picker-trigger")).not.toBeInTheDocument();
    fireEvent.click(minute);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    for (const value of ["", "-1", "60", "1.5"]) {
      fireEvent.change(minute, { target: { value } });
      expect(screen.getByTestId("schedule-error")).toHaveTextContent(
        "Enter a valid minute from 0 to 59",
      );
      expect(minute).toHaveAttribute("aria-invalid", "true");
      expect(minute).toHaveAccessibleDescription("Enter a valid minute from 0 to 59.");
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    }
    for (const value of ["0", "59", "7"]) {
      fireEvent.change(minute, { target: { value } });
      expect(minute).not.toHaveAttribute("aria-invalid");
      expect(minute).not.toHaveAttribute("aria-describedby");
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
    }
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].rrule).toBe("FREQ=HOURLY;BYMINUTE=7");
  });

  it.each(["Every hour", "Custom schedule"])(
    "recovers an invalid typed time when changing to hourly through %s",
    async (preset) => {
      renderDialog();
      fireEvent.change(screen.getByTestId("schedule-time"), { target: { value: "invalid" } });
      selectSchedulePreset(preset);
      if (preset === "Custom schedule") {
        fireEvent.keyDown(screen.getByLabelText("Repeat unit"), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name: "Hourly" }));
      }
      expect(screen.getByLabelText("Minute of the hour")).toHaveValue(0);
      expect(screen.queryByText(/NaN/)).not.toBeInTheDocument();
      expect(screen.queryByTestId("schedule-error")).not.toBeInTheDocument();
    },
  );

  it.each([
    { sandbox: false, subhour: true, minimum: 10 },
    { sandbox: true, subhour: true, minimum: 60 },
    { sandbox: false, subhour: false, minimum: 60 },
  ])(
    "offers minutes directly through Interval (sandbox=$sandbox, subhour=$subhour)",
    async ({ sandbox, subhour, minimum }) => {
      render(
        <CapabilitiesProvider
          info={{
            ...SERVER_INFO_OFFLINE_FALLBACK,
            features: { automations_v2: true },
            managed_sandboxes_enabled: sandbox,
            scheduled_task_subhour: subhour,
          }}
        >
          <CreateScheduledTaskDialog open onOpenChange={vi.fn()} />
        </CapabilitiesProvider>,
      );
      fireEvent.change(screen.getByLabelText("Automation title"), {
        target: { value: "Interval check" },
      });
      fireEvent.change(screen.getByLabelText("Instructions"), {
        target: { value: "Review recent changes" },
      });
      await userEvent.click(screen.getByLabelText("Timezone", { exact: true }));
      expect(screen.getByLabelText("Search timezones")).toBeVisible();
      expect(screen.queryByRole("button", { name: "Done" })).not.toBeInTheDocument();
      fireEvent.change(screen.getByLabelText("Search timezones"), {
        target: { value: "Singapore" },
      });
      await userEvent.click(await screen.findByRole("menuitemradio", { name: "Asia/Singapore" }));
      expect(screen.queryByLabelText("Search timezones")).not.toBeInTheDocument();
      expect(screen.getByLabelText("Timezone", { exact: true })).toHaveTextContent(
        "Asia/Singapore",
      );
      selectSchedulePreset("Interval");
      expect(screen.queryByLabelText("Repeat unit")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Apply" })).not.toBeInTheDocument();
      const interval = screen.getByLabelText("Repeat every");
      expect(interval).toHaveValue(minimum);
      expect(interval).toHaveAttribute("min", String(minimum));
      fireEvent.change(interval, { target: { value: String(minimum - 1) } });
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      expect(interval).toHaveAttribute("aria-invalid", "true");
      expect(interval).toHaveAccessibleDescription(
        screen.getByTestId("schedule-error").textContent!,
      );
      fireEvent.change(interval, { target: { value: String(minimum + 5) } });
      expect(interval).not.toHaveAttribute("aria-invalid");
      expect(interval).not.toHaveAttribute("aria-describedby");
      expect(screen.getByLabelText("Frequency")).toHaveTextContent("Interval");
      expect(screen.getByLabelText("Repeat every")).toHaveValue(minimum + 5);
      selectSchedulePreset("Every day");
      selectSchedulePreset("Interval");
      expect(screen.getByLabelText("Repeat every")).toHaveValue(minimum + 5);
      expect(screen.queryByTestId("custom-schedule-trigger")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Timezone", { exact: true })).not.toBeInTheDocument();
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
      expect(mutateAsync.mock.calls[0][0]).toMatchObject({
        rrule: `FREQ=MINUTELY;INTERVAL=${minimum + 5}`,
        timezone: "Asia/Singapore",
      });
    },
  );

  it("offers recurring presets and a visual custom schedule", async () => {
    renderDialog();
    const options = within(screen.getByTestId("schedule-preset-options"))
      .getAllByRole("radio")
      .map((option) => option.textContent);
    expect(options).toEqual([
      "Interval",
      "Every hour",
      "Every day",
      "Every weekday",
      "Every week",
      "Custom schedule",
    ]);
    selectSchedulePreset("Custom schedule");
    expect(screen.getByLabelText("Repeat every")).toHaveValue(1);
    fireEvent.keyDown(screen.getByLabelText("Repeat unit"), { key: "Enter" });
    expect((await screen.findAllByRole("option")).map((option) => option.textContent)).toEqual([
      "Hourly",
      "Daily",
      "Weekly",
      "Monthly",
      "Yearly",
    ]);
  });

  it("applies custom weekdays, validates intervals and submits the selected timezone and end count", async () => {
    renderDialog();
    selectSchedulePreset("Custom schedule");
    fireEvent.change(screen.getByLabelText("Repeat every"), { target: { value: "0" } });
    expect(screen.getByTestId("schedule-error")).toHaveTextContent("at least 1");
    fireEvent.change(screen.getByLabelText("Repeat every"), { target: { value: "2" } });
    fireEvent.keyDown(screen.getByLabelText("Repeat unit"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Weekly" }));
    await userEvent.click(screen.getByRole("button", { name: "Days of the week" }));
    fireEvent.click(screen.getByRole("button", { name: "Wednesday" }));
    fireEvent.keyDown(screen.getByRole("button", { name: "Wednesday" }), { key: "Escape" });
    expect(screen.queryByRole("group", { name: "Weekdays" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Days of the week" }));
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("group", { name: "Weekdays" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Repeat every")).toHaveValue(2);
    const recurrenceSummary = screen.getByTestId("schedule-preset-trigger").textContent;
    expect(recurrenceSummary).not.toContain("Select schedule");
    await userEvent.click(screen.getByLabelText("Timezone"));
    fireEvent.change(screen.getByLabelText("Search timezones"), { target: { value: "Singapore" } });
    await waitFor(() => expect(screen.getAllByRole("menuitemradio")).toHaveLength(1));
    await userEvent.click(screen.getByRole("menuitemradio", { name: "Asia/Singapore" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Timezone")).toHaveTextContent("Asia/Singapore"),
    );
    selectScheduleEnding("After");
    fireEvent.change(screen.getByLabelText("Occurrences"), { target: { value: "1" } });
    const occurrences = screen.getByLabelText("Occurrences");
    const endingError = within(screen.getByTestId("schedule-ending-fields")).getByRole("alert");
    expect(endingError).toHaveTextContent("at least 2");
    expect(occurrences).toHaveAttribute("aria-invalid", "true");
    expect(occurrences).toHaveAccessibleDescription(endingError.textContent!);
    expect(screen.queryByTestId("schedule-error")).not.toBeInTheDocument();
    openAutomationSection("Schedule");
    expect(screen.getByTestId("schedule-preset-trigger")).toHaveTextContent(recurrenceSummary!);
    selectScheduleEnding("On date");
    openAutomationSection("Schedule");
    expect(screen.getByTestId("schedule-preset-trigger")).toHaveTextContent(recurrenceSummary!);
    selectScheduleEnding("After");
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

  it("discards inline custom edits when the automation dialog is cancelled", async () => {
    const task = scheduledTask({ rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0" });
    const onOpenChange = vi.fn();
    const view = render(
      <CreateScheduledTaskDialog open onOpenChange={onOpenChange} editingTask={task} />,
    );
    selectSchedulePreset("Custom schedule");
    fireEvent.change(screen.getByLabelText("Repeat every"), { target: { value: "3" } });
    expect(screen.getByLabelText("Frequency")).toHaveTextContent("Custom schedule");
    expect(screen.queryByRole("button", { name: "Apply" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(updateMutateAsync).not.toHaveBeenCalled();
    view.rerender(
      <CreateScheduledTaskDialog open={false} onOpenChange={onOpenChange} editingTask={task} />,
    );
    view.rerender(
      <CreateScheduledTaskDialog open onOpenChange={onOpenChange} editingTask={task} />,
    );
    expect(screen.getByLabelText("Frequency")).toHaveTextContent("Every day");
    expect(screen.queryByLabelText("Repeat every")).not.toBeInTheDocument();
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
    selectSchedulePreset("Every hour");
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    selectSchedulePreset("Every day");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
      name: "Renamed",
      prompt: "Updated prompt",
      rrule: rule,
    });
  });

  it("edits yearly recurrence inline and retains edits when a selector closes", async () => {
    const onOpenChange = vi.fn();
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={onOpenChange}
        editingTask={scheduledTask({
          rrule: "FREQ=YEARLY;INTERVAL=2;BYMONTH=6;BYMONTHDAY=1,15;BYHOUR=9;BYMINUTE=0;COUNT=4",
          timezone: "Asia/Singapore",
        })}
      />,
    );
    expect(screen.getByLabelText("Frequency")).toHaveTextContent("Custom schedule");
    expect(screen.getByLabelText("Repeat unit")).toHaveTextContent("Yearly");
    expect(screen.getByLabelText("Repeat every")).toHaveValue(2);
    expect(screen.getByLabelText("Month")).toHaveTextContent("June");
    expect(screen.getByLabelText("Timezone")).toHaveTextContent("Asia/Singapore");
    openTopLevelPopover("task-ending-trigger");
    expect(screen.getByLabelText("Occurrences")).toHaveValue(4);
    openAutomationSection("Schedule");
    fireEvent.keyDown(screen.getByLabelText("Month"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "July" }));
    for (const label of ["Month", "Repeat unit"]) {
      fireEvent.keyDown(screen.getByLabelText(label), { key: "Enter" });
      fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });
      expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
      expect(screen.getByLabelText("Month")).toHaveTextContent("July");
      expect(screen.getByLabelText("Repeat every")).toHaveValue(2);
    }
    await userEvent.click(screen.getByRole("button", { name: "Days of the month" }));
    const days = within(screen.getByRole("group", { name: "Days of the month" }));
    expect(days.getByRole("button", { name: "15" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(days.getByRole("button", { name: "15" }));
    fireEvent.click(days.getByRole("button", { name: "20" }));
    fireEvent.keyDown(days.getByRole("button", { name: "20" }), { key: "Escape" });
    expect(screen.queryByRole("group", { name: "Days of the month" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Days of the month" }));
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("group", { name: "Days of the month" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Days of the month" })).toHaveTextContent("1, 20");
    expect(onOpenChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input.rrule).toBe(
      "FREQ=YEARLY;INTERVAL=2;BYMONTH=7;BYMONTHDAY=1,20;BYHOUR=9;BYMINUTE=0;COUNT=4",
    );
  });

  describe.each(["FREQ=DAILY;BYHOUR=9;BYMINUTE=0", "FREQ=MINUTELY;INTERVAL=60"])(
    "inclusive end date for %s",
    (rrule) => {
      const minutely = rrule.includes("MINUTELY");
      beforeEach(() => {
        vi.useFakeTimers({ toFake: ["Date"] });
        vi.setSystemTime(new Date("2026-10-01T00:00:00Z"));
        render(
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            editingTask={scheduledTask({ rrule, timezone: "Asia/Singapore" })}
          />,
        );
        if (!minutely) {
          expect(screen.getByLabelText("Time")).not.toHaveAttribute("aria-invalid");
          expect(
            within(screen.getByTestId("schedule-fields")).getByLabelText("Timezone"),
          ).toHaveTextContent("Asia/Singapore");
        }
        selectScheduleEnding("On date");
        expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
        if (minutely) {
          expect(
            within(screen.getByTestId("schedule-ending-fields")).getByLabelText("Timezone"),
          ).toHaveTextContent("Asia/Singapore");
        }
      });
      async function chooseTimezone(search: string, option: string) {
        if (!minutely) openAutomationSection("Schedule");
        await userEvent.click(screen.getByLabelText("Timezone"));
        fireEvent.change(screen.getByLabelText("Search timezones"), {
          target: { value: search },
        });
        await userEvent.click(await screen.findByRole("menuitemradio", { name: option }));
        if (!minutely) openTopLevelPopover("task-ending-trigger");
      }
      it("rejects past dates without leaving an error after correction", () => {
        for (const value of ["0002-06-10", "0020-06-10", "0203-06-10"]) {
          fireEvent.change(screen.getByLabelText("End date"), { target: { value } });
          expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
          const dateField = screen.getByLabelText("End date");
          expect(dateField).toHaveAttribute("aria-invalid", "true");
          expect(dateField).toHaveAccessibleDescription("Choose an end date in the future.");
          expect(screen.queryByTestId("schedule-error")).not.toBeInTheDocument();
        }
        fireEvent.change(screen.getByLabelText("End date"), { target: { value: "2027-06-10" } });
        expect(screen.getByLabelText("End date")).not.toHaveAttribute("aria-invalid");
        expect(screen.getByLabelText("End date")).not.toHaveAttribute("aria-describedby");
        expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
      });
      it("revalidates the maximum date when its timezone changes", async () => {
        await chooseTimezone("New_York", "America/New York");
        fireEvent.change(screen.getByLabelText("End date"), { target: { value: "9999-12-31" } });
        expect(screen.getByLabelText("End date")).toHaveValue("9999-12-31");
        expect(screen.getByTestId("schedule-ending-error")).toHaveTextContent(
          "This end date is unavailable in this timezone",
        );
        expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
        await chooseTimezone("Singapore", "Asia/Singapore");
        expect(screen.getByLabelText("End date")).toHaveValue("9999-12-31");
        expect(screen.queryByTestId("schedule-ending-error")).not.toBeInTheDocument();
        expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
      });
      it("saves the inclusive date after changing the selected timezone", async () => {
        fireEvent.change(screen.getByLabelText("End date"), { target: { value: "2027-06-10" } });
        await chooseTimezone("New_York", "America/New York");
        expect(screen.getByLabelText("End date")).toHaveValue("2027-06-10");
        expect(screen.getByLabelText("End date")).not.toHaveAttribute("aria-invalid");
        expect(screen.getByLabelText("End date")).not.toHaveAttribute("aria-describedby");
        fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
        await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
        expect(updateMutateAsync.mock.calls[0][0].input.rrule).toBe(
          `${rrule};UNTIL=20270611T035959Z`,
        );
      });
    },
  );

  it("does not render the 'Reads as' schedule preview", () => {
    renderDialog();
    expect(screen.queryByTestId("schedule-preview")).toBeNull();
    expect(screen.queryByText(/Reads as:/i)).toBeNull();
  });
});

describe("CreateScheduledTaskDialog model + effort controls", () => {
  it("keeps the independent setup controls available without a harness", () => {
    vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
      data: [],
    } as unknown as ReturnType<typeof agentsHook.useAvailableAgents>);
    renderDialog();
    expect(screen.getByTestId("task-host-trigger")).toBeVisible();
    expect(screen.getByTestId("task-schedule-trigger")).toBeVisible();
    expect(screen.getByTestId("task-ending-trigger")).toBeVisible();
    expect(screen.getByTestId("task-workspace-trigger")).toBeVisible();
  });

  it("closes the agent picker when the dialog reopens", () => {
    const onOpenChange = vi.fn();
    const { rerender } = render(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} />, {
      expandAdvanced: false,
    });
    fireEvent.click(screen.getByTestId("picker-open"));
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute("data-picker-open", "true");
    rerender(<CreateScheduledTaskDialog open={false} onOpenChange={onOpenChange} />);
    rerender(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} />);
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute("data-picker-open", "false");
  });

  it("renders model + effort for a capable (Claude-native) agent, hides them for a plain agent", () => {
    renderDialog();
    // Default effective agent is the capable claude-native harness → controls show.
    expect(screen.getByTestId("task-model-effort-field")).toBeInTheDocument();

    // Switch to Polly (claude-sdk, no permissionMode capability) → controls hide.
    fireEvent.click(screen.getByTestId("pick-agent-polly"));
    expect(screen.queryByTestId("task-model-effort-field")).not.toBeInTheDocument();
    expect(screen.queryByTestId("task-model-trigger")).not.toBeInTheDocument();
    expect(screen.queryByTestId("task-effort-trigger")).not.toBeInTheDocument();
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

    // The composer-style picker and independent permission control keep the chosen settings.
    expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("Opus");
    expect(screen.getByTestId("task-effort-trigger")).toHaveTextContent("High");
    expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Accept edits");

    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg.modelOverride).toBe("opus");
    expect(arg.reasoningEffort).toBe("high");
    expect(arg.permissionMode).toBe("acceptEdits");
  });

  it("pipes model and effort picks from the composer-style agent picker into create", async () => {
    renderDialog();
    const picker = screen.getByTestId("agent-picker-stub");
    expect(picker).toHaveAttribute("data-has-trigger-icon", "true");
    expect(picker).toHaveAttribute("data-has-config", "true");
    expect(picker).toHaveAttribute("data-trigger-details", "Model:Default|Effort:Default");

    fireEvent.click(screen.getByTestId("task-agent-model-opus-mock"));
    fireEvent.click(screen.getByTestId("task-agent-effort-high-mock"));
    expect(picker).toHaveAttribute("data-trigger-details", "Model:Opus|Effort:High");
    // Opening Edit selects the active row again before showing its config.
    fireEvent.click(screen.getByTestId("pick-harness-claude"));
    expect(picker).toHaveAttribute("data-trigger-details", "Model:Opus|Effort:High");

    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "N" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({
      modelOverride: "opus",
      reasoningEffort: "high",
    });
  });

  it("omits model + effort and starts permission at Auto on create", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "N" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg).not.toHaveProperty("modelOverride");
    expect(arg).not.toHaveProperty("reasoningEffort");
    // Nobody watches an automation's turns, so a new one starts in an auto-approve mode.
    expect(arg.permissionMode).toBe("auto");
  });

  it("starts on Auto again after the user picks Default and cancels", async () => {
    const onOpenChange = vi.fn();
    const view = renderDialog(onOpenChange);
    fireEvent.keyDown(screen.getByTestId("task-permission-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Default" }));
    expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Default");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    view.rerender(<CreateScheduledTaskDialog open={false} onOpenChange={onOpenChange} />);
    view.rerender(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} />);
    expect(await screen.findByTestId("task-permission-trigger")).toHaveTextContent("Auto");
  });

  it("omits permission on create when the user picks Default", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "N" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Auto");
    fireEvent.keyDown(screen.getByTestId("task-permission-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Default" }));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).not.toHaveProperty("permissionMode");
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
        "This automation continues in an existing session, so its model, effort, and permission mode are controlled by that session.",
      );
      expect(
        screen.queryByRole("button", { name: "Edit worker instructions" }),
      ).not.toBeInTheDocument();
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
        { expandAdvanced: false },
      );

      openAutomationSection("Context");
      fireEvent.click(screen.getByTestId("task-workspace-trigger"));
      fireEvent.click(screen.getByRole("button", { name: "pick-workspace" }));
      expect(screen.getByTestId("task-model-trigger")).toBeDisabled();
      closeTopLevelPopovers();
      fireEvent.keyDown(screen.getByTestId("task-session-mode-trigger"), { key: "Enter" });
      fireEvent.click(screen.getByTestId("task-start-new-session"));
      fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
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
        fireEvent.keyDown(screen.getByTestId("task-session-mode-trigger"), { key: "Enter" });
        fireEvent.click(screen.getByTestId("task-start-new-session"));
        fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
        expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("Sonnet");
        expect(screen.getByTestId("task-effort-trigger")).toHaveTextContent("Medium");
        expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Plan");
        expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute(
          "data-trigger-details",
          "Model:Sonnet|Effort:Medium",
        );
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

  it.each([
    ["bypassPermissions", false, { permissionMode: null }],
    ["bypassPermissions", true, {}],
    ["acceptEdits", false, {}],
  ])(
    "on a continued-session edit, clears a saved %s only for a server that refuses it (accepts: %s)",
    async (savedMode, accepted, expectedPermission) => {
      render(
        <CapabilitiesProvider
          info={{
            ...SERVER_INFO_OFFLINE_FALLBACK,
            scheduled_task_session_modes: true,
            scheduled_task_approval_skipping_modes: accepted,
          }}
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
              permissionMode: savedMode,
            })}
          />
        </CapabilitiesProvider>,
      );
      expect(screen.getByTestId("task-permission-trigger")).toBeDisabled();
      fireEvent.change(screen.getByLabelText("Instructions"), { target: { value: "Keep going" } });
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const { input } = updateMutateAsync.mock.calls[0][0];
      expect(input).toMatchObject({ prompt: "Keep going", ...expectedPermission });
      // The session binding and the session's own model settings are left alone.
      expect(input).not.toHaveProperty("sessionId");
      expect(input).not.toHaveProperty("modelOverride");
      expect(input).not.toHaveProperty("reasoningEffort");
      if (!("permissionMode" in expectedPermission)) {
        expect(input).not.toHaveProperty("permissionMode");
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
        { expandAdvanced: false },
      );
      expect(screen.getByTestId("task-model-trigger")).toBeDisabled();
      if (change === "host") {
        fireEvent.keyDown(screen.getByTestId("task-host-trigger"), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name: "laptop" }));
      } else {
        fireEvent.keyDown(screen.getByTestId("task-session-mode-trigger"), { key: "Enter" });
        fireEvent.click(screen.getByRole("menuitemradio", { name: "New session each run" }));
      }
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
      openAutomationSection("Context");
      fireEvent.click(screen.getByTestId("task-workspace-trigger"));
      fireEvent.click(screen.getByRole("button", { name: "pick-workspace" }));
      openAutomationSection("Context");
      expect(screen.getByTestId("task-workspace-trigger")).toHaveTextContent("/home/me/repo");
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

  it("files new automations in the default project at save time", async () => {
    resolveOrCreateProjectId.mockResolvedValueOnce("proj_automation");
    renderWithProjects();
    fillRequired();
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("Omnigent Automation");
    expect(resolveOrCreateProjectId).not.toHaveBeenCalled();
    expect(await submitCreate()).toMatchObject({ projectId: "proj_automation" });
    expect(resolveOrCreateProjectId).toHaveBeenCalledWith("Omnigent Automation");
  });

  it("allows opting out of the default project", async () => {
    renderWithProjects();
    fillRequired();
    openProjectMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: /Remove from/ }));
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("No project");
    expect(await submitCreate()).not.toHaveProperty("projectId");
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
  it("clears an open picker dismiss guard when the dialog is reopened", async () => {
    const onOpenChange = vi.fn();
    const view = renderDialog(onOpenChange);
    fireEvent.click(screen.getByTestId("picker-open"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    view.rerender(<CreateScheduledTaskDialog open={false} onOpenChange={onOpenChange} />);
    view.rerender(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} />);
    onOpenChange.mockClear();
    await userEvent.click(document.querySelector('[data-slot="dialog-overlay"]')!);
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  });

  it.each(["host", "model", "effort", "permission", "project"])(
    "Escape closes only the %s menu and keeps unsaved fields",
    (field) => {
      const onOpenChange = vi.fn();
      render(
        <CapabilitiesProvider
          info={{ ...SERVER_INFO_OFFLINE_FALLBACK, scheduled_task_projects: true }}
        >
          <CreateScheduledTaskDialog open onOpenChange={onOpenChange} />
        </CapabilitiesProvider>,
      );
      for (const testId of [
        "task-schedule-trigger",
        "task-ending-trigger",
        "task-workspace-trigger",
      ]) {
        const openTrigger = screen.queryByTestId(testId);
        if (openTrigger?.getAttribute("aria-expanded") === "true") fireEvent.click(openTrigger);
      }
      fireEvent.change(screen.getByLabelText("Automation title"), {
        target: { value: "Unsaved draft" },
      });
      const trigger = screen.getByTestId(`task-${field}-trigger`);
      const role = field === "project" ? "menu" : "listbox";
      for (let attempt = 0; attempt < 2; attempt++) {
        fireEvent.keyDown(trigger, { key: "Enter" });
        if (field === "model" || field === "effort") {
          expect(screen.getByTestId(`task-${field}-options`)).toBeVisible();
        } else {
          expect(screen.getByRole(role)).toBeVisible();
        }
        fireEvent.keyDown(window, { key: "Escape" });
        if (field === "model" || field === "effort") {
          expect(screen.queryByTestId(`task-${field}-options`)).not.toBeInTheDocument();
        } else {
          expect(screen.queryByRole(role)).not.toBeInTheDocument();
        }
        expect(screen.getByLabelText("Automation title")).toHaveValue("Unsaved draft");
        expect(onOpenChange).not.toHaveBeenCalled();
      }
    },
  );

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
    extraInfo: Partial<typeof SERVER_INFO_OFFLINE_FALLBACK> = {},
  ) {
    return render(
      <CapabilitiesProvider
        info={{
          ...SERVER_INFO_OFFLINE_FALLBACK,
          scheduled_task_session_modes: true,
          ...extraInfo,
        }}
      >
        <CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={editingTask} />
      </CapabilitiesProvider>,
    );
  }

  function chooseContinue() {
    closeTopLevelPopovers();
    const trigger = screen.getByTestId("task-session-mode-trigger");
    if (!trigger.textContent?.includes("Continue one session")) {
      fireEvent.keyDown(trigger, { key: "Enter" });
      fireEvent.click(screen.getByRole("menuitemradio", { name: "Continue one session" }));
    }
  }

  it("hides the Session control on a server that does not accept session modes", () => {
    renderDialog();
    expect(screen.queryByTestId("task-session-mode-field")).toBeNull();
  });

  it("sends sessionMode only when continue is chosen on create", async () => {
    renderWithSessionModes(undefined, { managed_sandboxes_enabled: true });
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
    chooseContinue();
    fireEvent.keyDown(screen.getByLabelText("Run on"), { key: "Enter" });
    fireEvent.click(screen.getByTestId("task-host-sandbox-option"));
    expect(screen.getByTestId("task-session-mode-trigger")).toHaveTextContent(
      "Continue one session",
    );
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

  it("clears the continued session when asked to start a new one on the next run", async () => {
    renderWithSessionModes(scheduledTask({ sessionMode: "continue", sessionId: "conv_1" }));
    closeTopLevelPopovers();
    fireEvent.keyDown(screen.getByTestId("task-session-mode-trigger"), { key: "Enter" });
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
    expect(screen.getByTestId("task-session-mode-trigger")).toHaveTextContent(
      "Continue one session",
    );
    closeTopLevelPopovers();
    fireEvent.keyDown(screen.getByTestId("task-session-mode-trigger"), { key: "Enter" });
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
      "Continue one session",
    );
  });

  it.each(["connected_host", "managed_sandbox"] as const)(
    "preserves a minute schedule while changing target and session mode to %s",
    async (target) => {
      renderWithSessionModes(
        scheduledTask({
          sessionMode: "continue",
          rrule: "FREQ=MINUTELY;INTERVAL=10",
          executionTarget: "managed_sandbox",
          hostId: null,
          workspace: null,
        }),
        {
          features: { automations_v2: true },
          scheduled_task_subhour: true,
          managed_sandboxes_enabled: true,
        },
      );
      openAutomationSection("Run setup");
      expect(screen.getByTestId("task-host-trigger")).toBeVisible();
      expect(screen.getByTestId("task-session-mode-trigger")).toBeVisible();
      expect(
        screen
          .getByTestId("task-host-trigger")
          .compareDocumentPosition(screen.getByTestId("task-schedule-trigger")),
      ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
      closeTopLevelPopovers();
      fireEvent.keyDown(screen.getByTestId("task-session-mode-trigger"), { key: "Enter" });
      fireEvent.click(screen.getByRole("menuitemradio", { name: "New session each run" }));
      openAutomationSection("Schedule");
      expect(screen.getByTestId("schedule-preset-trigger")).toHaveTextContent("Interval");
      expect(screen.getByLabelText("Repeat every")).toHaveValue(10);
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      expect(screen.getByTestId("schedule-error")).toHaveTextContent(
        "New sandbox sessions require schedules of at least 60 minutes",
      );
      openAutomationSection("Run setup");
      if (target === "connected_host") {
        fireEvent.keyDown(screen.getByTestId("task-host-trigger"), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name: "laptop" }));
      } else {
        chooseContinue();
      }
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const input = updateMutateAsync.mock.calls[0][0].input;
      expect(input.rrule).toBe("FREQ=MINUTELY;INTERVAL=10");
      if (target === "connected_host") {
        expect(input).toMatchObject({ sessionMode: "new", executionTarget: "connected_host" });
      } else {
        // Unchanged fields are omitted from PATCH, preserving sandbox/continue.
        expect(input).not.toHaveProperty("sessionMode");
        expect(input).not.toHaveProperty("executionTarget");
      }
    },
  );

  it.each(
    [undefined, false, true].flatMap((supported) =>
      [false, true].map((trigger) => ({ supported, trigger })),
    ),
  )(
    "negotiates minute schedules with subhour support $supported for trigger $trigger",
    async ({ supported, trigger }) => {
      renderWithSessionModes(
        scheduledTask({
          sessionMode: "continue",
          rrule: "FREQ=MINUTELY;INTERVAL=10",
          ...(trigger && {
            prompt: renderTriggerPrompt(
              "jira",
              {
                ...emptyTriggerFields("jira"),
                scope: "ES",
                instructions: "Triage it.",
              },
              { placement: "same_host", sessionLink: "https://omni.example.com/c/CONVERSATION_ID" },
            ),
          }),
        }),
        {
          features: { automations_v2: true },
          automation_trigger_poll_minutes: 240,
          ...(supported === undefined ? {} : { scheduled_task_subhour: supported }),
        },
      );
      const submit = screen.getByTestId("create-scheduled-task-submit");
      if (trigger) {
        expect(screen.queryByTestId("task-schedule-trigger")).toBeNull();
        expect(screen.queryByTestId("custom-schedule-trigger")).toBeNull();
        expect(screen.queryByTestId("schedule-preset-trigger")).toBeNull();
        expect(screen.queryByTestId("task-trigger-cadence")).toBeNull();
        expect(screen.queryByTestId("task-session-mode-trigger")).toBeNull();
        if (!supported) {
          expect(submit).toBeDisabled();
          fireEvent.click(screen.getByRole("button", { name: "Use supported schedule" }));
        }
        await waitFor(() => expect(submit).toBeEnabled());
        fireEvent.click(submit);
        await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
        expect(updateMutateAsync.mock.calls[0][0].input.rrule).toBe(
          supported ? "FREQ=MINUTELY;INTERVAL=10" : "FREQ=MINUTELY;INTERVAL=240",
        );
        expect(updateMutateAsync.mock.calls[0][0].input).not.toHaveProperty("sessionMode");
        return;
      }
      expect(screen.getByTestId("schedule-preset-trigger")).toHaveTextContent("Interval");
      expect(screen.getByLabelText("Repeat every")).toHaveValue(10);
      if (supported) expect(submit).toBeEnabled();
      else {
        expect(submit).toBeDisabled();
        expect(screen.getByTestId("schedule-error")).toHaveTextContent("at least 60 minutes");
        fireEvent.click(submit);
        expect(updateMutateAsync).not.toHaveBeenCalled();
      }
      selectSchedulePreset("Custom schedule");
      fireEvent.keyDown(screen.getByTestId("custom-freq-trigger"), { key: "Enter" });
      const minutes = await screen.findByRole("option", { name: "Minutely" });
      expect(minutes).not.toHaveAttribute("aria-disabled", "true");
      expect(screen.getByTestId("custom-interval")).toHaveAttribute("min", supported ? "10" : "60");
      fireEvent.click(screen.getByRole("option", { name: "Hourly" }));
      expect(screen.getByTestId("custom-interval")).toHaveValue(1);
      expect(submit).toBeEnabled();
      fireEvent.click(submit);
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      expect(updateMutateAsync.mock.calls[0][0].input.rrule).toBe(
        "FREQ=HOURLY;INTERVAL=1;BYMINUTE=0",
      );
      expect(updateMutateAsync.mock.calls[0][0].input).not.toHaveProperty("sessionMode");
    },
  );

  it.each([false, true])(
    "offers Minutes on create only with subhour support %s",
    async (supported) => {
      renderWithSessionModes(undefined, {
        features: { automations_v2: true },
        scheduled_task_subhour: supported,
      });
      selectSchedulePreset("Custom schedule");
      fireEvent.keyDown(screen.getByTestId("custom-freq-trigger"), { key: "Enter" });
      await screen.findByRole("option", { name: "Hourly" });
      expect(screen.queryByRole("option", { name: "Minutely" }) !== null).toBe(supported);
    },
  );

  it("offers no new-session option before the first run creates a session", () => {
    renderWithSessionModes(scheduledTask({ sessionMode: "continue" }));
    expect(screen.queryByTestId("task-start-new-session")).toBeNull();
  });
});

describe("automation trigger type", () => {
  function dialog(
    props: Partial<React.ComponentProps<typeof CreateScheduledTaskDialog>> = {},
    info: Partial<typeof SERVER_INFO_OFFLINE_FALLBACK> = {},
  ) {
    return (
      <CapabilitiesProvider
        info={{
          ...SERVER_INFO_OFFLINE_FALLBACK,
          scheduled_task_session_modes: true,
          scheduled_task_subhour: true,
          automation_trigger_poll_minutes: 30,
          scheduled_task_trigger_headers: true,
          features: { automations_v2: true, session_orchestration: true },
          ...info,
        }}
      >
        <CreateScheduledTaskDialog open onOpenChange={vi.fn()} {...props} />
      </CapabilitiesProvider>
    );
  }

  async function choose(label: string, testId = "task-trigger-type") {
    if (testId === "schedule-preset-trigger") {
      selectSchedulePreset(label);
      return;
    }
    fireEvent.keyDown(screen.getByTestId(testId), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: label }));
    if (testId === "task-trigger-type" && label === "Schedule") {
      openAutomationSection("Schedule");
    }
  }

  function type(testId: string, value: string) {
    fireEvent.change(screen.getByTestId(testId), { target: { value } });
  }

  it.each([
    ["Jira", "jira", "ES"],
    ["Slack", "slack", "support"],
    ["GitHub", "github", "acme/app"],
    ["Linear", "linear", "ENG"],
    ["PagerDuty", "pagerduty", "Checkout API"],
  ])(
    "defaults to Schedule and creates a %s trigger from the selector",
    async (label, source, scope) => {
      render(dialog());
      expect(screen.getByLabelText("Trigger type")).toHaveTextContent("Schedule");
      expect(screen.getByTestId("schedule-preset-trigger")).toBeVisible();
      await choose(label);
      expect(screen.getByLabelText("Trigger type")).toHaveTextContent(label);
      expect(screen.queryByTestId("schedule-preset-trigger")).not.toBeInTheDocument();
      expect(screen.queryByTestId("task-schedule-trigger")).not.toBeInTheDocument();
      expect(screen.queryByTestId("task-session-mode-field")).not.toBeInTheDocument();
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      type("task-trigger-scope", scope);
      type("task-trigger-instructions", "Investigate the item.");
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
      expect(mutateAsync.mock.calls[0][0]).toMatchObject({
        templateId: source,
        sessionMode: "continue",
        rrule: "FREQ=MINUTELY;INTERVAL=30",
        prompt: expect.stringContaining(scope),
      });
    },
  );

  it("keeps the task session choice when switching integrations", async () => {
    render(dialog());
    const sessionMode = () => screen.getByTestId("task-trigger-session-mode");
    await choose("Jira");
    chooseTriggerSessionMode("One task session");
    await choose("Slack");
    expect(sessionMode()).toHaveTextContent("One task session for all matches");
    await choose("Schedule");
    expect(screen.queryByTestId("task-trigger-session-mode-field")).not.toBeInTheDocument();
    await choose("Slack");
    expect(sessionMode()).toHaveTextContent("One task session for all matches");
    type("task-trigger-scope", "support");
    type("task-trigger-instructions", "Answer the question.");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(isTaskSessionTriggerPrompt(mutateAsync.mock.calls[0][0].prompt)).toBe(true);
  });

  it("sets the polling session's and matching sessions' models and efforts separately", async () => {
    render(dialog());
    await choose("GitHub");
    type("task-trigger-scope", "acme/app");
    type("task-trigger-instructions", "Review the pull request.");
    const workerSettings = screen.getByTestId("task-worker-settings");
    // Matching sessions follow the polling session until they get their own settings.
    await choose("Haiku", "task-model-trigger");
    expect(screen.getByTestId("task-worker-model-trigger")).toHaveTextContent("Haiku");
    expect(workerSettings).toHaveTextContent(
      "They match the polling session until you change them.",
    );
    await choose("Opus", "task-worker-model-trigger");
    await choose("High", "task-worker-effort-trigger");
    expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("Haiku");
    expect(screen.getByTestId("task-effort-trigger")).toHaveTextContent("Default");
    expect(workerSettings).not.toHaveTextContent("They match the polling session");
    expect(workerSettings).toHaveTextContent(
      "Each session started for a matching item uses these.",
    );
    // The agent picker carries the polling session's model and effort.
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute(
      "data-trigger-details",
      "Model:Haiku|Effort:Default",
    );
    expect(screen.getByTestId("task-matching-settings")).toHaveTextContent("Matching sessions");
    // A later polling change no longer carries over.
    await choose("Sonnet", "task-model-trigger");
    expect(screen.getByTestId("task-worker-model-trigger")).toHaveTextContent("Opus");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const created = mutateAsync.mock.calls[0][0];
    expect(created).toMatchObject({ templateId: "github", modelOverride: "sonnet" });
    expect(created).not.toHaveProperty("reasoningEffort");
    expect(readTriggerWorkerSettings(created.prompt)).toEqual({
      agentId: null,
      model: "opus",
      effort: "high",
      permissionMode: "",
    });
  });

  it("starts matching sessions with the polling session's settings unless changed", async () => {
    render(dialog({}, { managed_sandboxes_enabled: true }));
    await choose("Slack");
    type("task-trigger-scope", "support");
    type("task-trigger-instructions", "Answer the question.");
    await choose("Opus", "task-worker-model-trigger");
    // Choosing another agent resets the workers to follow the polling session.
    fireEvent.click(screen.getByTestId("pick-agent-polly"));
    fireEvent.click(screen.getByTestId("pick-harness-claude"));
    expect(screen.getByTestId("task-worker-model-trigger")).toHaveTextContent("Default");
    // So does choosing where it runs.
    await choose("Opus", "task-worker-model-trigger");
    const hostTrigger = screen.getByTestId("task-host-trigger");
    fireEvent.pointerDown(hostTrigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    fireEvent.click(hostTrigger);
    fireEvent.click(screen.getByRole("option", { name: /laptop/ }));
    expect(screen.getByTestId("task-worker-model-trigger")).toHaveTextContent("Default");
    await choose("Haiku", "task-model-trigger");
    await choose("Low", "task-effort-trigger");
    expect(screen.getByTestId("task-worker-model-trigger")).toHaveTextContent("Haiku");
    expect(screen.getByTestId("task-worker-effort-trigger")).toHaveTextContent("Low");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const created = mutateAsync.mock.calls[0][0];
    expect(created).toMatchObject({ modelOverride: "haiku", reasoningEffort: "low" });
    expect(readTriggerWorkerSettings(created.prompt)).toMatchObject({
      model: "haiku",
      effort: "low",
    });
  });

  // This full draft round-trip needs extra time with coverage on shared CI runners.
  it("restores schedule and source drafts without carrying conditions between integrations", async () => {
    async function chooseTimezone(zone: string) {
      await userEvent.click(screen.getByLabelText("Timezone", { exact: true }));
      fireEvent.change(screen.getByLabelText("Search timezones"), { target: { value: zone } });
      await userEvent.click(
        await screen.findByRole("menuitemradio", { name: zone.replaceAll("_", " ") }),
      );
    }

    render(dialog());
    type("task-name-input", "Team work");
    type("task-prompt-input", "Prepare the weekly report.");
    await choose("Every week", "schedule-preset-trigger");
    await chooseTimezone("America/New_York");
    await choose("Jira");
    expect(screen.getByTestId("task-name-input")).toHaveValue("Team work");
    expect(screen.getByTestId("task-trigger-instructions")).toHaveValue(
      "Prepare the weekly report.",
    );
    type("task-trigger-scope", "ES");
    type("task-trigger-instructions", "Triage the ticket.");
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    type("task-trigger-condition-0-value", "bug");
    await choose("Slack");
    expect(screen.getByTestId("task-trigger-scope")).toHaveValue("");
    expect(screen.getByTestId("task-trigger-instructions")).toHaveValue("Triage the ticket.");
    expect(screen.queryByTestId("task-trigger-condition-0-value")).not.toBeInTheDocument();
    type("task-trigger-scope", "support");
    type("task-trigger-instructions", "Summarize the message.");
    await choose("Jira");
    expect(screen.getByTestId("task-trigger-scope")).toHaveValue("ES");
    expect(screen.getByTestId("task-trigger-instructions")).toHaveValue("Triage the ticket.");
    expect(screen.getByTestId("task-trigger-condition-0-value")).toHaveValue("bug");
    await choose("Schedule");
    expect(screen.getByTestId("task-prompt-input")).toHaveValue("Prepare the weekly report.");
    expect(screen.getByTestId("schedule-preset-trigger")).toHaveTextContent("Every week");
    expect(screen.getByLabelText("Timezone", { exact: true })).toHaveTextContent(
      "America/New York",
    );
    expect(screen.getByTestId("task-session-mode-trigger")).toHaveTextContent(
      "New session each run",
    );
    await choose("Jira");
    expect(screen.queryByTestId("task-schedule-trigger")).not.toBeInTheDocument();
    expect(screen.getByTestId("task-trigger-scope")).toHaveValue("ES");
    await choose("Schedule");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].rrule).toContain("FREQ=WEEKLY");
    expect(mutateAsync.mock.calls[0][0].timezone).toBe("America/New_York");
    expect(mutateAsync.mock.calls[0][0]).not.toHaveProperty("templateId");
    expect(mutateAsync.mock.calls[0][0]).not.toHaveProperty("sessionMode");
  }, 10_000);

  it("attributes a switched gallery template to the selected source", async () => {
    render(dialog({ initialTrigger: "jira", initialTemplateId: "jira" }));
    expect(screen.getByLabelText("Trigger type")).toHaveTextContent("Jira");
    await choose("GitHub");
    expect(screen.getByTestId("task-name-input")).toHaveValue("GitHub");
    type("task-trigger-scope", "acme/app");
    type("task-trigger-instructions", "Investigate it.");
    await choose("Slack");
    await choose("GitHub");
    expect(screen.getByTestId("task-trigger-scope")).toHaveValue("acme/app");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({
      templateId: "github",
      prompt: expect.stringContaining("repo:acme/app"),
    });
    expect(decodeTriggerPrompt(mutateAsync.mock.calls[0][0].prompt)?.instructions).toBe(
      "Investigate it.",
    );
  });

  it("clears canceled drafts and selects the next entry point's default", async () => {
    const { rerender } = render(dialog());
    await choose("Slack");
    type("task-trigger-scope", "support");
    rerender(dialog({ open: false }));
    rerender(dialog({ initialTrigger: "linear" }));
    expect(screen.getByLabelText("Trigger type")).toHaveTextContent("Linear");
    await choose("Slack");
    expect(screen.getByTestId("task-trigger-scope")).toHaveValue("");
    rerender(dialog({ open: false }));
    rerender(dialog());
    expect(screen.getByLabelText("Trigger type")).toHaveTextContent("Schedule");
    expect(screen.getByTestId("task-prompt-input")).toHaveValue("");
  });

  it.each(["continuity", "orchestration"])(
    "disables integration types without %s",
    async (missing) => {
      render(
        <CapabilitiesProvider
          info={{
            ...SERVER_INFO_OFFLINE_FALLBACK,
            scheduled_task_session_modes: missing !== "continuity",
            features: { session_orchestration: missing !== "orchestration" },
          }}
        >
          <CreateScheduledTaskDialog open onOpenChange={vi.fn()} />
        </CapabilitiesProvider>,
      );
      fireEvent.keyDown(screen.getByLabelText("Trigger type"), { key: "Enter" });
      await screen.findByRole("listbox");
      for (const label of ["Jira", "Slack", "GitHub", "Linear", "PagerDuty"]) {
        expect(screen.getByRole("option", { name: label })).toHaveAttribute(
          "aria-disabled",
          "true",
        );
      }
      expect(screen.getByRole("option", { name: "Schedule" })).not.toHaveAttribute(
        "aria-disabled",
        "true",
      );
    },
  );

  it("shows each integration's logo in the Trigger type options and the selected value", async () => {
    render(dialog());
    const trigger = screen.getByLabelText("Trigger type");
    expect(trigger.querySelector("svg.lucide-clock")).not.toBeNull();
    fireEvent.keyDown(trigger, { key: "Enter" });
    await screen.findByRole("listbox");
    for (const option of ["schedule", "jira", "slack", "github", "linear", "pagerduty"]) {
      expect(screen.getByTestId(`task-trigger-type-${option}`).querySelector("svg")).not.toBeNull();
    }
    fireEvent.click(screen.getByRole("option", { name: "PagerDuty" }));
    expect(trigger).toHaveTextContent("PagerDuty");
    expect(trigger.querySelector("svg.lucide-clock")).toBeNull();
    expect(trigger.querySelector("svg")).not.toBeNull();
  });

  it("preserves the existing editor for saved automations", () => {
    render(dialog({ editingTask: scheduledTask() }));
    expect(screen.queryByLabelText("Trigger type")).not.toBeInTheDocument();
    expect(screen.getByTestId("task-prompt-input")).toHaveValue("Summarize overnight activity");
  });

  describe("editing a trigger's source", () => {
    const slackPrompt = updateTriggerWorkerSettings(
      renderTriggerPrompt(
        "slack",
        { ...emptyTriggerFields("slack"), scope: "support", instructions: "Answer the question." },
        { placement: "same_host", sessionLink: "https://example.test/c/CONVERSATION_ID" },
      ),
      { agentId: "ag_claude_native", model: "opus", effort: "", permissionMode: "" },
    )!;
    const slackTask = scheduledTask({
      name: "Slack messages",
      prompt: slackPrompt,
      sessionMode: "continue",
      sessionId: "poller",
      rrule: "FREQ=MINUTELY;INTERVAL=30",
    });

    it("offers only integrations, with the saved source selected and its prompt editor shown", async () => {
      render(dialog({ editingTask: slackTask }));
      expect(screen.getByLabelText("Trigger type")).toHaveTextContent("Slack");
      expect(screen.getByTestId("task-prompt-input")).toHaveValue("Answer the question.");
      expect(screen.queryByTestId("task-trigger-type-note")).not.toBeInTheDocument();
      fireEvent.keyDown(screen.getByLabelText("Trigger type"), { key: "Enter" });
      await screen.findByRole("listbox");
      expect(screen.queryByRole("option", { name: "Schedule" })).not.toBeInTheDocument();
      for (const label of ["Jira", "Slack", "GitHub", "Linear", "PagerDuty"]) {
        expect(screen.getByRole("option", { name: label })).toBeInTheDocument();
      }
    });

    it("saves the new source's trigger with the saved instructions and worker, in a new polling session", async () => {
      render(dialog({ editingTask: slackTask }));
      await choose("Jira");
      expect(screen.getByTestId("task-name-input")).toHaveValue("Jira tickets");
      expect(screen.getByTestId("task-trigger-scope")).toHaveValue("");
      expect(screen.getByTestId("task-trigger-instructions")).toHaveValue("Answer the question.");
      expect(screen.getByTestId("task-trigger-type-note")).toHaveTextContent(
        "Saving replaces the Slack trigger. The next run starts a new polling session that watches Jira.",
      );
      expect(screen.getByTestId("task-worker-agent-trigger")).toHaveTextContent("Claude Code");
      expect(screen.queryByTestId("schedule-preset-trigger")).not.toBeInTheDocument();
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      type("task-trigger-scope", "ES");
      // Instructions that repeat the section heading must not drop the saved workers.
      const instructions =
        "Answer the question.\n\nInstructions for each session:\nInclude this heading.";
      type("task-trigger-instructions", instructions);
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const input = updateMutateAsync.mock.calls[0][0].input;
      expect(triggerPromptSource(input.prompt)).toBe("jira");
      expect(input.prompt).toContain('project = "ES"');
      expect(slackPrompt).toContain("support");
      expect(input.prompt).not.toContain("support");
      expect(input.prompt).toContain(instructions);
      expect(input.prompt).toMatch(
        /call sys_session_create with agent_id "ag_claude_native", placement "same_host", title [^\n]+, model "opus", and a message/,
      );
      expect(input).toMatchObject({ name: "Jira tickets", sessionId: null });
      expect(input.rrule).toBe("FREQ=MINUTELY;INTERVAL=30");
      expect(input).not.toHaveProperty("sessionMode");
    });

    it("keeps sending a task-session trigger's matches to one task session", async () => {
      const taskSessionPrompt = renderTriggerPrompt(
        "slack",
        { ...emptyTriggerFields("slack"), scope: "support", instructions: "Answer the question." },
        {
          placement: "same_host",
          sessionLink: "https://example.test/c/CONVERSATION_ID",
          taskSessionMode: "continue",
          taskSessionTitle: "Support desk",
        },
      );
      render(
        dialog({
          editingTask: { ...slackTask, name: "Support desk", prompt: taskSessionPrompt },
        }),
      );
      await choose("Jira");
      expect(screen.getByLabelText("Instructions for the task session")).toHaveValue(
        "Answer the question.",
      );
      expect(screen.getByTestId("task-trigger-type-note")).toHaveTextContent(
        "Its first match starts a new task session.",
      );
      openAutomationSection("Run setup");
      expect(screen.getByTestId("task-top-level-controls")).toBeInTheDocument();
      expect(screen.queryByTestId("task-trigger-session-mode-field")).not.toBeInTheDocument();
      type("task-trigger-scope", "ES");
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const input = updateMutateAsync.mock.calls[0][0].input;
      expect(triggerPromptSource(input.prompt)).toBe("jira");
      expect(isTaskSessionTriggerPrompt(input.prompt)).toBe(true);
      expect(input.prompt).toContain('placement "same_host", title "Support desk"');
      expect(input).toMatchObject({ name: "Support desk", sessionId: null });
    });

    it("keeps each source's draft and restores the saved prompt for the original source", async () => {
      render(dialog({ editingTask: { ...slackTask, name: "Support desk" } }));
      await choose("GitHub");
      type("task-trigger-scope", "acme/app");
      type("task-trigger-instructions", "Answer the question quickly.");
      await choose("Linear");
      expect(screen.getByTestId("task-trigger-scope")).toHaveValue("");
      expect(screen.getByTestId("task-trigger-instructions")).toHaveValue(
        "Answer the question quickly.",
      );
      type("task-trigger-instructions", "Summarize the issue.");
      await choose("GitHub");
      expect(screen.getByTestId("task-trigger-scope")).toHaveValue("acme/app");
      expect(screen.getByTestId("task-trigger-instructions")).toHaveValue(
        "Answer the question quickly.",
      );
      await choose("Slack");
      expect(screen.queryByTestId("task-trigger-fields")).not.toBeInTheDocument();
      expect(screen.queryByTestId("task-trigger-type-note")).not.toBeInTheDocument();
      expect(screen.getByTestId("task-name-input")).toHaveValue("Support desk");
      expect(screen.getByTestId("task-prompt-input")).toHaveValue("Answer the question.");
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const input = updateMutateAsync.mock.calls[0][0].input;
      expect(input.prompt).toBe(slackPrompt);
      expect(input).not.toHaveProperty("sessionId");
    });

    it("keeps a worker harness chosen while switched when switching back", async () => {
      render(dialog({ editingTask: slackTask }));
      await choose("Jira");
      await choose("Polly", "task-worker-agent-trigger");
      await choose("Slack");
      expect(screen.getByTestId("task-worker-agent-trigger")).toHaveTextContent("Polly");
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const input = updateMutateAsync.mock.calls[0][0].input;
      expect(triggerPromptSource(input.prompt)).toBe("slack");
      expect(readTriggerWorkerSettings(input.prompt)).toMatchObject({ agentId: "ag_1" });
      expect(splitTriggerPrompt(input.prompt).userPrompt).toBe("Answer the question.");
      expect(input).not.toHaveProperty("sessionId");
    });

    it("keeps a worker permission mode chosen while switched when switching back", async () => {
      render(dialog({ editingTask: slackTask }, { session_create_permission_modes: true }));
      await choose("Jira");
      await choose("Auto", "task-worker-permission-trigger");
      await choose("Slack");
      expect(screen.getByTestId("task-worker-permission-trigger")).toHaveTextContent("Auto");
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const input = updateMutateAsync.mock.calls[0][0].input;
      expect(triggerPromptSource(input.prompt)).toBe("slack");
      // Only the mode changed, so the restored prompt must still pick it up.
      expect(readTriggerWorkerSettings(input.prompt)).toEqual({
        agentId: "ag_claude_native",
        model: "opus",
        effort: "",
        permissionMode: "auto",
      });
      expect(input).not.toHaveProperty("permissionMode");
    });

    it("keeps sandboxed workers beside a host poller on the new source", async () => {
      const managedPrompt = slackPrompt.replace('placement "same_host"', 'placement "managed"');
      render(dialog({ editingTask: { ...slackTask, prompt: managedPrompt } }));
      await choose("Jira");
      type("task-trigger-scope", "ES");
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const prompt = updateMutateAsync.mock.calls[0][0].input.prompt;
      expect(triggerPromptSource(prompt)).toBe("jira");
      expect(readTriggerWorkerPlacement(prompt)).toBe("managed");
    });

    it("keeps same-host workers beside a sandbox poller on the new source", async () => {
      vi.mocked(sandboxModelsHook.useSandboxModelOptions).mockReturnValue({
        data: { models: [], configured: false, status: "ready" },
      } as unknown as ReturnType<typeof sandboxModelsHook.useSandboxModelOptions>);
      render(
        <CapabilitiesProvider
          info={{
            ...SERVER_INFO_OFFLINE_FALLBACK,
            scheduled_task_session_modes: true,
            scheduled_task_subhour: true,
            automation_trigger_poll_minutes: 30,
            features: { automations_v2: true, session_orchestration: true },
            managed_sandboxes_enabled: true,
            sandbox_provider: "modal",
            sandbox_provider_capabilities: { modal: { default_models: true } },
          }}
        >
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            editingTask={{ ...slackTask, executionTarget: "managed_sandbox", hostId: null }}
          />
        </CapabilitiesProvider>,
      );
      await choose("Jira");
      type("task-trigger-scope", "ES");
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const prompt = updateMutateAsync.mock.calls[0][0].input.prompt;
      expect(triggerPromptSource(prompt)).toBe("jira");
      expect(readTriggerWorkerPlacement(prompt)).toBe("same_host");
    });

    it("always starts a new polling session after a switch", async () => {
      render(dialog({ editingTask: slackTask }));
      await choose("Jira");
      type("task-trigger-scope", "ES");
      fireEvent.keyDown(screen.getByTestId("task-session-mode-trigger"), { key: "Enter" });
      const startNewSession = screen.getByTestId("task-start-new-session");
      expect(startNewSession).toBeChecked();
      expect(startNewSession).toHaveAttribute("aria-disabled", "true");
      fireEvent.click(startNewSession);
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const input = updateMutateAsync.mock.calls[0][0].input;
      expect(triggerPromptSource(input.prompt)).toBe("jira");
      expect(input).toMatchObject({ sessionId: null });
    });

    it("keeps a source's form draft when switching away and back", async () => {
      render(dialog({ editingTask: slackTask }));
      await choose("GitHub");
      type("task-trigger-scope", "acme/app");
      type("task-trigger-instructions", "Answer the question in English.");
      await choose("Linear");
      expect(screen.getByTestId("task-trigger-scope")).toHaveValue("");
      await choose("GitHub");
      expect(screen.getByTestId("task-trigger-scope")).toHaveValue("acme/app");
      expect(screen.getByTestId("task-trigger-instructions")).toHaveValue(
        "Answer the question in English.",
      );
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const input = updateMutateAsync.mock.calls[0][0].input;
      expect(decodeTriggerPrompt(input.prompt)).toMatchObject({
        source: "github",
        scope: "acme/app",
        instructions: "Answer the question in English.",
      });
      expect(input).toMatchObject({ sessionId: null });
    });

    it("restores a headered trigger's saved form after switching away and back", async () => {
      const headered = scheduledTask({
        name: "Slack messages",
        prompt: encodeTriggerPrompt(
          {
            source: "slack",
            ...emptyTriggerFields("slack"),
            scope: "support",
            instructions: "Answer the question.",
            worker: {
              placement: "same_host",
              agentId: "ag_claude_native",
              model: "opus",
              effort: "",
              permissionMode: "",
            },
          },
          { sessionLink: "https://example.test/c/CONVERSATION_ID" },
        ),
        sessionMode: "continue",
        sessionId: "poller",
        rrule: "FREQ=MINUTELY;INTERVAL=30",
      });
      render(dialog({ editingTask: headered }));
      expect(screen.getByLabelText("Trigger type")).toHaveTextContent("Slack");
      type("task-trigger-instructions", "Answer in English.");
      await choose("Jira");
      expect(screen.getByTestId("task-trigger-instructions")).toHaveValue("Answer in English.");
      await choose("Slack");
      expect(screen.queryByTestId("task-trigger-type-note")).not.toBeInTheDocument();
      expect(screen.getByTestId("task-trigger-scope")).toHaveValue("support");
      expect(screen.getByTestId("task-trigger-instructions")).toHaveValue("Answer in English.");
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const input = updateMutateAsync.mock.calls[0][0].input;
      expect(decodeTriggerPrompt(input.prompt)).toMatchObject({
        source: "slack",
        scope: "support",
        instructions: "Answer in English.",
        worker: { agentId: "ag_claude_native", model: "opus" },
      });
      // Back on the saved source, the polling session is kept.
      expect(input).not.toHaveProperty("sessionId");
    });

    it.each([
      ["can't be split", `${slackPrompt}\n\nInstructions for each session:\nAlso thank them.`],
      [
        "has a hand-edited worker call",
        slackPrompt.replace('agent_id "ag_claude_native"', "agent_id chosen dynamically"),
      ],
    ])("doesn't offer a switch when the saved prompt %s", (_case, savedPrompt) => {
      expect(readTriggerWorkerSettings(savedPrompt)).toBeNull();
      render(dialog({ editingTask: { ...slackTask, prompt: savedPrompt } }));
      expect(screen.queryByTestId("task-trigger-type")).not.toBeInTheDocument();
      expect(screen.getByTestId("task-prompt-input")).toBeInTheDocument();
    });
  });
});

describe("CreateScheduledTaskDialog trigger template", () => {
  function renderTrigger(
    source: TriggerSource,
    extraInfo: Partial<typeof SERVER_INFO_OFFLINE_FALLBACK> = {},
    sessionLink = "https://omni.example.com/c/CONVERSATION_ID",
  ) {
    const onOpenChange = vi.fn();
    const result = render(
      <CapabilitiesProvider
        info={{
          ...SERVER_INFO_OFFLINE_FALLBACK,
          scheduled_task_session_modes: true,
          scheduled_task_trigger_headers: true,
          ...extraInfo,
        }}
      >
        <CreateScheduledTaskDialog
          open
          onOpenChange={onOpenChange}
          initialTrigger={source}
          sessionLink={sessionLink}
        />
      </CapabilitiesProvider>,
    );
    return { ...result, onOpenChange };
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

  function expectWarningMessage(testId: string, message: string) {
    expect(screen.getByTestId(testId)).toHaveAttribute(
      "aria-label",
      expect.stringContaining(message),
    );
  }

  function showMoreTriggerDetails() {
    fireEvent.focus(screen.getByRole("button", { name: "More trigger details" }));
    return screen.getByRole("tooltip");
  }

  async function submittedPrompt(): Promise<string> {
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    return mutateAsync.mock.calls[0][0].prompt;
  }

  it.each([
    { configured: undefined, subhour: true, minutes: 10 },
    { configured: 30, subhour: true, minutes: 30 },
    { configured: 240, subhour: true, minutes: 240 },
    { configured: 240, subhour: false, minutes: 240 },
    { configured: 10, subhour: false, minutes: 60 },
  ])(
    "uses server polling $configured with subhour support $subhour",
    async ({ configured, subhour, minutes }) => {
      renderTrigger("jira", {
        features: { automations_v2: true },
        scheduled_task_subhour: subhour,
        automation_trigger_poll_minutes: configured,
      });
      fillJira();
      expect(screen.queryByLabelText("Frequency")).not.toBeInTheDocument();
      expect(screen.queryByTestId("task-schedule-trigger")).not.toBeInTheDocument();
      expect(screen.queryByTestId("task-trigger-cadence")).not.toBeInTheDocument();
      const prompt = await submittedPrompt();
      expect(prompt).toContain(
        `On the first run, use ${minutes > 60 ? `${minutes} minutes` : "one hour"} ago and nothing pending.`,
      );
      expect(mutateAsync.mock.calls[0][0]).toMatchObject({
        rrule: minutes === 60 ? "FREQ=HOURLY;BYMINUTE=0" : `FREQ=MINUTELY;INTERVAL=${minutes}`,
        sessionMode: "continue",
      });
    },
  );

  it("starts a session per match unless advanced settings use one task session", async () => {
    renderTrigger("jira");
    fillJira();
    const sessionMode = screen.getByTestId("task-trigger-session-mode");
    expect(screen.getByTestId("task-trigger-session-mode-field")).toHaveTextContent(
      "Session continuity",
    );
    expect(sessionMode).toHaveTextContent("New session for each match");
    expect(screen.getByLabelText("Instructions for each session")).toHaveValue("Triage it.");
    expect(screen.queryByTestId("task-trigger-cadence")).not.toBeInTheDocument();
    expect(screen.queryByTestId("task-schedule-trigger")).not.toBeInTheDocument();

    chooseTriggerSessionMode("One task session");
    expect(sessionMode).toHaveTextContent("One task session for all matches");
    expect(screen.getByLabelText("Instructions for the task session")).toHaveValue("Triage it.");
    expect(screen.queryByTestId("task-trigger-cadence")).not.toBeInTheDocument();
    expect(screen.queryByTestId("task-schedule-trigger")).not.toBeInTheDocument();
    type("task-name-input", "Ticket triage");

    const prompt = await submittedPrompt();
    expect(isTaskSessionTriggerPrompt(prompt)).toBe(true);
    expect(prompt).toContain('title "Ticket triage"');
    expect(prompt).toContain("Instructions for the task session:\nTriage it.\n");
    // The polling session still continues; only matches share one task session.
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({ sessionMode: "continue" });
  });

  it("replaces the prompt with a trigger builder, runs hourly and keeps one session", () => {
    const { onOpenChange } = renderTrigger("jira");
    expect(screen.getByTestId("task-name-input")).toHaveValue("Jira tickets");
    expect(screen.queryByTestId("task-prompt-input")).toBeNull();
    expect(screen.getByTestId("task-trigger-event")).toHaveTextContent("Ticket created");
    expect(screen.getByTestId("task-trigger-builder")).toHaveTextContent("where project");
    expect(screen.queryByTestId("task-schedule-trigger")).not.toBeInTheDocument();
    // The trigger always keeps one session, so the form doesn't ask.
    expect(screen.queryByTestId("task-session-mode-field")).toBeNull();
    expect(screen.getByTestId("task-trigger-description")).toHaveTextContent(
      "Runs your automation on all new tickets that match these conditions.",
    );
    const triggerDetails = showMoreTriggerDetails();
    expect(triggerDetails).toHaveTextContent(
      "Older tickets are skipped, even if they are edited to match later.",
    );
    expect(within(triggerDetails).getByTestId("task-trigger-attribution-note")).toHaveTextContent(
      'Up to 10 matches per run; remaining matches wait for the next run · Posts include "Sent with Omnigent".',
    );
    fireEvent.keyDown(screen.getByRole("button", { name: "More trigger details" }), {
      key: "Escape",
    });
    const triggerCard = screen.getByTestId("task-trigger-card");
    expect(triggerCard).toContainElement(screen.getByTestId("task-trigger-description"));
    expect(triggerCard).toContainElement(screen.getByTestId("task-trigger-builder"));
    expect(triggerCard).toContainElement(screen.getByTestId("task-trigger-link-back"));
    expect(screen.queryByTestId("task-trigger-safety")).toBeNull();
    const triggerTab = screen.getByRole("tab", { name: "Trigger" });
    const instructionsTab = screen.getByRole("tab", { name: "Instructions" });
    expect(screen.getByTestId("task-trigger-instructions-required")).toBeInTheDocument();
    type("task-trigger-instructions", "Triage it.");
    expect(screen.queryByTestId("task-trigger-instructions-required")).toBeNull();
    type("task-trigger-instructions", "");
    expect(screen.getByTestId("task-trigger-instructions-required")).toBeInTheDocument();
    const tabs = screen.getByTestId("task-trigger-tabs");
    expect(tabs).toContainElement(triggerTab);
    expect(tabs).toContainElement(instructionsTab);
    expect(tabs).toContainElement(triggerCard);
    expect(triggerTab).toHaveAttribute("aria-selected", "true");
    expect(triggerCard).toBeVisible();
    expect(screen.getByTestId("task-trigger-instructions")).not.toBeVisible();
    const linkBack = screen.getByRole("checkbox", {
      name: "Comment the session link on the ticket",
    });
    expect(screen.getByTestId("task-trigger-untrusted-note")).toBeVisible();
    expect(screen.getByTestId("task-trigger-untrusted-note")).toHaveClass("text-muted-foreground");
    expect(screen.getByTestId("task-trigger-untrusted-note")).toHaveTextContent("Untrusted source");
    expectWarningMessage(
      "task-trigger-untrusted-note",
      "Anyone who can create a ticket in this project may direct the agent using your tools and permissions. Only use trusted sources.",
    );
    expectWarningMessage("task-trigger-untrusted-note", "Links are posted with your Jira account.");
    // Runs can only search where the agent has the service's tools.
    expect(screen.getByTestId("task-trigger-tools-note")).toHaveTextContent("MCP required");
    expectWarningMessage(
      "task-trigger-tools-note",
      "This only works if the agent has a Jira MCP server where this task runs. Without one, each run stops and says so in its reply.",
    );
    const toolsTag = screen.getByTestId("task-trigger-tools-note");
    fireEvent.focus(toolsTag);
    expect(screen.getByRole("tooltip")).toHaveTextContent(
      "This only works if the agent has a Jira MCP server where this task runs.",
    );
    fireEvent.keyDown(toolsTag, { key: "Escape" });
    expect(onOpenChange).not.toHaveBeenCalled();
    fireEvent.focus(instructionsTab);
    fireEvent.click(instructionsTab);
    expect(instructionsTab).toHaveAttribute("aria-selected", "true");
    expect(tabs).toContainElement(screen.getByTestId("task-trigger-instructions-panel"));
    expect(screen.getByTestId("task-trigger-instructions")).toBeVisible();
    expect(triggerCard).not.toBeVisible();
    fireEvent.focus(triggerTab);
    fireEvent.click(triggerTab);
    expect(linkBack).toBeChecked();
    expect(screen.getByTestId("task-trigger-builder")).not.toContainElement(linkBack);
    expect(screen.queryByRole("combobox", { name: "Then" })).toBeNull();
    fireEvent.click(linkBack);
    expect(linkBack).not.toBeChecked();
    expect(screen.getByTestId("task-trigger-untrusted-note")).toHaveAttribute(
      "aria-label",
      expect.not.stringContaining("Links are posted"),
    );
    fireEvent.click(screen.getByText("Comment the session link on the ticket"));
    expect(linkBack).toBeChecked();
    expectWarningMessage("task-trigger-untrusted-note", "Links are posted with your Jira account.");
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
    fireEvent.click(screen.getByRole("option", { name: "laptop" }));
    // On a connected host the agent may lack the tools, so the warning shows.
    expect(screen.getByTestId("task-trigger-tools-note")).toBeInTheDocument();
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
    expect(screen.queryByTestId("task-trigger-preview")).toBeNull();

    expect(screen.getByTestId("task-trigger-scope-operator")).toHaveTextContent("equals");
    type("task-trigger-scope", "ES, OPS");
    expect(submit).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("Equals matches one value");
    const scopeOperator = screen.getByTestId("task-trigger-scope-operator");
    fireEvent.pointerDown(
      scopeOperator,
      new MouseEvent("pointerdown", { bubbles: true, button: 0 }),
    );
    fireEvent.click(scopeOperator);
    fireEvent.click(screen.getByRole("option", { name: "in" }));
    expect(submit).toBeEnabled();
    expect(screen.queryByRole("alert")).toBeNull();

    // A new row starts empty and blocks saving until it's filled in or removed.
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    expect(submit).toBeDisabled();
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
    // The Instructions panel is hidden behind the Trigger tab, so the reason stays visible here.
    expect(screen.getByRole("tab", { name: "Trigger" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("task-trigger-too-long")).not.toBeVisible();
    expect(screen.getByTestId("task-trigger-instructions-invalid")).toBeInTheDocument();
    expect(screen.getByTestId("task-trigger-instructions-too-long-notice")).toBeVisible();
    expect(screen.getByTestId("task-trigger-instructions-too-long-notice")).toHaveTextContent(
      "Keep instructions to 2,000 characters or fewer",
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit instructions" }));
    expect(screen.getByRole("tab", { name: "Instructions" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByTestId("task-trigger-too-long")).toBeVisible();
    expect(screen.queryByTestId("task-trigger-instructions-too-long-notice")).toBeNull();
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

  it("filters on a Jira custom field named in its row", async () => {
    renderTrigger("jira");
    fillJira();
    const submit = screen.getByTestId("create-scheduled-task-submit");
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    expect(screen.queryByTestId("task-trigger-condition-0-name")).toBeNull();
    pick("task-trigger-condition-0-field", "task-trigger-condition-0-field-custom_field");
    type("task-trigger-condition-0-value", "Backend");
    // The value alone doesn't say which field to compare.
    expect(submit).toBeDisabled();
    expect(screen.getByTestId("task-trigger-empty-condition")).toBeInTheDocument();
    type("task-trigger-condition-0-name", "ES Component");
    expect(submit).toBeEnabled();

    expect(await submittedPrompt()).toContain('(project = "ES" AND "ES Component" = "Backend")');
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

  it("still starts sessions without a link-back when the checkbox is unchecked", async () => {
    renderTrigger("slack");
    expect(screen.getByTestId("task-trigger-event")).toHaveTextContent("Message posted");
    expectWarningMessage(
      "task-trigger-untrusted-note",
      "Anyone who can post in this channel may direct the agent",
    );
    type("task-trigger-scope", "#eng-support");
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    type("task-trigger-condition-0-value", "error");
    type("task-trigger-instructions", "Reproduce it.");
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "Include the session link with the result in the thread",
      }),
    );

    const prompt = await submittedPrompt();
    expect(mutateAsync.mock.calls[0][0].templateId).toBe("slack");
    expect(prompt).toContain(
      'Watch: new Slack messages in #eng-support whose text contains "error"',
    );
    expect(prompt).not.toContain("Omnigent session:");
    expect(prompt).toContain("call sys_session_create");
  });

  it("watches Slack DMs from listed senders, or one group DM, with prefixed scopes", async () => {
    renderTrigger("slack");
    expect(screen.getByTestId("task-trigger-scope-prefix")).toHaveTextContent("#");
    // Pasted channel names keep a single, visual "#".
    type("task-trigger-scope", "#eng-support");
    expect(screen.getByTestId("task-trigger-scope")).toHaveValue("eng-support");
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    pick("task-trigger-condition-0-field", "task-trigger-condition-0-field-author");

    pick("task-trigger-event", "task-trigger-event-dm");
    expect(screen.getByTestId("task-trigger-builder")).toHaveTextContent("where sender");
    expect(screen.getByTestId("task-trigger-scope-prefix")).toHaveTextContent("@");
    // Channel names aren't people, and Author repeats the sender.
    expect(screen.getByTestId("task-trigger-scope")).toHaveValue("");
    expect(screen.queryByTestId("task-trigger-condition-0")).not.toBeInTheDocument();
    expectWarningMessage(
      "task-trigger-untrusted-note",
      "Anyone who can send you a direct message from one of these accounts may direct the agent",
    );

    pick("task-trigger-event", "task-trigger-event-group_dm");
    expect(screen.getByTestId("task-trigger-builder")).toHaveTextContent("where group DM with");
    expect(screen.queryByTestId("task-trigger-scope-operator")).not.toBeInTheDocument();
    type("task-trigger-scope", "@alice");
    type("task-trigger-instructions", "Answer the question.");
    expect(screen.getByTestId("task-trigger-group-hint")).toBeVisible();
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    // A trailing comma or a repeated name still lists one person.
    for (const scope of ["alice,", "alice, @Alice"]) {
      type("task-trigger-scope", scope);
      expect(screen.getByTestId("task-trigger-group-hint")).toBeVisible();
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    }
    type("task-trigger-scope", "alice, @bob");
    expect(screen.getByTestId("task-trigger-scope")).toHaveValue("alice, bob");

    const prompt = await submittedPrompt();
    expect(prompt).toContain("Watch: new Slack messages in your group DM with @alice and @bob\n");
  });

  it("builds a Linear trigger for a team, with an unassigned row and the issue link-back", async () => {
    renderTrigger("linear");
    expect(screen.getByTestId("task-name-input")).toHaveValue("Linear issues");
    expect(screen.getByTestId("task-trigger-event")).toHaveTextContent("Issue created");
    expect(screen.getByTestId("task-trigger-builder")).toHaveTextContent("where team");
    expectWarningMessage(
      "task-trigger-untrusted-note",
      "Anyone who can create an issue in this team may direct the agent",
    );
    expectWarningMessage(
      "task-trigger-tools-note",
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

  it("builds a PagerDuty trigger for a service, with an urgency row and the note link-back", async () => {
    renderTrigger("pagerduty");
    expect(screen.getByTestId("task-name-input")).toHaveValue("PagerDuty incidents");
    expect(screen.getByTestId("task-trigger-event")).toHaveTextContent("Incident triggered");
    expect(screen.getByTestId("task-trigger-builder")).toHaveTextContent("where service");
    expectWarningMessage(
      "task-trigger-untrusted-note",
      "Anyone who can trigger an incident on this service may direct the agent",
    );
    expectWarningMessage(
      "task-trigger-tools-note",
      "This only works if the agent has a PagerDuty MCP server where this task runs.",
    );
    type("task-trigger-scope", "Checkout API");
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    type("task-trigger-condition-0-value", "high");
    type("task-trigger-instructions", "Find the likely cause.");

    const prompt = await submittedPrompt();
    expect(mutateAsync.mock.calls[0][0].templateId).toBe("pagerduty");
    expect(prompt).toContain(
      'Watch: new PagerDuty incidents on service Checkout API that have urgency "high"',
    );
    expect(prompt).toContain(
      'Add a note "Omnigent session: https://omni.example.com/c/CONVERSATION_ID" to each incident',
    );
  });

  it("matches PagerDuty services by name with contains, which other sources don't offer", async () => {
    function scopeOperatorOptions() {
      const scopeOperator = screen.getByTestId("task-trigger-scope-operator");
      fireEvent.pointerDown(
        scopeOperator,
        new MouseEvent("pointerdown", { bubbles: true, button: 0 }),
      );
      fireEvent.click(scopeOperator);
      return screen.getAllByRole("option").map((option) => option.textContent);
    }

    const { unmount } = renderTrigger("jira");
    expect(scopeOperatorOptions()).toEqual(["equals", "in"]);
    unmount();

    renderTrigger("pagerduty");
    expect(scopeOperatorOptions()).toEqual(["equals", "in", "contains"]);
    fireEvent.click(screen.getByRole("option", { name: "contains" }));
    expect(screen.getByTestId("task-trigger-scope-operator")).toHaveTextContent("contains");
    type("task-trigger-scope", "payments, checkout");
    type("task-trigger-instructions", "Find the likely cause.");
    expect(screen.getByRole("alert")).toHaveTextContent("Contains matches one piece of text.");
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    type("task-trigger-scope", "payments");
    expect(screen.queryByRole("alert")).toBeNull();

    const prompt = await submittedPrompt();
    expect(prompt).toContain(
      'Watch: new PagerDuty incidents on services whose name contains "payments"',
    );
  });

  it("watches review requests from a named reviewer, with link-back off by default", async () => {
    renderTrigger("github");
    expect(screen.queryByTestId("task-trigger-target")).toBeNull();
    pick("task-trigger-event", "task-trigger-event-review");
    expect(screen.getByTestId("task-trigger-event")).toHaveTextContent("Review requested");
    // The reviewer is part of the event sentence, defaulting to the owner.
    expect(screen.getByTestId("task-trigger-builder")).toHaveTextContent("from");
    expect(screen.getByTestId("task-trigger-target")).toHaveValue("me");
    expect(screen.getByTestId("task-trigger-description")).toHaveTextContent(
      "Runs your automation each time a matching review is requested on an open pull request that isn't a draft, including re-requests.",
    );
    expect(showMoreTriggerDetails()).toHaveTextContent(
      "Requests made before this automation existed are skipped.",
    );
    fireEvent.keyDown(screen.getByRole("button", { name: "More trigger details" }), {
      key: "Escape",
    });
    expect(
      screen.getByRole("checkbox", { name: "Comment the session link on it" }),
    ).not.toBeChecked();
    // Unchecking on an opened event survives a round trip through Review requested.
    const linkBack = () => screen.getByRole("checkbox", { name: "Comment the session link on it" });
    pick("task-trigger-event", "task-trigger-event-pr");
    fireEvent.click(linkBack());
    pick("task-trigger-event", "task-trigger-event-review");
    pick("task-trigger-event", "task-trigger-event-pr");
    expect(linkBack()).not.toBeChecked();
    pick("task-trigger-event", "task-trigger-event-review");
    // Assignee and the opened-only Reviewer filter don't apply to the queue.
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    fireEvent.keyDown(screen.getByTestId("task-trigger-condition-0-field"), { key: "Enter" });
    expect(screen.queryByTestId("task-trigger-condition-0-field-assignee")).toBeNull();
    expect(screen.queryByTestId("task-trigger-condition-0-field-reviewer")).toBeNull();
    fireEvent.keyDown(screen.getByTestId("task-trigger-condition-0-field"), { key: "Escape" });
    fireEvent.click(screen.getByTestId("task-trigger-condition-0-remove"));

    type("task-trigger-scope", "acme/app");
    type("task-trigger-instructions", "Review the pull request.");
    type("task-trigger-target", " ");
    expect(screen.getByTestId("task-trigger-target")).toHaveAttribute(
      "placeholder",
      "username or org/team",
    );
    expect(screen.getByTestId("task-trigger-target-hint")).toHaveTextContent(
      "Enter a reviewer: me, a username, or an org/team.",
    );
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    type("task-trigger-target", "octo cat");
    expect(screen.getByTestId("task-trigger-target-hint")).toBeVisible();
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    type("task-trigger-target", "me");
    const prompt = await submittedPrompt();
    expect(prompt).toContain(
      "gh api -X GET search/issues -f q='repo:acme/app is:pr is:open -is:draft user-review-requested:@me' -f per_page=100 --paginate",
    );
    expect(prompt).toContain('title "GitHub <owner/repo>#<number> review <event id>"');
    expect(prompt).not.toContain("created:>=");
    expect(prompt).not.toContain("Omnigent session:");
  });

  it("explains that a Reviewer filter on opened pull requests misses later requests", () => {
    renderTrigger("github");
    pick("task-trigger-event", "task-trigger-event-pr");
    expect(screen.getByTestId("task-trigger-description")).toHaveTextContent(
      "Runs your automation on all new pull requests that match these conditions.",
    );
    fireEvent.click(screen.getByTestId("task-trigger-add-condition"));
    expect(screen.queryByTestId("task-trigger-reviewer-hint")).toBeNull();
    pick("task-trigger-condition-0-field", "task-trigger-condition-0-field-reviewer");
    expect(screen.getByTestId("task-trigger-reviewer-hint")).toHaveTextContent(
      "Only matches reviewers requested before the next check after the pull request opens. To catch later requests and re-requests, choose Review requested.",
    );
  });

  it("watches Jira assignee changes to anyone when the assignee is cleared", async () => {
    renderTrigger("jira");
    pick("task-trigger-event", "task-trigger-event-assigned");
    expect(screen.getByTestId("task-trigger-builder")).toHaveTextContent("to");
    expect(screen.getByTestId("task-trigger-target")).toHaveValue("me");
    expect(screen.getByTestId("task-trigger-description")).toHaveTextContent(
      "Runs your automation each time a matching ticket is assigned, including when it is assigned again later.",
    );
    // Assignee changes keep link-back, which comments on the owner's own tickets.
    expect(
      screen.getByRole("checkbox", { name: "Comment the session link on the ticket" }),
    ).toBeChecked();
    type("task-trigger-scope", "ES");
    type("task-trigger-instructions", "Triage it.");
    type("task-trigger-target", "");
    expect(screen.queryByTestId("task-trigger-target-hint")).toBeNull();
    expect(screen.getByTestId("task-trigger-target")).toHaveAttribute("placeholder", "me or email");
    const prompt = await submittedPrompt();
    expect(prompt).toContain(
      '(project = "ES" AND assignee is not EMPTY) AND assignee CHANGED AFTER "-<A>m"',
    );
  });

  it.each([
    ["the server applies it", { session_create_permission_modes: true }, "Auto", true],
    ["the server would ignore it", {}, "Auto", false],
    // The server refuses an approval-skipping mode for a session an agent starts.
    [
      "it skips approval",
      { session_create_permission_modes: true, scheduled_task_approval_skipping_modes: true },
      "Bypass permissions",
      false,
    ],
  ])(
    "gives new workers the automation's permission mode unless %s",
    async (_case, extraInfo, permission, passed) => {
      renderTrigger("jira", extraInfo);
      if (permission !== "Auto") {
        fireEvent.keyDown(screen.getByTestId("task-permission-trigger"), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name: permission }));
      }
      type("task-trigger-scope", "ES");
      type("task-trigger-instructions", "Triage it.");
      const prompt = await submittedPrompt();
      const mode = mutateAsync.mock.calls[0][0].permissionMode;
      expect(mode).toBe(permission === "Auto" ? "auto" : "bypassPermissions");
      if (passed) {
        expect(prompt).toContain(
          'title "Jira <ticket key>", permission_mode "auto", and a message',
        );
      } else {
        expect(prompt).not.toContain("permission_mode");
      }
    },
  );

  it.each([
    // Claude lists its default mode as Manual, so no separate Default is offered.
    ["claude-native", "Bypass permissions", "bypassPermissions", "Manual", false],
    ["codex-native", "Full Access", "full-access", "Default", true],
  ])(
    "lets %s workers leave the polling session's mode for their harness's default",
    async (harness, pollingLabel, pollingMode, defaultLabel, offersDefault) => {
      vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
        data: [testAgent("selected-agent", `${harness}-ui`, { harness })],
      } as ReturnType<typeof agentsHook.useAvailableAgents>);
      renderTrigger("jira", {
        scheduled_task_harness_settings: true,
        session_create_permission_modes: true,
        scheduled_task_approval_skipping_modes: true,
      });
      fireEvent.keyDown(screen.getByTestId("task-permission-trigger"), { key: "Enter" });
      fireEvent.click(await screen.findByRole("option", { name: pollingLabel }));
      // Without a mode of their own, workers would inherit the polling session's.
      const workerPermission = () => screen.getByTestId("task-worker-permission-trigger");
      const pickWorkerPermission = async (name: string) => {
        fireEvent.keyDown(workerPermission(), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name }));
      };
      expect(workerPermission()).toHaveTextContent("Same as polling session");
      fireEvent.keyDown(workerPermission(), { key: "Enter" });
      await screen.findByRole("option", { name: defaultLabel });
      expect(screen.queryAllByRole("option", { name: "Default" })).toHaveLength(
        offersDefault ? 1 : 0,
      );
      fireEvent.click(screen.getByRole("option", { name: defaultLabel }));
      expect(workerPermission()).toHaveTextContent(defaultLabel);
      await pickWorkerPermission("Same as polling session");
      expect(workerPermission()).toHaveTextContent("Same as polling session");
      await pickWorkerPermission(defaultLabel);
      expect(screen.getByTestId("task-worker-settings")).toHaveTextContent(
        "Pick another mode so they don't run in the polling session's.",
      );
      type("task-trigger-scope", "ES");
      type("task-trigger-instructions", "Triage it.");
      const prompt = await submittedPrompt();
      expect(mutateAsync.mock.calls[0][0].permissionMode).toBe(pollingMode);
      expect(readTriggerWorkerSettings(prompt)?.permissionMode).toBe("default");
    },
  );

  it("keeps a worker Default chosen before the polling mode changes", async () => {
    vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
      data: [testAgent("selected-agent", "codex-native-ui", { harness: "codex-native" })],
    } as ReturnType<typeof agentsHook.useAvailableAgents>);
    renderTrigger("jira", {
      scheduled_task_harness_settings: true,
      session_create_permission_modes: true,
      scheduled_task_approval_skipping_modes: true,
    });
    const select = async (id: string, name: string) => {
      fireEvent.keyDown(screen.getByTestId(id), { key: "Enter" });
      fireEvent.click(await screen.findByRole("option", { name }));
    };
    await select("task-permission-trigger", "Default");
    await select("task-worker-permission-trigger", "Approve for me");
    await select("task-worker-permission-trigger", "Default");
    await select("task-permission-trigger", "Full Access");
    expect(screen.getByTestId("task-worker-permission-trigger")).toHaveTextContent("Default");
    type("task-trigger-scope", "ES");
    type("task-trigger-instructions", "Triage it.");
    const prompt = await submittedPrompt();
    expect(mutateAsync.mock.calls[0][0].permissionMode).toBe("full-access");
    expect(readTriggerWorkerSettings(prompt)?.permissionMode).toBe("default");
  });

  it("saves no header for a server that would send it to the model", async () => {
    renderTrigger("jira", { scheduled_task_trigger_headers: false });
    type("task-trigger-scope", "ES");
    type("task-trigger-instructions", "Triage it.");
    const submitted = await submittedPrompt();
    expect(hasTriggerHeader(submitted)).toBe(false);
    expect(isTriggerPrompt(submitted)).toBe(true);
    expect(submitted).toContain("Instructions for each session:\nTriage it.");
  });

  it("saves a GitHub pull request trigger with no way to edit its rendered prompt", async () => {
    renderTrigger("github");
    expect(screen.getByTestId("task-trigger-event")).toHaveTextContent("Issue opened");
    expectWarningMessage(
      "task-trigger-untrusted-note",
      "Anyone who can open an issue in this repository may direct the agent",
    );
    pick("task-trigger-event", "task-trigger-event-pr");
    expectWarningMessage(
      "task-trigger-untrusted-note",
      "Anyone who can open a pull request in this repository may direct the agent",
    );
    type("task-trigger-scope", "acme/app");
    type("task-trigger-instructions", "Fix it.");
    expect(screen.queryByTestId("task-trigger-edit-as-prompt")).toBeNull();
    expect(screen.queryByRole("button", { name: "System prompt" })).toBeNull();
    const submitted = await submittedPrompt();
    expect(submitted).toContain("repo:acme/app is:pr created:>=<that time>");
    expect(decodeTriggerPrompt(submitted)).toMatchObject({
      source: "github",
      event: "pr",
      scope: "acme/app",
      instructions: "Fix it.",
    });
    expect(mutateAsync.mock.calls[0][0].templateId).toBe("github");
  });
});

describe("editing template prompts", () => {
  function toggleStartNewSession() {
    // The checkbox keeps its menu open, so a second toggle finds it still showing.
    if (!screen.queryByTestId("task-start-new-session")) {
      closeTopLevelPopovers();
      fireEvent.keyDown(screen.getByTestId("task-session-mode-trigger"), { key: "Enter" });
    }
    fireEvent.click(screen.getByTestId("task-start-new-session"));
  }
  function withCodexAgent() {
    vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
      data: [
        ...AGENTS,
        testAgent("ag_codex", "codex-native-ui", {
          display_name: "Codex",
          harness: "codex-native",
        }),
      ],
    } as ReturnType<typeof agentsHook.useAvailableAgents>);
  }
  function templatePrompt(taskSessionMode: "new" | "continue" = "new") {
    return renderTriggerPrompt(
      "jira",
      { ...emptyTriggerFields("jira"), scope: "ENG", instructions: "Triage the ticket." },
      {
        placement: "same_host",
        sessionLink: "https://example.test/c/CONVERSATION_ID",
        taskSessionMode,
        taskSessionTitle: "Ticket triage",
      },
    );
  }
  const original = templatePrompt();

  it.each(["new", "continue"] as const)(
    "saves worker harness, model, and effort in %s mode without rebinding the polling session",
    async (taskSessionMode) => {
      const saved = templatePrompt(taskSessionMode);
      const task = scheduledTask({
        prompt: saved,
        sessionMode: "continue",
        sessionId: "poller",
      });
      const props = { onOpenChange: vi.fn(), editingTask: task };
      const { rerender } = render(<CreateScheduledTaskDialog open {...props} />, {
        expandAdvanced: false,
      });
      const select = async (id: string, name: string) => {
        fireEvent.keyDown(screen.getByTestId(id), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name }));
      };
      expect(screen.getByTestId("task-worker-agent-trigger")).toHaveTextContent("Polly");
      expect(screen.getByTestId("task-worker-agent-trigger")).toBeVisible();
      expect(screen.queryByTestId("task-trigger-session-mode-field")).not.toBeInTheDocument();
      expect(screen.getByTestId("task-worker-settings")).toHaveTextContent(
        taskSessionMode === "continue"
          ? "Applies to the next task session. The current task session and the polling session keep their settings"
          : "Applies to new sessions started for matching items.",
      );
      expect(screen.queryByTestId("task-worker-model-trigger")).not.toBeInTheDocument();
      await select("task-worker-agent-trigger", "Claude Code");
      openAutomationSection("Run setup");
      await select("task-worker-model-trigger", "Opus");
      await select("task-worker-effort-trigger", "High");
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const input = updateMutateAsync.mock.calls[0][0].input;
      expect(readTriggerWorkerSettings(input.prompt)).toEqual({
        agentId: "ag_claude_native",
        model: "opus",
        effort: "high",
        permissionMode: "",
      });
      for (const key of [
        "agentId",
        "modelOverride",
        "reasoningEffort",
        "sessionId",
        "permissionMode",
      ])
        expect(input).not.toHaveProperty(key);
      expect(splitTriggerPrompt(input.prompt).userPrompt).toBe("Triage the ticket.");
      expect(isTaskSessionTriggerPrompt(input.prompt)).toBe(taskSessionMode === "continue");
      rerender(<CreateScheduledTaskDialog open={false} {...props} />);
      rerender(
        <CreateScheduledTaskDialog
          open
          {...props}
          editingTask={{ ...task, prompt: input.prompt }}
        />,
      );
      expect(screen.getByTestId("task-worker-agent-trigger")).toHaveTextContent("Claude Code");
      openAutomationSection("Run setup");
      expect(screen.getByTestId("task-worker-model-trigger")).toHaveTextContent("Opus");
      expect(screen.getByTestId("task-worker-effort-trigger")).toHaveTextContent("High");
      await select("task-worker-agent-trigger", "Polly");
      expect(screen.queryByTestId("task-worker-model-trigger")).not.toBeInTheDocument();
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(2));
      expect(readTriggerWorkerSettings(updateMutateAsync.mock.calls[1][0].input.prompt)).toEqual({
        agentId: "ag_1",
        model: "",
        effort: "",
        permissionMode: "",
      });
    },
  );

  it("bases worker inheritance on the polling mode a reused session keeps", async () => {
    const saved = updateTriggerWorkerSettings(original, {
      agentId: "ag_claude_native",
      model: "",
      effort: "",
      permissionMode: "default",
    })!;
    const task = scheduledTask({
      prompt: saved,
      agentId: "ag_claude_native",
      permissionMode: "bypassPermissions",
      sessionMode: "continue",
      sessionId: "poller",
    });
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={task} />, {
      info: {
        scheduled_task_session_modes: true,
        session_create_permission_modes: true,
        scheduled_task_approval_skipping_modes: true,
      },
    });
    const select = async (id: string, name: string) => {
      fireEvent.keyDown(screen.getByTestId(id), { key: "Enter" });
      fireEvent.click(await screen.findByRole("option", { name }));
    };
    // A polling change discarded by unchecking leaves the poller in bypass.
    toggleStartNewSession();
    await select("task-polling-permission-trigger", "Default");
    toggleStartNewSession();
    expect(screen.getByTestId("task-polling-permission-trigger")).toHaveTextContent(
      "Bypass permissions",
    );
    expect(screen.getByTestId("task-worker-permission-trigger")).toHaveTextContent("Manual");
    await select("task-worker-permission-trigger", "Same as polling session");
    expect(screen.getByTestId("task-worker-permission-trigger")).toHaveTextContent(
      "Same as polling session",
    );
    fireEvent.keyDown(screen.getByTestId("task-worker-permission-trigger"), { key: "Enter" });
    await screen.findByRole("option", { name: "Manual" });
    expect(screen.queryByRole("option", { name: "Default" })).toBeNull();
    fireEvent.click(screen.getByRole("option", { name: "Manual" }));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const input = updateMutateAsync.mock.calls[0][0].input;
    expect(input).not.toHaveProperty("sessionId");
    expect(readTriggerWorkerSettings(input.prompt)?.permissionMode).toBe("default");
  });

  it("saves Same as polling session as no worker mode, keeping the poller", async () => {
    const saved = updateTriggerWorkerSettings(original, {
      agentId: "ag_claude_native",
      model: "",
      effort: "",
      permissionMode: "default",
    })!;
    const task = scheduledTask({
      prompt: saved,
      agentId: "ag_claude_native",
      permissionMode: "auto",
      sessionMode: "continue",
      sessionId: "poller",
    });
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={task} />, {
      info: { scheduled_task_session_modes: true, session_create_permission_modes: true },
    });
    fireEvent.keyDown(screen.getByTestId("task-worker-permission-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Same as polling session" }));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const input = updateMutateAsync.mock.calls[0][0].input;
    // Workers with no mode of their own run in the polling session's.
    expect(readTriggerWorkerSettings(input.prompt)?.permissionMode).toBe("");
    expect(input.prompt).not.toContain("permission_mode");
    for (const key of ["sessionId", "permissionMode"]) expect(input).not.toHaveProperty(key);
  });

  it("changes the polling session's settings on edit only for a new polling session", async () => {
    const saved = updateTriggerWorkerSettings(original, {
      agentId: "ag_claude_native",
      model: "opus",
      effort: "high",
      permissionMode: "",
    })!;
    const task = scheduledTask({
      prompt: saved,
      agentId: "ag_claude_native",
      modelOverride: "sonnet",
      sessionMode: "continue",
      sessionId: "poller",
    });
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={task} />, {
      info: { scheduled_task_session_modes: true },
    });
    const select = async (id: string, name: string) => {
      fireEvent.keyDown(screen.getByTestId(id), { key: "Enter" });
      fireEvent.click(await screen.findByRole("option", { name }));
    };
    expect(screen.getByTestId("task-polling-settings")).toHaveTextContent(
      "The current polling session keeps these. Check Start a new session on the next run to change them.",
    );
    expect(screen.getByTestId("task-polling-model-trigger")).toHaveTextContent("Sonnet");
    for (const field of ["model", "effort", "permission"])
      expect(screen.getByTestId(`task-polling-${field}-trigger`)).toBeDisabled();
    // A trigger's top-row agent and permission pickers stay hidden on edit.
    expect(screen.queryByTestId("task-agent-picker")).not.toBeInTheDocument();
    expect(screen.queryByTestId("task-permission-trigger")).not.toBeInTheDocument();
    expect(screen.getByTestId("task-matching-settings")).toHaveTextContent("Matching sessions");
    expect(screen.getByTestId("task-worker-model-trigger")).toHaveTextContent("Opus");
    expect(screen.getByTestId("task-worker-effort-trigger")).toHaveTextContent("High");
    toggleStartNewSession();
    expect(screen.getByTestId("task-polling-settings")).toHaveTextContent(
      "The next run starts a new polling session with these.",
    );
    await select("task-polling-model-trigger", "Haiku");
    await select("task-polling-effort-trigger", "Low");
    await select("task-polling-permission-trigger", "Accept edits");
    expect(screen.getByTestId("task-worker-model-trigger")).toHaveTextContent("Opus");
    expect(screen.getByTestId("task-worker-effort-trigger")).toHaveTextContent("High");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const input = updateMutateAsync.mock.calls[0][0].input;
    expect(input).toMatchObject({
      modelOverride: "haiku",
      reasoningEffort: "low",
      permissionMode: "acceptEdits",
      sessionId: null,
    });
    expect(input).not.toHaveProperty("agentId");
    expect(readTriggerWorkerSettings(input.prompt)).toEqual({
      agentId: "ag_claude_native",
      model: "opus",
      effort: "high",
      permissionMode: "",
    });
  });

  it.each([
    ["ag_claude_native", "auto", "Auto"],
    // Hand-written, so no picker offers it, but it still overrides the agent's own opt-ins.
    ["ag_codex", "default", "Default"],
  ])(
    "lets a saved %s worker mode %s be cleared on a server that can't apply it",
    async (agentId, permissionMode, label) => {
      const saved = updateTriggerWorkerSettings(original, {
        agentId,
        model: "",
        effort: "",
        permissionMode,
      })!;
      withCodexAgent();
      const task = scheduledTask({ prompt: saved, sessionMode: "continue", sessionId: "poller" });
      render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={task} />);
      expect(screen.queryByTestId("task-worker-permission-trigger")).not.toBeInTheDocument();
      expect(screen.getByTestId("task-worker-permission-unapplied")).toHaveTextContent(
        `Matching sessions are set to start in ${label}, which this server can't apply`,
      );
      fireEvent.click(screen.getByTestId("task-worker-permission-clear"));
      expect(screen.queryByTestId("task-worker-permission-unapplied")).not.toBeInTheDocument();
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const input = updateMutateAsync.mock.calls[0][0].input;
      expect(readTriggerWorkerSettings(input.prompt)).toEqual({
        agentId,
        model: "",
        effort: "",
        permissionMode: "",
      });
      expect(input.prompt).not.toContain("permission_mode");
    },
  );

  it.each([
    // Claude's mode, written by hand into a Codex worker call.
    ["auto", "Matching sessions are set to start in auto, which this harness doesn't offer here"],
    // Codex's own default is valid; it overrides the agent's opt-ins, so nothing to warn about.
    ["default", null],
  ])(
    "flags a saved Codex worker mode %s only when Codex doesn't offer it",
    async (permissionMode, warning) => {
      withCodexAgent();
      const saved = updateTriggerWorkerSettings(original, {
        agentId: "ag_codex",
        model: "",
        effort: "",
        permissionMode,
      })!;
      const task = scheduledTask({ prompt: saved, sessionMode: "continue", sessionId: "poller" });
      render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={task} />, {
        info: { session_create_permission_modes: true },
      });
      if (warning === null) {
        expect(screen.queryByTestId("task-worker-permission-unapplied")).not.toBeInTheDocument();
        return;
      }
      expect(screen.getByTestId("task-worker-permission-unapplied")).toHaveTextContent(warning);
      fireEvent.click(screen.getByTestId("task-worker-permission-clear"));
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      expect(updateMutateAsync.mock.calls[0][0].input.prompt).not.toContain("permission_mode");
    },
  );

  it("clears the workers' permission mode when their harness changes", async () => {
    withCodexAgent();
    const task = scheduledTask({ prompt: original, sessionMode: "continue", sessionId: "poller" });
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={task} />, {
      info: { session_create_permission_modes: true },
    });
    const select = async (id: string, name: string) => {
      fireEvent.keyDown(screen.getByTestId(id), { key: "Enter" });
      fireEvent.click(await screen.findByRole("option", { name }));
    };
    await select("task-worker-agent-trigger", "Claude Code");
    await select("task-worker-permission-trigger", "Auto");
    // Claude's Auto isn't a Codex mode.
    await select("task-worker-agent-trigger", "Codex");
    expect(screen.getByTestId("task-worker-permission-trigger")).toHaveTextContent("Default");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const input = updateMutateAsync.mock.calls[0][0].input;
    expect(readTriggerWorkerSettings(input.prompt)).toMatchObject({ permissionMode: "" });
    expect(input.prompt).not.toContain("permission_mode");
    expect(input).not.toHaveProperty("permissionMode");
  });

  it.each([true, false])(
    "saves the workers' permission mode, offering only modes that keep approval (server applies it: %s)",
    async (supported) => {
      const task = scheduledTask({
        prompt: original,
        sessionMode: "continue",
        sessionId: "poller",
      });
      render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={task} />, {
        info: { session_create_permission_modes: supported },
      });
      fireEvent.keyDown(screen.getByTestId("task-worker-agent-trigger"), { key: "Enter" });
      fireEvent.click(await screen.findByRole("option", { name: "Claude Code" }));
      if (!supported) {
        expect(screen.queryByTestId("task-worker-permission-trigger")).not.toBeInTheDocument();
        return;
      }
      expect(screen.getByTestId("task-worker-settings")).toHaveTextContent(
        "Modes that skip approval aren't available here.",
      );
      fireEvent.keyDown(screen.getByTestId("task-worker-permission-trigger"), { key: "Enter" });
      expect(await screen.findByRole("option", { name: "Auto" })).toBeInTheDocument();
      // With no polling mode, workers without one start in their agent's default.
      expect(screen.queryByRole("option", { name: "Same as polling session" })).toBeNull();
      // The server refuses these for a session an agent starts.
      for (const name of ["Bypass permissions", "Don't ask"])
        expect(screen.queryByRole("option", { name })).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("option", { name: "Auto" }));
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      const input = updateMutateAsync.mock.calls[0][0].input;
      expect(readTriggerWorkerSettings(input.prompt)).toEqual({
        agentId: "ag_claude_native",
        model: "",
        effort: "",
        permissionMode: "auto",
      });
      // The polling session's own mode is unchanged.
      expect(input).not.toHaveProperty("permissionMode");
    },
  );

  it("keeps polling settings unchanged while clearing and restoring Instructions", async () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          prompt: original,
          sessionMode: "continue",
          sessionId: "poller",
        })}
      />,
    );
    const editor = screen.getByRole("textbox", { name: "Instructions" }) as HTMLTextAreaElement;
    const saved = editor.value;
    fireEvent.change(editor, { target: { value: "" } });
    const pollerPicker = screen.queryByTestId("pick-harness-claude");
    if (pollerPicker) fireEvent.click(pollerPicker);
    fireEvent.change(editor, { target: { value: saved } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const input = updateMutateAsync.mock.calls[0][0].input;
    expect(input.prompt).toBe(original);
    for (const key of [
      "agentId",
      "sessionId",
      "modelOverride",
      "reasoningEffort",
      "permissionMode",
    ])
      expect(input).not.toHaveProperty(key);
    expect(pollerPicker).toBeNull();
  });

  it.each(
    [true, false].flatMap((workerReady) =>
      (
        [
          { change: "none", placement: "managed" },
          { change: "host", placement: "managed" },
          { change: "host", placement: "same_host" },
          { change: "sandbox", placement: "same_host" },
        ] as const
      ).map((context) => ({ workerReady, ...context })),
    ),
  )(
    "validates worker placement $placement after poller target change=$change (worker ready: $workerReady)",
    async ({ workerReady, change, placement }) => {
      vi.mocked(sandboxModelsHook.useSandboxModelOptions).mockImplementation(
        (_provider, _harness, agentId) =>
          ({
            data: {
              models: [],
              configured: false,
              status: agentId === "ag_claude_native" && workerReady ? "ready" : "unsupported",
              error: "This harness is unavailable in the sandbox.",
            },
          }) as unknown as ReturnType<typeof sandboxModelsHook.useSandboxModelOptions>,
      );
      render(
        <CapabilitiesProvider
          info={{
            ...SERVER_INFO_OFFLINE_FALLBACK,
            features: { automations_v2: true },
            managed_sandboxes_enabled: true,
            sandbox_provider: "modal",
            sandbox_provider_capabilities: { modal: { default_models: true } },
          }}
        >
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            editingTask={scheduledTask({
              prompt: original.replace('placement "same_host"', `placement "${placement}"`),
              executionTarget: change === "sandbox" ? "connected_host" : "managed_sandbox",
              hostId: change === "sandbox" ? "host_1" : null,
              sessionMode: "continue",
              sessionId: "poller",
            })}
          />
        </CapabilitiesProvider>,
      );
      if (change !== "sandbox")
        expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      if (change === "host") {
        fireEvent.keyDown(screen.getByTestId("task-host-trigger"), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name: "laptop" }));
      }
      fireEvent.keyDown(screen.getByTestId("task-worker-agent-trigger"), { key: "Enter" });
      fireEvent.click(await screen.findByRole("option", { name: "Claude Code" }));
      const submit = screen.getByTestId("create-scheduled-task-submit");
      if (change === "sandbox") {
        fireEvent.keyDown(screen.getByTestId("task-host-trigger"), { key: "Enter" });
        fireEvent.click(await screen.findByTestId("task-host-sandbox-option"));
        expect(submit).toBeDisabled();
        expect(screen.getByText(/Polling session cannot start:/)).toHaveTextContent(
          "This harness is unavailable in the sandbox.",
        );
        fireEvent.click(submit);
        expect(updateMutateAsync).not.toHaveBeenCalled();
        fireEvent.keyDown(screen.getByTestId("task-host-trigger"), { key: "Enter" });
        fireEvent.click(await screen.findByRole("option", { name: "laptop" }));
        expect(screen.queryByText(/Polling session cannot start:/)).not.toBeInTheDocument();
      }
      if (workerReady || (change !== "none" && placement === "same_host")) {
        expect(submit).toBeEnabled();
        fireEvent.click(submit);
        await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
        const input = updateMutateAsync.mock.calls[0][0].input;
        expect(readTriggerWorkerSettings(input.prompt)?.agentId).toBe("ag_claude_native");
        expect(input.prompt).toContain(`placement "${placement}"`);
        expect(input).not.toHaveProperty("agentId");
        expect(input).not.toHaveProperty("sessionId");
      } else {
        expect(submit).toBeDisabled();
        expect(screen.getByRole("alert")).toHaveTextContent(
          "This harness is unavailable in the sandbox.",
        );
        fireEvent.click(submit);
        expect(updateMutateAsync).not.toHaveBeenCalled();
      }
    },
  );

  it("saves a customized creation call unchanged without offering worker controls", async () => {
    const customized = original.replace(
      "agent_id set to your agent ID",
      "agent_id chosen dynamically",
    );
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          prompt: customized,
          agentId: "ag_claude_native",
          sessionMode: "continue",
          sessionId: "poller",
        })}
      />,
    );
    expect(screen.queryByTestId("task-worker-settings")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "System prompt" })).toBeNull();
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const input = updateMutateAsync.mock.calls[0][0].input;
    expect(input.prompt).toBe(customized);
    expect(input).not.toHaveProperty("agentId");
    expect(input).not.toHaveProperty("sessionId");
  });

  it("keeps a hand-written heading editable for unrelated changes", async () => {
    const prompt = "Write a training document.\n\nInstructions for each session:\n";
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ prompt })}
      />,
    );
    expect(screen.getByLabelText("Instructions", { exact: true })).toHaveValue(prompt);
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

  it("saves and reopens an Instructions edit that changes the template structure", async () => {
    const props = { onOpenChange: vi.fn(), editingTask: scheduledTask({ prompt: original }) };
    const { rerender } = render(<CreateScheduledTaskDialog open {...props} />);
    expect(screen.queryByText(/These edits change the template structure/)).not.toBeInTheDocument();
    const editor = screen.getByRole("textbox", { name: "Instructions" }) as HTMLTextAreaElement;
    const savedValue = editor.value;
    const changedValue = `${savedValue}\n\nInstructions for each session:\nExtra heading.`;
    fireEvent.change(editor, { target: { value: changedValue } });
    expect(screen.getByText(/After saving, the full text will appear as one prompt/)).toBeVisible();
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
    fireEvent.change(editor, { target: { value: savedValue } });
    expect(screen.queryByText(/These edits change the template structure/)).not.toBeInTheDocument();
    fireEvent.change(editor, { target: { value: changedValue } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const saved = updateMutateAsync.mock.calls[0][0].input.prompt;
    expect(saved).toContain("Extra heading.");
    expect(saved).toContain("Triage the ticket.");
    rerender(<CreateScheduledTaskDialog open={false} {...props} />);
    rerender(
      <CreateScheduledTaskDialog
        open
        {...props}
        editingTask={scheduledTask({
          prompt: saved,
          agentId: "ag_claude_native",
          sessionMode: "continue",
          sessionId: "conv_1",
        })}
      />,
    );
    expect(screen.getByRole("textbox", { name: "Instructions" })).toHaveValue(saved);
    expect(screen.queryByRole("button", { name: "System prompt" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Instructions" })).toBeVisible();
  });

  it("edits only the instructions of an older trigger, keeping its hidden template text", async () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ prompt: original })}
      />,
    );
    expect(screen.getByLabelText("Instructions")).toHaveValue("Triage the ticket.");
    expect(screen.queryByRole("button", { name: "System prompt" })).toBeNull();
    expect(screen.queryByText(/This automation watches Jira/)).toBeNull();
    for (const value of ["", "  \n"]) {
      fireEvent.change(screen.getByLabelText("Instructions"), { target: { value } });
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      expect(screen.getByText("Instructions can't be empty.")).toBeVisible();
      expect(screen.queryByText(/After saving, the full text/)).not.toBeInTheDocument();
    }
    fireEvent.change(screen.getByLabelText("Instructions"), {
      target: { value: "Investigate it." },
    });
    expect(screen.queryByText("Instructions can't be empty.")).not.toBeInTheDocument();
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input.prompt).toBe(
      original.replace("Triage the ticket.", "Investigate it."),
    );
  });
});

describe("editing triggers saved with a header", () => {
  const saved = encodeTriggerPrompt(
    {
      source: "slack",
      ...emptyTriggerFields("slack"),
      scope: "abc",
      conditions: [{ field: "text", operator: "contains", value: "hi" }],
      instructions: "Reply with a greeting.",
      worker: { placement: "same_host", agentId: null, model: "", effort: "", permissionMode: "" },
    },
    { sessionLink: "https://example.test/c/CONVERSATION_ID" },
  );
  const task = scheduledTask({ prompt: saved, sessionMode: "continue", sessionId: "poller" });

  it("reopens in the trigger form and saves an edited condition", async () => {
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={task} />);
    expect(screen.getByTestId("task-trigger-fields")).toBeVisible();
    expect(screen.getByTestId("task-trigger-scope")).toHaveValue("abc");
    expect(screen.getByTestId("task-trigger-condition-0-value")).toHaveValue("hi");
    expect(screen.getByTestId("task-trigger-instructions")).toHaveValue("Reply with a greeting.");
    expect(screen.queryByTestId("task-prompt-input")).toBeNull();
    expect(screen.queryByRole("button", { name: "System prompt" })).toBeNull();
    expect(screen.queryByTestId("task-trigger-preview")).toBeNull();

    fireEvent.change(screen.getByTestId("task-trigger-condition-0-value"), {
      target: { value: "hello" },
    });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const input = updateMutateAsync.mock.calls[0][0].input;
    expect(decodeTriggerPrompt(input.prompt)?.conditions).toEqual([
      { field: "text", operator: "contains", value: "hello" },
    ]);
    expect(stripTriggerHeader(input.prompt)).toContain('whose text contains "hello"');
    expect(stripTriggerHeader(input.prompt)).not.toContain("omnigent-trigger");
    for (const key of ["agentId", "sessionId", "modelOverride", "reasoningEffort"])
      expect(input).not.toHaveProperty(key);
  });

  it("opens a header edited outside the form as its full prompt and keeps that edit", async () => {
    const edited = saved.replace(
      "Instructions for each session:\nReply with a greeting.",
      "Instructions for each session:\nDo nothing.",
    );
    const props = { onOpenChange: vi.fn(), editingTask: { ...task, prompt: edited } };
    render(<CreateScheduledTaskDialog open {...props} />);
    expect(screen.queryByTestId("task-trigger-fields")).toBeNull();
    const editor = screen.getByRole("textbox", { name: "Instructions" });
    expect(editor).toHaveValue(stripTriggerHeader(edited));
    expect((editor as HTMLTextAreaElement).value).not.toContain("omnigent-trigger");
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const prompt = updateMutateAsync.mock.calls[0][0].input.prompt;
    expect(prompt).toBe(stripTriggerHeader(edited).trim());
    expect(prompt).toContain("Do nothing.");
    expect(prompt).not.toContain("Reply with a greeting.");
  });

  it("offers a fresh polling session without the new-session switch", async () => {
    render(
      <CapabilitiesProvider
        info={{ ...SERVER_INFO_OFFLINE_FALLBACK, scheduled_task_session_modes: true }}
      >
        <CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={task} />
      </CapabilitiesProvider>,
    );
    // Session continuity offers only the reset; the trigger keeps one polling session.
    fireEvent.keyDown(screen.getByTestId("task-session-mode-trigger"), { key: "Enter" });
    expect(screen.queryByRole("menuitemradio", { name: "New session each run" })).toBeNull();
    fireEvent.click(screen.getByTestId("task-start-new-session"));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({ sessionId: null });
  });

  it("saves worker settings into the header and reopens with them", async () => {
    const props = { onOpenChange: vi.fn(), editingTask: task };
    const { rerender } = render(<CreateScheduledTaskDialog open {...props} />);
    const select = async (id: string, name: string) => {
      fireEvent.keyDown(screen.getByTestId(id), { key: "Enter" });
      fireEvent.click(await screen.findByRole("option", { name }));
    };
    await select("task-worker-agent-trigger", "Claude Code");
    await select("task-worker-model-trigger", "Opus");
    await select("task-worker-effort-trigger", "High");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const prompt = updateMutateAsync.mock.calls[0][0].input.prompt;
    expect(decodeTriggerPrompt(prompt)?.worker).toEqual({
      placement: "same_host",
      agentId: "ag_claude_native",
      model: "opus",
      effort: "high",
      permissionMode: "",
    });
    expect(prompt).toContain('agent_id "ag_claude_native", placement "same_host"');
    expect(prompt).toContain('model "opus", reasoning_effort "high"');

    rerender(<CreateScheduledTaskDialog open={false} {...props} />);
    rerender(<CreateScheduledTaskDialog open {...props} editingTask={{ ...task, prompt }} />);
    expect(screen.getByTestId("task-worker-agent-trigger")).toHaveTextContent("Claude Code");
    expect(screen.getByTestId("task-worker-model-trigger")).toHaveTextContent("Opus");
    expect(screen.getByTestId("task-worker-effort-trigger")).toHaveTextContent("High");
    expect(screen.getByTestId("task-trigger-condition-0-value")).toHaveValue("hi");
  });
});

describe("scheduler capabilities", () => {
  it("offers an explicit replacement for unsupported hidden trigger schedules", async () => {
    const prompt = renderTriggerPrompt(
      "jira",
      { ...emptyTriggerFields("jira"), scope: "ES", instructions: "Triage it." },
      { placement: "same_host", sessionLink: "https://omni.example.com/c/CONVERSATION_ID" },
    );
    render(
      <CapabilitiesProvider
        info={{ ...SERVER_INFO_OFFLINE_FALLBACK, scheduled_task_session_modes: true }}
      >
        <CreateScheduledTaskDialog
          open
          onOpenChange={vi.fn()}
          editingTask={scheduledTask({
            prompt,
            sessionMode: "continue",
            rrule: "FREQ=HOURLY;BYMINUTE=0;COUNT=3",
          })}
        />
      </CapabilitiesProvider>,
      { expandAdvanced: false },
    );
    expect(screen.queryByRole("button", { name: "Schedule" })).not.toBeInTheDocument();
    expect(screen.getByTestId("task-worker-agent-trigger")).toBeVisible();
    expect(screen.queryByTestId("task-ending-trigger")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "saved schedule isn't supported by the current run setup",
    );
    expect(screen.queryByLabelText("Schedule ends")).not.toBeInTheDocument();
    const submit = screen.getByTestId("create-scheduled-task-submit");
    expect(submit).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Use supported schedule" }));
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input.rrule).toBe("FREQ=HOURLY;BYMINUTE=0");
    expect(updateMutateAsync.mock.calls[0][0].input).not.toHaveProperty("sessionMode");
  });

  it("preserves a saved trigger cadence while capabilities resolve", async () => {
    const prompt = renderTriggerPrompt(
      "jira",
      { ...emptyTriggerFields("jira"), scope: "ES", instructions: "Triage it." },
      { placement: "same_host", sessionLink: "https://omni.example.com/c/CONVERSATION_ID" },
    );
    const dialog = (
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          prompt,
          sessionMode: "continue",
          rrule: "FREQ=MINUTELY;INTERVAL=10",
        })}
      />
    );
    const view = render(<CapabilitiesProvider info="loading">{dialog}</CapabilitiesProvider>, {
      expandAdvanced: false,
    });
    expect(screen.queryByTestId("task-trigger-schedule-replacement")).not.toBeInTheDocument();
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Instructions"), {
      target: { value: "Triage it and summarize the result." },
    });

    view.rerender(
      <CapabilitiesProvider
        info={{
          ...SERVER_INFO_OFFLINE_FALLBACK,
          features: { automations_v2: true },
          scheduled_task_subhour: true,
          scheduled_task_session_modes: true,
          scheduled_task_harness_settings: true,
        }}
      >
        {dialog}
      </CapabilitiesProvider>,
    );

    const submit = screen.getByTestId("create-scheduled-task-submit");
    await waitFor(() => expect(submit).toBeEnabled());
    expect(screen.queryByTestId("task-trigger-schedule-replacement")).not.toBeInTheDocument();
    fireEvent.click(submit);
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input.rrule).toBe("FREQ=MINUTELY;INTERVAL=10");
  });

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
      expect(
        within(screen.getByLabelText("Frequency"))
          .getAllByRole("radio")
          .map((option) => option.textContent),
      ).toEqual(["Every hour", "Every day", "Every weekday", "Every week"]);
      selectSchedulePreset("Every hour");
      openTopLevelPopover("task-ending-trigger");
      expect(
        within(screen.getByLabelText("Schedule ends"))
          .getAllByRole("menuitemradio")
          .map((option) => option.textContent),
      ).toEqual(["Never", "On date"]);
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
      const dialog = (
        <CreateScheduledTaskDialog
          open
          onOpenChange={vi.fn()}
          editingTask={scheduledTask({ rrule: rule })}
        />
      );
      const view = render(<CapabilitiesProvider info={info}>{dialog}</CapabilitiesProvider>);
      expect(screen.getByRole("alert")).toHaveTextContent(
        "This server does not support custom schedules or occurrence limits",
      );
      fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Renamed" } });
      expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
      expect(updateMutateAsync).not.toHaveBeenCalled();
      for (const [index, kind] of replacements.entries()) {
        if (kind === "custom") {
          openAutomationSection("Schedule");
          expect(screen.getByRole("radio", { name: "Custom schedule" })).toBeDisabled();
          selectSchedulePreset("Every day");
        } else {
          openTopLevelPopover("task-ending-trigger");
          expect(screen.getByLabelText("Occurrences")).toHaveValue(3);
          expect(screen.getByLabelText("Occurrences")).toBeDisabled();
          fireEvent.click(screen.getByRole("menuitemradio", { name: "Never" }));
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
      if (enabled === "loading") {
        expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
        view.rerender(
          <CapabilitiesProvider info={{ ...SERVER_INFO_OFFLINE_FALLBACK }}>
            {dialog}
          </CapabilitiesProvider>,
        );
      }
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

  it.each([
    ["Full Access", "full-access"],
    ["Bypass approvals & sandbox", "bypass"],
  ])("saves Codex's %s mode, which skips approval", async (label, mode) => {
    mockHarness("codex-native");
    render(
      <CreateScheduledTaskDialog open onOpenChange={vi.fn()} initialName="R" initialPrompt="P" />,
    );
    await choose("permission", label);
    expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent(label);
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].permissionMode).toBe(mode);
  });

  it.each([
    ["codex-native", "Approve for me", ["Full Access", "Bypass approvals & sandbox"]],
    ["claude-native", "Auto", ["Bypass permissions", "Don't ask"]],
  ])(
    "hides %s modes that skip approval from a server that refuses them",
    async (harness, kept, hidden) => {
      mockHarness(harness);
      render(
        <CapabilitiesProvider info={REFUSES_APPROVAL_SKIPPING}>
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            initialName="R"
            initialPrompt="P"
          />
        </CapabilitiesProvider>,
      );
      fireEvent.keyDown(screen.getByTestId("task-permission-trigger"), { key: "Enter" });
      expect(await screen.findByRole("option", { name: kept })).toBeInTheDocument();
      for (const name of hidden) {
        expect(screen.queryByRole("option", { name })).not.toBeInTheDocument();
      }
    },
  );

  it("starts a new Codex automation on Approve for me", async () => {
    mockHarness("codex-native");
    render(
      <CreateScheduledTaskDialog open onOpenChange={vi.fn()} initialName="R" initialPrompt="P" />,
    );
    expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Approve for me");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].permissionMode).toBe("approve-for-me");
  });

  it("saves no Codex override when the user picks Default", async () => {
    mockHarness("codex-native");
    render(
      <CreateScheduledTaskDialog open onOpenChange={vi.fn()} initialName="R" initialPrompt="P" />,
    );
    await choose("permission", "Default");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).not.toHaveProperty("permissionMode");
  });

  it("keeps an existing Codex automation with no saved mode on Default", async () => {
    const agent = mockHarness("codex-native");
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ agentId: agent.id, permissionMode: null, hostId: "host_1" })}
      />,
    );
    expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Default");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input.permissionMode).toBeNull();
  });

  it.each([
    [false, null],
    [true, "skip"],
  ])(
    "on edit, keeps Antigravity's saved skip mode only if the server accepts it (%s)",
    async (accepted, expected) => {
      mockHarness("antigravity-native");
      render(
        <CapabilitiesProvider info={serverInfo(accepted)}>
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            editingTask={scheduledTask({ agentId: "selected-agent", permissionMode: "skip" })}
          />
        </CapabilitiesProvider>,
      );
      // Without the mode the picker has nothing to offer, so it is hidden.
      if (accepted) {
        expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Skip permissions");
      } else {
        expect(screen.queryByTestId("task-permission-trigger")).toBeNull();
      }
      fireEvent.change(screen.getByLabelText("Instructions"), {
        target: { value: "Review again" },
      });
      fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
      await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
      expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
        prompt: "Review again",
        permissionMode: expected,
      });
    },
  );

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
    await choose("permission", "Read Only");
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
    expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Read Only");
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
        openAutomationSection("Context");
        fireEvent.click(screen.getByTestId("task-workspace-trigger"));
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
        openAutomationSection("Context");
        fireEvent.click(screen.getByTestId("task-workspace-trigger"));
        fireEvent.click(screen.getByRole("button", { name: "pick-workspace" }));
      }
      openAutomationSection("Context");
      expect(screen.queryByRole("checkbox", { name: "Random worktree" })).not.toBeInTheDocument();
      if (transition === "host-roundtrip" || transition === "sandbox-roundtrip") {
        if (transition === "sandbox-roundtrip") gitWorkspace.available = true;
        fireEvent.keyDown(screen.getByTestId("task-host-trigger"), { key: "Enter" });
        fireEvent.click(screen.getByRole("option", { name: "laptop" }));
        openAutomationSection("Context");
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
        openAutomationSection("Context");
        fireEvent.click(screen.getByTestId("task-workspace-trigger"));
        fireEvent.click(screen.getByRole("button", { name: "pick-workspace" }));
        openAutomationSection("Context");
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
    openAutomationSection("Context");
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
    openAutomationSection("Context");
    fireEvent.click(screen.getByTestId("task-workspace-trigger"));
    fireEvent.click(screen.getByRole("button", { name: "pick-workspace" }));
    openAutomationSection("Context");
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
    openAutomationSection("Context");
    expect(screen.queryByRole("checkbox", { name: "Random worktree" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Creates a fresh randomly named worktree/)).not.toBeInTheDocument();
    gitWorkspace.available = true;
    rerender(dialog());
    openAutomationSection("Context");
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
    openAutomationSection("Context");
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

describe("creating an automation from a session", () => {
  const RUNTIME_REPO = "https://github.com/example/runtime";
  const SOURCE: AutomationSourceSession = {
    id: "conv_src",
    title: "Weekly rubric",
    agentId: "ag_claude_native",
    agentName: "Claude Code",
    hostId: "host_1",
    workspace: "/home/me/repo",
    labels: {},
  };

  beforeEach(() => {
    sourceSnapshot.current = null;
  });

  function renderFromSession({
    source = SOURCE,
    info = {},
    onCreated = vi.fn(),
    editingTask = null,
  }: {
    source?: AutomationSourceSession | null;
    info?: Partial<ServerInfo>;
    onCreated?: (task: ScheduledTasksApiModule.ScheduledTask) => void;
    editingTask?: ScheduledTasksApiModule.ScheduledTask | null;
  } = {}) {
    return render(
      <CapabilitiesProvider
        info={{
          ...SERVER_INFO_OFFLINE_FALLBACK,
          features: { automations_v2: true },
          scheduled_task_harness_settings: true,
          scheduled_task_session_modes: true,
          ...info,
        }}
      >
        <MemoryRouter>
          <CreateScheduledTaskDialog
            open
            onOpenChange={vi.fn()}
            sourceSession={source}
            editingTask={editingTask}
            onCreated={onCreated}
          />
        </MemoryRouter>
      </CapabilitiesProvider>,
    );
  }

  async function submit() {
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    return mutateAsync.mock.calls[0][0];
  }

  it("names the automation after the session, preselects its agent and fixes trigger and session mode", () => {
    renderFromSession();

    expect(screen.getByText("New automation from “Weekly rubric”")).toBeInTheDocument();
    expect(screen.getByLabelText("Automation title")).toHaveValue("Weekly rubric");
    const startsFrom = screen.getByTestId("task-starts-from-link");
    expect(startsFrom).toHaveAttribute("href", "/c/conv_src");
    expect(startsFrom).toHaveTextContent("Weekly rubric");
    expect(screen.getByRole("textbox", { name: "Additional instructions" })).toHaveValue("");
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute(
      "data-effective",
      "ag_claude_native",
    );
    expect(screen.queryByText("Trigger type")).toBeNull();
    expect(screen.queryByTestId("task-session-mode-field")).toBeNull();
  });

  it("saves the session as the source, allows empty instructions, and runs where the session ran", async () => {
    const onCreated = vi.fn();
    renderFromSession({ onCreated });

    expect(screen.getByTestId("create-scheduled-task-submit")).toBeEnabled();
    const input = await submit();
    expect(input).toMatchObject({
      sourceSessionId: "conv_src",
      agentId: "ag_claude_native",
      prompt: "",
      hostId: "host_1",
      workspace: "/home/me/repo",
    });
    expect(input).not.toHaveProperty("sessionMode");
    expect(input).not.toHaveProperty("executionTarget");
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith({ id: "st_new" }));
  });

  it.each<[string, Record<string, string>]>([
    ["no repository", {}],
    ["its joined repository label", { "omnigent.sandbox.repo": RUNTIME_REPO }],
    // A long repository list carries only the indexed labels.
    ["only indexed repository labels", { "omnigent.sandbox.repo.0": RUNTIME_REPO }],
  ])("defaults to a sandbox whose provider lists repos (session with %s)", async (_, labels) => {
    renderFromSession({
      source: { ...SOURCE, labels },
      info: {
        managed_sandboxes_enabled: true,
        sandbox_provider: "repo_sandbox",
        scheduled_task_sandbox_workspaces: true,
        sandbox_provider_capabilities: {
          repo_sandbox: { repo_catalog: [{ name: "runtime", url: RUNTIME_REPO }] },
        },
      },
    });

    const input = await submit();
    expect(input).toMatchObject({
      executionTarget: "managed_sandbox",
      sourceSessionId: "conv_src",
    });
    expect(input).not.toHaveProperty("hostId");
    expect(input.workspace ?? "").toBe(Object.keys(labels).length > 0 ? RUNTIME_REPO : "");
  });

  it("does not default to a sandbox whose provider lists no repositories", async () => {
    renderFromSession({
      info: {
        managed_sandboxes_enabled: true,
        sandbox_provider: "plain_sandbox",
        sandbox_provider_capabilities: { plain_sandbox: { multi_repo: true } },
      },
    });

    const input = await submit();
    expect(input).toMatchObject({ hostId: "host_1", workspace: "/home/me/repo" });
    expect(input).not.toHaveProperty("executionTarget");
  });

  it("falls back to the first online host when the session's host isn't listed", async () => {
    renderFromSession({ source: { ...SOURCE, hostId: "host_sandbox", workspace: "/workspace" } });

    const input = await submit();
    expect(input).toMatchObject({ hostId: "host_1" });
    expect(input).not.toHaveProperty("workspace");
  });

  it.each<[string, Partial<ServerInfo>, boolean]>([
    ["acceptEdits", {}, true],
    // Servers that don't advertise approval-skipping modes refuse them.
    ["bypassPermissions", {}, false],
    ["bypassPermissions", { scheduled_task_approval_skipping_modes: true }, true],
  ])(
    "starts from the session's permission mode %s unless automations refuse it (%o)",
    async (mode, info, kept) => {
      sourceSnapshot.current = {
        id: "conv_src",
        labels: { "omnigent.claude_native.permission_mode": mode },
      };
      renderFromSession({ info });

      expect((await submit()).permissionMode === mode).toBe(kept);
    },
  );

  it("runs a session-based automation on another agent when one is picked", async () => {
    vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
      data: [...AGENTS, testAgent("ag_codex", "codex-native-ui", { harness: "codex-native" })],
    } as ReturnType<typeof agentsHook.useAvailableAgents>);
    renderFromSession();

    fireEvent.click(screen.getByRole("button", { name: "pick codex-native" }));
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute("data-effective", "ag_codex");
    // The session's own settings arrive after the switch; they stay off the Codex automation.
    sourceSnapshot.current = { id: "conv_src", modelOverride: "opus", labels: {} };
    fireEvent.change(screen.getByRole("textbox", { name: "Additional instructions" }), {
      target: { value: "Do it" },
    });
    const input = await submit();

    expect(input).toMatchObject({ agentId: "ag_codex", sourceSessionId: "conv_src" });
    expect(input).not.toHaveProperty("modelOverride");
  });

  it("edits a session-based automation with its source session shown", async () => {
    sourceSnapshot.current = { id: "conv_copy", title: "Weekly rubric copy" };
    renderFromSession({
      source: null,
      editingTask: scheduledTask({
        agentId: "ag_claude_native",
        prompt: "",
        hostId: "host_1",
        sourceSessionId: "conv_copy",
      }),
    });

    const startsFrom = screen.getByTestId("task-starts-from-link");
    expect(startsFrom).toHaveAttribute("href", "/c/conv_copy");
    expect(startsFrom).toHaveTextContent("Weekly rubric copy");
    expect(screen.getByRole("textbox", { name: "Additional instructions" })).toHaveValue("");
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute(
      "data-effective",
      "ag_claude_native",
    );
    expect(screen.queryByTestId("task-session-mode-field")).toBeNull();

    fireEvent.change(screen.getByLabelText("Automation title"), {
      target: { value: "Rubric refresh" },
    });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const { input } = updateMutateAsync.mock.calls[0][0];
    expect(input).toMatchObject({ name: "Rubric refresh", prompt: "" });
    expect(input).not.toHaveProperty("agentId");
  });
});
