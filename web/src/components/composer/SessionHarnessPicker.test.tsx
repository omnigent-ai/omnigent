import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { MaestroIcon } from "@/components/icons/MaestroIcon";
import { useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { SessionHarnessPicker } from "./SessionHarnessPicker";

const OPTIONS = [
  { id: "opus", displayName: "Opus" },
  { id: "sonnet", displayName: "Sonnet" },
];
const initialState = useChatStore.getState();

function renderPicker(overrides: Partial<ComponentProps<typeof SessionHarnessPicker>> = {}) {
  return render(
    <TooltipProvider>
      <SessionHarnessPicker
        sessionId="cell"
        busy={false}
        busyRef={{ current: false }}
        setBusy={() => {}}
        agentName="maestro"
        harnessLabel="Claude Code"
        showModels
        showEffort
        effortLevels={["low", "high"]}
        modelPickerKind="claude"
        supportsModelReset={false}
        codexModelOptions={OPTIONS}
        modelLabelOptions={OPTIONS}
        modelLabelHostId={null}
        costRoutingEligible
        modelPickWakesSession={false}
        disabled={false}
        {...overrides}
      />
    </TooltipProvider>,
  );
}

afterEach(() => {
  cleanup();
  conversationRegistry.clear();
  vi.restoreAllMocks();
  useChatStore.setState(initialState, true);
});

it.each(["native", "provided", "bound", "fork"])(
  "shows and changes the given session, not the active conversation (custom identity: %s)",
  async (customIdentity) => {
    useChatStore.setState({ conversationId: "active", llmModel: "opus" });
    conversationRegistry.acquire("cell").setState({
      sessionHarness: "claude-native",
      llmModel: "sonnet",
      sessionReasoningEffort: "high",
      costControlModeOverride: "on",
    });
    const store = useChatStore.getState();
    const setModel = vi.spyOn(store, "setModel").mockResolvedValue();
    const setEffort = vi.spyOn(store, "setEffort").mockResolvedValue();
    const setCostControlMode = vi.spyOn(store, "setCostControlMode").mockResolvedValue();
    renderPicker({
      agentIcon: customIdentity === "provided" ? <MaestroIcon /> : undefined,
      agentName:
        customIdentity === "native"
          ? null
          : customIdentity === "fork"
            ? "maestro (fork session-a)"
            : "maestro",
    });
    const openMenu = () => {
      fireEvent.keyDown(screen.getByTestId("composer-config-gear"), { key: "ArrowDown" });
      expect(screen.getByTestId("composer-agent-edit")).toHaveTextContent(
        customIdentity === "native" ? "Claude Code" : "Maestro",
      );
      if (customIdentity !== "native")
        expect(screen.getByTestId("composer-agent-edit")).not.toHaveTextContent("Claude Code");
      fireEvent.click(screen.getByTestId("composer-agent-edit"));
    };

    expect(screen.getByTestId("composer-config-gear")).toHaveTextContent("Smart Routing");
    expect(
      screen
        .getByTestId("composer-config-gear")
        .querySelector(
          customIdentity !== "native"
            ? '[data-agent-icon="maestro"]'
            : '[data-harness-icon="claude"]',
        ),
    ).not.toBeNull();
    if (customIdentity !== "native")
      expect(
        screen.getByTestId("composer-config-gear").querySelector("[data-harness-icon]"),
      ).toBeNull();
    openMenu();
    fireEvent.click(screen.getByTestId("composer-agent-model-opus"));
    // Picking a model with routing on also turns this session's routing off.
    await waitFor(() => expect(setCostControlMode).toHaveBeenCalledWith("off", "cell"));
    expect(setModel).toHaveBeenCalledWith("opus", { expectConfirmation: true, sessionId: "cell" });

    act(() => conversationRegistry.peek("cell")!.setState({ costControlModeOverride: null }));
    expect(screen.getByTestId("composer-config-gear")).toHaveTextContent("Sonnet");
    expect(screen.getByTestId("composer-config-gear")).toHaveTextContent("High");
    expect(screen.getByTestId("composer-config-gear")).not.toHaveTextContent("Claude Code");
    openMenu();
    fireEvent.click(screen.getByTestId("composer-agent-effort-high"));
    await waitFor(() => expect(setEffort).toHaveBeenCalledWith("high", "cell"));

    act(() =>
      conversationRegistry.peek("cell")!.setState({ sessionReasoningEffort: null, llmModel: null }),
    );
    expect(screen.getByTestId("composer-config-gear")).toHaveTextContent("Default model");
    expect(screen.getByTestId("composer-config-gear")).toHaveTextContent("Default");
    expect(screen.getByTestId("composer-config-gear")).not.toHaveTextContent("Claude Code");
  },
);

it.each([undefined, "cell"])(
  "resolves Codex model and inherited effort consistently without changing settings (scope: %s)",
  async (sessionId) => {
    const options = [
      {
        id: "model_reported",
        displayName: "Reported model",
        isDefault: true,
        defaultReasoningEffort: "medium",
        supportedReasoningEfforts: [{ reasoningEffort: "medium" }, { reasoningEffort: "max" }],
      },
      {
        id: "model_saved",
        displayName: "Saved model",
        defaultReasoningEffort: "low",
        supportedReasoningEfforts: [{ reasoningEffort: "low" }, { reasoningEffort: "high" }],
      },
    ];
    useChatStore.setState({ conversationId: "active", llmModel: "unrelated" });
    const target = sessionId === undefined ? useChatStore : conversationRegistry.acquire(sessionId);
    target.setState({
      sessionHarness: "codex-native",
      llmModel: null,
      sessionModelOverride: "model_saved",
      sessionReasoningEffort: null,
    });
    const setModel = vi.spyOn(useChatStore.getState(), "setModel").mockResolvedValue();
    const setEffort = vi.spyOn(useChatStore.getState(), "setEffort").mockResolvedValue();
    renderPicker({
      sessionId,
      harnessLabel: "Codex",
      modelPickerKind: "codex",
      codexModelOptions: options,
      modelLabelOptions: [], // The host catalog can arrive before the session's own catalog.
    });
    const trigger = screen.getByTestId("composer-config-gear");
    expect(trigger).toHaveTextContent("Saved model");
    expect(trigger).toHaveTextContent("Low");
    expect(trigger).not.toHaveTextContent("Default");
    fireEvent.focus(trigger);
    expect(await screen.findByTestId("composer-config-gear-tooltip")).toHaveTextContent(
      "Low (model default)",
    );
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    fireEvent.click(screen.getByTestId("composer-agent-edit"));
    expect(screen.getByTestId("composer-agent-model-summary")).toHaveTextContent("Saved model");
    expect(screen.getByTestId("composer-agent-effort-low")).toHaveAttribute("aria-checked", "true");
    expect(setModel).not.toHaveBeenCalled();
    expect(setEffort).not.toHaveBeenCalled();
    expect(target.getState().sessionReasoningEffort).toBeNull();

    act(() => target.setState({ llmModel: "model_reported", sessionReasoningEffort: "max" }));
    expect(trigger).toHaveTextContent("Reported model");
    expect(trigger).toHaveTextContent("Max");
    expect(trigger).not.toHaveTextContent("Saved model");
    expect(screen.getByTestId("composer-agent-effort-max")).toHaveAttribute("aria-checked", "true");

    act(() =>
      target.setState({ llmModel: null, sessionModelOverride: null, sessionReasoningEffort: null }),
    );
    // The catalog's global default is not evidence of this session's selection.
    expect(trigger).not.toHaveTextContent("Reported model");
    expect(trigger).not.toHaveTextContent("Medium");
  },
);
