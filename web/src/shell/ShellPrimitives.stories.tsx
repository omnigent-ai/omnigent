import type { Meta, StoryObj } from "@storybook/react-vite";
import { CliCommandBlock } from "./CliCommandBlock";
import { CloseShellDialog } from "./CloseShellDialog";
import { HostLabel } from "./HostLabel";
import { RunnerAsleepHint } from "./RunnerAsleepHint";
import { TerminalFirstContextProvider } from "./TerminalFirstContext";
import { TerminalStatusBadge, type TerminalStatus } from "./terminalStatus";
import { TruncatedBanner } from "./TruncatedBanner";
import { ViewModeToggle } from "./ViewModeToggle";

const meta = {
  title: "Components/Shell/Primitives",
  tags: ["visual-snapshot"],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const CloseShellConfirmation: Story = {
  render: () => (
    <CloseShellDialog
      open
      shellLabel="zsh · implementation"
      onConfirm={() => undefined}
      onCancel={() => undefined}
    />
  ),
};

export const LongCliCommand: Story = {
  render: () => (
    <div className="w-[620px]">
      <CliCommandBlock
        command="omnigent host --server https://workspace.example.com/api/2.0/omnigent --workspace /Users/example/projects/very-long-repository-name"
        testIdPrefix="story-cli"
      />
    </div>
  ),
};

export const TerminalStatusSpectrum: Story = {
  render: () => (
    <div className="flex flex-wrap items-center gap-4 rounded-lg border bg-card p-4">
      {(["active", "idle", "connecting", "error", "closed"] as TerminalStatus[]).map((status) => (
        <TerminalStatusBadge key={status} status={status} />
      ))}
    </div>
  ),
};

export const TerminalStartingToggle: Story = {
  render: () => (
    <TerminalFirstContextProvider
      value={{
        isClaudeNative: true,
        isNativeWrapper: true,
        isTerminalFirst: true,
        isShellView: false,
        view: "terminal",
        terminalViewKey: "terminal:main",
        setView: () => undefined,
        terminalsAvailable: false,
        terminalStartingUp: true,
      }}
    >
      <ViewModeToggle />
    </TerminalFirstContextProvider>
  ),
};

export const WorkspaceIndicators: Story = {
  render: () => (
    <div className="w-[700px] space-y-4 rounded-lg border bg-card p-4">
      <TruncatedBanner />
      <RunnerAsleepHint />
      <div className="flex flex-wrap gap-5">
        <HostLabel
          host={{ host_id: "local", name: "MacBook Pro", owner: "user", status: "online" }}
        />
        <HostLabel
          host={{ host_id: "cloud", name: "Cloud runner", owner: "user", status: "offline" }}
        />
      </div>
    </div>
  ),
};
