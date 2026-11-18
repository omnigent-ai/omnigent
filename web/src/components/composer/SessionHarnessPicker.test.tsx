import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { SessionHarnessPicker } from "./SessionHarnessPicker";

const OPTIONS = [
  { id: "opus", displayName: "Opus" },
  { id: "sonnet", displayName: "Sonnet" },
];
const initialState = useChatStore.getState();

afterEach(() => {
  cleanup();
  conversationRegistry.clear();
  vi.restoreAllMocks();
  useChatStore.setState(initialState, true);
});

it("shows and changes the given session, not the active conversation", async () => {
  useChatStore.setState({ conversationId: "active", llmModel: "opus" });
  conversationRegistry.acquire("cell").setState({
    sessionHarness: "claude-native",
    llmModel: "sonnet",
    costControlModeOverride: "on",
  });
  const store = useChatStore.getState();
  const setModel = vi.spyOn(store, "setModel").mockResolvedValue();
  const setEffort = vi.spyOn(store, "setEffort").mockResolvedValue();
  const setCostControlMode = vi.spyOn(store, "setCostControlMode").mockResolvedValue();
  render(
    <TooltipProvider>
      <SessionHarnessPicker
        sessionId="cell"
        busy={false}
        busyRef={{ current: false }}
        setBusy={() => {}}
        agentName={null}
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
      />
    </TooltipProvider>,
  );
  const openMenu = () => {
    fireEvent.keyDown(screen.getByTestId("composer-config-gear"), { key: "ArrowDown" });
    fireEvent.click(screen.getByTestId("composer-agent-edit"));
  };

  expect(screen.getByTestId("composer-config-gear")).toHaveTextContent("Smart Routing");
  openMenu();
  fireEvent.click(screen.getByTestId("composer-agent-model-opus"));
  // Picking a model with routing on also turns this session's routing off.
  await waitFor(() => expect(setCostControlMode).toHaveBeenCalledWith("off", "cell"));
  expect(setModel).toHaveBeenCalledWith("opus", { expectConfirmation: true, sessionId: "cell" });

  act(() => conversationRegistry.peek("cell")!.setState({ costControlModeOverride: null }));
  expect(screen.getByTestId("composer-config-gear")).toHaveTextContent("Sonnet");
  openMenu();
  fireEvent.click(screen.getByTestId("composer-agent-effort-high"));
  await waitFor(() => expect(setEffort).toHaveBeenCalledWith("high", "cell"));
});

it("renders and switches Antigravity live models and reflects terminal /model mirrors", async () => {
  const agyOptions = [
    { id: "gemini-3.8-flash-high", displayName: "Gemini 3.8 Flash (High)" },
    { id: "claude-sonnet-4-6", displayName: "Claude Sonnet 4.6 (Thinking)" },
    { id: "gemini-3.8-flash-low", displayName: "Gemini 3.8 Flash (Low)" },
  ];
  conversationRegistry.acquire("agy_cell").setState({
    sessionHarness: "antigravity-native",
    nativeVendorOwnsModel: true,
    llmModel: "Gemini 3.8 Flash (High)",
    sessionModelOverride: null,
  });
  const store = useChatStore.getState();
  const setModel = vi.spyOn(store, "setModel").mockResolvedValue();

  render(
    <TooltipProvider>
      <SessionHarnessPicker
        sessionId="agy_cell"
        busy={false}
        busyRef={{ current: false }}
        setBusy={() => {}}
        agentName="antigravity-native-ui"
        harnessLabel="Antigravity"
        showModels
        showEffort={false}
        effortLevels={[]}
        modelPickerKind="antigravity"
        supportsModelReset={false}
        codexModelOptions={agyOptions}
        modelLabelOptions={agyOptions}
        modelLabelHostId={null}
        costRoutingEligible={false}
        modelPickWakesSession={false}
        disabled={false}
      />
    </TooltipProvider>,
  );

  expect(screen.getByTestId("composer-config-gear")).toHaveTextContent("Gemini 3.8 Flash (High)");
  fireEvent.keyDown(screen.getByTestId("composer-config-gear"), { key: "ArrowDown" });
  fireEvent.click(screen.getByTestId("composer-agent-edit"));
  fireEvent.click(screen.getByTestId("composer-agent-model-claude-sonnet-4-6"));
  await waitFor(() =>
    expect(setModel).toHaveBeenCalledWith("claude-sonnet-4-6", {
      expectConfirmation: false,
      sessionId: "agy_cell",
    }),
  );

  // Terminal `/model gemini-3.8-flash-low` updates llmModel while sessionModelOverride
  // still holds the earlier web pick; the reported model wins on the trigger.
  act(() =>
    conversationRegistry.peek("agy_cell")!.setState({
      sessionModelOverride: "claude-sonnet-4-6",
      llmModel: "Gemini 3.8 Flash (Low)",
    }),
  );
  expect(screen.getByTestId("composer-config-gear")).toHaveTextContent("Gemini 3.8 Flash (Low)");
});
