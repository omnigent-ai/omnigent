import type { Meta, StoryObj } from "@storybook/react-vite";
import { ChatStoreSeed } from "@/storybook/StoryProviders";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "@/shell/TerminalFirstContext";
import {
  ConnectionIndicator,
  McpStartupIndicator,
  RunnerStartingIndicator,
} from "./ChatIndicators";

const terminalContext = (
  overrides: Partial<TerminalFirstContextValue> = {},
): TerminalFirstContextValue => ({
  isClaudeNative: true,
  isNativeWrapper: true,
  isTerminalFirst: true,
  isShellView: false,
  view: "chat",
  terminalViewKey: null,
  setView: () => undefined,
  terminalsAvailable: false,
  terminalStartingUp: false,
  ...overrides,
});

const meta = {
  title: "Components/Chat/StatusIndicators",
  tags: ["visual-snapshot"],
  decorators: [
    (Story) => (
      <div className="flex min-h-36 w-[720px] items-center justify-center rounded-xl border bg-background p-4">
        <Story />
      </div>
    ),
  ],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const SandboxProvisioningRow: Story = {
  decorators: [
    (Story) => (
      <ChatStoreSeed seed={{ sandboxStatus: { stage: "provisioning", error: null } }}>
        <Story />
      </ChatStoreSeed>
    ),
  ],
  render: () => <RunnerStartingIndicator variant="row" />,
};

export const TerminalSpinUpHero: Story = {
  decorators: [
    (Story) => (
      <ChatStoreSeed seed={{ sandboxStatus: null }}>
        <TerminalFirstContextProvider value={terminalContext({ terminalStartingUp: true })}>
          <Story />
        </TerminalFirstContextProvider>
      </ChatStoreSeed>
    ),
  ],
  render: () => <RunnerStartingIndicator variant="hero" />,
};

export const SandboxFailed: Story = {
  decorators: [
    (Story) => (
      <ChatStoreSeed
        seed={{
          sandboxStatus: {
            stage: "failed",
            error: "Managed sandbox launch failed: the workspace is out of quota.",
          },
        }}
      >
        <Story />
      </ChatStoreSeed>
    ),
  ],
  render: () => (
    <ConnectionIndicator liveness={{ kind: "online" }} onShowReconnectHelp={() => undefined} />
  ),
};

export const LocalAgentDisconnected: Story = {
  decorators: [
    (Story) => (
      <ChatStoreSeed seed={{ sandboxStatus: null }}>
        <Story />
      </ChatStoreSeed>
    ),
  ],
  render: () => (
    <ConnectionIndicator
      liveness={{ kind: "local_stranded" }}
      onShowReconnectHelp={() => undefined}
    />
  ),
};

export const HostOfflineTerminalView: Story = {
  decorators: [
    (Story) => (
      <ChatStoreSeed seed={{ sandboxStatus: null }}>
        <TerminalFirstContextProvider value={terminalContext({ view: "terminal" })}>
          <Story />
        </TerminalFirstContextProvider>
      </ChatStoreSeed>
    ),
  ],
  render: () => (
    <ConnectionIndicator
      liveness={{ kind: "host_offline", isOwner: true }}
      onShowReconnectHelp={() => undefined}
    />
  ),
};

export const Connecting: Story = {
  decorators: [
    (Story) => (
      <ChatStoreSeed seed={{ sandboxStatus: null }}>
        <Story />
      </ChatStoreSeed>
    ),
  ],
  render: () => (
    <ConnectionIndicator liveness={{ kind: "starting" }} onShowReconnectHelp={() => undefined} />
  ),
};

export const McpServersStarting: Story = {
  decorators: [
    (Story) => (
      <ChatStoreSeed
        seed={{
          mcpStartup: {
            glean: { status: "starting", error: null },
            jira: { status: "starting", error: null },
            safe: { status: "starting", error: null },
            storage: { status: "ready", error: null },
          },
        }}
      >
        <Story />
      </ChatStoreSeed>
    ),
  ],
  render: () => <McpStartupIndicator />,
};

export const McpStartupIncomplete: Story = {
  decorators: [
    (Story) => (
      <ChatStoreSeed
        seed={{
          mcpStartup: {
            github: { status: "failed", error: "Authentication failed" },
            jira: { status: "ready", error: null },
            storage: { status: "cancelled", error: null },
          },
        }}
      >
        <Story />
      </ChatStoreSeed>
    ),
  ],
  render: () => <McpStartupIndicator />,
};
