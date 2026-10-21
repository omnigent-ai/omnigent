import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { ChatStoreSeed, StoryQueryRouter } from "@/storybook/StoryProviders";
import { useChatStore } from "@/store/chatStore";
import type { Session } from "@/lib/types";
import { Composer } from "./ChatPage";

const meta = {
  title: "Components/Agents/SessionHarnessPicker",
  component: Composer,
  tags: ["visual-snapshot"],
  args: {
    status: "idle",
    isWorking: false,
    disabled: false,
    onSend: () => undefined,
    onNewSession: () => undefined,
    onStop: () => undefined,
    agents: [{ id: "claude", name: "claude" }],
    selectedAgentId: "claude",
    permissionLevel: null,
    readOnlyReason: null,
    effortLevels: ["low", "medium", "high"],
    showEffort: true,
    showModels: true,
    modelPickerKind: "claude",
    codexModelOptions: [
      { id: "opus", model: "system.ai.claude-opus-4-6", displayName: "Opus", isDefault: true },
      { id: "sonnet", model: "system.ai.claude-sonnet-4-6", displayName: "Sonnet" },
    ],
    showCodexPlanMode: false,
  },
  decorators: [
    (Story) => (
      <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
        <StoryQueryRouter
          seed={(queryClient) =>
            queryClient.setQueryData(["session", "child-session-model-effort"], {
              id: "child-session-model-effort",
              parentSessionId: "parent-session",
              reasoningEffort: "high",
              hostId: null,
              workspace: null,
              labels: {},
              gitBranch: null,
            } as Session)
          }
        >
          <ChatStoreSeed
            seed={{
              conversationId: null,
              sessionHarness: "claude-native",
              llmModel: "system.ai.claude-opus-4-6",
              sessionReasoningEffort: "high",
              costControlModeOverride: null,
              pendingModelChange: null,
              nativeVendorOwnsModel: false,
            }}
          >
            {/* A column so the composer is width-constrained like its real mounts;
                a shrink-to-fit item would collapse its labels for lack of room. */}
            <div className="flex min-h-[480px] w-[620px] flex-col justify-end rounded-xl border bg-card p-4">
              <Story />
            </div>
          </ChatStoreSeed>
        </StoryQueryRouter>
      </CapabilitiesProvider>
    ),
  ],
} satisfies Meta<typeof Composer>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Open: Story = {
  play: async ({ canvasElement }) => {
    await userEvent.click(within(canvasElement).getByTestId("composer-config-gear"));
  },
};

export const ReadOnlyChildModelEffort: Story = {
  args: {
    disabled: true,
    readOnlyReason: "Sub-agent sessions are read-only",
    showModels: false,
    showEffort: false,
    modelPickerKind: null,
  },
  play: async () => {
    useChatStore.setState({
      conversationId: "child-session-model-effort",
      sessionHarness: "pi",
      llmModel: "databricks-gpt-5-4",
      sessionReasoningEffort: null,
    });
  },
};

export const SmartRouting: Story = {
  args: { costRoutingEligible: true },
  play: async ({ canvasElement }) => {
    const page = within(canvasElement.ownerDocument.body);
    await userEvent.click(within(canvasElement).getByTestId("composer-config-gear"));
    await userEvent.click(await page.findByTestId("composer-agent-edit"));
    await page.findByRole("menuitem", { name: "Smart Routing" });
  },
};

export const UnknownCurrentModel: Story = {
  decorators: [
    (Story) => (
      <ChatStoreSeed
        seed={{
          conversationId: null,
          sessionHarness: "claude-native",
          llmModel: "claude-future-unknown",
          sessionReasoningEffort: "high",
          costControlModeOverride: null,
          pendingModelChange: null,
          nativeVendorOwnsModel: false,
        }}
      >
        <Story />
      </ChatStoreSeed>
    ),
  ],
  play: async ({ canvasElement }) => {
    await userEvent.click(within(canvasElement).getByTestId("composer-config-gear"));
  },
};

export const SmartRoutingSelected: Story = {
  args: { costRoutingEligible: true },
  decorators: [
    (Story) => (
      <ChatStoreSeed
        seed={{
          conversationId: null,
          sessionHarness: "claude-native",
          llmModel: null,
          sessionReasoningEffort: null,
          costControlModeOverride: "on",
          pendingModelChange: null,
          nativeVendorOwnsModel: false,
        }}
      >
        <Story />
      </ChatStoreSeed>
    ),
  ],
  play: async ({ canvasElement }) => {
    await userEvent.click(within(canvasElement).getByTestId("composer-config-gear"));
  },
};
