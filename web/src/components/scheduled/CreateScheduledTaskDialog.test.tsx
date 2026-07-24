// Tests for the manual create dialog: submit stays disabled until the required
// fields are filled, the workspace-without-host pairing rule surfaces inline,
// and a valid submit calls the create mutation with the RRULE built from the
// schedule fields (host/workspace omitted when unset).
//
// The agent/host hooks and the create mutation are mocked; WorkspacePicker is
// stubbed (its filesystem browsing is out of scope here).

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CreateScheduledTaskDialog,
  isInsidePopper,
  shouldGuardDialogDismiss,
} from "./CreateScheduledTaskDialog";
import * as agentsHook from "@/hooks/useAvailableAgents";
import * as hostsHook from "@/hooks/useHosts";
import * as scheduledHooks from "@/hooks/useScheduledTasks";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";

vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
vi.mock("@/hooks/useScheduledTasks", () => ({
  useCreateScheduledTask: vi.fn(),
  useUpdateScheduledTask: vi.fn(),
}));
vi.mock("@/lib/agentLabels", () => ({ useBrainHarnessLabels: () => ({}) }));
vi.mock("@/shell/WorkspacePicker", () => ({
  WorkspacePicker: ({ onNavigate }: { onNavigate?: (p: string) => void }) => (
    <button type="button" onClick={() => onNavigate?.("/home/me/repo")}>
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
  }: {
    onSelectAgent: (a: AvailableAgent) => void;
    onOpenChange?: (open: boolean) => void;
    effectiveAgentId: string | null;
    agentLabel: string;
    host?: { host_id: string } | null;
    dropdownModal?: boolean;
  }) => (
    <div
      data-testid="agent-picker-stub"
      data-effective={effectiveAgentId ?? ""}
      // Surface the host the dialog feeds the picker for badge computation, so
      // a test can assert it's populated even when no host is pinned.
      data-badge-host={host?.host_id ?? ""}
      data-dropdown-modal={dropdownModal === false ? "false" : "true"}
    >
      <span>{agentLabel}</span>
      <button
        type="button"
        data-testid="pick-harness-claude"
        onClick={() =>
          onSelectAgent({
            id: "ag_claude_native",
            name: "claude-native-ui",
            display_name: "Claude Code",
            description: null,
            harness: "claude-native",
            skills: [],
          })
        }
      >
        pick claude harness
      </button>
      <button
        type="button"
        data-testid="pick-agent-polly"
        onClick={() =>
          onSelectAgent({
            id: "ag_1",
            name: "polly",
            display_name: "Polly",
            description: null,
            harness: "claude-sdk",
            skills: [],
          })
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

// nativeAgentHasCapability(claude-native, "permissionMode") must be true so the
// model/effort knobs map onto the payload; polly (claude-sdk) has no knobs.
vi.mock("@/lib/nativeCodingAgents", async (orig) => {
  const actual = await orig<typeof import("@/lib/nativeCodingAgents")>();
  return {
    ...actual,
    isNativeCodingAgent: (a: AvailableAgent) => a?.name === "claude-native-ui",
    nativeAgentHasCapability: (a: AvailableAgent | undefined | null, cap: string) =>
      a?.name === "claude-native-ui" && cap === "permissionMode",
  };
});

const AGENTS: AvailableAgent[] = [
  {
    id: "ag_1",
    name: "polly",
    display_name: "Polly",
    description: null,
    harness: "claude-sdk",
    skills: [],
  },
  {
    id: "ag_claude_native",
    name: "claude-native-ui",
    display_name: "Claude Code",
    description: null,
    harness: "claude-native",
    skills: [],
  },
];

const mutateAsync = vi.fn();
const updateMutateAsync = vi.fn();

beforeEach(() => {
  mutateAsync.mockReset().mockResolvedValue({ id: "st_new" });
  updateMutateAsync.mockReset().mockResolvedValue({ id: "st_1" });
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

afterEach(() => cleanup());

function renderDialog(onOpenChange: (open: boolean) => void = vi.fn()) {
  return render(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} />);
}

function scheduledTask(overrides: Partial<import("@/lib/scheduledTasksApi").ScheduledTask> = {}) {
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
    workspace: null,
    hostId: null,
    state: "active",
    lastRunAt: null,
    lastRunConversationId: null,
    ...overrides,
  } satisfies import("@/lib/scheduledTasksApi").ScheduledTask;
}

describe("agent picker readiness (needs-setup badges)", () => {
  it("explains that scheduled tasks use the selected agent's runtime defaults", () => {
    renderDialog();
    expect(
      screen.getByText("Uses this agent's default model, effort, and permission settings"),
    ).toBeInTheDocument();
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
});

describe("CreateScheduledTaskDialog validation", () => {
  it("keeps submit disabled until name + prompt are set (agent defaults to the first)", () => {
    renderDialog();
    const submit = screen.getByTestId("create-scheduled-task-submit");
    // Agent is never blank — the picker resolves a default (first agent) — so
    // only name + prompt gate submit.
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
    expect(submit).toBeEnabled();
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
    expect(screen.getByText("Edit scheduled task")).toBeInTheDocument();
    expect(screen.getByText(/Update this recurring agent session/i)).toBeInTheDocument();
    expect(screen.getByTestId("create-scheduled-task-submit")).toHaveTextContent("Save changes");
    expect((screen.getByTestId("task-name-input") as HTMLInputElement).value).toBe("Morning brief");
    expect((screen.getByTestId("task-prompt-input") as HTMLTextAreaElement).value).toBe(
      "Summarize overnight activity",
    );
    expect(screen.getByTestId("task-agent-readonly")).toHaveTextContent("Polly");
    expect(screen.queryByTestId("agent-picker-stub")).not.toBeInTheDocument();
    expect(screen.getByTestId("schedule-time")).toHaveValue("08:30 AM");
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

  it("blocks update when the existing RRULE cannot be represented by the form", () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ rrule: "FREQ=DAILY;INTERVAL=2;BYHOUR=9;BYMINUTE=0" })}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("This schedule can't be edited");
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
  });
});

describe("CreateScheduledTaskDialog submit", () => {
  it("submits required fields with the built RRULE and omits host/workspace when unset", async () => {
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
    });
    expect(arg).not.toHaveProperty("hostId");
    expect(arg).not.toHaveProperty("workspace");
    // Timezone has no visible control but is still inferred + sent: a non-empty
    // IANA-ish string (whatever the test env's local zone resolves to).
    expect(typeof arg.timezone).toBe("string");
    expect(arg.timezone.length).toBeGreaterThan(0);
  });

  // Model/effort controls are not offered, so the create
  // body carries agent_id and NEVER model_override / reasoning_effort, whether
  // the pick is a bare harness (claude-native) or a plain agent (polly).
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

  it("does not render a visible timezone picker (inferred silently)", () => {
    renderDialog();
    expect(screen.queryByTestId("task-timezone-trigger")).toBeNull();
  });

  it("renders the Time field as an input with a compact picker trigger", () => {
    renderDialog();
    const timeField = screen.getByTestId("schedule-time");
    expect(timeField.tagName).toBe("INPUT");
    expect(timeField).toHaveAttribute("placeholder", "5:00 PM");
    expect(timeField).toHaveClass("text-sm");
    expect(screen.getByTestId("schedule-time-picker-trigger")).toBeInTheDocument();
  });

  it("lays out Frequency and Time in one compact row", () => {
    renderDialog();
    const row = screen.getByTestId("schedule-frequency-time-row");
    expect(row).toContainElement(screen.getByText("Frequency"));
    expect(row).toContainElement(screen.getByText("Time"));
    expect(row).toContainElement(screen.getByTestId("schedule-preset-trigger"));
    expect(row).toContainElement(screen.getByTestId("schedule-time"));
    expect(row).toHaveClass("sm:grid-cols-2", "sm:gap-6");
    expect(screen.getByTestId("schedule-frequency-control")).toHaveClass("w-full");
    expect(screen.getByTestId("schedule-time-control")).toHaveClass("w-full");
    expect(screen.getByTestId("schedule-preset-trigger")).toHaveClass("w-full");
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

  it("uses text-sm for dialog text fields that wrap shared primitives", () => {
    renderDialog();
    expect(screen.getByTestId("task-name-input")).toHaveClass("text-sm");
    expect(screen.getByTestId("task-prompt-input")).toHaveClass("text-sm");
  });

  it("blocks submit while the typed time is invalid", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.change(screen.getByTestId("schedule-time"), { target: { value: "25:99" } });
    expect(screen.getByTestId("schedule-error")).toHaveTextContent("Enter a valid time");
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
  });

  it("Hourly preset shows a minute-only text input", async () => {
    renderDialog();
    fireEvent.keyDown(screen.getByTestId("schedule-preset-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Hourly" }));
    const minuteField = screen.getByTestId("schedule-minute");
    expect(minuteField.tagName).toBe("INPUT");
    expect(minuteField).toHaveClass("text-sm");
    expect(minuteField).toHaveAttribute("placeholder", "0");
    expect(screen.queryByTestId("schedule-time-picker-trigger")).toBeNull();
    fireEvent.change(minuteField, { target: { value: "7" } });
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].rrule).toBe("FREQ=HOURLY;BYMINUTE=7");
  });

  it("Hourly preset strips non-digits, caps to two digits, and clamps above 59", async () => {
    renderDialog();
    fireEvent.keyDown(screen.getByTestId("schedule-preset-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Hourly" }));
    const minuteField = screen.getByTestId("schedule-minute");

    fireEvent.change(minuteField, { target: { value: "a:-" } });
    expect(minuteField).toHaveValue("");
    fireEvent.change(minuteField, { target: { value: "3a" } });
    expect(minuteField).toHaveValue("3");
    fireEvent.blur(minuteField);
    expect(minuteField).toHaveValue("3");
    fireEvent.change(minuteField, { target: { value: "75" } });
    expect(minuteField).toHaveValue("59");
    fireEvent.change(minuteField, { target: { value: "123" } });
    expect(minuteField).toHaveValue("12");
  });

  it("offers exactly the four frequency presets with no Custom entry point", async () => {
    renderDialog();
    // Open the frequency Select (keyboard is the reliable jsdom path).
    fireEvent.keyDown(screen.getByTestId("schedule-preset-trigger"), { key: "Enter" });
    const options = (await screen.findAllByRole("option")).map((o) => o.textContent);
    expect(options).toEqual(["Hourly", "Daily", "Weekdays", "Weekly"]);
    expect(options).not.toContain("Custom");
    // The Custom-only sub-controls are not reachable from this form.
    expect(screen.queryByTestId("custom-freq-trigger")).toBeNull();
    expect(screen.queryByTestId("custom-interval")).toBeNull();
    expect(screen.queryByTestId("schedule-month-trigger")).toBeNull();
  });

  it("does not render the 'Reads as' schedule preview", () => {
    renderDialog();
    expect(screen.queryByTestId("schedule-preview")).toBeNull();
    expect(screen.queryByText(/Reads as:/i)).toBeNull();
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
