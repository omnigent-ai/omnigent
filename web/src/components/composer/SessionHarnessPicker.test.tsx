import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { SessionHarnessPicker } from "./SessionHarnessPicker";

const OPTIONS = [
  { id: "opus", displayName: "Opus" },
  { id: "sonnet", displayName: "Sonnet" },
];

afterEach(() => {
  cleanup();
  conversationRegistry.clear();
  vi.restoreAllMocks();
});

it("shows and changes the given session, not the active conversation", () => {
  useChatStore.setState({ conversationId: "active", llmModel: "opus" });
  conversationRegistry
    .acquire("cell")
    .setState({ sessionHarness: "claude-native", llmModel: "sonnet" });
  const setModel = vi.spyOn(useChatStore.getState(), "setModel").mockResolvedValue();
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
        showEffort={false}
        effortLevels={[]}
        modelPickerKind="claude"
        supportsModelReset={false}
        codexModelOptions={OPTIONS}
        modelLabelOptions={OPTIONS}
        modelLabelHostId={null}
        costRoutingEligible={false}
        modelPickWakesSession={false}
        disabled={false}
      />
    </TooltipProvider>,
  );

  expect(screen.getByTestId("composer-config-gear")).toHaveTextContent("Sonnet");
  fireEvent.keyDown(screen.getByTestId("composer-config-gear"), { key: "ArrowDown" });
  fireEvent.click(screen.getByTestId("composer-agent-edit"));
  fireEvent.click(screen.getByTestId("composer-agent-model-opus"));

  expect(setModel).toHaveBeenCalledWith("opus", { expectConfirmation: true, sessionId: "cell" });
  expect(useChatStore.getState()).toMatchObject({ conversationId: "active", llmModel: "opus" });
});
