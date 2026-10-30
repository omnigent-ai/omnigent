import type { Meta, StoryObj } from "@storybook/react-vite";
import { useState } from "react";
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
const openCode = agent({
  id: "agent-opencode",
  name: "opencode-native-ui",
  display_name: "OpenCode",
  harness: "opencode-native",
});
const pi = agent({
  id: "agent-pi",
  name: "pi-native-ui",
  display_name: "Pi",
  harness: "pi-native",
});
const extraHarnesses = [
  agent({ id: "agent-kiro", name: "kiro-native-ui", display_name: "Kiro", harness: "kiro-native" }),
  agent({
    id: "agent-antigravity",
    name: "antigravity-native-ui",
    display_name: "Antigravity",
    harness: "antigravity-native",
  }),
  agent({ id: "agent-kimi", name: "kimi-native-ui", display_name: "Kimi", harness: "kimi-native" }),
];
const otherHarnesses = [claude, codex, cursor, openCode, pi];
const allHarnesses = [...otherHarnesses, ...extraHarnesses];
const manyCustomAgents = Array.from({ length: 24 }, (_, index) =>
  agent({
    id: `agent-custom-${index}`,
    name: `custom-agent-${index}`,
    display_name: `Custom Agent ${index + 1}`,
    harness: "claude-sdk",
    builtin: false,
  }),
);

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
    "opencode-native": true,
    "pi-native": true,
    "kiro-native": true,
    "antigravity-native": true,
    "kimi-native": true,
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
        // SDK agents (polly/debby) stay available; the intended badges are the
        // native codex/cursor rows, which demote to the "Other..." flyout when
        // they can't launch on the host.
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
  play: async ({ canvasElement }) => {
    await openPicker(canvasElement);
    // The badged rows live behind the harness overflow flyout now.
    const page = within(canvasElement.ownerDocument.body);
    await userEvent.click(await page.findByTestId("new-chat-landing-harness-more"));
  },
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

export const WithOtherHarnesses: Story = {
  args: { harnessEntries: otherHarnesses },
  play: async ({ canvasElement }) => openPicker(canvasElement),
};

function ExternallySelectedOtherHarness(args: React.ComponentProps<typeof AgentHarnessPicker>) {
  const [selectedId, setSelectedId] = useState(claude.id);
  return (
    <AgentHarnessPicker
      {...args}
      effectiveAgentId={selectedId}
      agentLabel="OpenCode"
      onOpenChange={(open) => {
        if (open) window.setTimeout(() => setSelectedId(openCode.id), 0);
      }}
    />
  );
}

export const OtherHarnessSelected: Story = {
  args: {
    harnessEntries: [
      claude,
      codex,
      cursor,
      { ...openCode, display_name: "OpenCode Experimental Extended Harness" },
      pi,
    ],
  },
  render: (args) => <ExternallySelectedOtherHarness {...args} />,
  play: async ({ canvasElement }) => {
    await openPicker(canvasElement);
    await expect(
      await within(canvasElement.ownerDocument.body).findByTestId("new-chat-landing-harness-more"),
    ).toHaveTextContent("Other... (OpenCode Experimental Extended Harness)");
  },
};

export const MobileMorePage: Story = {
  args: { harnessEntries: otherHarnesses },
  beforeEach: () => {
    const original = window.matchMedia.bind(window);
    window.matchMedia = (query) => {
      if (query !== "(max-width: 767.98px)" && query !== "(pointer: coarse)")
        return original(query);
      return {
        media: query,
        matches: true,
        onchange: null,
        addListener: () => undefined,
        removeListener: () => undefined,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        dispatchEvent: () => true,
      } satisfies MediaQueryList;
    };
    return () => {
      window.matchMedia = original;
    };
  },
  decorators: [
    (Story) => (
      <div className="w-[390px]">
        <Story />
      </div>
    ),
  ],
  play: async ({ canvasElement }) => {
    await openPicker(canvasElement);
    const page = within(canvasElement.ownerDocument.body);
    await userEvent.click(await page.findByTestId("new-chat-landing-harness-more"));
    await page.findByTestId("new-chat-landing-page-back");
  },
};

export const CustomAgentsSubmenuOpen: Story = {
  args: { agentEntries: [polly, debby, customReviewer, ...manyCustomAgents.slice(0, 3)] },
  play: async ({ canvasElement }) => {
    await openPicker(canvasElement);
    await userEvent.click(
      await within(canvasElement.ownerDocument.body).findByTestId("new-chat-landing-custom-agents"),
    );
  },
};

export const LongLists: Story = {
  args: { harnessEntries: allHarnesses, agentEntries: [polly, debby, ...manyCustomAgents] },
  play: async ({ canvasElement }) => {
    await openPicker(canvasElement);
    await userEvent.click(
      await within(canvasElement.ownerDocument.body).findByTestId("new-chat-landing-custom-agents"),
    );
  },
};

export const LongHarnessList: Story = {
  args: { harnessEntries: allHarnesses, agentEntries: [polly, debby, ...manyCustomAgents] },
  play: async ({ canvasElement }) => {
    await openPicker(canvasElement);
    await userEvent.click(
      await within(canvasElement.ownerDocument.body).findByTestId("new-chat-landing-harness-more"),
    );
  },
};

export const LongNames: Story = {
  args: {
    harnessEntries: [
      claude,
      codex,
      { ...cursor, display_name: "Cursor Experimental Harness With a Very Long Name" },
    ],
    effectiveAgentId: cursor.id,
    agentLabel: "Cursor Experimental Harness With a Very Long Name",
    agentEntries: [
      polly,
      debby,
      {
        ...customReviewer,
        display_name: "Custom Reviewer Agent With a Very Long Name That Should Truncate",
      },
    ],
    host: {
      ...readyHost,
      configured_harnesses: { ...readyHost.configured_harnesses, "cursor-native": false },
    },
    autoHarnessAvailable: true,
    triggerTooltip:
      "Smart Routing selects a harness and model from a very long project context description",
  },
  play: async ({ canvasElement }) => {
    await openPicker(canvasElement);
    await userEvent.click(
      await within(canvasElement.ownerDocument.body).findByTestId("new-chat-landing-custom-agents"),
    );
  },
};

export const LongSmartRoutingContext: Story = {
  args: {
    autoHarnessAvailable: true,
    autoHarnessActive: true,
    agentLabel: "Auto",
    triggerTooltip:
      "Smart Routing selects a harness and model from a very long project context description with several constraints and preferences",
  },
  play: async ({ canvasElement }) => {
    const page = within(canvasElement.ownerDocument.body);
    within(canvasElement).getByTestId("new-chat-landing-agent-select").focus();
    await page.findByTestId("new-chat-landing-agent-tooltip");
  },
};

export const NarrowWidth: Story = {
  args: { harnessEntries: otherHarnesses, agentEntries: [polly, debby, customReviewer] },
  decorators: [
    (Story) => (
      <div className="w-[320px]">
        <Story />
      </div>
    ),
  ],
  play: async ({ canvasElement }) => openPicker(canvasElement),
};

export const NoAgents: Story = {
  args: {
    agentEntries: [],
    harnessEntries: [],
    effectiveAgentId: null,
    agentLabel: "No agents",
    hasAgents: false,
  },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByTestId("new-chat-landing-agent-select")).toBeDisabled();
  },
};

export const EveryHarnessUnavailable: Story = {
  args: {
    host: {
      ...readyHost,
      configured_harnesses: {
        "claude-sdk": true,
        "claude-native": false,
        "codex-native": false,
        "cursor-native": false,
      },
    },
  },
  play: async ({ canvasElement }) => openPicker(canvasElement),
};
