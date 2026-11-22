import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import type { ScheduledTask } from "@/lib/scheduledTasksApi";
import { type PaletteCommand, usePaletteCommandsStore } from "@/store/paletteCommands";
import {
  chooseSettingValue,
  expectSetting,
  registeredPaletteCommands,
} from "@/test/paletteCommands";

import { AUTOMATION_SAVING } from "./automationDetailActions";
import {
  AUTOMATIONS_FILTER_LABEL,
  type AutomationListHandlers,
  AutomationListPaletteCommands,
  type AutomationListState,
  automationListPaletteCommands,
  NEW_AUTOMATION_LABEL,
} from "./automationListPaletteCommands";
import { OWNER_ONLY_REASONS } from "./ownerOnlyActions";
import type * as TriggerTemplatesModule from "./triggerTemplates";

vi.mock("./triggerTemplates", async (importOriginal) => ({
  ...(await importOriginal<typeof TriggerTemplatesModule>()),
  isTriggerPrompt: (prompt: string) => prompt.startsWith("[trigger]"),
}));

function task(overrides: Partial<ScheduledTask> = {}): ScheduledTask {
  return {
    id: "st_1",
    name: "Daily digest",
    prompt: "Summarize",
    rrule: "FREQ=DAILY;BYHOUR=8;BYMINUTE=0",
    ownerUserId: null,
    agentId: "ag_1",
    timezone: "UTC",
    createdAt: 1,
    updatedAt: 2,
    modelOverride: null,
    reasoningEffort: null,
    permissionMode: null,
    mcpServices: [],
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
  };
}

function state(overrides: Partial<AutomationListState> = {}): AutomationListState {
  return {
    view: "yours",
    tasks: [task()],
    loadFailed: false,
    filter: "all",
    busyTaskId: undefined,
    canShare: () => true,
    shareDisabledReason: () => undefined,
    cloneAvailable: true,
    ...overrides,
  };
}

function handlers(): AutomationListHandlers {
  return {
    newAutomation: vi.fn(),
    changeFilter: vi.fn(),
    runNow: vi.fn(),
    edit: vi.fn(),
    share: vi.fn(),
    clone: vi.fn(),
    pauseToggle: vi.fn(),
  };
}

const commandsFor = (overrides: Partial<AutomationListState> = {}) =>
  automationListPaletteCommands(state(overrides), { current: handlers() });
const labels = (commands: readonly PaletteCommand[]) => commands.map(({ label }) => label);
const named = (commands: readonly PaletteCommand[], label: string) =>
  commands.find((command) => command.label === label)!;

describe("automationListPaletteCommands", () => {
  it("lists New, the filter, and each automation's row menu actions without Delete", () => {
    expect(labels(commandsFor())).toEqual([
      NEW_AUTOMATION_LABEL,
      AUTOMATIONS_FILTER_LABEL,
      "Run “Daily digest” now",
      "Edit “Daily digest”…",
      "Share “Daily digest”…",
      "Clone “Daily digest”",
      "Pause “Daily digest”",
    ]);
  });

  it("offers Resume for a paused automation, and no Run now for a trigger automation", () => {
    const paused = labels(commandsFor({ tasks: [task({ state: "paused" })] }));
    expect(paused).toContain("Resume “Daily digest”");
    expect(paused).not.toContain("Pause “Daily digest”");
    const trigger = labels(
      commandsFor({ tasks: [task({ sessionMode: "continue", prompt: "[trigger] Slack" })] }),
    );
    expect(trigger).not.toContain("Run “Daily digest” now");
  });

  it("follows the row's Share and Clone rules", () => {
    expect(labels(commandsFor({ canShare: () => false }))).toEqual([
      NEW_AUTOMATION_LABEL,
      AUTOMATIONS_FILTER_LABEL,
      "Run “Daily digest” now",
      "Edit “Daily digest”…",
      "Clone “Daily digest”",
      "Pause “Daily digest”",
    ]);
    expect(labels(commandsFor({ cloneAvailable: false }))).not.toContain("Clone “Daily digest”");
    expect(
      named(commandsFor({ shareDisabledReason: () => "Sharing is off." }), "Share “Daily digest”…")
        .availability,
    ).toEqual({ available: false, reason: "Sharing is off." });
  });

  it("greys a saving automation's rows, and an automation shared with you with the owner's reasons", () => {
    const saving = commandsFor({ busyTaskId: "st_1" });
    // Share stays available while saving, as the header's Share button does.
    expect(named(saving, "Share “Daily digest”…").availability).toEqual({ available: true });
    expect(named(saving, "Pause “Daily digest”").availability).toEqual({
      available: false,
      reason: AUTOMATION_SAVING,
    });
    expect(named(saving, "Clone “Daily digest”").availability).toEqual({
      available: false,
      reason: AUTOMATION_SAVING,
    });
    const shared = commandsFor({ tasks: [task({ viewerRole: "viewer" })], canShare: () => false });
    expect(named(shared, "Edit “Daily digest”…").availability).toEqual({
      available: false,
      reason: OWNER_ONLY_REASONS.edit,
    });
    expect(labels(shared).filter((label) => label === "Clone “Daily digest”")).toHaveLength(1);
    expect(named(shared, "Clone “Daily digest”").availability).toEqual({
      available: false,
      reason: OWNER_ONLY_REASONS.clone,
    });
  });

  it("offers only New while the list loads, after a failed load, and on the other tabs", () => {
    for (const overrides of [
      { tasks: undefined },
      { loadFailed: true },
      { view: "shared" as const },
      { view: "templates" as const },
    ]) {
      const listed = labels(commandsFor(overrides));
      expect(listed.filter((label) => label !== AUTOMATIONS_FILTER_LABEL)).toEqual([
        NEW_AUTOMATION_LABEL,
      ]);
    }
    expect(labels(commandsFor({ tasks: [] }))).toEqual([NEW_AUTOMATION_LABEL]);
  });

  it("runs the row menu's handlers, and the filter setting picks a tab", async () => {
    const run = handlers();
    const commands = automationListPaletteCommands(state(), { current: run });
    named(commands, "Pause “Daily digest”").run();
    expect(run.pauseToggle).toHaveBeenCalledWith(expect.objectContaining({ id: "st_1" }));
    named(commands, "Clone “Daily digest”").run();
    expect(run.clone).toHaveBeenCalledOnce();
    named(commands, NEW_AUTOMATION_LABEL).run();
    expect(run.newAutomation).toHaveBeenCalledOnce();
    await chooseSettingValue(
      expectSetting(commands, AUTOMATIONS_FILTER_LABEL, {
        current: "All",
        choices: ["All", "Active", "Paused"],
      }),
      "Paused",
    );
    expect(run.changeFilter).toHaveBeenCalledWith("paused");
  });
});

describe("AutomationListPaletteCommands", () => {
  afterEach(() => {
    cleanup();
    usePaletteCommandsStore.setState({ groups: {} });
  });

  const mount = (
    props: AutomationListState & { handlers: AutomationListHandlers },
    flag = true,
  ) => (
    <CapabilitiesProvider
      info={{ ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: flag } }}
    >
      <AutomationListPaletteCommands {...props} />
    </CapabilitiesProvider>
  );

  it("keeps its rows across re-renders with the same list, and runs the newest handlers", () => {
    const first = handlers();
    const { rerender } = render(mount({ ...state(), handlers: first }));
    const listed = registeredPaletteCommands().find(
      ({ label }) => label === "Pause “Daily digest”",
    )!;
    const latest = handlers();
    rerender(mount({ ...state({ tasks: [task()] }), handlers: latest }));
    expect(registeredPaletteCommands().find(({ label }) => label === listed.label)).toBe(listed);
    act(() => listed.run());
    expect(latest.pauseToggle).toHaveBeenCalledOnce();
    expect(first.pauseToggle).not.toHaveBeenCalled();
  });

  it("replaces an automation's rows once it changes, so Resume follows a pause", () => {
    const { rerender } = render(mount({ ...state(), handlers: handlers() }));
    rerender(
      mount({
        ...state({ tasks: [task({ state: "paused", updatedAt: 3 })] }),
        handlers: handlers(),
      }),
    );
    expect(labels(registeredPaletteCommands())).toContain("Resume “Daily digest”");
    expect(labels(registeredPaletteCommands())).not.toContain("Pause “Daily digest”");
  });

  it("replaces an automation's rows after an edit within the same second, so Edit opens the new content", () => {
    const { rerender } = render(mount({ ...state(), handlers: handlers() }));
    const listed = registeredPaletteCommands().find(
      ({ label }) => label === "Edit “Daily digest”…",
    )!;
    const latest = handlers();
    // updatedAt has second precision, so a prompt-only edit can keep it.
    rerender(
      mount({
        ...state({ tasks: [task({ prompt: "Summarize, then file issues" })] }),
        handlers: latest,
      }),
    );
    const edit = registeredPaletteCommands().find(({ label }) => label === listed.label)!;
    expect(edit.id).not.toBe(listed.id);
    act(() => edit.run());
    expect(latest.edit).toHaveBeenCalledWith(
      expect.objectContaining({ prompt: "Summarize, then file issues" }),
    );
  });

  it("registers nothing with quick_pick_palette off", () => {
    render(mount({ ...state(), handlers: handlers() }, false));
    expect(registeredPaletteCommands()).toEqual([]);
  });
});
