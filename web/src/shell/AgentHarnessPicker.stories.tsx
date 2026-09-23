import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import type { AgentBundleInput } from "@/lib/agentBundle";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { AgentHarnessPicker } from "./NewChatDialog";

const agent = (
  overrides: Partial<AvailableAgent> &
    Pick<AvailableAgent, "id" | "name" | "display_name" | "harness">,
): AvailableAgent => ({
  description: `${overrides.display_name} integration`,
  skills: [],
  builtin: true,
  ...overrides,
});

const claude = agent({
  id: "agent-claude",
  name: "claude-native-ui",
  display_name: "Claude Code",
  harness: "claude-native",
});
const codex = agent({
  id: "agent-codex",
  name: "codex-native-ui",
  display_name: "Codex",
  harness: "codex-native",
});
const cursor = agent({
  id: "agent-cursor",
  name: "cursor-native-ui",
  display_name: "Cursor",
  harness: "cursor-native",
});
const polly = agent({
  id: "agent-polly",
  name: "polly",
  display_name: "Polly",
  harness: "claude-sdk",
});
const debby = agent({
  id: "agent-debby",
  name: "debby",
  display_name: "Debby",
  harness: "claude-sdk",
});
const customReviewer = agent({
  id: "agent-reviewer",
  name: "pr-reviewer",
  display_name: "PR Reviewer",
  harness: "claude-sdk",
  builtin: false,
});

const readyHost: Host = {
  host_id: "host-story",
  name: "Dev MacBook",
  owner: "developer",
  status: "online",
  configured_harnesses: {
    // claude-sdk backs the polly/debby/custom (SDK) agents; a host reporting the
    // native harnesses reports the SDK ones too, so include it or those agents
    // read as unconfigured.
    "claude-sdk": true,
    "claude-native": true,
    "codex-native": true,
    "cursor-native": true,
  },
};

const pendingAgent: AgentBundleInput = {
  name: "Uploaded Agent",
  harness: "claude-sdk",
  model: "claude-sonnet-4-5",
};

const meta = {
  title: "Components/Agents/AgentHarnessPicker",
  component: AgentHarnessPicker,
  tags: ["visual-snapshot"],
  args: {
    agentEntries: [polly, debby],
    harnessEntries: [claude, codex, cursor],
    effectiveAgentId: claude.id,
    agentLabel: "Claude Code",
    hasAgents: true,
    host: readyHost,
    onSelectAgent: () => undefined,
    pendingAgent: null,
    pendingAgentId: "pending-agent",
    onSelectPending: () => undefined,
    onCreateCustomAgent: () => undefined,
    sandboxSelected: false,
  },
  decorators: [
    (Story) => (
      <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
        <StoryQueryRouter>
          <div className="flex min-h-[480px] w-[620px] items-end justify-end rounded-xl border bg-card p-4">
            <Story />
          </div>
        </StoryQueryRouter>
      </CapabilitiesProvider>
    ),
  ],
} satisfies Meta<typeof AgentHarnessPicker>;

export default meta;
type Story = StoryObj<typeof meta>;

async function openPicker(canvasElement: HTMLElement): Promise<void> {
  await userEvent.click(within(canvasElement).getByTestId("new-chat-landing-agent-select"));
}

export const ReadyOnHost: Story = {
  play: async ({ canvasElement }) => openPicker(canvasElement),
};

export const Configured: Story = {
  args: {
    triggerDetails: [{ label: "Model", value: "Opus 4.6" }],
    selectedConfigContent: <span>Model configuration</span>,
  },
  play: async ({ canvasElement }) => openPicker(canvasElement),
};

export const NeedsSetupBadges: Story = {
  args: {
    host: {
      ...readyHost,
      configured_harnesses: {
        // SDK agents (polly/debby) stay available; the intended badges here are
        // the native codex/cursor rows below.
        "claude-sdk": true,
        "claude-native": true,
        "codex-native": "needs-auth",
        "cursor-native": false,
      },
    },
  },
  decorators: [
    (Story) => (
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, features: { harness_install: true } }}>
        <Story />
      </CapabilitiesProvider>
    ),
  ],
  play: async ({ canvasElement }) => openPicker(canvasElement),
};

export const ClaudeSelected: Story = {
  args: {
    effectiveAgentId: claude.id,
    agentLabel: "Claude Code",
    triggerDetails: [
      { label: "Model", value: "Opus 4.6" },
      { label: "Effort", value: "High" },
    ],
  },
  play: async ({ canvasElement }) => openPicker(canvasElement),
};

export const ReadOnlyPermissionSummary: Story = {
  args: {
    triggerDetails: [
      { label: "Model", value: "Opus 4.6" },
      { label: "Permission mode", value: "Plan" },
    ],
    triggerTooltipRows: [
      { label: "Harness", value: "Claude Code" },
      { label: "Model", value: "Opus 4.6" },
      { label: "Permission mode", value: "Plan" },
    ],
  },
  play: async ({ canvasElement }) => {
    const page = within(canvasElement.ownerDocument.body);
    within(canvasElement).getByTestId("new-chat-landing-agent-select").focus();
    await expect(await page.findByTestId("new-chat-landing-agent-tooltip")).toHaveTextContent(
      "Permission mode: Plan",
    );
  },
};

export const SmartRoutingWithCustomAgents: Story = {
  args: {
    agentEntries: [polly, debby, customReviewer],
    pendingAgent,
    autoHarnessAvailable: true,
    autoHarnessActive: true,
    onSelectAutoHarness: () => undefined,
    agentLabel: "Auto",
    triggerTooltip: "Smart Routing picks the harness per turn",
  },
  play: async ({ canvasElement }) => openPicker(canvasElement),
};
