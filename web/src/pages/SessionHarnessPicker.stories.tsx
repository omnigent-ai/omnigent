import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { ChatStoreSeed, StoryQueryRouter } from "@/storybook/StoryProviders";
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
        <StoryQueryRouter>
          <ChatStoreSeed
            seed={{
              conversationId: null,
              sessionHarness: "claude-native",
              llmModel: "system.ai.claude-opus-4-6",
              selectedEffort: "high",
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

export const SmartRouting: Story = {
  args: { costRoutingEligible: true },
  play: async ({ canvasElement }) => {
    const page = within(canvasElement.ownerDocument.body);
    await userEvent.click(within(canvasElement).getByTestId("composer-config-gear"));
    await userEvent.click(await page.findByTestId("composer-agent-edit"));
    await page.findByRole("menuitem", { name: "Smart Routing" });
  },
};
